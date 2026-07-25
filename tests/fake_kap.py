"""In-process fake kap-server for unit tests (no real kimi binary needed).

Implements just enough of the kap wire contract to exercise the adapter:

- REST under /api/v1 with the {code, msg, data, request_id} envelope
  (http.server, Bearer auth, the shutdown Content-Type gotcha enforced),
- WS /api/v1/ws with server_hello / client_hello / subscribe / ack /
  resync_required frames, {seq, epoch} cursor replay, the pre-ack
  resync_required nuance, and the cold-session (not-yet-warmed) nuance,
- a `__main__` entry that emulates the `kimi web --no-open` CLI (instance
  registry + server.token + port-conflict retry) so KapServerProcess can be
  tested against a fake `kimi` binary stub.

REST and WS run on separate ports (the real server shares one port; the
adapter takes host/port per client, so tests wire them independently).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import websockets.sync.server

FAKE_TOKEN = "fake-test-token"
FAKE_WS_PROTOCOL_VERSION = 2


class FakeSession:
    def __init__(self, session_id: str, title: str = "", cwd: str | None = None) -> None:
        self.id = session_id
        self.title = title
        self.cwd = cwd
        self.busy = False
        self.pending_interaction: str | None = None
        self.archived = False
        self.updated_at = "2026-01-01T00:00:00Z"
        self.epoch = f"epoch-{uuid.uuid4().hex[:6]}"
        self.seq = 0
        self.journal: list[dict[str, Any]] = []
        self.active_prompt: str | None = None
        self.queued_prompts: list[str] = []
        # Pending approvals/questions for the interaction sweep endpoints.
        self.pending_approvals: list[dict[str, Any]] = []
        self.pending_questions: list[dict[str, Any]] = []
        # Optional in-flight turn projection for the snapshot route (wire
        # inFlightTurnSchema: turn_id / assistant_text / thinking_text / ...).
        self.in_flight_turn: dict[str, Any] | None = None
        # Cold-session nuance: subscribe with a cursor before any resume-backed
        # REST touch yields an unexplained resync_required.
        self.warm = False
        # When False, the resume-backed REST touch cannot warm the session
        # (models the upstream warmup race / deleted-session window, audit
        # M7): subscribe keeps answering resync_required without a cursor
        # and establishes no server-side subscription.
        self.warmable = True


class FakeKapState:
    """Shared state between the fake REST and WS servers."""

    def __init__(self, *, token: str = FAKE_TOKEN, replay_window: int = 10) -> None:
        self.token = token
        self.replay_window = replay_window
        self.lock = threading.Lock()
        self.sessions: dict[str, FakeSession] = {}
        self.log: list[str] = []
        self.hello_count = 0
        self.shutdown_requested = False
        self.last_shutdown_content_type: str | None = None
        self.prompt_submissions: list[dict[str, Any]] = []
        self.approval_resolutions: list[dict[str, Any]] = []
        self.question_dismissals: list[tuple[str, str]] = []
        self._subscribers: dict[int, tuple[Any, set[str]]] = {}

    def note(self, entry: str) -> None:
        with self.lock:
            self.log.append(entry)

    def create_session(
        self, session_id: str | None = None, title: str = "", cwd: str | None = None
    ) -> FakeSession:
        with self.lock:
            sid = session_id or f"s-{uuid.uuid4().hex[:8]}"
            session = FakeSession(sid, title=title, cwd=cwd)
            self.sessions[sid] = session
            return session

    def simulate_server_restart(self) -> None:
        """Journals survive (seq/epoch kept); warmth does not."""
        with self.lock:
            for session in self.sessions.values():
                session.warm = False

    def append_event(self, session: FakeSession, event_type: str, payload: Any) -> dict[str, Any]:
        with self.lock:
            session.seq += 1
            frame = {
                "type": event_type,
                "seq": session.seq,
                "epoch": session.epoch,
                "session_id": session.id,
                "timestamp": "2026-01-01T00:00:00Z",
                "payload": payload,
            }
            session.journal.append(frame)
            subscribers = list(self._subscribers.values())
        for connection, session_ids in subscribers:
            if session.id in session_ids:
                try:
                    connection.send(json.dumps(frame))
                except Exception:  # noqa: BLE001 - a dead subscriber is dropped lazily
                    pass
        return frame

    def append_volatile_event(
        self, session: FakeSession, event_type: str, payload: Any, *, offset: int | None = None
    ) -> dict[str, Any]:
        """Broadcast a volatile frame (never journaled, seq stays the last
        durable watermark — mirroring the upstream broadcaster)."""
        with self.lock:
            frame = {
                "type": event_type,
                "seq": session.seq,
                "epoch": session.epoch,
                "session_id": session.id,
                "volatile": True,
                "timestamp": "2026-01-01T00:00:00Z",
                "payload": payload,
            }
            if offset is not None:
                frame["offset"] = offset
            subscribers = list(self._subscribers.values())
        for connection, session_ids in subscribers:
            if session.id in session_ids:
                try:
                    connection.send(json.dumps(frame))
                except Exception:  # noqa: BLE001 - a dead subscriber is dropped lazily
                    pass
        return frame

    def register(self, connection: Any) -> int:
        with self.lock:
            key = id(connection)
            self._subscribers[key] = (connection, set())
            return key

    def unregister(self, key: int) -> None:
        with self.lock:
            self._subscribers.pop(key, None)

    def set_subscriptions(self, key: int, session_ids: set[str]) -> None:
        with self.lock:
            if key in self._subscribers:
                connection, _ = self._subscribers[key]
                self._subscribers[key] = (connection, session_ids)

    def add_pending_approval(self, session: FakeSession, approval_id: str) -> None:
        with self.lock:
            session.pending_approvals.append({
                "approval_id": approval_id,
                "session_id": session.id,
                "tool_call_id": f"tc-{approval_id}",
                "tool_name": "Bash",
                "action": "execute",
                "tool_input_display": {"kind": "command", "command": "rm -rf build/"},
                "created_at": "2026-01-01T00:00:00Z",
                "expires_at": "2026-01-01T01:00:00Z",
            })

    def add_pending_question(self, session: FakeSession, question_id: str) -> None:
        with self.lock:
            session.pending_questions.append({
                "question_id": question_id,
                "session_id": session.id,
                "items": [{"question": "继续吗？", "options": [{"label": "是"}, {"label": "否"}]}],
                "created_at": "2026-01-01T00:00:00Z",
            })

    def send_error_frame(self, session: FakeSession, code: str, message: str) -> None:
        """Broadcast a WS ``error`` frame in its real durable shape (audit
        T3): journaled with seq/epoch/session_id like any durable event and
        fanned out to the session's subscribers."""
        self.append_event(
            session,
            "error",
            {
                "code": code,
                "message": message,
                "retryable": False,
                "agentId": "main",
                "sessionId": session.id,
            },
        )


