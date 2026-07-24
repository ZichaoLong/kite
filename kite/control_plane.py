"""Loopback control plane between kitectl and kited.

Ported from FOCUS's ``bot/service_control_plane.py`` with renames
(docs/decisions/control-plane.md): JSON-lines over loopback TCP, one-line
request/response, 1 MB cap, an exact-match auth token, and an endpoint
published via a metadata file so kitectl discovers the LIVE daemon instead of
trusting a recorded port ("live outranks recorded"). KITE has no
service-lease concept, so FOCUS's lease-ownership verification is dropped;
the metadata pid-liveness check plus the daemon-issued token (``control.token``
in the config dir, 0600) are the only guards. No TLS: this never leaves the
host.

Client error taxonomy (three-way, so a non-idempotent submit is never
retried blindly):

- ``ControlRefusedError``: the connection never came up — the request was
  definitely NOT delivered (safe to retry);
- ``ControlOutcomeUnknownError``: the request was sent but no valid response
  arrived (lost / invalid / timeout) — it may have been delivered;
- ``ControlError``: everything else, notably business errors the daemon
  answered explicitly (carries the wire ``code``/``msg``).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import pathlib
import socket
import socketserver
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from kite.file_permissions import ensure_private_file_permissions
from kite.process_utils import process_exists

logger = logging.getLogger("kite.control_plane")

_MAX_MESSAGE_BYTES = 1024 * 1024
_LISTEN_HOST = "127.0.0.1"
_METADATA_FILE_NAME = "control_plane.json"

DEFAULT_TIMEOUT_SECONDS = 5.0


class ControlError(RuntimeError):
    """A control-plane request failed. Carries the wire ``code``/``msg``."""

    def __init__(self, msg: str, *, code: str = "internal_error") -> None:
        super().__init__(msg)
        self.code = str(code or "internal_error")
        self.msg = str(msg)


class ControlRefusedError(ControlError):
    """Connect failed: the request was definitely NOT delivered."""

    def __init__(self, msg: str) -> None:
        super().__init__(msg, code="control_refused")


class ControlOutcomeUnknownError(ControlError):
    """Sent but no valid response: the request MAY have been delivered."""

    def __init__(self, msg: str) -> None:
        super().__init__(msg, code="outcome_unknown")


# ---------------------------------------------------------------------------
# Endpoint metadata (data dir; how kitectl finds the live daemon)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ControlPlaneMetadata:
    """The published endpoint facts: ephemeral port, owner pid, start time."""

    port: int
    pid: int
    started_at: float


def control_metadata_path(data_dir: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(data_dir) / _METADATA_FILE_NAME


def read_control_metadata(data_dir: pathlib.Path | str) -> ControlPlaneMetadata | None:
    """Parse the published metadata; None when absent or invalid."""
    try:
        raw = json.loads(control_metadata_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    port = raw.get("port")
    pid = raw.get("pid")
    started_at = raw.get("started_at")
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        return None
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
        return None
    return ControlPlaneMetadata(port=port, pid=pid, started_at=float(started_at))


def discover_live_control_metadata(
    data_dir: pathlib.Path | str,
) -> ControlPlaneMetadata | None:
    """The metadata of a LIVE daemon; a stale pid means the daemon is down."""
    metadata = read_control_metadata(data_dir)
    if metadata is None or not process_exists(metadata.pid):
        return None
    return metadata


def _write_control_metadata(data_dir: pathlib.Path, *, port: int) -> None:
    path = control_metadata_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"port": int(port), "pid": os.getpid(), "started_at": time.time()},
        ensure_ascii=False,
        indent=2,
    )
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    ensure_private_file_permissions(tmp_path)
    os.replace(tmp_path, path)


# ---------------------------------------------------------------------------
# Server (kited side)
# ---------------------------------------------------------------------------


class _ThreadingTcpServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class _ControlRequestHandler(socketserver.StreamRequestHandler):
    server: "_ControlServer"

    def handle(self) -> None:
        raw = self.rfile.readline(_MAX_MESSAGE_BYTES + 1)
        if not raw:
            return
        if len(raw) > _MAX_MESSAGE_BYTES:
            self._respond(
                {
                    "ok": False,
                    "error": {
                        "code": "request_too_large",
                        "msg": f"control request exceeds {_MAX_MESSAGE_BYTES} bytes",
                    },
                }
            )
            return
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ControlError("control request must be an object", code="invalid_request")
            token = str(request.get("auth_token") or "")
            expected = str(self.server.auth_token() or "")
            if not expected or not hmac.compare_digest(token, expected):
                raise ControlError(
                    "control request authentication failed", code="unauthorized"
                )
            method = str(request.get("method") or "").strip()
            if not method:
                raise ControlError("control request missing method", code="invalid_request")
            params = request.get("params") or {}
            if not isinstance(params, dict):
                raise ControlError(
                    "control request params must be an object", code="invalid_request"
                )
            result = self.server.dispatch(method, params)
            response: dict[str, Any] = {"ok": True, "data": result}
        except ControlError as exc:
            response = {"ok": False, "error": {"code": exc.code, "msg": exc.msg}}
        except Exception as exc:  # noqa: BLE001 - never leak a traceback onto the wire
            logger.exception("control request failed with an internal error")
            response = {"ok": False, "error": {"code": "internal_error", "msg": str(exc)}}
        self._respond(response)

    def _respond(self, response: Mapping[str, Any]) -> None:
        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


class _ControlServer(_ThreadingTcpServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        dispatch: Callable[[str, dict[str, Any]], Any],
        auth_token: Callable[[], str],
    ) -> None:
        self.dispatch = dispatch
        self.auth_token = auth_token
        super().__init__(server_address, _ControlRequestHandler)


class ControlPlaneServer:
    """The daemon-side control plane: serves ``dispatch`` on a loopback port.

    ``start`` binds an ephemeral port and publishes ``control_plane.json`` in
    the data dir; ``stop`` shuts the server down and removes the metadata file
    so a stopped daemon never looks discoverable.
    """

    def __init__(
        self,
        *,
        data_dir: pathlib.Path | str,
        dispatch: Callable[[str, dict[str, Any]], Any],
        auth_token: Callable[[], str],
    ) -> None:
        self._data_dir = pathlib.Path(data_dir)
        self._dispatch = dispatch
        self._auth_token = auth_token
        self._lock = threading.Lock()
        self._server: _ControlServer | None = None
        self._thread: threading.Thread | None = None
        self._port = 0

    @property
    def port(self) -> int:
        """The actual bound port (0 until started)."""
        return self._port

    def start(self) -> int:
        """Bind, publish metadata, and serve. Returns the actual port."""
        with self._lock:
            if self._server is not None:
                return self._port
            server = _ControlServer((_LISTEN_HOST, 0), self._dispatch, self._auth_token)
            thread = threading.Thread(
                target=server.serve_forever, name="kite-control-plane", daemon=True
            )
            self._server = server
            self._thread = thread
            self._port = int(server.server_address[1])
            # The socket is already listening (bound + activated in the
            # constructor), so publishing before serve_forever is safe:
            # early connections wait in the listen backlog.
            _write_control_metadata(self._data_dir, port=self._port)
            thread.start()
            return self._port

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
            self._port = 0
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=1)
        try:
            control_metadata_path(self._data_dir).unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Client (kitectl side)
# ---------------------------------------------------------------------------


class ControlClient:
    """One request per connection, one-line JSON responses."""

    def __init__(
        self,
        *,
        port: int,
        token: str,
        host: str = _LISTEN_HOST,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._host = host
        self._port = int(port)
        self._token = str(token or "")
        self._timeout = float(timeout_seconds)

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send one request; returns the response ``data``. See the module
        docstring for the three-way error taxonomy."""
        payload = json.dumps(
            {
                "auth_token": self._token,
                "method": str(method or "").strip(),
                "params": dict(params or {}),
            },
            ensure_ascii=False,
        ).encode("utf-8") + b"\n"
        endpoint = f"{self._host}:{self._port}"
        try:
            with socket.create_connection((self._host, self._port), timeout=self._timeout) as sock:
                sock.settimeout(self._timeout)
                try:
                    sock.sendall(payload)
                except (TimeoutError, OSError) as exc:
                    raise ControlOutcomeUnknownError(
                        "control request send result unknown; the request may have "
                        f"been partially or fully delivered: {endpoint}"
                    ) from exc
                try:
                    response = _recv_line(sock)
                except (OSError, ControlError, json.JSONDecodeError, UnicodeError) as exc:
                    raise ControlOutcomeUnknownError(
                        f"control request sent, but no valid response arrived: {endpoint}: {exc}"
                    ) from exc
        except (ControlRefusedError, ControlOutcomeUnknownError):
            raise
        except (ConnectionRefusedError, TimeoutError, OSError) as exc:
            # Connect failed: nothing was sent, nothing could be delivered.
            raise ControlRefusedError(f"control plane unreachable: {endpoint}: {exc}") from exc
        if not isinstance(response, dict):
            raise ControlOutcomeUnknownError("control request sent, but the response was invalid")
        if response.get("ok") is True:
            return response.get("data")
        if response.get("ok") is not False:
            raise ControlOutcomeUnknownError(
                "control request sent, but the response had no explicit status"
            )
        error = response.get("error")
        if not isinstance(error, dict):
            raise ControlOutcomeUnknownError(
                "control request sent, but the error response was invalid"
            )
        raise ControlError(
            str(error.get("msg") or "control request failed"),
            code=str(error.get("code") or "internal_error"),
        )


def _recv_line(sock: socket.socket) -> Any:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_MESSAGE_BYTES:
            raise ControlError("control response too large", code="response_too_large")
        if b"\n" in chunk:
            break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise ControlError("control plane returned no data", code="empty_response")
    return json.loads(raw.decode("utf-8"))
