import threading
import unittest

from kite.runtime_loop import RuntimeLoop, RuntimeLoopClosedError


class RuntimeLoopTests(unittest.TestCase):
    def _make_loop(self) -> RuntimeLoop:
        loop = RuntimeLoop(name="test-runtime-loop")
        self.addCleanup(loop.stop)
        return loop

    def test_call_runs_function_on_worker_thread_and_returns_result(self) -> None:
        loop = self._make_loop()

        result = loop.call(lambda: (threading.current_thread().name, 40 + 2))

        self.assertEqual(result, ("test-runtime-loop", 42))

    def test_submit_executes_function_on_worker_thread(self) -> None:
        loop = self._make_loop()
        done = threading.Event()
        seen: list[str] = []

        loop.submit(lambda: (seen.append(threading.current_thread().name), done.set()))

        self.assertTrue(done.wait(timeout=5))
        self.assertEqual(seen, ["test-runtime-loop"])

    def test_submissions_are_serialized_in_order(self) -> None:
        loop = self._make_loop()
        appended: list[int] = []

        def append(value: int) -> None:
            appended.append(value)

        loop.call(lambda: [loop.submit(append, i) for i in range(50)])
        loop.call(lambda: None)

        self.assertEqual(appended, list(range(50)))

    def test_call_propagates_exceptions(self) -> None:
        loop = self._make_loop()

        def boom() -> None:
            raise KeyError("nope")

        with self.assertRaises(KeyError):
            loop.call(boom)

    def test_call_from_worker_thread_runs_inline(self) -> None:
        loop = self._make_loop()

        def outer() -> str:
            inner_thread = loop.call(lambda: threading.current_thread().name)
            return inner_thread

        self.assertEqual(loop.call(outer), "test-runtime-loop")

    def test_submit_from_worker_thread_runs_inline(self) -> None:
        loop = self._make_loop()
        seen: list[str] = []

        def outer() -> None:
            loop.submit(lambda: seen.append("inline"))

        loop.call(outer)
        self.assertEqual(seen, ["inline"])

    def test_submit_after_stop_raises_closed_error(self) -> None:
        loop = self._make_loop()
        loop.stop()

        with self.assertRaises(RuntimeLoopClosedError):
            loop.submit(lambda: None)
        with self.assertRaises(RuntimeLoopClosedError):
            loop.call(lambda: None)
        with self.assertRaises(RuntimeLoopClosedError):
            loop.start()

    def test_stop_is_idempotent(self) -> None:
        loop = self._make_loop()
        loop.call(lambda: None)
        loop.stop()
        loop.stop()
        self.assertTrue(loop._closed)

    def test_start_is_idempotent_while_running(self) -> None:
        loop = self._make_loop()
        loop.start()
        worker = loop._worker
        loop.start()
        self.assertIs(loop._worker, worker)


if __name__ == "__main__":
    unittest.main()
