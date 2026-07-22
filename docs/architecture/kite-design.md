# KITE Overall Architecture Design (Draft)

> Status: **active** (first alignment round + spike validation completed 2026-07-21; see `docs/verification/spike-results.md`). Any inconsistency between this document and the code is a contract gap.

## 1. Goals and Non-goals

### Goals

- Feishu conversations (single chat first, group chat later) drive kimi-code sessions: send prompts, observe streaming output, approve, query/switch sessions.
- Single shared backend: all Feishu conversations in one instance operate sessions through the same kap-server.
- Reasonable behavior: every state axis and every failure mode has an explicit contract; fail-closed.

### Non-goals

- Local TUI wrapper (the counterpart of FOCUS's `focus`/`fcodex`) — see
  `docs/decisions/process-shape-and-language.md`.
- Memory, voice ASR, device control, text-to-image, MCP/Skills hot reload — see
  `docs/research/okbot-vs-focus.md`.
- Session deletion (upstream has no such capability; do not work around it).
- Multi-instance / cross-instance coordination (MVP assumes a single instance; see
  `docs/decisions/concurrency-model.md`).

## 2. Process Shape

```
┌─────────────────────────────────────────────┐
│ kited (Python daemon, managed by systemd    │
│ --user etc.)                                │
│  ┌───────────────────────────────────────┐  │
│  │ Feishu transport → application → adapter │  │
│  └──────────────┬────────────────────────┘  │
│                 │ spawn & supervise (managed child) │
│                 ▼                           │
│  ┌───────────────────────────────────────┐  │
│  │ kap-server (kimi web --no-open,        │  │
│  │ also serves local web UI)              │  │
│  │  REST /api/v1  +  WS /api/v1/ws       │  │
│  └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
kitectl —— local admin surface (config/start-stop/binding/session/prompt/image)
```

- `kited` is the parent process of kap-server: it handles spawning, port-conflict
  retry, token reading, crash restart, and graceful shutdown. kap-server itself
  has no daemon mode; KITE's managed shape fills exactly that gap (isomorphic to
  FOCUS's managed mode for codex app-server).
- kap-server is not registered as a service on its own; it lives and dies with
  `kited`.
- Authentication reuses kap-server's own token (`~/.kimi-code/server.token`);
  KITE does not create a second credential. WS connections authenticate via
  `Authorization: Bearer`.
- The same kap-server doubles as the **local operation surface**: the web UI
  bundled with `kimi web` is a pure /api/v1 client, equal in standing to KITE,
  and naturally shares the same set of sessions (queueing and broadcast are
  guaranteed upstream; see `docs/decisions/concurrency-model.md`). It binds to
  127.0.0.1 by default; LAN/phone access (exposed via `--host`) is off by
  default — see aligned item 5 in the last section.

## 3. Layers

| Layer | Responsibility | Key constraint |
| --- | --- | --- |
| Feishu transport | lark-oapi WS long connection; message dedup, send/receive, card patch, attachment download | Depends only on the Feishu SDK; does not know kap-server |
| Application | command routing, binding resolution, state machines, card model, approval routing | All cross-connection/cross-task state changes are serialized through the single-threaded **RuntimeLoop** |
| Adapter | kap-server REST client + WS subscription client; type normalization; resync discipline | kap's schema/envelope/DomainEvent **may only appear in this layer** |
| Local state | JSON stores for bindings, UI transient state, event cursors, etc. (atomic write + file lock) | Session metadata has kimi-code as its single source of truth; not replicated locally |

Vocabulary uniformly adopts kimi-code native terms: **session, agent, prompt,
approval, question**. Do not introduce codex-era thread/turn naming (this batch
of symbols is the first that must be renamed when porting FOCUS assets). Note
that kap-server's session contains an agent dimension, and prompts are sent to
an agent inside a session — this layer must be preserved when modeling state
axes.

## 4. State Axes

The MVP recognizes only four axes, each with a clear owner:

1. **binding** (local, persistent): the logical bookmark of chat ↔ session.
   Survives kited restarts.
2. **attached/detached** (local, persistent): whether a chat receives Feishu
   pushes for that session.
3. **work state** (upstream, obtained via subscription): whether a session is
   busy and whether it has pending_interaction — comes from
   `event.session.work_changed`; KITE does not infer it on its own, and rebuilds
   it from a REST snapshot after a disconnect.
4. **prompt ownership** (local, in-memory): which chat initiated each
   active/queued prompt; determines whom approval/form cards route to. After a
   restart, rebuilt on a best-effort basis via `GET .../prompts` + snapshot;
   approval cards that cannot be rebuilt are explicitly expired and closed out
   (fail-closed).

**Reserved concepts (not implemented)**: interaction owner (write-exclusive
lease), cross-instance loaded gate. Registered in
`docs/decisions/concurrency-model.md`; introduce them only when the product
proves the need. When that happens, any new axis must be added to this document
before the code changes.

## 5. Event Consumption Strategy

**Durable first, volatile later.**

- The sole driver of MVP card updates is durable events
  (turn.started / tool.call.* / turn.ended / prompt.aborted / prompt.steered /
  approval.* / question.* / session.work_changed). There are no unreliable
  events on this path. `prompt.submitted` / `prompt.completed` are
  schema-defined but have **no producer** in agent-core-v2 (spike S2):
  submission is acknowledged by the REST response; completion is observed via
  `turn.ended` / `prompt.aborted`.
- Disconnect-compensation discipline is concentrated in one place in the
  adapter: resubscribe with the `{seq, epoch}` cursor → on `resync_required`
  or window overflow → rebuild from a REST snapshot → refresh cards wholesale
  from the rebuilt result. Spike S3 nuances the adapter must honor:
  a `resync_required` frame may arrive **before** the subscribe ack (buffer
  frames while awaiting acks); subscribing to a cold session right after a
  kap-server restart yields an unexplained `resync_required` (lazy
  activation) — warm the session with a resume-backed REST call
  (`GET .../prompts`) before subscribing; the journal survives restarts with
  the same epoch (the epoch rotates only on journal corruption).
- Cursor source of truth: WS subscribe acks and snapshot `as_of_seq`. REST
  `session.last_seq` is a hardcoded placeholder 0 (spike S3) — never use it.
- WS has no heartbeat: the adapter implements stale detection (proactively
  reconnect when no frame of any kind arrives for N seconds).
- **Volatile streaming** (assistant.delta per-token patch) is an independent
  enhancement, deferred to Phase 2; then it will use offset-gap detection, and
  any gap falls into the snapshot-rebuild path — never guess.

## 6. Card Model

Carry over FOCUS experience, rewritten to kap event semantics:

- **Single-anchor execution card**: at most one current execution card per chat
  at any moment, anchored by `{chat_id, session_id, prompt_id,
  card_message_id}`; prompt-scoped events must match prompt_id to modify the
  card (kap's prompt FIFO semantics make this simpler than FOCUS's turn
  matching: queued prompts do not create a card; only started ones do).
- **Terminal result card**: when a prompt finishes (completed/aborted/failed),
  send a separate terminal result card and freeze the execution card; the
  terminal text is stored locally for `/last`-style commands to read.
- **Approval card**: approval.requested → three-button card
  (approve/reject/feedback); patched and frozen after the REST response;
  repeated clicks within the 60s idempotency window get an "already processed"
  notice, not an error.
- question form card: in the MVP, passed through as text (list the options,
  reply with a number to choose); a rich form comes in Phase 2.

## 7. Persistence

- All JSON files + atomic write (tmp + rename); no SQLite. Write
  serialization: single writer (kited) with in-process locks — FOCUS's proven
  discipline; advisory file locks only where a file may be written across
  processes. Atomic rename makes cross-process reads (e.g., `kitectl`) safe
  without locks.
- stores: binding store (chat ↔ session, attached, permission mode, plan
  mode), terminal result store, event cursor store (per-session
  `{seq, epoch}`), attachment staging store (later).
- Binding-level **permission mode** (mapping to kap `permission_mode`:
  auto/manual/yolo) and **plan mode** (kap `plan_mode`, a separate boolean)
  are persisted; once written to disk they **do not drift with
  the instance default**, and every prompt carries them explicitly (kap natively
  supports per-prompt override, which happens to implement FOCUS's "reapply
  explicitly every turn" contract). The **model** is likewise carried
  explicitly on every prompt — REST-created sessions inherit neither the env
  overlay nor `config.toml`'s `default_model` (spike-results §0); it resolves
  from `kap.model` config → `config.toml` `default_model`.
- Session metadata on the kimi-code side (id, cwd, title, history) has
  `~/.kimi-code` as its single source of truth; KITE does not copy or mirror it.

## 8. Command Surface (Draft)

| Command | Purpose |
| --- | --- |
| `kited` | daemon entrypoint, invoked by the service manager |
| `kitectl` | local admin surface: config / service / binding / session / prompt / image |

Feishu slash commands: see `docs/contracts/mvp-scope.md`.

**Explicitly no** `kite` / `kcode` local TUI wrapper commands (see decisions).
If upstream supports remote attach in the future, add them back following
FOCUS's wrapper design; the command names `kite` (local entrypoint) and `kcode`
(an alias emphasizing the thin Kimi Code shell) are reserved.

## 9. Service and Deployment

- Platform dispatch reuses FOCUS's service_manager design: Linux systemd
  --user, macOS launchd, Windows Task Scheduler.
- Install: `install.sh` → managed venv + wrapper + service definition (written
  but not started); `pip install .` / `-e .` is forbidden (same discipline as
  FOCUS).
- Single instance is the premise: one config/data directory; multi-instance
  (multiple Feishu apps) is registered as a Phase 3 candidate, and requires a
  cross-instance concurrency contract first.

## 10. Upstream Dependency Management (aligned 2026-07-21: follow, don't pin)

- **Do not hard-pin the kimi-code version.** Both kimi-code and kap-server are
  evolving fast (kap-server went from appearing to being investigated in just
  two weeks); as a brand-new project, KITE chooses to follow the evolution: it
  does not expect to stay on one old version long-term — that would actually
  hinder KITE's own evolution; keep the freedom to start over at any time.
- At install/startup, detect the upstream version; when it differs from the
  "verified version", **warn but do not block**. Current verified version:
  **kimi 0.28.1** (spike passed 2026-07-21;
  `docs/verification/spike-results.md`).
- CI guardrail: snapshot-diff `/openapi.json` and the WS operation catalog
  pulled from the target kap-server, and run adapter contract tests (loopback
  against a real kap-server). The snapshot diff is a **drift-awareness**
  mechanism; when drift is found, adapt explicitly inside the adapter and
  update the verified version.
- The adapter is the only place in the whole repo allowed to know upstream
  schemas; any upstream-drift adaptation change must be confined to the
  adapter.

## Aligned (2026-07-21)

1. How kap-server is launched: a `kimi web --no-open` child process. It is both
   the backend and the local web UI operation surface (see
   `docs/decisions/concurrency-model.md`); the TS shim route is abandoned.
2. Config/data directories: KITE's own directories (modeled on FOCUS's
   platform_paths: `~/.config/kite` + `~/.local/share/kite`), not under
   `~/.kimi-code`.
3. Python-side reuse: fork-copy modules from FOCUS, then rename and rework them
   to KITE vocabulary; no shared package (to avoid the two repos dragging each
   other).
4. `kitectl prompt send` is included in the MVP (the control-plane entry for
   later scheduled capabilities).
5. LAN exposure: loopback by default; whether to provide a `kitectl` config
   option to enable `--host` exposure is deferred to Phase 2 (when enabled, it
   must also prompt to set `KIMI_CODE_PASSWORD` and warn about the no-TLS
   risk).
6. Spike Milestone 0 passed on kimi 0.28.1 (2026-07-21;
   `docs/verification/spike-results.md`); its findings are folded into §5
   (event consumption) and §10 (verified version).
