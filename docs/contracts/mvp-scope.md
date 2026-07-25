# MVP Feature Contract (Draft)

> Status: **active** (first alignment round + spike validation completed
> 2026-07-21; see `docs/verification/spike-results.md`). This document
> defines what the KITE MVP does, what it does not do, and the failure mode of
> each behavior. Any inconsistency between code behavior and this document is
> a contract gap.

## 1. Carrying-Capacity Gate

Before any feature (including post-MVP ones) enters development, it must
first answer in a contract document:

1. **Which layer does it belong to?** (Feishu transport / application /
   adapter / local state)
2. **Which state axis does it touch?** (binding / attached / work state /
   prompt ownership; a new axis must first change `kite-design.md`)
3. **How does it recover after a crash/restart?** (can durable events +
   snapshot rebuild it?)
4. **Which tests lock down the behavior?**

If it cannot answer, cut the requirement. No exceptions.

## 2. MVP Scope

### Included

| Feature | Behavior contract |
| --- | --- |
| Single-chat text conversation | plain text → `POST /sessions/{id}/prompts`; the execution card is updated driven by durable events; a terminal result card is sent when the prompt completes |
| Automatic session creation on first use | first message of an unbound chat: create a session with its cwd (the instance `default_working_dir`) and bind it |
| Approval card | approval.requested → three-button card (approve / reject / reject with feedback) → REST response → card patch to freeze |
| question form card | question.requested → option-button card (answer via buttons, numbered reply as fallback); auto-dismiss on timeout |
| `/new` | unbind the current session, create a new session and bind it; the old session is kept as-is (not archived; upstream has no delete) |
| `/sessions` | list sessions visible on kap-server (title/cwd/busy), switch binding via buttons |
| `/switch <id>` | switch the binding to an existing session (auto-attached) |
| `/detach` / `/attach` | pause/resume Feishu push for the current binding; the binding itself is kept |
| `/mode <auto\|manual\|yolo>` | read/write the binding-level permission mode (kap `permission_mode`); carried explicitly on every prompt |
| `/plan [on\|off]` | read/toggle the binding-level plan mode (kap `plan_mode`, orthogonal to permission mode); carried explicitly on every prompt |
| `/status` | show binding, session, work state, queue status |
| `/last` | reply with the bound session's most recent terminal result text from the local store (truncated past 15000 chars) |
| `/abort` | abort the active prompt; only available to that prompt's initiator and admins; aborting an already-finished prompt gets upstream 40402 (not pending) → show "already finished", do not transition the card to failed (spike S2) |
| `/help` | command navigation |
| `/whoami` | show the sender's identity (open_id, display name, admin status), chat/binding state; available to non-admins |
| `kitectl` | config / service (start/stop, status, log) / binding (list) / session (list, status) / prompt send |

### Not included (Non-goals, explicitly rejected during the MVP)

- Group chats (all of Phase 2)
- Image/attachment inbound and outbound (Phase 2/3)
- Volatile streaming cards (Phase 2)
- Local TUI wrapper (`kite`/`kcode` commands)
- Multi-instance, multi Feishu apps
- Session delete, fork, compact, undo (upstream capabilities exist, but the
  MVP does not expose them; exposing them requires their own contracts and
  tests)
- Memory, voice, device control, MCP/Skills management (permanent non-goals)

## 3. Concurrency Behavior (cross-referenced with concurrency-model.md)

- Multiple messages sent in a row in the same chat: all enter kap's prompt
  FIFO; the execution card shows the active prompt, and the queue length is
  visible on the card; **no "new message interrupts"** (the MVP does not
  expose a steer user surface). `/abort` is in the MVP: only available to the
  active prompt's initiator and admins.
- Multiple chats bound to the same session (only reachable via explicit admin
  operation): prompts all enqueue; normal output is broadcast to all attached
  chats; approval/form cards are sent only to the **chat that initiated the
  prompt**, and other chats see a read-only notice "waiting for the initiator
  of prompt #N to handle the approval".
- Approval timeout (default 5 minutes, configurable): respond to upstream as
  rejected and explicitly notify the initiator; never auto-approve (**never
  fail-open**).