def _session_wire(session: FakeSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "workspace_id": "ws-1",
        "title": session.title,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": session.updated_at,
        "busy": session.busy,
        "pending_interaction": session.pending_interaction or "none",
        "archived": session.archived,
        "metadata": {"cwd": session.cwd} if session.cwd else {},
        "message_count": 0,
        "last_seq": 0,  # upstream placeholder — the adapter must never use it
    }


def _prompt_wire(prompt_id: str, status: str) -> dict[str, Any]:
    return {
        "prompt_id": prompt_id,
        "user_message_id": f"um-{prompt_id}",
        "status": status,
        "content": [{"type": "text", "text": "fake"}],
        "created_at": "2026-01-01T00:00:00Z",
    }


def _envelope(code: int, data: Any, msg: str = "") -> dict[str, Any]:
    return {"code": code, "msg": msg or ("success" if code == 0 else "error"), "data": data,
            "request_id": f"req-{uuid.uuid4().hex[:8]}"}


class FakeKapRestHandler(BaseHTTPRequestHandler):
    """Speaks the /api/v1 envelope contract against a shared FakeKapState."""

    state: FakeKapState  # injected by make_rest_server

    def log_message(self, *_args: Any) -> None:  # silence request logs
        return

    def _send(self, payload: dict[str, Any], *, http_status: int = 200) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(http_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {self.state.token}":
            return True
        self._send(_envelope(40101, None, "unauthorized"), http_status=401)
        return False

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode())
        except ValueError:
            return None

    def _route(self) -> tuple[str, list[str]]:
        path = self.path.split("?", 1)[0]
        prefix = "/api/v1"
        if not path.startswith(prefix):
            return "", []
        return path[len(prefix):], [p for p in path[len(prefix):].split("/") if p]

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        if not self._authorized():
            return
        _, parts = self._route()
        self.state.note(f"rest:GET {'/'.join(parts)}")
        if parts == ["meta"]:
            self._send(_envelope(0, {
                "server_version": "0.0.2-fake",
                "server_id": f"srv-{uuid.uuid4().hex[:8]}",
                "backend": "v2",
                "started_at": "2026-01-01T00:00:00Z",
                "dangerous_bypass_auth": False,
            }))
            return
        if parts == ["sessions"]:
            items = [_session_wire(s) for s in self.state.sessions.values()]
            self._send(_envelope(0, {"items": items, "has_more": False}))
            return
        if len(parts) == 2 and parts[0] == "sessions":
            session = self.state.sessions.get(parts[1])
            if session is None:
                self._send(_envelope(40401, None, "session not found"))
                return
            self._send(_envelope(0, _session_wire(session)))
            return
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "prompts":
            session = self.state.sessions.get(parts[1])
            if session is None:
                self._send(_envelope(40401, None, "session not found"))
                return
            session.warm = session.warmable  # resume-backed route: activates cold sessions
            self._send(_envelope(0, {
                "active": _prompt_wire(session.active_prompt, "running")
                if session.active_prompt else None,
                "queued": [_prompt_wire(pid, "queued") for pid in session.queued_prompts],
            }))
            return
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "approvals":
            session = self.state.sessions.get(parts[1])
            if session is None:
                self._send(_envelope(40401, None, "session not found"))
                return
            self._send(_envelope(0, {"items": list(session.pending_approvals)}))
            return
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "questions":
            session = self.state.sessions.get(parts[1])
            if session is None:
                self._send(_envelope(40401, None, "session not found"))
                return
            self._send(_envelope(0, {"items": list(session.pending_questions)}))
            return
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "snapshot":
            session = self.state.sessions.get(parts[1])
            if session is None:
                self._send(_envelope(40401, None, "session not found"))
                return
            self._send(_envelope(0, {
                "as_of_seq": session.seq,
                "epoch": session.epoch,
                "session": _session_wire(session),
                "messages": {"items": [], "has_more": False},
                "in_flight_turn": session.in_flight_turn,
                "pending_approvals": [],
                "pending_questions": [],
            }))
            return
        self._send(_envelope(40404, None, "route not found"))

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        if not self._authorized():
            return
        _, parts = self._route()
        body = self._read_body()
        self.state.note(f"rest:POST {'/'.join(parts)}")
        if parts == ["shutdown"]:
            # Fastify gotcha: Content-Type on an empty body is a 50001.
            self.state.last_shutdown_content_type = self.headers.get("Content-Type")
            if self.headers.get("Content-Type") and body is None:
                self._send(_envelope(50001, None, "empty body with content-type"))
                return
            self.state.shutdown_requested = True
            self._send(_envelope(0, {"ok": True}))
            return
        if parts == ["sessions"]:
            body = body if isinstance(body, dict) else {}
            metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
            session = self.state.create_session(
                title=str(body.get("title") or ""), cwd=metadata.get("cwd")
            )
            self._send(_envelope(0, _session_wire(session)))
            return
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "profile":
            session = self.state.sessions.get(parts[1])
            if session is None:
                self._send(_envelope(40401, None, "session not found"))
                return
            if isinstance(body, dict) and body.get("title") is not None:
                session.title = str(body["title"])
            self.state.append_event(session, "session.meta.updated", {"title": session.title})
            self._send(_envelope(0, _session_wire(session)))
            return
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "prompts":
            session = self.state.sessions.get(parts[1])
            if session is None:
                self._send(_envelope(40401, None, "session not found"))
                return
            self.state.prompt_submissions.append(body if isinstance(body, dict) else {})
            prompt_id = f"p-{uuid.uuid4().hex[:6]}"
            if session.active_prompt is None:
                session.active_prompt = prompt_id
                session.busy = True
                status = "running"
            else:
                session.queued_prompts.append(prompt_id)
                status = "queued"
            self._send(_envelope(0, _prompt_wire(prompt_id, status)))
            return
        if len(parts) == 4 and parts[0] == "sessions" and parts[2] == "approvals":
            session = self.state.sessions.get(parts[1])
            if session is None:
                self._send(_envelope(40401, None, "session not found"))
                return
            remaining = [a for a in session.pending_approvals if a["approval_id"] != parts[3]]
            if len(remaining) == len(session.pending_approvals):
                self._send(_envelope(40404, None, "approval not found"))
                return
            session.pending_approvals = remaining
            self.state.approval_resolutions.append(
                {"session_id": parts[1], "approval_id": parts[3], "body": body}
            )
            self._send(_envelope(0, {"resolved": True, "resolved_at": "2026-01-01T00:00:00Z"}))
            return
        if len(parts) == 4 and parts[0] == "sessions" and parts[2] == "questions" and parts[3].endswith(":dismiss"):
            session = self.state.sessions.get(parts[1])
            if session is None:
                self._send(_envelope(40401, None, "session not found"))
                return
            question_id = parts[3][: -len(":dismiss")]
            remaining = [q for q in session.pending_questions if q["question_id"] != question_id]
            if len(remaining) == len(session.pending_questions):
                self._send(_envelope(40404, None, "question not found"))
                return
            session.pending_questions = remaining
            self.state.question_dismissals.append((parts[1], question_id))
            self._send(_envelope(40909, {"dismissed": True, "dismissed_at": "2026-01-01T00:00:00Z"}))
            return
        self._send(_envelope(40404, None, "route not found"))


