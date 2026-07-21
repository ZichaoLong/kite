#!/usr/bin/env python3
"""S1 — multi-client concurrency + approval routing/idempotency.

Two WS clients subscribe to one session. A prompt with permission_mode=manual
asks the model to run a shell command -> approval requested. "Client A"
resolves via REST. Both clients must observe event.approval.requested and
event.approval.resolved; GET approvals?status=pending must clear; duplicate
resolve must return 40902; unknown approval id -> 40404.

Requires a working model (KIMI_API_KEY etc.).
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kap


def main() -> None:
    kap.log("=== S1: multi-client + approval idempotency ===")
    workdir = tempfile.mkdtemp(prefix="kite-spike-s1-")
    srv = kap.Server().launch()
    try:
        rest = srv.rest()
        sid = kap.create_session(rest, workdir, title="spike-s1")
        kap.log(f"session {sid} on port {srv.port}")

        a = srv.ws("A")
        b = srv.ws("B")
        try:
            ack_a = a.hello_handshake(subscriptions=[sid])
            ack_b = b.hello_handshake(subscriptions=[sid])
            kap.log(f"A hello ack: accepted={ack_a['payload'].get('accepted_subscriptions')} "
                    f"cursors={ack_a['payload'].get('cursors')}")
            kap.log(f"B hello ack: accepted={ack_b['payload'].get('accepted_subscriptions')}")
            kap.obs(
                "S1.0 dual subscribe",
                "both WS clients accepted on the same session",
                f"A accepted={ack_a['payload'].get('accepted_subscriptions')}, "
                f"B accepted={ack_b['payload'].get('accepted_subscriptions')}",
            )

            events_a: list[kap.WsEvent] = []
            events_b: list[kap.WsEvent] = []
            sub = kap.submit_prompt(
                rest, sid,
                "Run the shell command `echo spike-s1-marker` now using your shell tool. "
                "Do not explain, just call the tool.",
                permission_mode="manual",
            )
            kap.log(f"prompt submit: code={sub.get('code')} data={sub.get('data')}")
            pid = (sub.get("data") or {}).get("prompt_id")

            # Wait for approval.requested on BOTH clients.
            is_req = lambda ev: ev.type == "event.approval.requested"
            ra = a.wait_for(is_req, timeout=120, collect=events_a)
            rb = b.wait_for(is_req, timeout=10, collect=events_b) if ra else None
            if ra is None:
                kap.obs("S1.1 approval broadcast", "event.approval.requested on A and B",
                        f"no approval.requested within 120s. A saw: "
                        f"{[e.type for e in events_a][-20:]}", "BLOCKED(model?)")
                return
            aid = ra.payload.get("approval_id")
            kap.obs(
                "S1.1 approval broadcast",
                "event.approval.requested delivered to BOTH WS clients",
                f"A got it (approval_id={aid}, tool={ra.payload.get('tool_name')}, "
                f"action={ra.payload.get('action')}); B got it: {rb is not None}",
                "PASS" if rb is not None else "FAIL",
            )

            # Pending list shows the approval.
            pend = rest.get(f"/sessions/{sid}/approvals?status=pending")
            items = (pend.get("data") or {}).get("items", [])
            kap.obs(
                "S1.2 pending list before resolve",
                "GET approvals?status=pending contains exactly this approval",
                f"items={[i.get('approval_id') for i in items]}",
                "PASS" if any(i.get("approval_id") == aid for i in items) else "FAIL",
            )

            # Client A resolves via REST.
            res = rest.post(f"/sessions/{sid}/approvals/{aid}", {"decision": "approved"})
            kap.obs(
                "S1.3 resolve via REST",
                "code=0, data.resolved=true",
                f"code={res.get('code')} data={res.get('data')}",
                "PASS" if res.get("code") == 0 and (res.get("data") or {}).get("resolved") else "FAIL",
            )

            # Both clients observe approval.resolved; pending list clears.
            is_res = lambda ev: ev.type == "event.approval.resolved"
            ea = a.wait_for(is_res, timeout=20, collect=events_a)
            eb = b.wait_for(is_res, timeout=20, collect=events_b)
            pend2 = rest.get(f"/sessions/{sid}/approvals?status=pending")
            items2 = (pend2.get("data") or {}).get("items", [])
            kap.obs(
                "S1.4 resolved broadcast + pending clears",
                "event.approval.resolved on A and B; pending list empty",
                f"A resolved evt={ea is not None} (decision={ea.payload.get('decision') if ea else None}); "
                f"B resolved evt={eb is not None}; pending after={items2}",
                "PASS" if (ea and eb and items2 == []) else "FAIL",
            )

            # Duplicate resolve -> 40902; unknown id -> 40404.
            dup = rest.post(f"/sessions/{sid}/approvals/{aid}", {"decision": "approved"})
            unk = rest.post(f"/sessions/{sid}/approvals/nonexistent-id", {"decision": "approved"})
            kap.obs(
                "S1.5 resolve idempotency",
                "duplicate resolve -> 40902 with data.resolved=false; unknown id -> 40404",
                f"duplicate: code={dup.get('code')} data={dup.get('data')}; "
                f"unknown: code={unk.get('code')}",
                "PASS" if dup.get("code") == 40902 and unk.get("code") == 40404 else "FAIL",
            )

            # Let the turn finish; record the event stream shape for reference.
            done = a.wait_for(lambda ev: ev.type == "turn.ended", timeout=120,
                              collect=events_a)
            durable_types = [e.type for e in events_a if not e.volatile]
            kap.log(f"turn.ended seen: {done is not None}; "
                    f"durable events on A: {durable_types}")
            prompt_events = [(e.type, (e.payload or {}).get("promptId") or (e.payload or {}).get("prompt_id"))
                             for e in events_a if e.type.startswith("prompt.")]
            kap.obs(
                "S1.6 prompt attribution",
                "prompt.* events carry a prompt id attributable to the submitted prompt",
                f"submitted prompt_id={pid}; prompt.* events seen: {prompt_events}",
            )
        finally:
            a.close()
            b.close()
    finally:
        srv.stop()


if __name__ == "__main__":
    main()
