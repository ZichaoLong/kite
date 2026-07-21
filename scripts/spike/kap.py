"""Shared helpers for the KITE Milestone-0 kap-server spike.

Covers: subprocess lifecycle of `kimi web --no-open` with an isolated
KIMI_CODE_HOME, readiness wait, a REST client with envelope unwrapping, and
a WS subscribe client with {seq, epoch} cursor support.

Every server launch uses a fresh temporary KIMI_CODE_HOME unless an explicit
home is passed (S3/S4 reuse a home across restarts on purpose). The real
~/.kimi-code is never touched: KIMI_CODE_HOME is always overridden in the
child environment.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import websockets.sync.client as wsync

KIMI_BIN = os.environ.get("KIMI_BIN", os.path.expanduser("~/.kimi-code/bin/kimi"))
DEFAULT_PORT = 58627
API = "/api/v1"

# Env vars the child needs for real model calls even with an isolated home.
PASSTHROUGH_ENV = [
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM",
    "KIMI_API_KEY", "KIMI_BASE_URL", "MOONSHOT_API_KEY", "MOONSHOT_BASE_URL",
    "https_proxy", "http_proxy", "no_proxy", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def obs(label: str, expected: str, actual: str, verdict: Optional[str] = None) -> None:
    """Print one structured observation line for the results doc."""
    line = f"OBS | {label}\n  expected: {expected}\n  actual:   {actual}"
    if verdict:
        line += f"\n  verdict:  {verdict}"
    print(line, flush=True)


def child_env(home: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in PASSTHROUGH_ENV}
    env["KIMI_CODE_HOME"] = home
    # An isolated home has no config.toml, hence no provider -> prompt submit
    # fails 40110. agent-core-v2 synthesizes a provider from KIMI_MODEL_* env
    # (packages/agent-core-v2/src/app/provider/configSection.ts,
    # app/model/envOverlay.ts). Map the shell's KIMI_API_KEY/KIMI_BASE_URL onto
    # it; KIMI_MODEL_NAME can be overridden from the caller's env.
    if os.environ.get("KIMI_API_KEY") and "KIMI_MODEL_API_KEY" not in env:
        env["KIMI_MODEL_API_KEY"] = os.environ["KIMI_API_KEY"]
    if os.environ.get("KIMI_BASE_URL") and "KIMI_MODEL_BASE_URL" not in env:
        env["KIMI_MODEL_BASE_URL"] = os.environ["KIMI_BASE_URL"]
    env.setdefault("KIMI_MODEL_NAME", os.environ.get("SPIKE_MODEL", "kimi-for-coding"))
    return env


class RestClient:
    def __init__(self, port: int, token: str, host: str = "127.0.0.1"):
        self.base = f"http://{host}:{port}{API}"
        self.token = token

    def call(self, method: str, path: str, body: Any = None,
             token: Optional[str] = "__default__") -> dict[str, Any]:
        """Call the API. Returns the parsed envelope dict (never raises on
        business errors; raises on transport errors)."""
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            # Fastify rejects an empty body when content-type is application/json.
            req.add_header("Content-Type", "application/json")
        tok = self.token if token == "__default__" else token
        if tok:
            req.add_header("Authorization", f"Bearer {tok}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return {"http": resp.status, **json.loads(raw)}
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return {"http": e.code, **json.loads(raw)}
            except json.JSONDecodeError:
                return {"http": e.code, "code": -1, "msg": raw, "data": None}
        except urllib.error.URLError as e:
            return {"http": 0, "code": -1, "msg": f"transport: {e.reason}", "data": None}

    def get(self, path: str, **kw) -> dict[str, Any]:
        return self.call("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw) -> dict[str, Any]:
        return self.call("POST", path, body, **kw)


@dataclass
class WsEvent:
    frame: dict[str, Any]

    @property
    def type(self) -> str:
        return self.frame.get("type", "?")

    @property
    def seq(self) -> Optional[int]:
        return self.frame.get("seq")

    @property
    def epoch(self) -> Optional[str]:
        return self.frame.get("epoch")

    @property
    def payload(self) -> Any:
        return self.frame.get("payload")

    @property
    def volatile(self) -> bool:
        return bool(self.frame.get("volatile"))

    def __repr__(self) -> str:
        p = json.dumps(self.payload, ensure_ascii=False)
        if len(p) > 160:
            p = p[:157] + "..."
        return f"<{self.type} seq={self.seq} vol={self.volatile} {p}>"


class WsClient:
    """One /api/v1/ws connection. Speaks client_hello + subscribe with cursors."""

    def __init__(self, port: int, token: str, name: str, host: str = "127.0.0.1"):
        self.name = name
        self.url = f"ws://{host}:{port}{API}/ws"
        self.ws = wsync.connect(
            self.url,
            additional_headers={"Authorization": f"Bearer {token}"},
            open_timeout=15,
        )
        self.hello: Optional[dict[str, Any]] = None
        self.last_cursor: dict[str, dict[str, Any]] = {}
        # Non-ack frames that arrive while waiting for an ack (e.g. the
        # standalone resync_required frame is sent BEFORE the ack).
        self.pre_frames: list[dict[str, Any]] = []
        # First frame must be server_hello.
        frame = json.loads(self.ws.recv(timeout=15))
        if frame.get("type") != "server_hello":
            raise RuntimeError(f"{name}: expected server_hello, got {frame}")
        self.hello = frame["payload"]

    def _send(self, type_: str, payload: dict[str, Any]) -> str:
        id_ = f"{self.name}-{uuid.uuid4().hex[:8]}"
        self.ws.send(json.dumps({"type": type_, "id": id_, "payload": payload}))
        return id_

    def recv_frame(self, timeout: float = 10.0) -> dict[str, Any]:
        return json.loads(self.ws.recv(timeout=timeout))

    def wait_for(self, pred, timeout: float = 30.0,
                 collect: Optional[list[WsEvent]] = None) -> Optional[WsEvent]:
        """Read frames until pred(WsEvent) is true or timeout. All non-ack
        frames seen are appended to collect (if given)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                frame = self.recv_frame(timeout=max(0.1, deadline - time.monotonic()))
            except TimeoutError:
                break
            ev = WsEvent(frame)
            if collect is not None and frame.get("type") not in ("ack", "server_hello"):
                collect.append(ev)
            if pred(ev):
                return ev
        return None

    def hello_handshake(self, subscriptions: Optional[list[str]] = None,
                        cursors: Optional[dict[str, dict[str, Any]]] = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"client_id": self.name,
                                   "subscriptions": subscriptions or []}
        if cursors:
            payload["cursors"] = cursors
        id_ = self._send("client_hello", payload)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            frame = self.recv_frame(timeout=15)
            if frame.get("type") == "ack" and frame.get("id") == id_:
                return frame
            self.pre_frames.append(frame)
        raise RuntimeError(f"{self.name}: no ack for client_hello")

    def subscribe(self, session_ids: list[str],
                  cursors: Optional[dict[str, dict[str, Any]]] = None,
                  timeout: float = 15.0) -> dict[str, Any]:
        payload: dict[str, Any] = {"session_ids": session_ids}
        if cursors:
            payload["cursors"] = cursors
        id_ = self._send("subscribe", payload)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.recv_frame(timeout=max(0.1, deadline - time.monotonic()))
            if frame.get("type") == "ack" and frame.get("id") == id_:
                return frame
            if frame.get("type") == "resync_required":
                return frame  # surfaced to caller
            self.pre_frames.append(frame)
        raise RuntimeError(f"{self.name}: no ack for subscribe")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


