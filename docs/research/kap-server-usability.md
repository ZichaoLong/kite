# kap-server Usability Investigation

> Type: research (evidence material, not a contract). Investigation date: 2026-07-21.
> Subject: `packages/kap-server` and its surroundings within the kimi-code monorepo
> (`apps/kimi-code`, `apps/kimi-web`, `packages/klient`, `packages/agent-core-v2`).
> kap-server evolves extremely fast; facts in this document may go stale. When they conflict with the latest upstream code, the upstream code wins.

## Conclusion

As KITE's shared backend, kap-server's API surface coverage is already sufficient to support a complete bridge loop
(REST writes + WS subscription + approval/form responses). It is very young (0.0.2, first
commit on 2026-07-12), carries no stability guarantees, and calls for version tracking (follow, don't pin — see `docs/architecture/kite-design.md` §10) + snapshot diff guardrails.

## 1. Lifecycle and Deployment Shape

- `kimi web` launches kap-server **in-process** (`await startServer(...)`), not as a child process:
  `apps/kimi-code/src/cli/sub/web/run.ts:236-312` (`runServerInProcess`, comment
  "The server always runs in the current process, attached to the terminal").
- **No daemon/service support**: foreground process, stopped by Ctrl+C; official comment "there is no
  kill/ps subcommand" (`apps/kimi-code/src/cli/sub/web/index.ts:5-10`).
  Legacy background servers from before 0.28.0 are cleaned up with `kimi server kill` (`legacy-kill.ts`).
- **Exported as a library**: `packages/kap-server/src/index.ts:6` exports `startServer()`;
  omitting `webAssetsDir` yields a pure API server (`start.ts:114-119`).
- Port: default 58627 (`start.ts:139-140`); on EADDRINUSE retries with port+1, up to 100 attempts
  (`start.ts:548`, `listenWithPortRetry` 577-611); `port: 0` uses ephemeral.
- Multi-instance coexistence with no single-instance lock: each instance writes `<home>/server/instances/<serverId>.json`
  (`instanceRegistry.ts`, pid liveness probe + 15s heartbeat + lazy sweep);
  `listLiveServerInstances()` discovers live instances.
- Graceful shutdown: SIGINT/SIGTERM → `app.close()` + `core.dispose()` + instance deregistration;
  there is also `POST /api/v1/shutdown` (enabled by default on loopback; non-loopback requires
  `--allow-remote-shutdown`).

### Token Authentication

- Located at `~/.kimi-code/server.token` (0600, directory 0700, atomic rename write);
  256-bit random → base64url 43 characters (`services/auth/persistentToken.ts:28-31`).
- Auto-generated on first boot, reused across restarts; `kimi web rotate-token` rewrites it atomically;
  a running server **hot-reloads it without restart** via mtime+inode change detection at the next auth check
  (`services/auth/tokenStore.ts:34-66`).
- **No permission bits**: a single all-powerful bearer. Optional second credential `KIMI_CODE_PASSWORD` (bcrypt)
  and startup option `rpcToken`; `--dangerous-bypass-auth` disables auth entirely.
- WS authentication happens at upgrade time: `Authorization: Bearer` or carried via the `sec-websocket-protocol`
  subprotocol (`start.ts:421-458`); after connecting, `client_hello` may carry the token again (defense in depth).

## 2. API Surface Coverage for Bridge Needs

All routes are mounted under `/api/v1` (`routes/registerApiV1Routes.ts`), with a uniform envelope
`{code, msg, data, request_id}` (HTTP always 200; the business result is in code).

| Bridge need | Endpoint | Evidence |
| --- | --- | --- |
| session creation (with cwd) | `POST /sessions`, body `{workspace_id \| metadata.cwd, title?}` | `routes/sessions.ts:248-341` |
| resume | No explicit endpoint — every per-session route internally `resume()`s cold-loaded sessions, transparent to clients | `routes/prompts.ts:103-113` |
| list | `GET /sessions` (id-cursor pagination; busy/archive/workspace filters) | `routes/sessions.ts:343-463` |
| read history | `GET /sessions/{id}/messages`; turn-granularity `GET .../transcript`; IM-style full rebuild `GET .../snapshot` (with `as_of_seq`+`epoch`+`in_flight_turn`) | `routes/messages.ts:85-117`, `routes/transcript.ts:93`, `routes/snapshot.ts:86-99` |
| rename | `POST /sessions/{id}/profile` (title/metadata/agent_config/permission_rules) | `routes/sessions.ts:557-602` |
| archive / restore | `POST /sessions/{id}:archive` / `:restore` | `routes/sessions.ts:747-773` |
| delete | **None** (server-wide only workspaces/files/oauth have DELETE) | confirmed by grep |
| other actions | `:fork` `:compact` `:undo` `:abort` `:btw` (side channel), children | `routes/sessions.ts:604-916` |
| start a turn | `POST /sessions/{id}/prompts` (content: text/image/video/file; may carry per-request `model`/`thinking`/`permission_mode`/`plan_mode`/`goal_*` overrides) | `routes/prompts.ts:163-229`, `protocol/rest-prompt.ts:31-42` |
| queue query | `GET /sessions/{id}/prompts` (active + queued) | `routes/prompts.ts:140-161` |
| interrupt | `POST .../prompts/{pid}:abort`; `POST /sessions/{id}:abort` | `routes/prompts.ts:260-302`, `routes/sessions.ts:721-728` |
| steer | `POST /sessions/{id}/prompts::steer` — **can only inject an already-queued prompt into the active turn**; the engine's `inject()` is not exposed over REST | `routes/prompts.ts:231-258`, `agent-core-v2/.../promptService.ts:115-137` |
| approval push | WS `event.approval.requested` / `event.approval.resolved` | `sessionEventBroadcaster.ts:1281-1348` |
| approval query/response | `GET .../approvals?status=pending`; `POST .../approvals/{aid}` `{decision: approved\|rejected\|cancelled, scope?, feedback?}`; idempotent: repeated resolve within 60s → 40902 | `routes/approvals.ts`, `protocol/approval.ts:5-30` |
| question push/response | WS `event.question.requested/answered/dismissed`; `GET/POST .../questions/{qid}`, `:dismiss` | `routes/questions.ts:1-42` |
| runtime settings | Global `GET/POST /config`; `GET /models`, `POST /models/{alias}:set_default`. **No dedicated endpoint for per-session settings** — only carried along with a prompt or via profile.agent_config | `routes/config.ts`, `routes/modelCatalog.ts` |
| event stream | WS `/api/v1/ws`, protocol version 2 (`protocol/ws-control.ts:19`) | see next section |

## 3. WS Event Model

Frame format: `{type, seq, epoch, volatile?, offset?, session_id, timestamp, payload}`.

- **durable** (numbered into the journal, replayable): `turn.started/ended`, `turn.step.*`,
  `tool.call.started`, `tool.result`, `prompt.submitted/completed/aborted/steered`,
  `subagent.*`, `compaction.*`, `task.*`, `event.session.work_changed`
  (busy/main_turn_active/pending_interaction), `event.session.created`,
  `session.meta.updated`, `event.config.changed`, `event.approval/question.*`.
- **volatile** (no seq consumed, not replayed): `assistant.delta`, `thinking.delta`,
  `tool.call.delta`, `tool.progress`, `shell.*`, `agent.status.updated`;
  text deltas carry a cumulative `offset` for gap detection (`ws-control.ts:46-53`).
- Subscription semantics: subscribe per session (multiple allowed), optional `agent_filter`; control frames
  `client_hello`/`subscribe`/`unsubscribe`/`ack`/`resync_required`.
- **Disconnect compensation**: the client re-subscribes with a `{seq, epoch}` cursor; the server replays
  incrementally from the in-memory tail or the on-disk journal (`<home>/server/events/`, seq preserved
  across restarts); beyond the window (default 1000 entries) or epoch mismatch → `resync_required` →
  the client rebuilds via the REST snapshot (`sessionEventBroadcaster.ts:516-563`). This is the IM-style
  multi-device sync model.
- WS has **no server heartbeat** (no ping/pong); connection liveness must be self-checked by the client
  (kimi-web uses stale detection).

## 4. Multi-Client Concurrency Semantics

- **Multiple WS clients subscribing to the same session: natively supported.** The broadcaster keeps
  filter/grades per connection independently and fans out to all targets; slow clients get per-connection
  backpressure + delta coalescing.
- **No ownership/lease/active-client concept.** The REST write path only validates the bearer token;
  `GET /connections` merely lists connections read-only.
- **Two clients sending prompts to the same session simultaneously: queued, not rejected.**
  `IAgentPromptService.enqueue` feeds a per-agent FIFO consumed serially
  (`agent-core-v2/src/agent/prompt/promptService.ts:84-109`). The `session.busy`
  error code in v2 is only defined, with no throw site.
- **Official web UI multi-tab** (ready-made evidence): each tab opens its own WS, **no leader
  election / tab coordination**; on disconnect, reconnect by cursor + snapshot re-seeding. In other words,
  the official frontend itself follows the "equal concurrent multi-clients + server-side queueing +
  event-broadcast convergence" pattern.
- **⚠ No cross-process session lock**: two kap-server instances sharing a homeDir can resume
  the same session simultaneously, each maintaining its own in-memory state and each writing the
  journal/wire files. Resuming with TUI `kimi -S` while kap-server live-holds
  a session means two processes writing the same session directory.

## 5. Maturity and Stability Signals

- **Dogfooding**: kimi-web runs entirely on `/api/v1` REST + `/api/v1/ws`. Counter-examples: the vscode
  extension and the interactive TUI do not go through kap-server (in-process SDK harness).
- **Tests**: 55 test files, about 671 test/it cases (including boot/auth/ws unit tests and some
  e2e); `test/apiSurface.snapshot.test.ts` uses a route-table snapshot derived from `/openapi.json`
  as an API-surface regression guardrail.
- **OpenAPI/AsyncAPI are both code-generated** (route-level zod schemas via @fastify/swagger;
  AsyncAPI generated from the ws-control operation catalog); schemas are the single source of truth,
  not hand-written docs.
- **Version**: 0.0.2, CHANGELOG has only two entries, **no stability guarantees**; git history has 43 commits
  (starting 2026-07-12, two weeks). A `/api/v2` RPC surface once appeared and was removed in 5ae60fa —
  the API surface is still evolving rapidly.
- **Meaning of the "v1" prefix**: inherited from the wire-compat surface of the deleted old v1 server; the
  `backend: 'v2'` in `/meta` refers to the engine being agent-core-v2. API generation (v1) and engine
  generation (v2) are orthogonal; the WS side additionally has an explicit `protocol_version: 2`.
- **Engine identity**: kap-server = agent-core-v2; the interactive TUI defaults to agent-core v1
  (`main.ts:98`); `kimi -p` can switch to v2 via an env flag. **The TUI and the server run different engine generations.**

## 6. TUI Remote Continuation and klient

- The interactive `kimi` TUI **cannot** connect to kap-server; there is no `codex --remote`-style mode.
- `kimi -S/--session` resumes an **on-disk session**, continued by the local process; it cannot attach to
  a kap-server live session.
- The ipc transport of `packages/klient` (unix socket + ndjson, `serveKlientIpc`) has **no production
  consumers in the repo**, only examples and its own tests; kap-server itself does not expose ipc.
- The kimi-cli-era `--wire` (stdio JSON-RPC) has been **removed** in kimi-code; a test comment
  states "held back from the first release … for when those flags return" —
  it may come back in the future.

## 7. Risks and Gaps (from the KITE Shared-Backend Standpoint)

### Hard Gaps

1. No session delete (only archive)
2. No ownership/lease/single-writer semantics
3. No dedicated read/write endpoint for per-session model/permission (can only be carried along with a prompt)
4. steer can only inject an "already-queued" prompt; it cannot push directly into an in-progress turn
5. Token has no fine-grained permissions, no per-client identity (only self-reported user_agent)
6. No cross-process session lock — operating the same session concurrently with the TUI/`kimi web` risks tearing
7. WS is a pure subscription surface: all writes go through REST; both channels must be maintained

### Soft Risks

1. 0.0.2, two weeks of history, no stability guarantees; upgrades must follow snapshot diffs
2. No daemon mode; process management is on you (or supervised by KITE as a child process)
3. WS has no heartbeat; connection liveness is client-self-checked
4. Volatile events are unreliable: rely on offset gap detection → resync → snapshot rebuild
5. No TLS for non-loopback deployment; rate limit only covers the auth-failure path

### Points Requiring Hands-On Verification

See `docs/verification/spike-checklist.md`.

---

## Supplementary Investigation (2026-07-21): Web UI Coexistence, Product Focus, LAN Access

### A. Web UI Coexistence with the Bridge

- kimi-web is a pure /api/v1 + /api/v1/ws client; the server gives it no privileged treatment: static assets
  go through an auth exemption (`src/middleware/auth.ts:64-79`), and API calls use the same
  server.token; the `X-Kimi-Client-*` headers only appear in CORS allow-headers and play no part in
  authorization (`src/middleware/origin.ts:28`).
- Three ways the token enters the web UI (header comment of `apps/kimi-web/src/api/daemon/serverAuth.ts`):
  URL `#token=` (appended when `kimi web` opens the browser, wiped immediately after reading), manual entry
  in the login dialog, localStorage (7-day TTL).
- No exclusive-backend assumption: a repo-wide grep of `apps/kimi-web` finds no shutdown/connections calls;
  comments explicitly state a multi-client design (`useKimiWebClient.ts:958` etc.); CHANGELOG 0.20.0
  #1081 treats cross-client title sync as a first-class scenario.
- `POST /api/v1/shutdown`: mounted by default on loopback (any token holder can shut down
  the entire server); on non-loopback it defaults to 404 and requires `--allow-remote-shutdown`
  (`src/start.ts:170`, `routes/registerApiV1Routes.ts:168-172`).

### B. Product Focus Signals

- Built-in TUI tip: `apps/kimi-code/src/tui/constant/tips.ts:33`
  `/web: use the Web UI for a better experience`, appearing in both the working tips
  and the footer rotation (every 10s) — not a one-off.
- There is also a remote tips banner channel (`cdn.kimi.com/kimi-code-tips/tips.json`,
  supporting version targeting/time windows/cooldowns), currently not promoting web.
- Official stance: root AGENTS.md:18 calls the web UI "a peer to the TUI"; README/guides
  still center on the TUI.
- Investment signals: 87 `web:`-prefixed entries in the 0.24.0→0.28.1 CHANGELOG; 0.28.0 replaced
  the entire `kimi server` command tree with `kimi web`; sustained mobile adaptation investment (mobile
  shell, bottom sheet, iOS anti-zoom).
- Engine: the TUI still runs agent-core v1 in-process; no roadmap evidence of TUI migrating to v2
  or TUI connecting to the server (no klip-style docs).
- The full "wire held back" comment is at
  `test/cli/session-flag-picker.test.ts:70-74`: --print/--wire are flags whose release is
  postponed, with a validateOptions guard retained in the source.

### C. LAN / Phone Access

- `kimi web --host`: omitted = 127.0.0.1; bare flag = 0.0.0.0; a specific LAN IP can be given
  (`apps/kimi-code/src/cli/sub/web/shared.ts:90-94`). When bound to 0.0.0.0, the startup
  banner prints a Network URL with `#token=` for each network interface (`access-urls.ts:62-85`),
  with a comment stating it is for "opening the link on another device and authenticating automatically".
- Host header validation (DNS rebinding defense): by default allows localhost/literal IPs/
  bound addresses/whitelist (`--allowed-host` or KIMI_CODE_ALLOWED_HOSTS), rejects with
  40301 (`src/middleware/hostnames.ts:119-153`); enforced on both HTTP and WS.
- Non-loopback hardening: auth failures rate-limited per source IP (42901), security response headers,
  shutdown/terminals default to 404; password not enforced, only warned about
  (`src/start.ts:216-227`).
- No built-in TLS; official posture: terminate via reverse proxy/tunnel (`src/start.ts:167,217-220`).
- Phone login: opening a token-bearing URL authenticates automatically; or /login pops up a ServerAuthDialog
  for token/password entry (`apps/kimi-web/src/composables/useAuthGate.ts:19-47`).
- Mobile adaptation: viewport-fit=cover; ≤640px single-column mobile shell
  (`useIsMobile.ts` + three components under `components/mobile/`); the CHANGELOG has mobile-specific
  entries (0.28.0 mobile permission sheet etc.).

---

## Spike Corrections (2026-07-21, kimi 0.28.1)

Found by executing `docs/verification/spike-checklist.md` (details:
`docs/verification/spike-results.md`). Where this section conflicts with the
body above, this section wins.

1. `sessionEventBroadcaster.ts` moved →
   `src/transport/ws/v1/sessionEventBroadcaster.ts`.
2. Steer URL on the wire is single-colon `POST /sessions/{id}/prompts:steer`;
   the `prompts::steer` spelling in §2 is the route-registration form and
   returns 40001 when called.
3. REST `session.last_seq` is a hardcoded placeholder 0
   (`routes/sessions.ts:1069`); the real journal seq is only available from
   WS ack cursors or snapshot `as_of_seq`.
4. Re-aborting a finished prompt returns **40402** (the record is dropped
   from the queue); the 40903 idempotent path appears dead in 0.28.1.
5. Subscribing to a cold session right after a server restart returns an
   unexplained `resync_required` (lazy activation via
   `ISessionLifecycleService.get`, not `resume`); touching a resume-backed
   REST route first avoids it. Restart alone never rotates the epoch
   (journal JSONL preserved); the epoch rotates only on journal corruption.
6. `prompt.submitted` / `prompt.completed` have **no producer** in
   agent-core-v2 (schema-defined only) and never appear on the wire; use the
   REST submit ack + `prompt.aborted` / `prompt.steered` + `turn.*`.
7. The replay window (1000) is a constructor option only — not configurable
   via CLI/env at runtime; a `resync_required` frame may arrive **before**
   the subscribe ack (buffer frames while awaiting acks).
8. Provisioning: an isolated `KIMI_CODE_HOME` gets model config from the
   `KIMI_MODEL_NAME` / `KIMI_MODEL_API_KEY` / `KIMI_MODEL_BASE_URL` env
   overlay, and REST-created sessions do not inherit the overlay
   `defaultModel` — pass `model` explicitly per prompt.
