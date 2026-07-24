"""CardPatchDispatcher tests (streaming-cards contract §3.2/§3.4, §5).

The dispatcher is exercised with real worker threads against a scripted
blocking patch callable; retry timers are manual (deterministic fire).
"""

from __future__ import annotations

import threading
import time
import unittest

from kite.message_patch_result import MessagePatchResult
from kite.patch_dispatcher import CardPatchDispatcher


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class ManualTimer:
    def __init__(self, delay: float, callback) -> None:
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.callback()


class ManualTimerFactory:
    def __init__(self) -> None:
        self.created: list[ManualTimer] = []

    def __call__(self, delay: float, callback) -> ManualTimer:
        timer = ManualTimer(delay, callback)
        self.created.append(timer)
        return timer


class ScriptedPatch:
    """Recording patch IO with a blocking gate, a result script, and an
    in-flight counter (to prove the ≤1-in-flight-per-card discipline)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.results: list[MessagePatchResult] = []
        self.errors: list[Exception] = []
        self.gate: threading.Event | None = None
        self.max_inflight = 0
        self._inflight = 0
        self._lock = threading.Lock()

    def __call__(self, message_id: str, content: str) -> MessagePatchResult:
        with self._lock:
            self._inflight += 1
            self.max_inflight = max(self.max_inflight, self._inflight)
        try:
            if self.gate is not None:
                self.gate.wait(5)
            with self._lock:
                if self.errors:
                    raise self.errors.pop(0)
                self.calls.append((message_id, content))
                return self.results.pop(0) if self.results else MessagePatchResult.success()
        finally:
            with self._lock:
                self._inflight -= 1

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    @property
    def contents(self) -> list[str]:
        return [content for _mid, content in self.calls]


class DispatcherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.patch = ScriptedPatch()
        self.timers = ManualTimerFactory()
        self.dispatcher = CardPatchDispatcher(
            self.patch, timer_factory=self.timers, worker_count=1
        )
        self.addCleanup(self.dispatcher.shutdown)


class CoalescingTests(DispatcherTestCase):
    def test_flood_coalesces_to_inflight_plus_one_trailing_flush(self) -> None:
        self.patch.gate = threading.Event()
        self.dispatcher.submit("om_1", lambda: "v1")
        self.assertTrue(wait_until(lambda: self.patch.inflight == 1))
        for i in range(2, 8):
            self.dispatcher.submit("om_1", lambda i=i: f"v{i}")
        time.sleep(0.1)  # the blocked in-flight patch must not multiply
        self.patch.gate.set()

        self.assertTrue(wait_until(lambda: len(self.patch.calls) == 2))
        time.sleep(0.1)
        # ~2 patches per burst: the first render plus exactly one trailing
        # flush; order preserved; ≤1 in-flight at any moment.
        self.assertEqual(len(self.patch.calls), 2)
        self.assertEqual(self.patch.contents[0], "v1")
        self.assertEqual(self.patch.contents[1], "v7")
        self.assertEqual(self.patch.max_inflight, 1)

    def test_submit_during_inflight_replaces_the_pending_render(self) -> None:
        self.patch.gate = threading.Event()
        self.dispatcher.submit("om_1", lambda: "v1")
        self.assertTrue(wait_until(lambda: self.patch.inflight == 1))
        self.dispatcher.submit("om_1", lambda: "v2")
        self.dispatcher.submit("om_1", lambda: "v3")
        self.patch.gate.set()

        self.assertTrue(wait_until(lambda: len(self.patch.calls) == 2))
        # The intermediate render is superseded before ever being patched.
        self.assertNotIn("v2", self.patch.contents)
        self.assertEqual(self.patch.contents, ["v1", "v3"])

    def test_cards_are_independent(self) -> None:
        self.dispatcher.submit("om_1", lambda: "a")
        self.dispatcher.submit("om_2", lambda: "b")
        self.assertTrue(wait_until(lambda: len(self.patch.calls) == 2))
        self.assertEqual(self.patch.calls, [("om_1", "a"), ("om_2", "b")])


class RetryAfterTests(DispatcherTestCase):
    def test_retryable_failure_requeues_and_the_retry_timer_applies(self) -> None:
        self.patch.results = [MessagePatchResult.retry_later(2.0)]
        self.dispatcher.submit("om_1", lambda: "v1")
        self.assertTrue(wait_until(lambda: len(self.patch.calls) == 1))
        self.assertTrue(wait_until(lambda: len(self.timers.created) == 1))
        self.assertEqual(self.timers.created[0].delay, 2.0)

        # Nothing repatches until the retry timer fires.
        time.sleep(0.1)
        self.assertEqual(len(self.patch.calls), 1)
        self.timers.created[0].fire()
        self.assertTrue(wait_until(lambda: len(self.patch.calls) == 2))
        self.assertEqual(self.patch.contents, ["v1", "v1"])

    def test_newer_render_supersedes_the_failed_render(self) -> None:
        self.patch.results = [MessagePatchResult.retry_later(2.0)]
        self.dispatcher.submit("om_1", lambda: "v1")
        self.assertTrue(wait_until(lambda: len(self.patch.calls) == 1))
        self.assertTrue(wait_until(lambda: len(self.timers.created) == 1))

        self.dispatcher.submit("om_1", lambda: "v2")
        self.timers.created[0].fire()
        self.assertTrue(wait_until(lambda: len(self.patch.calls) == 2))
        self.assertEqual(self.patch.contents, ["v1", "v2"])

    def test_non_retryable_failure_is_dropped_without_crash(self) -> None:
        self.patch.results = [MessagePatchResult.failure()]
        self.dispatcher.submit("om_1", lambda: "v1")
        self.assertTrue(wait_until(lambda: len(self.patch.calls) == 1))

        time.sleep(0.1)
        self.assertEqual(self.timers.created, [])  # no retry scheduled
        self.assertEqual(len(self.patch.calls), 1)
        # The slot is clean: the next submit patches immediately.
        self.dispatcher.submit("om_1", lambda: "v2")
        self.assertTrue(wait_until(lambda: len(self.patch.calls) == 2))

    def test_raising_patch_is_a_plain_failure_and_the_worker_survives(self) -> None:
        self.patch.errors = [RuntimeError("boom")]
        self.dispatcher.submit("om_1", lambda: "v1")
        time.sleep(0.2)
        self.assertEqual(self.patch.calls, [])
        self.assertEqual(self.timers.created, [])
        self.dispatcher.submit("om_1", lambda: "v2")
        self.assertTrue(wait_until(lambda: len(self.patch.calls) == 1))

    def test_stale_render_none_skips_the_patch(self) -> None:
        self.dispatcher.submit("om_1", lambda: None)
        time.sleep(0.2)
        self.assertEqual(self.patch.calls, [])
        # A render-None is not a failure: no retry, and the slot stays clean.
        self.assertEqual(self.timers.created, [])
        self.dispatcher.submit("om_1", lambda: "v1")
        self.assertTrue(wait_until(lambda: len(self.patch.calls) == 1))


class ShutdownCancelTests(DispatcherTestCase):
    def test_shutdown_cancels_retry_timers_and_ignores_new_submits(self) -> None:
        self.patch.results = [MessagePatchResult.retry_later(2.0)]
        self.dispatcher.submit("om_1", lambda: "v1")
        self.assertTrue(wait_until(lambda: len(self.patch.calls) == 1))
        self.assertTrue(wait_until(lambda: len(self.timers.created) == 1))

        self.dispatcher.shutdown()
        self.assertTrue(self.timers.created[0].cancelled)
        self.dispatcher.submit("om_1", lambda: "v2")
        time.sleep(0.1)
        self.assertEqual(len(self.patch.calls), 1)

    def test_cancel_drops_the_queued_render(self) -> None:
        self.patch.gate = threading.Event()
        self.dispatcher.submit("om_1", lambda: "v1")
        self.assertTrue(wait_until(lambda: self.patch.inflight == 1))
        self.dispatcher.submit("om_1", lambda: "v2")
        self.dispatcher.cancel("om_1")
        self.patch.gate.set()

        time.sleep(0.2)
        # The in-flight patch finishes; the queued render is gone.
        self.assertEqual(self.patch.contents, ["v1"])

    def test_cancel_drops_the_retry_timer(self) -> None:
        self.patch.results = [MessagePatchResult.retry_later(2.0)]
        self.dispatcher.submit("om_1", lambda: "v1")
        self.assertTrue(wait_until(lambda: len(self.patch.calls) == 1))
        self.assertTrue(wait_until(lambda: len(self.timers.created) == 1))

        self.dispatcher.cancel("om_1")
        self.assertTrue(self.timers.created[0].cancelled)
        time.sleep(0.1)
        self.assertEqual(len(self.patch.calls), 1)


if __name__ == "__main__":
    unittest.main()