@dataclass
class Server:
    """A managed `kimi web --no-open` subprocess with isolated KIMI_CODE_HOME."""

    port_requested: int = DEFAULT_PORT
    home: Optional[str] = None
    extra_args: list[str] = field(default_factory=list)

    proc: Optional[subprocess.Popen] = None
    port: Optional[int] = None
    token: Optional[str] = None
    stdout_path: Optional[str] = None
    _own_home: bool = False

    def launch(self, timeout: float = 60.0) -> "Server":
        if self.home is None:
            self.home = tempfile.mkdtemp(prefix="kite-spike-home-")
            self._own_home = True
        os.makedirs(self.home, exist_ok=True)
        self.stdout_path = os.path.join(self.home, "server.stdout.log")
        out = open(self.stdout_path, "wb")
        args = [KIMI_BIN, "web", "--no-open", "--port", str(self.port_requested),
                *self.extra_args]
        self.proc = subprocess.Popen(
            args, stdout=out, stderr=subprocess.STDOUT, env=child_env(self.home),
            start_new_session=True,  # own process group: SIGTERM/SIGKILL target it alone
        )
        self._wait_ready(timeout)
        return self

    def _wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        token_path = os.path.join(self.home, "server.token")
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                tail = ""
                try:
                    with open(self.stdout_path, "rb") as f:
                        tail = f.read()[-3000:].decode(errors="replace")
                except OSError:
                    pass
                raise RuntimeError(
                    f"kimi web exited early rc={self.proc.returncode}\n{tail}")
            # Port is known once the instance registry file appears.
            port = self._port_from_registry()
            if port is not None and os.path.exists(token_path):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1):
                        pass
                except OSError:
                    time.sleep(0.2)
                    continue
                self.port = port
                with open(token_path) as f:
                    self.token = f.read().strip()
                return
            time.sleep(0.2)
        raise RuntimeError(f"kimi web not ready within {timeout}s; log: {self.stdout_path}")

    def _port_from_registry(self) -> Optional[int]:
        inst_dir = os.path.join(self.home, "server", "instances")
        try:
            names = sorted(os.listdir(inst_dir))
        except OSError:
            return None
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(inst_dir, name)) as f:
                    info = json.load(f)
                if info.get("pid") == self.proc.pid:
                    return int(info["port"])
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                continue
        return None

    def instance_files(self) -> list[str]:
        inst_dir = os.path.join(self.home, "server", "instances")
        try:
            return sorted(os.listdir(inst_dir))
        except OSError:
            return []

    def read_instances(self) -> list[dict[str, Any]]:
        inst_dir = os.path.join(self.home, "server", "instances")
        out = []
        for name in self.instance_files():
            try:
                with open(os.path.join(inst_dir, name)) as f:
                    out.append(json.load(f))
            except (OSError, json.JSONDecodeError):
                pass
        return out

    def rest(self) -> RestClient:
        assert self.port and self.token
        return RestClient(self.port, self.token)

    def ws(self, name: str) -> WsClient:
        assert self.port and self.token
        return WsClient(self.port, self.token, name)

    def terminate(self, grace: float = 10.0) -> Optional[int]:
        if self.proc is None:
            return None
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        return self.proc.returncode

    def kill9(self) -> Optional[int]:
        if self.proc is None:
            return None
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(timeout=5)
        return self.proc.returncode

    def stop(self) -> None:
        self.terminate()

    def cleanup_home(self) -> None:
        # Homes are left on disk for post-run inspection; they live in /tmp and
        # are small. Uncomment to remove:
        # if self._own_home and self.home: shutil.rmtree(self.home, ignore_errors=True)
        pass

    def __enter__(self) -> "Server":
        return self.launch()

    def __exit__(self, *exc) -> None:
        self.stop()


def create_session(rest: RestClient, cwd: str, title: str = "spike") -> str:
    env = rest.post("/sessions", {"title": title, "metadata": {"cwd": cwd}})
    assert env.get("code") == 0, f"create session failed: {env}"
    return env["data"]["id"]


# Sessions created via REST do not inherit the env-overlay defaultModel
# (profileService reads the per-session profile state; a fresh session has no
# modelAlias -> turn fails model.not_configured). Carry the alias per prompt.
MODEL_ALIAS = os.environ.get("SPIKE_MODEL_ALIAS", "__kimi_env_model__")


def submit_prompt(rest: RestClient, sid: str, text: str,
                  permission_mode: Optional[str] = None,
                  model: Optional[str] = MODEL_ALIAS) -> dict[str, Any]:
    body: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if permission_mode:
        body["permission_mode"] = permission_mode
    if model:
        body["model"] = model
    return rest.post(f"/sessions/{sid}/prompts", body)
