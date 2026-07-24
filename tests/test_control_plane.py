"""Loopback control plane contract tests.

Only real ephemeral loopback sockets are used: the server under test binds
port 0 and the client connects to the published port, so the JSON-lines
wire protocol, auth check, size cap, and metadata discovery are exercised
end to end.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from kite.control_plane import (
    ControlClient,
    ControlError,
    ControlOutcomeUnknownError,
    ControlPlaneServer,
    ControlRefusedError,
    control_metadata_path,
    discover_live_control_metadata,
    read_control_metadata,
)

TOKEN = "test-control-token"


def dead_pid() -> int:
    """A pid that was alive and is now reaped (deterministic stale pid)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def unused_port() -> int:
    """A loopback port that is closed by the time this returns."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class DropConnectionServer:
    """Accepts TCP connections and closes them without responding."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            while True:
                conn, _addr = self._sock.accept()
                conn.close()
        except OSError:
            pass

    def close(self) -> None:
        self._sock.close()


class ControlPlaneTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = pathlib.Path(self._tmp.name)
        self.dispatches: list[tuple[str, dict]] = []
        self.dispatch_error: Exception | None = None
        self.server: ControlPlaneServer | None = None

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.stop()

    def _dispatch(self, method: str, params: dict) -> object:
        self.dispatches.append((method, params))
        if self.dispatch_error is not None:
            raise self.dispatch_error
        if method == "echo":
            return {"echo": params}
        raise ControlError(f"unknown control method: {method}", code="unknown_method")

    def _start(self, *, token: str = TOKEN) -> int:
        self.server = ControlPlaneServer(
            data_dir=self.data_dir,
            dispatch=self._dispatch,
            auth_token=lambda: token,
        )
        return self.server.start()

    def _client(self, port: int, *, token: str = TOKEN, timeout: float = 5.0) -> ControlClient:
        return ControlClient(port=port, token=token, timeout_seconds=timeout)


class RoundTripTests(ControlPlaneTestCase):
    def test_round_trip_returns_data_and_records_dispatch(self) -> None:
        port = self._start()

        data = self._client(port).request("echo", {"a": 1, "b": "两"})

        self.assertEqual(data, {"echo": {"a": 1, "b": "两"}})
        self.assertEqual(self.dispatches, [("echo", {"a": 1, "b": "两"})])

    def test_wrong_token_is_a_structured_business_error(self) -> None:
        port = self._start()

        with self.assertRaises(ControlError) as ctx:
            self._client(port, token="wrong-token").request("echo", {})

        self.assertEqual(ctx.exception.code, "unauthorized")
        self.assertIn("authentication failed", ctx.exception.msg)

    def test_unknown_method_is_a_structured_business_error(self) -> None:
        port = self._start()

        with self.assertRaises(ControlError) as ctx:
            self._client(port).request("nope/nothing", {})

        self.assertEqual(ctx.exception.code, "unknown_method")

    def test_dispatch_crash_surfaces_as_internal_error_without_traceback(self) -> None:
        self.dispatch_error = ValueError("boom")
        port = self._start()

        with self.assertRaises(ControlError) as ctx:
            self._client(port).request("echo", {})

        self.assertEqual(ctx.exception.code, "internal_error")
        self.assertEqual(ctx.exception.msg, "boom")

    def test_oversize_request_line_is_rejected(self) -> None:
        port = self._start()
        big = "x" * (1024 * 1024)  # payload with framing exceeds the 1 MB cap

        with self.assertRaises(ControlError) as ctx:
            self._client(port).request("echo", {"big": big})

        self.assertEqual(ctx.exception.code, "request_too_large")
        self.assertEqual(self.dispatches, [])

    def test_malformed_json_is_a_structured_error(self) -> None:
        port = self._start()
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(b"this is not json\n")
            response = b""
            while b"\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
        payload = json.loads(response.decode("utf-8").splitlines()[0])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "internal_error")


class MetadataTests(ControlPlaneTestCase):
    def test_start_publishes_metadata_and_stop_removes_it(self) -> None:
        port = self._start()
        assert self.server is not None

        metadata = read_control_metadata(self.data_dir)
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata.port, port)
        self.assertEqual(metadata.pid, os.getpid())
        self.assertGreater(metadata.started_at, 0)
        self.assertIsNotNone(discover_live_control_metadata(self.data_dir))

        self.server.stop()
        self.assertFalse(control_metadata_path(self.data_dir).exists())
        self.assertIsNone(discover_live_control_metadata(self.data_dir))

    def test_stale_pid_is_treated_as_daemon_down(self) -> None:
        control_metadata_path(self.data_dir).write_text(
            json.dumps({"port": unused_port(), "pid": dead_pid(), "started_at": time.time()}),
            encoding="utf-8",
        )

        self.assertIsNotNone(read_control_metadata(self.data_dir))
        self.assertIsNone(discover_live_control_metadata(self.data_dir))

    def test_invalid_metadata_is_treated_as_absent(self) -> None:
        control_metadata_path(self.data_dir).write_text("{not json", encoding="utf-8")
        self.assertIsNone(read_control_metadata(self.data_dir))

        control_metadata_path(self.data_dir).write_text(
            json.dumps({"port": 0, "pid": os.getpid(), "started_at": time.time()}),
            encoding="utf-8",
        )
        self.assertIsNone(read_control_metadata(self.data_dir))


class ClientTaxonomyTests(ControlPlaneTestCase):
    def test_connection_refused_means_definitely_not_delivered(self) -> None:
        client = self._client(unused_port())

        with self.assertRaises(ControlRefusedError) as ctx:
            client.request("echo", {})

        self.assertEqual(ctx.exception.code, "control_refused")

    def test_closed_without_response_means_outcome_unknown(self) -> None:
        dropper = DropConnectionServer()
        self.addCleanup(dropper.close)

        with self.assertRaises(ControlOutcomeUnknownError) as ctx:
            self._client(dropper.port).request("echo", {})

        self.assertEqual(ctx.exception.code, "outcome_unknown")

    def test_response_timeout_means_outcome_unknown(self) -> None:
        # A server that accepts but never responds: the request was sent.
        hold = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        hold.bind(("127.0.0.1", 0))
        hold.listen(1)
        self.addCleanup(hold.close)
        accepted: list[socket.socket] = []

        def _serve() -> None:
            try:
                conn, _addr = hold.accept()
                accepted.append(conn)
                time.sleep(2)
            except OSError:
                pass

        threading.Thread(target=_serve, daemon=True).start()
        port = hold.getsockname()[1]

        with self.assertRaises(ControlOutcomeUnknownError):
            self._client(port, timeout=0.2).request("echo", {})
        for conn in accepted:
            conn.close()


if __name__ == "__main__":
    unittest.main()
