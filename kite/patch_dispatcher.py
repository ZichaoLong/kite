"""Coalescing card-patch dispatcher (streaming-cards contract §3.2/§3.4).

Ported from FOCUS ``bot/runtime_card_publisher.py`` (the dispatcher half of
ExecutionCardPatchDispatcher) and renamed to KITE terms. It sits at the
transport edge so the Feishu RTT never blocks the RuntimeLoop:

- the pipeline (on the loop) submits render-thunks per card message; every
  patch re-renders the whole card, so a lost or coalesced patch loses
  nothing (full-snapshot invariant §3.1);
- worker threads invoke the thunk through the injected invoker — kited
  routes it back onto the RuntimeLoop, so rendering always reads current
  state — and perform the blocking patch IO off-loop;
- latest-wins coalescing: at most one in-flight patch per card message and
  exactly one trailing flush, so a delta flood becomes ~2 patches per burst;
- retryable failures (Feishu 230020 rate limit, transport timeouts — see
  ``MessagePatchResult``) requeue after ``retry_after`` with the newer
  render winning; non-retryable failures are dropped (safe by §3.1);
- ``cancel`` drops one card's queued work (terminal transitions), and
  ``shutdown`` cancels retry timers and stops the workers (§3.8).
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from kite.message_patch_result import MessagePatchResult

logger = logging.getLogger(__name__)

# A render-thunk produces the serialized card content to patch, or None when
# there is nothing to patch (stale anchor / terminal transition / shutdown).
RenderThunk = Callable[[], Optional[str]]
# The invoker runs a thunk wherever rendering must happen (kited: the
# RuntimeLoop, via RuntimeLoop.call).
RenderInvoker = Callable[[RenderThunk], Optional[str]]
# The blocking Feishu patch IO (runs on dispatcher worker threads).
PatchCallable = Callable[[str, str], MessagePatchResult]


class TimerHandle(Protocol):
    """Cancel-able timer handle (same shape as event_pipeline.TimerHandle)."""

    def cancel(self) -> None:  # pragma: no cover - interface
        ...


class _ThreadingTimerHandle:
    def __init__(self, timer: threading.Timer) -> None:
        self._timer = timer

    def cancel(self) -> None:
        self._timer.cancel()


def _threading_timer_factory(delay_seconds: float, callback: Callable[[], None]) -> TimerHandle:
    timer = threading.Timer(delay_seconds, callback)
    timer.daemon = True
    timer.start()
    return _ThreadingTimerHandle(timer)


@dataclass(slots=True)
class _PatchSlot:
    queued: bool = False
    inflight: bool = False
    retry_scheduled: bool = False


_DISPATCHER_STOP = object()


class CardPatchDispatcher:
    """Latest-wins coalescing patch queue with retry-after honoring."""

    def __init__(
        self,
        patch: PatchCallable,
        *,
        render_invoker: Optional[RenderInvoker] = None,
        timer_factory: Callable[[float, Callable[[], None]], TimerHandle] = _threading_timer_factory,
        worker_count: int = 2,
    ) -> None:
        self._patch = patch
        self._render_invoker: RenderInvoker = render_invoker or (lambda thunk: thunk())
        self._timer_factory = timer_factory
        self._worker_count = max(int(worker_count), 1)
        self._queue: queue.Queue[str | object] = queue.Queue()
        self._lock = threading.Lock()
        self._pending: dict[str, RenderThunk] = {}
        self._slots: dict[str, _PatchSlot] = {}
        self._retry_timers: dict[str, TimerHandle] = {}
        self._workers: list[threading.Thread] = []
        self._closed = False

    def submit(self, message_id: str, render: RenderThunk) -> None:
        """Queue a render for a card message (latest-wins per message)."""
        normalized = str(message_id or "").strip()
        if not normalized:
            return
        with self._lock:
            if self._closed:
                return
            # Latest-wins: a newer render replaces any still-queued render.
            self._pending[normalized] = render
            slot = self._slots.setdefault(normalized, _PatchSlot())
            if slot.queued or slot.inflight or slot.retry_scheduled:
                return
            slot.queued = True
            self._ensure_workers_locked()
            self._queue.put(normalized)

    def cancel(self, message_id: str) -> None:
        """Drop a card's queued render and retry timer (terminal transition).

        An in-flight patch is allowed to finish; queued work becomes a no-op.
        """
        normalized = str(message_id or "").strip()
        if not normalized:
            return
        with self._lock:
            self._pending.pop(normalized, None)
            timer = self._retry_timers.pop(normalized, None)
            slot = self._slots.get(normalized)
            if slot is not None:
                slot.retry_scheduled = False
        if timer is not None:
            timer.cancel()

    def shutdown(self) -> None:
        """Stop accepting work, cancel retry timers, and stop the workers."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            workers = list(self._workers)
            timers = list(self._retry_timers.values())
            self._retry_timers.clear()
        for _ in workers:
            self._queue.put(_DISPATCHER_STOP)
        for timer in timers:
            timer.cancel()
        for worker in workers:
            if worker.is_alive():
                worker.join(timeout=1)

    def _ensure_workers_locked(self) -> None:
        while len(self._workers) < self._worker_count:
            worker = threading.Thread(
                target=self._run_worker,
                name=f"card-patch-{len(self._workers) + 1}",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()

    def _run_worker(self) -> None:
        while True:
            message_id = self._queue.get()
            if message_id is _DISPATCHER_STOP:
                return
            assert isinstance(message_id, str)
            with self._lock:
                slot = self._slots.setdefault(message_id, _PatchSlot())
                slot.queued = False
                render = self._pending.pop(message_id, None)
                if render is None:
                    if not slot.inflight:
                        self._slots.pop(message_id, None)
                    continue
                slot.inflight = True
            result = MessagePatchResult.failure()
            try:
                content = self._render_invoker(render)
                if content is None:
                    # Stale render (the anchor moved on): nothing to patch
                    # and nothing worth retrying.
                    result = MessagePatchResult.success()
                else:
                    result = self._patch(message_id, content)
            except Exception:
                # A worker must never die with work still queued behind it.
                logger.exception("card patch raised message=%s", message_id)
                result = MessagePatchResult.failure()
            finally:
                with self._lock:
                    slot = self._slots.setdefault(message_id, _PatchSlot())
                    slot.inflight = False
                    if result.retryable and not self._closed:
                        # Retryable (230020 / timeout): requeue after
                        # retry_after; a render submitted meanwhile wins.
                        if message_id not in self._pending:
                            self._pending[message_id] = render
                        self._schedule_retry_locked(message_id, result.retry_after_seconds)
                    if (
                        self._pending.get(message_id) is not None
                        and not slot.queued
                        and not slot.retry_scheduled
                        and not self._closed
                    ):
                        # Exactly one trailing flush for whatever accumulated
                        # while this patch was in flight.
                        slot.queued = True
                        self._queue.put(message_id)
                    elif (
                        message_id not in self._pending
                        and not slot.queued
                        and not slot.retry_scheduled
                    ):
                        self._slots.pop(message_id, None)

    def _schedule_retry_locked(self, message_id: str, delay_seconds: float) -> None:
        slot = self._slots.setdefault(message_id, _PatchSlot())
        if slot.retry_scheduled or self._closed:
            return
        slot.retry_scheduled = True
        try:
            handle = self._timer_factory(
                max(float(delay_seconds), 0.0),
                lambda: self._retry_ready(message_id),
            )
        except Exception:
            # Never wedge the slot on a timer failure: retry inline instead.
            slot.retry_scheduled = False
            logger.exception("retry timer start failed message=%s", message_id)
            if not slot.queued and not slot.inflight and not self._closed:
                slot.queued = True
                self._queue.put(message_id)
            return
        self._retry_timers[message_id] = handle

    def _retry_ready(self, message_id: str) -> None:
        with self._lock:
            self._retry_timers.pop(message_id, None)
            slot = self._slots.get(message_id)
            if slot is None:
                return
            slot.retry_scheduled = False
            if self._closed:
                if not slot.queued and not slot.inflight and message_id not in self._pending:
                    self._slots.pop(message_id, None)
                return
            if slot.queued or slot.inflight:
                return
            if message_id not in self._pending:
                self._slots.pop(message_id, None)
                return
            slot.queued = True
            self._ensure_workers_locked()
            self._queue.put(message_id)
