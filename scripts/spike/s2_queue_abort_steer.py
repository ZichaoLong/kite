#!/usr/bin/env python3
"""S2 — prompt queue + abort/steer boundaries.

Enqueue 3 prompts (first one long-running, yolo mode so no approvals stall
the queue). While the first is active:
  (a) abort the active prompt
  (b) abort a queued prompt
  (c) steer a queued prompt into the active turn
  (d) steer against an empty queue
Record error codes + event ordering; every prompt.* event must carry a
prompt id attributable to one of the three submissions.

Requires a working model.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kap


def drain(ws: kap.WsClient, seconds: float, bag: list[kap.WsEvent]) -> None:
    ws.wait_for(lambda ev: False, timeout=seconds, collect=bag)


def main() -> None:
    kap.log("=== S2: queue + abort/steer boundaries ===")
    workdir = tempfile.mkdtemp(prefix="kite-spike-s2-")
    srv = kap.Server().launch()
    try:
        rest = srv.rest()
        sid = kap.create_session(rest, workdir, title="spike-s2")
        kap.log(f"session {sid}")

        ws = srv.ws("S2")
        try:
            ack = ws.hello_handshake(subscriptions=[sid])
            cur = (ack["payload"].get("cursors") or {}).get(sid)
            kap.log(f"subscribed, cursor={cur}")
            events: list[kap.WsEvent] = []

            # Prompt 1: long-running yolo task (keeps the turn alive while we
            # act on the queue). Prompts 2 and 3: trivial.
            p1 = kap.submit_prompt(rest, sid,
                "Write the integers 1 to 40, one per file, into files named n001.txt .. n040.txt "
                "inside the current directory. Each file's content is just the number. "
                "Do them one tool call at a time, do not batch.",
                permission_mode="yolo")
            pid1 = (p1.get("data") or {}).get("prompt_id")
            p2 = kap.submit_prompt(rest, sid, "Reply with exactly: second", permission_mode="yolo")
            pid2 = (p2.get("data") or {}).get("prompt_id")
            p3 = kap.submit_prompt(rest, sid, "Reply with exactly: third", permission_mode="yolo")
            pid3 = (p3.get("data") or {}).get("prompt_id")
            kap.log(f"prompts: p1={pid1} p2={pid2} p3={pid3} "
                    f"(codes {p1.get('code')},{p2.get('code')},{p3.get('code')})")
            kap.obs(
                "S2.0 triple enqueue",
                "3 prompts accepted; 1 active + 2 queued (queued, not rejected)",
                f"submit codes: {p1.get('code')},{p2.get('code')},{p3.get('code')}; "
                f"statuses: {(p1.get('data') or {}).get('status')},{(p2.get('data') or {}).get('status')},{(p3.get('data') or {}).get('status')}",
            )
            time.sleep(1.5)
            q = rest.get(f"/sessions/{sid}/prompts")
            kap.obs(
                "S2.1 queue snapshot after enqueue",
                "GET prompts: active=p1, queued=[p2,p3] in FIFO order",
                f"active={((q.get('data') or {}).get('active') or {}).get('prompt_id')}, "
                f"queued={[p.get('prompt_id') for p in (q.get('data') or {}).get('queued', [])]}",
            )

            # (b) abort a QUEUED prompt (p3) while p1 is active.
            ab_q = rest.post(f"/sessions/{sid}/prompts/{pid3}:abort")
            kap.obs(
                "S2.2 abort queued prompt",
                "queued prompt abort: documented result (code 0 aborted:true, or a clear error)",
                f"code={ab_q.get('code')} msg={ab_q.get('msg')} data={ab_q.get('data')}",
            )
            q2 = rest.get(f"/sessions/{sid}/prompts")
            kap.log(f"queue after abort-queued: active={((q2.get('data') or {}).get('active') or {}).get('prompt_id')}, "
                    f"queued={[p.get('prompt_id') for p in (q2.get('data') or {}).get('queued', [])]}")

            # (c) steer the queued p2 into the active turn. NOTE: the matchable
            # URL is SINGLE-colon `prompts:steer` (the registered fastify
            # pattern is `prompts::steer`, where `:steer` is a wildcard param —
            # kimi-web calls the single-colon form).
            st = rest.post(f"/sessions/{sid}/prompts:steer", {"prompt_ids": [pid2]})
            kap.obs(
                "S2.3 steer queued prompt",
                "steer a queued prompt into the active turn: code 0 {steered:true}",
                f"code={st.get('code')} msg={st.get('msg')} data={st.get('data')}",
            )
            q3 = rest.get(f"/sessions/{sid}/prompts")
            kap.log(f"queue after steer: {q3.get('data')}")

            # (d) steer against an empty queue (p2 steered, p3 aborted).
            st_e = rest.post(f"/sessions/{sid}/prompts:steer", {"prompt_ids": [pid3]})
            kap.obs(
                "S2.4 steer non-queued prompt",
                "steer of a prompt that is not queued -> deterministic error (40402 expected)",
                f"code={st_e.get('code')} msg={st_e.get('msg')} data={st_e.get('data')}",
            )

            # (a) abort the ACTIVE prompt (p1).
            ab_a = rest.post(f"/sessions/{sid}/prompts/{pid1}:abort")
            kap.obs(
                "S2.5 abort active prompt",
                "active prompt abort -> code 0 {aborted:true}",
                f"code={ab_a.get('code')} msg={ab_a.get('msg')} data={ab_a.get('data')}",
            )

            # Idempotent re-abort of the already-aborted active prompt.
            ab_a2 = rest.post(f"/sessions/{sid}/prompts/{pid1}:abort")
            kap.obs(
                "S2.6 re-abort completed prompt",
                "second abort of same prompt -> 40903 {aborted:false} (idempotent)",
                f"code={ab_a2.get('code')} msg={ab_a2.get('msg')} data={ab_a2.get('data')}",
            )

            # Abort a never-existent prompt id.
            ab_x = rest.post(f"/sessions/{sid}/prompts/does-not-exist:abort")
            kap.obs(
                "S2.7 abort unknown prompt",
                "unknown prompt id -> 40402",
                f"code={ab_x.get('code')} msg={ab_x.get('msg')}",
            )

            # Collect events until the queue fully drains. A single
            # work_changed(busy=false) is NOT sufficient: aborting the active
            # prompt produces a transient idle before the next prompt starts.
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                ws.wait_for(
                    lambda ev: ev.type == "event.session.work_changed"
                    and (ev.payload or {}).get("busy") is False,
                    timeout=max(1, deadline - time.monotonic()), collect=events)
                qp = (rest.get(f"/sessions/{sid}/prompts").get("data") or {})
                if qp.get("active") is None and not qp.get("queued"):
                    break
                time.sleep(1)
            # trailing drain for late frames
            ws.wait_for(lambda ev: False, timeout=3, collect=events)
            def attributed_ids(e: kap.WsEvent) -> set[str]:
                """All prompt ids a prompt.* payload attributes to. prompt.steered
                uses activePromptId + promptIds; others use promptId."""
                p = e.payload or {}
                ids: set[str] = set()
                for k in ("promptId", "prompt_id", "activePromptId"):
                    if isinstance(p.get(k), str):
                        ids.add(p[k])
                for k in ("promptIds", "prompt_ids"):
                    if isinstance(p.get(k), list):
                        ids.update(x for x in p[k] if isinstance(x, str))
                return ids

            prompt_evts = [
                (e.seq, e.type, sorted(attributed_ids(e)),
                 (e.payload or {}).get("status") or (e.payload or {}).get("reason"))
                for e in events if e.type.startswith("prompt.")]
            known = {pid1, pid2, pid3}
            unattributable = [t for t in prompt_evts if any(i not in known for i in t[2])]
            missing_id = [t for t in prompt_evts if not t[2]]
            kap.log("prompt.* event stream (seq, type, attributed prompt ids, status/reason):")
            for t in prompt_evts:
                kap.log(f"  {t}")
            kap.obs(
                "S2.8 event attribution",
                "every prompt.* event attributes to prompt ids within {p1,p2,p3} "
                "(prompt.steered uses activePromptId+promptIds)",
                f"{len(prompt_evts)} prompt.* events; unattributable={unattributable}; missing_id={missing_id}",
                "PASS" if not unattributable and not missing_id else "FAIL",
            )
            kap.obs(
                "S2.9 final queue state",
                "queue drains to empty after aborts",
                f"prompts now: {rest.get(f'/sessions/{sid}/prompts').get('data')}",
            )
        finally:
            ws.close()
    finally:
        srv.stop()


if __name__ == "__main__":
    main()
