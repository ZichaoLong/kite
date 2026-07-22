"""Opt-in contract test: the adapter slice against a REAL kap-server.

Skipped unless a `kimi` binary is on PATH (set KIMI_BIN to override). Runs the
full vertical slice with an isolated temporary KIMI_CODE_HOME — the real
~/.kimi-code is never touched:

    KapServerProcess spawn -> /meta -> create session -> warm GET prompts
    -> WS subscribe -> rename emits session.meta.updated over WS
    -> cursor advanced -> snapshot watermark -> POST /shutdown (rc 0).

Run it explicitly with:

    python3 -m pytest tests/test_kap_contract.py -q

No model calls are made, so no provider credentials are needed.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest

from kite.adapters.kap_server import (
    KapRestClient,
    KapServerProcess,
    KapWsClient,
)
from kite.stores.event_cursor_store import EventCursorStore

KIMI_BIN = shutil.which("kimi")


def wait_until(predicate, timeout: float = 30.0, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@unittest.skipIf(KIMI_BIN is None, "kimi binary not on PATH (contract test is opt-in)")
class KapContractTests(unittest.TestCase):
    def test_full_slice_against_real_kap_server(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = f"{tmp.name}/kimi-home"
        data_dir = f"{tmp.name}/kite-data"

        proc = KapServerProcess(
            kimi_bin=KIMI_BIN or "kimi", home=home, requested_port=0,
            readiness_timeout_seconds=90.0,
        )
        ws_client: KapWsClient | None = None
        try:
            proc.start()
            self.assertIsNotNone(proc.port)
            self.assertIsNotNone(proc.token)
            rest = KapRestClient("127.0.0.1", proc.port, proc.token or "")

            meta = rest.meta()
            self.assertTrue(meta.server_version)
            self.assertEqual(meta.backend, "v2")

            created = rest.post(
                "/sessions", {"title": "kite-contract", "metadata": {"cwd": tmp.name}}
            )
            session_id = created["id"]

            sessions = {s.session_id: s for s in rest.list_sessions()}
            self.assertIn(session_id, sessions)
            self.assertFalse(sessions[session_id].busy)

            queue = rest.get_prompts(session_id)  # also the subscribe warmup
            self.assertEqual(queue.queue_depth, 0)

            received = threading.Event()
            seen_types: list[str] = []

            def on_event(event) -> None:
                seen_types.append(event.type)
                if event.type == "session.meta.updated":
                    received.set()

            cursors = EventCursorStore(data_dir)
            ws_client = KapWsClient(
                host="127.0.0.1",
                port=proc.port,
                token=proc.token or "",
                rest_client=rest,
                cursor_store=cursors,
                stale_seconds=10.0,
                reconnect_delay_seconds=0.5,
                on_event=on_event,
            )
            ws_client.start()
            self.assertTrue(wait_until(lambda: ws_client.connected, timeout=20))
            ws_client.subscribe(session_id)

            rest.post(f"/sessions/{session_id}/profile", {"title": "kite-contract-2"})
            self.assertTrue(
                received.wait(timeout=20),
                f"no session.meta.updated event; saw {seen_types}",
            )
            cursor = cursors.get(session_id)
            self.assertIsNotNone(cursor)
            assert cursor is not None
            self.assertGreaterEqual(cursor.seq, 1)
            self.assertTrue(cursor.epoch)

            snapshot = rest.get_snapshot(session_id)
            self.assertGreaterEqual(snapshot.as_of_seq, cursor.seq - 1)
            self.assertEqual(snapshot.epoch, cursor.epoch)

            ws_client.stop()
            ws_client = None

            rest.shutdown()
            returncode = proc.wait(timeout=20)
            self.assertEqual(returncode, 0)
        finally:
            if ws_client is not None:
                ws_client.stop()
            if proc.poll() is None:
                proc.stop()


if __name__ == "__main__":
    unittest.main()
