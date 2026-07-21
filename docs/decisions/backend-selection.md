# Decision: Choose kap-server as the shared backend

> Status: Decided (to be re-checked by a spike before work starts; see `docs/verification/spike-checklist.md`).
> Evidence: `docs/research/kap-server-usability.md`.

## Problem

KITE needs a long-running kimi-code runtime backend that multiple clients can
share, to support the Feishu-side shape of "multiple chats operating on the
same set of sessions". kimi-code offers several programmatic integration
surfaces; which one should be the foundation of the bridge?

## Candidates and eliminations

| Candidate | Verdict | Reason |
| --- | --- | --- |
| `kimi -p` (headless print) | Eliminated | One-shot execution; no session sharing, no approval interaction, no event subscription |
| stdio wire protocol | Eliminated | Explicitly removed ("held back") by kimi-code; does not currently exist |
| `kimi acp` (ACP protocol) | Eliminated | Aimed at editor integration; approval/terminal semantics are designed for IDE scenarios, and it is built on top of node-sdk, so its capability surface is a subset of kap-server |
| `@moonshot-ai/kimi-code-sdk` (node-sdk, in-process) | Eliminated (at the shape level) | Equivalent capabilities but requires KITE to live in the same process and language (TS) as the engine; the language conclusion is in `process-shape-and-language.md` |
| `packages/klient` ipc transport | Eliminated | No production consumer in the repo; kap-server does not expose ipc; there is nowhere external to attach |
| **kap-server** (REST + WS) | **Chosen** | See below |

## Rationale

1. **Complete capability loop**: session CRUD (except delete), prompt FIFO,
   abort/steer, push + REST responses for approval/question, dual-layer
   durable+volatile events, cursor replay + snapshot rebuild — every primitive
   the bridge needs is present, and it is genuinely used by the project's own
   kimi-web (dogfooding).
2. **Event model is isomorphic to the IM scenario**: durable journal + cursor
   resync + snapshot is exactly the IM multi-device sync model; the part of
   the sync discipline that FOCUS had to build on top of app-server is
   provided natively by kap-server.
3. **Native per-prompt overrides**: `model`/`permission_mode`/`plan_mode`
   are carried with each prompt, directly fulfilling the contract of
   "binding-level settings applied explicitly every turn", without the
   one-shot override detour FOCUS needed.
4. **Single source of contract truth**: OpenAPI/AsyncAPI are generated from
   zod schemas in code, and upstream has its own snapshot regression tests for
   the API surface — this surface is one upstream stands behind, making it
   suitable as an external dependency.
5. **Auth and discovery are ready-made**: hot-reloaded token file, automatic
   port +1, instance registry — a perfect fit to be taken over by KITE's
   managed subprocess shape.

## Known costs and mitigations

| Cost | Mitigation |
| --- | --- |
| 0.0.2, no stability commitment; the API surface has seen `/api/v2` deleted outright within two weeks | Do not hard-pin the version (aligned 2026-07-21; see kite-design §10); CI runs a snapshot diff of `/openapi.json` + the WS operation catalog to detect drift, with explicit adaptation |
| No ownership / single-writer semantics | KITE adopts queue semantics + prompt-level ownership (see `concurrency-model.md`); does not demand exclusivity from upstream |
| No cross-process session lock | Single-instance deployment premise + the "bare kimi is out of contract" clause (see `process-shape-and-language.md`) |
| No daemon mode | KITE supervises it as a managed parent process |
| No session delete | Non-goal; do not work around it |
| Volatile events are unreliable; WS has no heartbeat | MVP consumes durable events only; stale detection is self-checked on the client side |

## Fallback kept in reserve

If the spike exposes a hard defect in kap-server, the fallback is: embed a thin
TS sidecar inside the KITE process (calling `startServer()` or node-sdk), with
the Python side still speaking the same REST/WS vocabulary — the adapter layer
boundary stays unchanged, so the loss is contained.
