"""
Serialized runtime event loop for handler state mutations.

The Feishu transport layer, kap-server callback threads, and timer callbacks
should not mutate application handler runtime state directly. They enqueue
commands onto this loop instead, so the handler can behave like a small
event-driven runtime instead of a pile of cross-thread shared-state callbacks.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable

_Thread = threading.Thread


class RuntimeLoopClosedError(RuntimeError):
    """Raised when work is submitted after the runtime loop has stopped."""


@dataclass(slots=True)
class _Task:
    fn: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    done: threading.Event | None = None
    result: Any = None
    error: BaseException | None = None


_STOP = object()


class RuntimeLoop:
    """A single-threaded command loop for stateful runtime operations."""

    def __init__(self, *, name: str = "runtime-loop") -> None:
        self._name = name
        self._queue: queue.Queue[_Task | object] = queue.Queue()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._closed = False

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeLoopClosedError(f"{self._name} is closed")
            if self._worker is not None and self._worker.is_alive():
                return
            worker = _Thread(target=self._run, name=self._name, daemon=True)
            self._worker = worker
            worker.start()

    def stop(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put(_STOP)
            worker = self._worker
        if worker is not None and worker.is_alive() and threading.current_thread() is not worker:
            worker.join(timeout=1)

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        if self._is_worker_thread():
            fn(*args, **kwargs)
            return
        self._enqueue(_Task(fn=fn, args=args, kwargs=kwargs))

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._is_worker_thread():
            return fn(*args, **kwargs)
        task = _Task(fn=fn, args=args, kwargs=kwargs, done=threading.Event())
        self._enqueue(task)
        assert task.done is not None
        task.done.wait()
        if task.error is not None:
            raise task.error
        return task.result

    def _enqueue(self, task: _Task) -> None:
        self.start()
        with self._lock:
            if self._closed:
                raise RuntimeLoopClosedError(f"{self._name} is closed")
            self._queue.put(task)

    def _run(self) -> None:
        while True:
            task = self._queue.get()
            if task is _STOP:
                return
            assert isinstance(task, _Task)
            try:
                task.result = task.fn(*task.args, **task.kwargs)
            except BaseException as exc:  # pragma: no cover - exercised via call()
                task.error = exc
            finally:
                if task.done is not None:
                    task.done.set()

    def _is_worker_thread(self) -> bool:
        worker = self._worker
        return worker is not None and threading.current_thread() is worker
