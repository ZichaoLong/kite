#!/usr/bin/env python3
"""S3 — durable replay window + epoch semantics.

Part 0 (static): is the replay window configurable? Source answer:
  packages/kap-server/src/transport/ws/v1/sessionEventBroadcaster.ts:180
  DEFAULT_MAX_BUFFER_SIZE = 1000 — constructor option only; start.ts does not
  pass it and the `kimi web` CLI exposes no flag. Runtime check: server_hello
  advertises max_event_buffer_size.

Part A (buffer_overflow): generate >1000 durable events WITHOUT model calls by
repeatedly renaming the session (POST /sessions/{id}/profile emits durable
`session.meta.updated` — verified live below). Disconnect, overflow, reconnect
with the old cursor -> expect resync_required(buffer_overflow).

Part B (partial replay): reconnect with a cursor only ~10 events behind ->
incremental replay, no resync.

Part C (restart/epoch): restart the server on the SAME home; resubscribe with
the pre-restart cursor. Source says the journal file survives, so seq+epoch
survive: expect plain replay, NOT epoch_changed. Then resubscribe with a
bogus epoch -> expect resync_required(epoch_changed).

Part D (snapshot sufficiency): print snapshot fields and judge whether a card
can be redrawn from them.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kap

RENAME_OVERFLOW = 1050


def main() -> None:
    kap.log("=== S3: replay window + epoch ===")
    workdir = tempfile.mkdtemp(prefix="kite-spike-s3-")
    srv = kap.Server().launch()
    home = srv.home
    try:
        rest = srv.rest()
        sid = kap.create_session(rest, workdir, title="spike-s3")
        kap.log(f"session {sid}")

        # --- confirm rename emits a durable session.meta.updated event ---
        ws = srv.ws("S3-probe")
        ack = ws.hello_handshake(subscriptions=[sid])
        kap.obs(
            "S3.0 advertised buffer size",
            "server_hello.max_event_buffer_size == 1000 (window NOT configurable via CLI)",
            f"max_event_buffer_size={ws.hello.get('max_event_buffer_size')}",
        )
        cursor0 = (ack["payload"].get("cursors") or {}).get(sid)
        kap.log(f"initial cursor: {cursor0}")
        probe: list[kap.WsEvent] = []
        r = rest.post(f"/sessions/{sid}/profile", {"title": "probe-rename"})
        ev = ws.wait_for(lambda e: e.type == "session.meta.updated", timeout=8, collect=probe)
        kap.obs(
            "S3.1 rename -> durable event",
            "POST profile(title) emits durable session.meta.updated with a seq",
            f"profile code={r.get('code')}; event={'seq=' + str(ev.seq) if ev else 'NONE'}",
        )
        ws.close()
        if ev is None:
            kap.obs("S3", "rename-driven overflow feasible",
                    "rename emits no durable event; overflow via model calls too slow "
                    "for the spike timebox", "BLOCKED(no cheap durable event)")
            return

        # --- Part A: overflow the window ---
        kap.log(f"generating {RENAME_OVERFLOW} durable events via rename (no model calls)...")
        t0 = time.monotonic()
        for i in range(RENAME_OVERFLOW):
            rr = rest.post(f"/sessions/{sid}/profile", {"title": f"rename-{i}"})
            if rr.get("code") != 0:
                kap.log(f"rename {i} failed: {rr}")
                break
            if i % 200 == 199:
                kap.log(f"  {i + 1} renames in {time.monotonic() - t0:.1f}s")
        cur = rest.get(f"/sessions/{sid}")
        last_seq = (cur.get("data") or {}).get("last_seq")
        kap.log(f"session last_seq now {last_seq} (cursor was {cursor0}) "
                f"-- note: REST session.last_seq is a placeholder, journal seq comes from the ack")

        # Reconnect with the stale cursor -> expect resync_required(buffer_overflow)
        ws2 = srv.ws("S3-stale")
        events2: list[kap.WsEvent] = []
        # subscribe ack carries resync_required list; the standalone
        # resync_required frame (with reason) is sent BEFORE the ack and lands
        # in ws.pre_frames.
        hello2 = ws2.hello_handshake(subscriptions=[sid], cursors={sid: cursor0})
        kap.log(f"stale-cursor hello ack: {hello2}")
        resync_list = hello2["payload"].get("resync_required", [])
        pre = [f for f in ws2.pre_frames if f.get("type") == "resync_required"]
        pre_reason = (pre[0].get("payload") or {}).get("reason") if pre else None
        server_current = (hello2["payload"].get("cursors") or {}).get(sid)
        kap.obs(
            "S3.2 buffer overflow on stale cursor",
            "reconnect with cursor >1000 behind -> resync_required(buffer_overflow)",
            f"hello ack resync_required={resync_list}; pre-ack frame reason={pre_reason} "
            f"frame={pre[0] if pre else None}; server current cursor={server_current}",
            "PASS" if (resync_list and pre_reason == "buffer_overflow") else "CHECK",
        )
        ws2.close()
        assert server_current, "server did not report current cursor in ack"
        journal_seq = server_current["seq"]

        # --- Part B: small gap replays cleanly ---
        gap_cursor = {"seq": journal_seq - 10, "epoch": cursor0.get("epoch")}
        ws3 = srv.ws("S3-recent")
        hello3 = ws3.hello_handshake(subscriptions=[sid], cursors={sid: gap_cursor})
        replayed: list[kap.WsEvent] = [kap.WsEvent(f) for f in ws3.pre_frames
                                       if f.get("seq") is not None]
        # Replay continues right after the ack; drain briefly for the rest.
        ws3.wait_for(lambda e: False, timeout=3, collect=replayed)
        seqs = [e.seq for e in replayed if e.seq is not None]
        kap.obs(
            "S3.3 small-gap replay",
            "cursor ~10 behind -> replay of exactly the missed durable events, no resync",
            f"ack resync={hello3['payload'].get('resync_required')}; "
            f"replayed {len(seqs)} events seq {seqs[:3]}..{seqs[-3:] if seqs else []}",
            "PASS" if not hello3["payload"].get("resync_required") and len(seqs) >= 10 else "CHECK",
        )
        ws3.close()

        # --- Part C: restart on the same home; journal survives ---
        # Cursor 5 behind the watermark: after restart the server must replay
        # those 5 from the on-disk journal (memory tail is empty post-restart).
        pre_restart_cursor = {"seq": journal_seq - 5, "epoch": cursor0.get("epoch")}
        srv.stop()
        kap.log("server stopped; relaunching on the SAME home...")
        srv2 = kap.Server(home=home).launch()
        try:
            rest2 = srv2.rest()
            token_same = srv2.token == srv.token

            # C.1: naive subscribe right after restart. The broadcaster
            # activates sessions via ISessionLifecycleService.get (NOT resume),
            # so a cold session is not subscribable until a REST route
            # (e.g. snapshot) resumes it: expect resync_required with NO
            # standalone reason frame and NO server cursor.
            ws4 = srv2.ws("S3-after-restart")
            hello4 = ws4.hello_handshake(subscriptions=[sid], cursors={sid: pre_restart_cursor})
            new_cursor = (hello4["payload"].get("cursors") or {}).get(sid)
            pre4 = [f for f in ws4.pre_frames if f.get("type") == "resync_required"]
            kap.obs(
                "S3.4a subscribe to cold session right after restart",
                "documented behavior: subscribe before any REST resume -> resync_required "
                "(activation is lazy via REST get/resume; broadcaster uses .get not .resume)",
                f"token reused={token_same}; ack accepted={hello4['payload'].get('accepted_subscriptions')}; "
                f"ack resync={hello4['payload'].get('resync_required')}; "
                f"reason frame={'yes: ' + str(pre4[0]) if pre4 else 'NONE'}; server cursor={new_cursor}",
            )
            ws4.close()

            # C.2: the standard rebuild flow — resume the session via a
            # resume-backed REST route (GET prompts -> resolveSession uses
            # ISessionLifecycleService.resume), then re-subscribe with the
            # ORIGINAL pre-restart cursor. Expect disk-journal replay of the
            # 5 missed events with the SAME epoch: restart alone must not
            # epoch-change. NOTE: GET snapshot in default 'auto' reader mode
            # reads disk directly and does NOT resume the live session.
            snap_c = rest2.get(f"/sessions/{sid}/snapshot")
            snap_cur = {"seq": (snap_c.get("data") or {}).get("as_of_seq"),
                        "epoch": (snap_c.get("data") or {}).get("epoch")}
            warm = rest2.get(f"/sessions/{sid}/prompts")
            ws4b = srv2.ws("S3-after-snapshot")
            hello4b = ws4b.hello_handshake(subscriptions=[sid], cursors={sid: pre_restart_cursor})
            after_events: list[kap.WsEvent] = [kap.WsEvent(f) for f in ws4b.pre_frames
                                               if f.get("seq") is not None]
            ws4b.wait_for(lambda e: False, timeout=3, collect=after_events)
            cursor4b = (hello4b["payload"].get("cursors") or {}).get(sid)
            replayed4 = [e.seq for e in after_events if e.seq is not None]
            kap.obs(
                "S3.4b snapshot-resume, then re-subscribe with pre-restart cursor",
                "journal survives restart: same epoch, replay of the 5 missed events from disk, no resync",
                f"snapshot cursor={snap_cur}; resume-trigger GET prompts code={warm.get('code')}; "
                f"ack resync={hello4b['payload'].get('resync_required')}; "
                f"server cursor={cursor4b}; replayed seqs={replayed4}",
                "PASS" if (not hello4b["payload"].get("resync_required")
                           and cursor4b and cursor4b.get("epoch") == pre_restart_cursor["epoch"]
                           and len(replayed4) >= 5)
                else "CHECK",
            )
            ws4b.close()

            # Bogus epoch -> epoch_changed (session is warm now, so the epoch
            # check actually runs).
            ws5 = srv2.ws("S3-bogus-epoch")
            hello5 = ws5.hello_handshake(
                subscriptions=[sid],
                cursors={sid: {"seq": pre_restart_cursor["seq"], "epoch": "ep_bogus"}})
            pre5 = [f for f in ws5.pre_frames if f.get("type") == "resync_required"]
            reason5 = (pre5[0].get("payload") or {}).get("reason") if pre5 else None
            kap.obs(
                "S3.5 epoch mismatch",
                "cursor with foreign epoch -> resync_required(epoch_changed) with current epoch",
                f"ack resync={hello5['payload'].get('resync_required')}; "
                f"pre-ack frame={pre5[0] if pre5 else None}",
                "PASS" if (hello5["payload"].get("resync_required")
                           and reason5 == "epoch_changed")
                else "CHECK",
            )
            ws5.close()

            # --- Part D: snapshot sufficiency ---
            snap = rest2.get(f"/sessions/{sid}/snapshot")
            data = snap.get("data") or {}
            keys = sorted(data.keys())
            sess = data.get("session") or {}
            kap.obs(
                "S3.6 snapshot fields for card redraw",
                "snapshot carries as_of_seq, epoch, in_flight_turn, work state, recent messages",
                f"code={snap.get('code')}; keys={keys}; as_of_seq={data.get('as_of_seq')}; "
                f"epoch={data.get('epoch')}; busy={sess.get('busy')}; "
                f"pending_interaction={sess.get('pending_interaction')}; "
                f"messages={len((data.get('messages') or {}).get('items', []))} "
                f"(has_more={(data.get('messages') or {}).get('has_more')}); "
                f"in_flight_turn={data.get('in_flight_turn')}",
            )
        finally:
            srv2.stop()
            srv.proc = None  # already stopped; avoid double-handling in outer finally
    finally:
        if srv.proc is not None:
            srv.stop()


if __name__ == "__main__":
    main()
