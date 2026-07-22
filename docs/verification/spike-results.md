# Spike Results (Milestone 0)

> Type: verification (validation support, not product semantics).
> Execution date: 2026-07-21. Verified version: **kimi 0.28.1** (kap-server 0.0.2).
> Scripts: `scripts/spike/` (shared helper `kap.py` + one script per item).
> Spec: `docs/verification/spike-checklist.md`.
>
> **Verdict: ALL PASS (S1–S5 hard gate; S6 survey done).** Findings that
> changed documents are listed in §8; each was folded into the affected doc
> on 2026-07-21, and `contracts/`, `architecture/`, `decisions/` flipped to
> active.

## 0. Environment and Provisioning

- Every run: `kimi web --no-open` subprocess with a fresh temporary
  `KIMI_CODE_HOME` (`mktemp -d`), loopback only; the real `~/.kimi-code` was
  never touched; no orphan `kimi` processes remained after the runs.
- **Provisioning note (matters for kited)**: an isolated `KIMI_CODE_HOME` has
  no provider config, so prompt submit fails with `40110`. agent-core-v2
  reads an env overlay (`KIMI_MODEL_NAME` / `KIMI_MODEL_API_KEY` /
  `KIMI_MODEL_BASE_URL`; `app/provider/configSection.ts` +
  `app/model/envOverlay.ts`). Additionally, sessions created via REST do
  **not** inherit the overlay `defaultModel` — the scripts pass
  `model: "__kimi_env_model__"` per prompt. Model used: `kimi-for-coding`.
  Live finding (2026-07-22, joint debugging): REST-created sessions do not
  inherit `config.toml`'s `default_model` either — KITE therefore carries an
  explicit model on every prompt (resolution: `kap.model` config →
  `config.toml` `default_model`).

## S4. Managed Subprocess Lifecycle — PASS (6/6)

1. Port 58627 occupied → server bound **58628** (+1 retry); instance registry
   `<home>/server/instances/<serverId>.json` had correct pid/port.
2. `server.token`: 43-char base64url, file **0600**, home dir **0700**.
3. `kimi web rotate-token` (same `KIMI_CODE_HOME`) rewrote the token file;
   the running server **hot-reloaded** it: old token → `40101`/HTTP 401 on
   `/meta`, new token → code 0, no restart.
4. SIGTERM → rc 0; instance entry deregistered (directory empty).
5. `kill -9` → stale entry remained; a fresh launch on the same home
   succeeded and **lazily swept the dead-pid entry on register**.
6. `POST /api/v1/shutdown` on loopback → `{code:0, data:{ok:true}}`, rc 0,
   deregistration. Client-side gotcha: the POST must not carry
   `Content-Type: application/json` with an empty body (Fastify 50001s it) —
   not a server bug.

## S3. Durable Replay Window and Epoch — PASS (with design-relevant nuances)

- **Window is not configurable at runtime**: `DEFAULT_MAX_BUFFER_SIZE = 1000`
  (`transport/ws/v1/sessionEventBroadcaster.ts:180`), constructor option
  only; `start.ts` does not pass it, no CLI/env. Overflow was generated with
  1050 `POST /sessions/{id}/profile` renames (durable `session.meta.updated`;
  no model calls needed).
- Cursor >1000 behind → `resync_required` (`reason=buffer_overflow`,
  `current_seq`, `epoch`) arrives as a standalone frame **before** the
  subscribe ack (clients must buffer frames while awaiting acks). PASS.
- Cursor 10 behind → exactly the 10 missed events replayed; no resync. PASS.
- **Cold-session subscribe right after a server restart → unexplained
  `resync_required`** (no reason frame, no server cursor): the broadcaster
  activates sessions lazily via `ISessionLifecycleService.get` (not
  `resume`). Workaround: touch a resume-backed REST route
  (`GET /sessions/{id}/prompts`) before subscribing. Noted in
  `kite-design.md` §5.
- After touching a resume-backed route, re-subscribing with the pre-restart
  cursor replays missed events **from the on-disk journal with the SAME
  epoch** — restart alone never rotates the epoch (journal JSONL at
  `<home>/server/events/<sid>.jsonl`; epoch rotates only on journal
  corruption). PASS.
- Foreign epoch → `resync_required(epoch_changed)` with current epoch. PASS.
- Snapshot carries `as_of_seq`, `epoch`, `session(busy/pending_interaction)`,
  `messages{items,has_more}`, `in_flight_turn`, `pending_approvals`,
  `pending_questions`, `subagents` — sufficient to redraw a card. Note:
  `GET /snapshot` in default `KIMI_SNAPSHOT_READER=auto` mode reads disk
  directly and does **not** resume the live session (not a warmup trigger).

