#!/usr/bin/env python3
"""S6 — question.requested trigger-frequency survey.

Runs a few representative prompts with permission_mode=auto and counts
event.question.requested occurrences. Cheap prompts, one session each, yolo
off. Survey only — no pass criteria.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kap

PROMPTS = [
    ("write-code", "Write a Python file hello.py in the current directory that prints 'hello'. Then stop."),
    ("install-deps", "Install the Python package 'cowsay' with pip into the user site. Then stop."),
    ("web-search", "Search the web for today's UTC date and tell me what you found. Then stop."),
]


def run_one(srv: kap.Server, label: str, text: str) -> None:
    workdir = tempfile.mkdtemp(prefix=f"kite-spike-s6-{label}-")
    rest = srv.rest()
    sid = kap.create_session(rest, workdir, title=f"spike-s6-{label}")
    ws = srv.ws(f"S6-{label}")
    events: list[kap.WsEvent] = []
    try:
        ws.hello_handshake(subscriptions=[sid])
        sub = kap.submit_prompt(rest, sid, text, permission_mode="auto")
        if sub.get("code") != 0:
            kap.log(f"{label}: submit failed {sub}")
            return
        done = ws.wait_for(
            lambda e: e.type == "event.session.work_changed" and (e.payload or {}).get("busy") is False,
            timeout=180, collect=events)
        q_req = [e for e in events if e.type == "event.question.requested"]
        approvals = [e for e in events if e.type == "event.approval.requested"]
        kap.obs(
            f"S6.{label}",
            "count question.requested occurrences for a representative auto-mode prompt",
            f"turn_finished={done is not None}; question.requested={len(q_req)} "
            f"{[ (e.payload or {}).get('question', (e.payload or {}).get('questions')) for e in q_req ]}; "
            f"approval.requested={len(approvals)}; "
            f"durable event types={sorted({e.type for e in events if not e.volatile})}",
        )
    finally:
        ws.close()


def main() -> None:
    kap.log("=== S6: question trigger survey ===")
    srv = kap.Server().launch()
    try:
        for label, text in PROMPTS:
            try:
                run_one(srv, label, text)
            except Exception as e:  # survey: record and continue
                kap.log(f"{label}: ERROR {e!r}")
    finally:
        srv.stop()


if __name__ == "__main__":
    main()