def make_rest_server(state: FakeKapState) -> ThreadingHTTPServer:
    """Start the fake REST server on an ephemeral port; returns the server."""
    handler_class = type("BoundFakeKapRestHandler", (FakeKapRestHandler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="fake-kap-rest", daemon=True)
    thread.start()
    return server


# ---------------------------------------------------------------------------
# WS side
# ---------------------------------------------------------------------------


def _handle_subscribe(
    state: FakeKapState,
    connection: Any,
    session_ids: list[str],
    cursors: dict[str, Any],
    *,
    hello: bool,
) -> dict[str, Any]:
    """Shared client_hello/subscribe logic mirroring wsConnectionV1 semantics."""
    accepted: list[str] = []
    not_found: list[str] = []
    resync_required: list[str] = []
    ack_cursors: dict[str, dict[str, Any]] = {}
    for sid in session_ids:
        session = state.sessions.get(sid)
        if session is None:
            (resync_required if hello else not_found).append(sid)
            continue
        cursor = cursors.get(sid) if isinstance(cursors, dict) else None
        if cursor is None:
            accepted.append(sid)
            ack_cursors[sid] = {"seq": session.seq, "epoch": session.epoch}
            continue
        if not session.warm:
            # Cold-session nuance: unexplained resync, no reason frame, no
            # cursor — and NO server-side subscription is established
            # (mirrors upstream wsConnectionV1; audit M7).
            resync_required.append(sid)
            continue
        accepted.append(sid)
        seq = cursor.get("seq")
        epoch = cursor.get("epoch")
        reason = None
        if epoch != session.epoch:
            reason = "epoch_changed"
        elif isinstance(seq, int) and session.seq - seq > state.replay_window:
            reason = "buffer_overflow"
        if reason is not None:
            # The standalone resync_required frame goes out BEFORE the ack.
            connection.send(json.dumps({
                "type": "resync_required",
                "timestamp": "2026-01-01T00:00:00Z",
                "payload": {
                    "session_id": sid,
                    "reason": reason,
                    "current_seq": session.seq,
                    "epoch": session.epoch,
                },
            }))
            resync_required.append(sid)
            ack_cursors[sid] = {"seq": session.seq, "epoch": session.epoch}
            continue
        # Replay the missed durable events before the ack (wire order).
        start = seq if isinstance(seq, int) else 0
        for frame in session.journal:
            if frame["seq"] > start:
                connection.send(json.dumps(frame))
        ack_cursors[sid] = {"seq": session.seq, "epoch": session.epoch}
    payload: dict[str, Any] = {"resync_required": resync_required, "cursors": ack_cursors}
    if hello:
        payload["accepted_subscriptions"] = accepted
    else:
        payload["accepted"] = accepted
        payload["not_found"] = not_found
    return payload


def _ws_handler(state: FakeKapState, connection: Any) -> None:
    if connection.request.headers.get("Authorization") != f"Bearer {state.token}":
        connection.close(code=4401, reason="unauthorized")
        return
    key = state.register(connection)
    subscribed: set[str] = set()
    state.note("ws:connect")
    connection.send(json.dumps({
        "type": "server_hello",
        "timestamp": "2026-01-01T00:00:00Z",
        "payload": {
            "ws_connection_id": f"conn-{uuid.uuid4().hex[:8]}",
            "protocol_version": FAKE_WS_PROTOCOL_VERSION,
            "max_event_buffer_size": state.replay_window,
            "capabilities": {"event_batching": False, "compression": False},
        },
    }))
    try:
        for raw in connection:
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            frame_type = frame.get("type")
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            if frame_type in ("client_hello", "subscribe"):
                hello = frame_type == "client_hello"
                if hello:
                    with state.lock:
                        state.hello_count += 1
                    session_ids = payload.get("subscriptions") or []
                else:
                    session_ids = payload.get("session_ids") or []
                state.note(f"ws:{frame_type}")
                ack_payload = _handle_subscribe(
                    state, connection, list(session_ids), payload.get("cursors") or {}, hello=hello
                )
                if hello:
                    subscribed = set(ack_payload.get("accepted_subscriptions") or [])
                else:
                    subscribed |= set(ack_payload.get("accepted") or [])
                state.set_subscriptions(key, subscribed)
                connection.send(json.dumps({
                    "type": "ack", "id": frame.get("id", ""), "code": 0, "msg": "success",
                    "payload": ack_payload,
                }))
            elif frame_type == "unsubscribe":
                for sid in payload.get("session_ids") or []:
                    subscribed.discard(sid)
                state.set_subscriptions(key, subscribed)
                connection.send(json.dumps({
                    "type": "ack", "id": frame.get("id", ""), "code": 0, "msg": "success",
                    "payload": {"accepted": [], "not_found": [], "resync_required": []},
                }))
    except Exception:  # noqa: BLE001 - connection dropped; clean up below
        pass
    finally:
        state.unregister(key)


class FakeWsServer:
    """websockets.sync.server wrapper with a stable close() API."""

    def __init__(self, state: FakeKapState, port: int = 0) -> None:
        def handler(connection: Any) -> None:
            _ws_handler(state, connection)

        self._server = websockets.sync.server.serve(handler, "127.0.0.1", port)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-kap-ws", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()


def make_ws_server(state: FakeKapState, port: int = 0) -> FakeWsServer:
    return FakeWsServer(state, port)


# ---------------------------------------------------------------------------
# Standalone `kimi web --no-open` emulation (used via a shim binary in tests)
# ---------------------------------------------------------------------------


def _write_fake_server_files(home: str, port: int) -> None:
    os.makedirs(os.path.join(home, "server", "instances"), exist_ok=True)
    with open(os.path.join(home, "server.token"), "w", encoding="utf-8") as handle:
        handle.write(f"{FAKE_TOKEN}\n")
    os.chmod(os.path.join(home, "server.token"), 0o600)
    server_id = f"srv-{uuid.uuid4().hex[:12]}"
    with open(
        os.path.join(home, "server", "instances", f"{server_id}.json"), "w", encoding="utf-8"
    ) as handle:
        json.dump({"pid": os.getpid(), "port": port}, handle)


def _run_fake_web(argv: list[str]) -> int:
    """Emulate `kimi web --no-open --port N` (registry + token + WS serving)."""
    home = os.environ.get("KIMI_CODE_HOME")
    if not home:
        print("KIMI_CODE_HOME not set", file=sys.stderr)
        return 2
    os.makedirs(home, exist_ok=True)
    requested = 58627
    if "--port" in argv:
        requested = int(argv[argv.index("--port") + 1])

    exit_code = os.environ.get("KITE_FAKE_KAP_EXIT")
    if exit_code is not None:
        print("fake kap exiting early as requested", file=sys.stderr)
        return int(exit_code)

    state = FakeKapState()
    server = None
    port = requested
    # Emulate the upstream listenWithPortRetry behavior (port+1 on conflict).
    for _ in range(100):
        try:
            server = make_ws_server(state, port)
            break
        except OSError:
            if requested == 0:
                raise
            port += 1
    if server is None:
        print("no free port", file=sys.stderr)
        return 1

    def _sigterm(_signum: int, _frame: Any) -> None:
        # os._exit: the WS server's non-daemon threads would otherwise keep
        # the interpreter alive past a plain sys.exit.
        os._exit(0)

    # Install handlers BEFORE the readiness files appear: once the registry
    # and token exist, the supervisor considers the server up and may signal it.
    signal.signal(signal.SIGTERM, _sigterm)
    _write_fake_server_files(home, server.port)

    die_after = os.environ.get("KITE_FAKE_KAP_DIE_AFTER")
    if die_after is not None:
        time.sleep(float(die_after))
        return 3
    signal.pause()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--version":
        print("kimi 0.28.1-fake")
        return 0
    if args and args[0] == "web":
        return _run_fake_web(args[1:])
    print("usage: fake_kap.py web --no-open [--port N] | --version", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
