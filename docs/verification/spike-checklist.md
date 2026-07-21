# Spike Verification Checklist (Milestone 0)

> Type: verification (validation support, not product semantics).
> Purpose: before writing any business code, run scripts against a real kap-server to verify
> the "needs live testing" items listed in `docs/research/kap-server-usability.md` §7.
> **Work may only start when all items pass; if any item falls short of expectations, go back to the corresponding decision doc and re-discuss.**

## General Conventions

- Environment: the kimi-code version used during verification (backfill the result here: `____`; recorded only as the "verified version", not a lock-in — see kite-design §10); `kimi web
  --no-open` launched on local loopback with an isolated temporary `KIMI_CODE_HOME`.
- Form: pure scripts (Python standard library + websockets, or plain curl + wscat); no business
  code enters the repo; scripts may be kept under `scripts/spike/`.
- Record per item: observed behavior, deviation from expectation, conclusion (pass / needs design adjustment).

## S1. Multi-client Concurrency and Approval Routing

- **Verify**: attach two WS clients to the same session; client A sends a prompt via REST;
  after an approval is triggered, client A resolves it.
- **Observe**: whether B receives `event.approval.requested` and `event.approval.resolved`;
  whether `GET .../approvals?status=pending` clears immediately after A resolves; whether a duplicate resolve
  returns 40902.
- **Pass criteria**: broadcasts arrive; idempotency semantics match the docs.
- **Impact**: `docs/architecture/kite-design.md` §4 prompt ownership routing, §6 approval card contract.

## S2. Prompt Queue and abort/steer Boundaries

- **Verify**: enqueue 3 prompts in a row on the same session; while one is active, try
  (a) abort active, (b) abort queued, (c) steer a queued prompt,
  (d) steer against an empty queue.
- **Observe**: error codes and event ordering for each operation (`prompt.submitted/aborted/steered`);
  whether abort-queued is supported; queue state after steer.
- **Pass criteria**: behavior is deterministic and mappable onto KITE's card state machine; no event
  that cannot be attributed to a prompt_id may appear.
- **Impact**: mvp-scope §3 concurrency behavior, whether `/abort` enters the MVP (alignment-pending item 1).

## S3. Durable Replay Window and Epoch Semantics

- **Verify**: produce a long turn with more than 1000 durable events (or shrink the window), with the
  WS disconnected in the middle; reconnect with an old cursor. Separately test the epoch behavior when
  kap-server restarts after a kited-side crash (journal
  across process restarts).
- **Observe**: when `resync_required(buffer_overflow)` is returned and when
  `epoch_changed`; whether the fields needed to rebuild the snapshot are complete (`as_of_seq`,
  `in_flight_turn`, work state).
- **Pass criteria**: both the window-overflow and restart paths deterministically fall into snapshot rebuild, and the snapshot
  is sufficient to redraw the execution card.
- **Impact**: `docs/architecture/kite-design.md` §5 event consumption strategy; adapter-layer resync discipline.

## S4. Managed Subprocess Full Lifecycle

- **Verify**: launch `kimi web --no-open` as a subprocess (or via the startServer shim):
  the +1 behavior when the port is taken, token file generation and permission bits, rotate-token hot reload,
  graceful SIGTERM shutdown, and port/instance-registry leftovers after an abnormal exit.
- **Pass criteria**: launch/shutdown/conflict/rotation all work end to end without manual intervention; the instance registry does not
  mislead later discovery.
- **Impact**: `docs/architecture/kite-design.md` §2 process shape; the alignment-pending item "choose one of the two launch methods".

## S5. Snapshot Rebuild of an In-Progress Session

- **Verify**: while a prompt is in progress (with a pending approval), disconnect all WS connections and call
  `GET .../snapshot` directly; separately, call snapshot on a "cold" session with no WS subscription at all.
- **Observe**: whether in_flight_turn, pending_interaction, recent events, and queue contents are
  complete; whether a cold session is implicitly loaded (side effect).
- **Pass criteria**: after a kited restart, the execution card and approval card can be rebuilt from
  snapshot + `GET .../prompts` alone; if fields are missing, record the gap and go back to `docs/architecture/kite-design.md` §4 to adjust
  the "prompt ownership rebuild" clause.
- **Impact**: mvp-scope §4.6 restart-recovery clause.

## S6. (Extra) Question Trigger Frequency Survey

- **Verify**: run a set of representative prompts (writing code, installing dependencies, web search) and count
  the frequency and shape of question.requested.
- **Pass criteria**: no hard criteria; the output feeds mvp-scope alignment-pending item 2 (whether the question form
  stays in the MVP).