## 4. Fail-Closed List

In all of the following cases, report an explicit error and close out
explicitly; "best-effort" silent degradation is forbidden:

1. kap-server unreachable / token invalid → reply with a clear error; do not
   enqueue, do not create a card.
2. WS event stream broken → stale detection reconnect + snapshot rebuild; if
   the rebuild fails, the execution card freezes as "state unknown", with a
   `kitectl session status` troubleshooting hint, **never guess the state**.
3. `resync_required` (window exceeded / epoch change) → snapshot rebuild,
   same as item 2.
4. Approval/form response REST returns an idempotency conflict (40902) → show
   "already handled", card freezes.
5. Prompt REST returns a business error code → the execution card transitions
   directly to terminal (failed), showing the upstream msg.
6. kited restart → binding/permission mode/plan mode/cursor restored from the
   store;
   in-memory prompt ownership is rebuilt best-effort from `GET .../prompts` +
   snapshot; approval cards that cannot be rebuilt are explicitly expired
   (card patched to "expired, please re-initiate or handle locally").
7. Session archived upstream → the next message errors and suggests switching
   via `/sessions`; do not auto-create a new one (**no implicit decisions on
   the user's behalf**).

## 5. Permissions and Identity

- The first admin registers by sending `/init <token>` to the bot (the init
  token is generated at install time, flow modeled on FOCUS); the admin set is
  stored in the instance config.
- The MVP has only two levels: **admin** (all commands + `kitectl`) and
  **non-admin** (cannot use, except `/help` and `/whoami`). An allowlist
  (multi-user) is a Phase 2 candidate.
- The binding-level permission mode defaults to `auto`; `yolo` requires an
  explicit admin setting, and every setting announces in the chat
  "auto-approval is now enabled for this chat".

## 6. Metrics and Observability

- Structured logs: one single-line log containing
  `{chat_id, session_id, prompt_id}` for every prompt lifecycle event
  (submitted/started/ended), every approval resolution, and every
  resync/snapshot rebuild.
- `kitectl session status` output: binding mapping, work state, queue depth,
  WS connection age, and last resync time.

## Aligned (2026-07-21)

1. `/abort` enters the MVP, available only to the active prompt's initiator
   and admins.
2. The question form enters the MVP, implemented as an option-button card
   (numbered reply as fallback). Spike S6's survey is kept, but its purpose
   changes from "basis for the trade-off" to "design input" (what question
   types and option shapes exist determines the card layout).
3. `/sessions` MVP is one page, sorted by most recent activity; pagination
   will be revisited once the session count grows.
4. Admin registration uses the FOCUS-style init token flow (a token is
   generated at install time, and `/init <token>` in Feishu registers the
   first admin).
5. `/mode` enum corrected to upstream `auto/manual/yolo` (evidence:
   `packages/protocol/src/rest/prompt.ts:41`); `plan` is not a
   `permission_mode` value but the separate boolean `plan_mode`, exposed as
   `/plan [on|off]` (corrected against upstream code, 2026-07-21).
6. Spike Milestone 0 passed on kimi 0.28.1 (2026-07-21;
   `docs/verification/spike-results.md`); the `/abort` row now covers the
   observed 40402 re-abort behavior.
7. `/new` is denied while the bound session has an active prompt (fail-closed:
   in-flight work must not lose its visibility) (2026-07-23).
8. kited shutdown fail-closes all pending approvals/questions: they are
   responded upstream (approvals → rejected, questions → dismissed) and the
   cards are patched to expired/closed locally, even when kap is unreachable
   (2026-07-23).
9. The running execution card carries a 取消执行 button (same permission
   rule as `/abort`: initiator or admin); clicks are idempotent — an
   already-finished prompt answers "已结束" (40402) (2026-07-24).
10. `kitectl interaction sweep [--session <id>] [--yes]` rejects/dismisses
    stale pending approvals/questions upstream (dry-run without `--yes`);
    these are upstream kap resources, so it talks to kap REST directly
    (2026-07-24).
