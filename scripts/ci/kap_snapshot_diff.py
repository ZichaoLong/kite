#!/usr/bin/env python3
"""
kap-server API surface snapshot diff — the drift-awareness guardrail of
docs/architecture/kite-design.md §10 ("follow, don't pin").

It fetches the documented API surface from a target kap-server:

  - the REST route table derived from `/openapi.json` (`METHOD path` pairs),
  - the WS operation catalog derived from `/asyncapi.json`
    (operations + component message ids),

and diffs it against the checked-in snapshot (default:
tests/snapshots/kap-api-surface.json). Any drift prints a clear report and
exits 1 — the signal to adapt explicitly inside kite/adapters/ and refresh the
verified version. Run it with `--update` to bless the new surface.

This script must run where `kimi` is installed (dev machine / self-hosted
runner): GitHub-hosted runners have no kimi binary, so the workflow is
workflow_dispatch-only and skips gracefully without one.

Usage:
  python3 scripts/ci/kap_snapshot_diff.py --spawn                 # spawn a temp-home kap-server
  python3 scripts/ci/kap_snapshot_diff.py --base-url http://127.0.0.1:58627 --token TOKEN
  python3 scripts/ci/kap_snapshot_diff.py --spawn --update        # regenerate the snapshot
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_SNAPSHOT_PATH = REPO_ROOT / "tests" / "snapshots" / "kap-api-surface.json"

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
META_ENDPOINTS = ("/openapi.json", "/asyncapi.json", "/")


def _fetch(base_url: str, path: str, token: str, *, timeout: float = 15.0) -> tuple[int, bytes]:
    request = urllib.request.Request(base_url.rstrip("/") + path, method="GET")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _fetch_json(base_url: str, path: str, token: str) -> dict:
    status, raw = _fetch(base_url, path, token)
    if status != 200:
        raise RuntimeError(f"GET {path} returned HTTP {status}")
    try:
        parsed = json.loads(raw.decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"GET {path} did not return JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"GET {path} returned a non-object JSON document")
    return parsed


def collect_surface(base_url: str, token: str) -> dict:
    """The documented API surface of the target kap-server, in a stable order."""
    openapi = _fetch_json(base_url, "/openapi.json", token)
    paths = openapi.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise RuntimeError("/openapi.json carries no paths")
    routes = sorted(
        f"{method.upper()} {path}"
        for path, item in paths.items()
        if isinstance(item, dict)
        for method in item
        if method.lower() in HTTP_METHODS
    )

    asyncapi = _fetch_json(base_url, "/asyncapi.json", token)
    components = asyncapi.get("components")
    messages = components.get("messages") if isinstance(components, dict) else None
    ws_messages = sorted(messages) if isinstance(messages, dict) else []
    operations = asyncapi.get("operations")
    ws_operations = sorted(operations) if isinstance(operations, dict) else []

    meta = []
    for endpoint in META_ENDPOINTS:
        status, _ = _fetch(base_url, endpoint, token)
        meta.append(f"GET {endpoint} -> {status}")

    return {
        "routes": routes,
        "ws_operations": ws_operations,
        "ws_messages": ws_messages,
        "meta_endpoints": sorted(meta),
    }


def diff_surface(expected: dict, actual: dict) -> list[str]:
    lines: list[str] = []
    for section in ("routes", "ws_operations", "ws_messages", "meta_endpoints"):
        before = expected.get(section) or []
        after = actual.get(section) or []
        removed = [entry for entry in before if entry not in after]
        added = [entry for entry in after if entry not in before]
        if removed or added:
            lines.append(f"[{section}]")
            lines.extend(f"  - {entry}" for entry in removed)
            lines.extend(f"  + {entry}" for entry in added)
    return lines


def _spawn_server() -> tuple[str, str, object]:
    """Spawn a throwaway kap-server (temp KIMI_CODE_HOME) via the adapter."""
    from kite.adapters.kap_server import KapServerProcess, resolve_kimi_bin

    kimi_bin = resolve_kimi_bin(None)
    if not kimi_bin:
        raise RuntimeError("kimi binary not found on PATH (set KIMI_BIN)")
    home = tempfile.mkdtemp(prefix="kite-snapshot-home-")
    proc = KapServerProcess(kimi_bin=kimi_bin, home=home).start()
    assert proc.port is not None and proc.token is not None
    return f"http://127.0.0.1:{proc.port}", proc.token, proc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--spawn", action="store_true", help="spawn a temp-home kap-server")
    source.add_argument("--base-url", help="base URL of a running kap-server")
    parser.add_argument("--token", help="Bearer token (with --base-url)")
    parser.add_argument("--kimi-bin", help="kimi binary override (with --spawn)")
    parser.add_argument(
        "--snapshot",
        default=str(DEFAULT_SNAPSHOT_PATH),
        help=f"snapshot file (default: {DEFAULT_SNAPSHOT_PATH})",
    )
    parser.add_argument("--update", action="store_true", help="overwrite the snapshot")
    args = parser.parse_args(argv)

    proc = None
    if args.spawn:
        import os

        if args.kimi_bin:
            os.environ["KIMI_BIN"] = args.kimi_bin
        base_url, token, proc = _spawn_server()
    else:
        if not args.token:
            parser.error("--token is required with --base-url")
        base_url, token = args.base_url, args.token

    try:
        surface = collect_surface(base_url, token)
    finally:
        if proc is not None:
            proc.stop()

    snapshot_path = Path(args.snapshot)
    if args.update:
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(
            json.dumps(surface, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"snapshot written: {snapshot_path}")
        return 0

    if not snapshot_path.exists():
        print(f"snapshot file missing: {snapshot_path} (run with --update to create it)")
        return 1
    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    diff = diff_surface(expected, surface)
    if not diff:
        print(f"OK: kap-server API surface matches {snapshot_path}")
        return 0
    print("kap-server API surface DRIFT detected:")
    print("\n".join(diff))
    print(
        "\nAdapt explicitly inside kite/adapters/, then bless the new surface with "
        "`--update` and bump the verified version (docs/architecture/kite-design.md §10)."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
