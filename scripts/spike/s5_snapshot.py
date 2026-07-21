#!/usr/bin/env python3
"""S5 — snapshot rebuild of an in-progress session.

Flow:
  A. Warm session: prompt running with a PENDING APPROVAL and one queued
     prompt; all WS disconnected -> GET snapshot. Check in_flight_turn,
     pending_approvals / pending_interaction, recent messages, and combine
     with GET prompts for the queue.
  B. Cold session: created over REST, never subscribed, then server RESTARTED
     (guaranteed cold) -> GET snapshot. Observe success + implicit-load side
     effects (journal file created? session busy state?).

Requires a working model for part A.
"""

import glob
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kap


def main() -> None:
    kap.log("=== S5: snapshot of in-progress session ===")
    workdir = tempfile.mkdtemp(prefix="kite-spike-s5-")
    srv = kap.Server().launch()
    home = srv.home
    try:
        rest = srv.rest()
        sid = kap.create_session(rest, workdir, title="spike-s5")
        kap.log(f"session {sid}")

        # --- A. in-progress session with a pending approval ---
        ws = srv.ws("S5")
        events: list[kap.WsEvent] = []
        ws.hello_handshake(subscriptions=[sid])
        p1 = kap.submit_prompt(
            rest, sid,
            "Run the shell command `sleep 120` using your shell tool. Just call the tool.",
            permission_mode="manual")
        pid1 = (p1.get("data") or {}).get("prompt_id")
        req = ws.wait_for(lambda e: e.type == "event.approval.requested",
                          timeout=120, collect=events)
        if req is None:
            kap.obs("S5.A", "approval pending while snapshot taken",
                    f"no approval.requested in 120s; seen {[e.type for e in events][-15:]}",
                    "BLOCKED(model?)")
            return
        aid = req.payload.get("approval_id")
        p2 = kap.submit_prompt(rest, sid, "Reply with exactly: queued-item", permission_mode="manual")
        pid2 = (p2.get("data") or {}).get("prompt_id")
        time.sleep(1.0)  # let the queue settle

        # ALL WS DISCONNECTED now.
        ws.close()
        time.sleep(0.5)

        snap = rest.get(f"/sessions/{sid}/snapshot")
        data = snap.get("data") or {}
        sess = data.get("session") or {}
        ift = data.get("in_flight_turn")
        prompts = rest.get(f"/sessions/{sid}/prompts").get("data") or {}
        kap.obs(
            "S5.A1 snapshot of in-progress session (no WS attached)",
            "in_flight_turn present with current_prompt_id; pending_approvals lists the approval; "
            "session busy + pending_interaction=approval",
            f"code={snap.get('code')}; as_of_seq={data.get('as_of_seq')}; epoch={data.get('epoch')}; "
            f"busy={sess.get('busy')} main_turn_active={sess.get('main_turn_active')} "
            f"pending_interaction={sess.get('pending_interaction')}; "
            f"in_flight_turn={{turn_id:{(ift or {}).get('turn_id')}, current_prompt_id:{(ift or {}).get('current_prompt_id')}, "
            f"running_tools:{len((ift or {}).get('running_tools', []))}, "
            f"assistant_text_len:{len((ift or {}).get('assistant_text', ''))}}}; "
            f"pending_approvals={[a.get('approval_id') for a in data.get('pending_approvals', [])]}; "
            f"pending_questions={data.get('pending_questions')}",
            "PASS" if (ift and (ift or {}).get("current_prompt_id") == pid1
                       and any(a.get("approval_id") == aid for a in data.get("pending_approvals", [])))
            else "CHECK",
        )
        kap.obs(
            "S5.A2 queue visibility alongside snapshot",
            "GET prompts shows active + queued so a card can rebuild the queue",
            f"active={((prompts.get('active') or {}).get('prompt_id'))} "
            f"(== p1 {pid1}); queued={[p.get('prompt_id') for p in prompts.get('queued', [])]} (== p2 {pid2})",
            "PASS" if ((prompts.get("active") or {}).get("prompt_id") == pid1
                       and [p.get("prompt_id") for p in prompts.get("queued", [])] == [pid2])
            else "CHECK",
        )
        msgs = (data.get("messages") or {}).get("items", [])
        kap.obs(
            "S5.A3 recent messages in snapshot",
            "snapshot.messages.items contains the recent history (user prompt visible)",
            f"messages={len(msgs)} has_more={(data.get('messages') or {}).get('has_more')}; "
            f"roles={[m.get('role') for m in msgs][-6:]}",
        )
        # Cleanup: reject the approval so the turn unwinds, then abort.
        rest.post(f"/sessions/{sid}/approvals/{aid}", {"decision": "rejected"})
        rest.post(f"/sessions/{sid}/prompts/{pid1}:abort")
        rest.post(f"/sessions/{sid}/prompts/{pid2}:abort")

        # --- B. cold session snapshot (created, never subscribed, restart) ---
        sid_cold = kap.create_session(rest, workdir, title="spike-s5-cold")
        kap.submit_prompt(rest, sid_cold, "Reply with exactly: cold", permission_mode="yolo")
        # Wait for the turn to finish so the session has history. A throwaway
        # WS subscription is fine here: the server restart below is what makes
        # the session cold again.
        ws_c = srv.ws("S5-coldwait")
        try:
            ws_c.hello_handshake(subscriptions=[sid_cold])
            done_c = ws_c.wait_for(
                lambda e: e.type == "event.session.work_changed"
                and (e.payload or {}).get("busy") is False,
                timeout=120)
            kap.log(f"cold-session turn finished: {done_c is not None}")
        finally:
            ws_c.close()
        srv.stop()
        kap.log("restarting on same home for guaranteed-cold session...")
        srv2 = kap.Server(home=home).launch()
        try:
            rest2 = srv2.rest()
            journal_glob = os.path.join(home, "server", "events", f"{sid_cold}*.jsonl")
            before = glob.glob(journal_glob)
            snap_c = rest2.get(f"/sessions/{sid_cold}/snapshot")
            dc = snap_c.get("data") or {}
            after = glob.glob(journal_glob)
            kap.obs(
                "S5.B1 cold-session snapshot",
                "snapshot succeeds on a session with no in-memory state (code 0)",
                f"code={snap_c.get('code')} msg={snap_c.get('msg')}; "
                f"as_of_seq={dc.get('as_of_seq')}; epoch={dc.get('epoch')}; "
                f"messages={len((dc.get('messages') or {}).get('items', []))}; "
                f"in_flight_turn={dc.get('in_flight_turn')}; busy={(dc.get('session') or {}).get('busy')}",
                "PASS" if snap_c.get("code") == 0 else "FAIL",
            )
            kap.obs(
                "S5.B2 cold snapshot side effects",
                "observe whether the cold snapshot implicitly loads/activates the session "
                "(journal file created as a tell-tale)",
                f"journal files before={ [os.path.basename(p) for p in before] } "
                f"after={ [os.path.basename(p) for p in after] }",
            )
            # Nonexistent session -> 40401
            snap_x = rest2.get("/sessions/no-such-session/snapshot")
            kap.obs(
                "S5.B3 snapshot of unknown session",
                "unknown session id -> 40401",
                f"code={snap_x.get('code')} msg={snap_x.get('msg')}",
            )
        finally:
            srv2.stop()
            srv.proc = None
    finally:
        if srv.proc is not None:
            srv.stop()


if __name__ == "__main__":
    main()