## S1. Multi-Client Concurrency and Approval Routing — PASS

Two WS clients on one session both accepted; a manual-mode prompt triggered
`event.approval.requested` on **both**; `GET approvals?status=pending` listed
it; A resolved via REST → `event.approval.resolved` on **both**; pending list
cleared immediately; duplicate resolve → **40902** `{resolved:false}`;
unknown approval id → **40404**.

## S2. Prompt Queue and abort/steer Boundaries — PASS (with contract notes)

- Triple enqueue → `running, queued, queued`; `GET prompts` shows
  active + FIFO queue; concurrent submits queued, never rejected.
- Abort **queued** prompt → code 0 `{aborted:true}`; queue shrinks.
- **Steer URL is single-colon** `POST /sessions/{id}/prompts:steer`; the
  double-colon `prompts::steer` is the route-registration spelling and
  calling it returns 40001. kimi-web uses the single-colon form.
- Steer of a non-pending id → **40402** "one or more prompts are not pending".
- Abort active → code 0. **Re-abort of a finished prompt → 40402**, not the
  documented 40903 (the record is dropped from the queue).
- `prompt.aborted` (queued + active aborts) and `prompt.steered` are durable
  and attributable (`promptId` / `activePromptId+promptIds`); the queue
  drains to `{active:null, queued:[]}`.
- **Gap: `prompt.submitted` and `prompt.completed` have no producer** in
  agent-core-v2 (schema-defined only; grep-confirmed) — they never appear on
  the wire. Prompt lifecycle attribution must rely on the REST submit ack +
  `prompt.aborted`/`prompt.steered` + `turn.*`. Folded into
  `kite-design.md` §5.

## S5. Snapshot Rebuild of an In-Progress Session — PASS

- Warm session (prompt running, approval pending, one queued prompt, all WS
  disconnected): snapshot returned `in_flight_turn{turn_id,
  current_prompt_id, running_tools, assistant_text}`,
  `pending_approvals=[aid]`, `session.busy=true`,
  `pending_interaction=approval`; `GET prompts` showed active + queued.
  Card rebuild from snapshot + prompts works.
- Cold session (after server restart): snapshot code 0 with
  `as_of_seq`/epoch from the journal; **no implicit activation** (no journal
  file created, reader mode). Unknown session → 40401.

## S6. Question Trigger Frequency Survey — done (n=3, no triggers)

Auto-mode prompts (write code / pip install / web search): **0
`event.question.requested`, 0 `event.approval.requested`**; all turns
finished unattended. Sample too small for a rate; the question form did not
fire on these representative flows. (The question form stays in the MVP per
mvp-scope aligned item 2; button-card layout remains the design.)

## 7. Upstream Drift vs `docs/research/kap-server-usability.md`

1. `sessionEventBroadcaster.ts` moved → `src/transport/ws/v1/`.
2. Steer URL on the wire: single-colon `prompts:steer`.
3. REST `session.last_seq` is a hardcoded placeholder 0.
4. Re-abort is 40402, not the documented 40903.
5. Cold-subscribe-after-restart → unexplained `resync_required` (lazy
   activation; was undocumented).
6. `prompt.submitted`/`prompt.completed` have no producers.

All corrections are recorded in `kap-server-usability.md`, "Spike
Corrections (2026-07-21)". Everything else (envelope, error codes
40902/40402/40404, cursor `{seq,epoch}` semantics, WS auth at upgrade,
shutdown route, instance registry, token hot-reload) matched the research
doc and the code.

## 8. Design Adjustments Folded Into Documents (2026-07-21)

- `kite-design.md` §5: durable driver list no longer relies on
  `prompt.submitted/completed`; resync discipline extended with the
  pre-ack frame and cold-session warmup nuances; cursor source of truth
  pinned to WS acks / snapshot `as_of_seq` (never `session.last_seq`).
- `mvp-scope.md`: `/abort` row now covers the 40402 re-abort behavior.
- Verified version backfilled: `kite-design.md` §10 and
  `spike-checklist.md` conventions → **kimi 0.28.1**.

## 9. Reproduction

`python3 scripts/spike/s4_lifecycle.py` (etc. — one script per item; shared
helper `scripts/spike/kap.py`). Scripts require `KIMI_API_KEY` /
`KIMI_BASE_URL` in the environment for model-backed items and always launch
their own isolated-home server.
