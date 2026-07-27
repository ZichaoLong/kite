# Decision: Concurrency model — queue semantics + prompt-level ownership

> Status: Decided (within MVP scope). This document and
> `docs/contracts/mvp-scope.md` §3 reference each other; on conflict, the
> contract prevails and this document is revised.

## Problem

When multiple Feishu chats (and potentially other kap clients) operate on the
same set of sessions concurrently, what concurrency semantics should KITE
provide? FOCUS's answer was "multi-subscriber broadcast + interaction-owner
exclusive writes + cross-instance lease" — should it be ported as-is?

## Decision: Do not port the owner lease; adopt upstream's native semantics

kap-server's native semantics (evidence: `docs/research/kap-server-usability.md`
§4):

- Multiple WS clients subscribing to the same session: natively supported,
  with converging broadcasts.
- Concurrent prompts: queued per-agent FIFO, never rejected; no busy error.
- No ownership/lease concept at all; the official web UI's multiple tabs are
  peers operating concurrently.

KITE follows suit:

1. **Writes are not exclusive**: every attached chat may send prompts; all
   enter the server-side FIFO. Cards display the active prompt and the queue
   length.
2. **Interactions are routed by prompt ownership**: approvals/forms are sent
   only to the chat that initiated the prompt; other chats get a read-only
   notice. This rule is consistent with the FOCUS group-chat contract that
   "an ordinary member can only handle turns they initiated", but the
   implementation is simplified from "lease" to "routing".
3. **Abort permission**: only the prompt initiator and administrators
   (decided 2026-07-21, in MVP).
4. **The web UI is a peer client** (verified 2026-07-21): kimi-web is a pure
   /api/v1 client with no privileged channel and no exclusivity assumptions
   (evidence: supplementary check in `docs/research/kap-server-usability.md`).
   The local web UI and the Feishu bridge naturally share the same set of
   sessions: queuing is guaranteed by the server-side FIFO, and approval
   idempotency (40902) makes simultaneous handling from both ends converge
   safely. KITE neither intercepts nor is aware of the web UI's writes; the
   event broadcasts of the two converge independently.

## Why not implement an interaction owner

- **Upstream offers no grip.** Owner semantics require "rejecting writes from
  non-owners", while kap's prompt endpoint is open to any bearer-token holder;
  implementing exclusivity inside the bridge means adding a home-made gate on
  every write path, plus handling owner loss, lease expiry, and cross-restart
  recovery — the complexity FOCUS paid for this mechanism is documented
  (five state axes, two layers of admission, a lease store).
- **The product value is unproven.** FOCUS needed an owner because codex
  app-server's interaction requests have a single exit and must have a
  deterministic route; kap turned approval/question into queryable REST
  resources with idempotent responses, so the routing problem simplifies to
  "direct by prompt ownership".
- **The restraint principle.** Run the simple semantics first and let real
  usage reveal whether exclusivity is truly needed; if so, add it back per the
  FOCUS design (concepts are already reserved; see `kite-design.md` §4).

## Risks and boundaries

| Risk | Handling |
| --- | --- |
| Cross-process session tearing (TUI/`kimi web` concurrently writing the same session while kap is live) | Single-writer premise, now per-instance: each instance's kap home is isolated (see `docs/decisions/multi-instance.md`), and the "bare kimi is out of contract" clause still applies (see `process-shape-and-language.md`); no loaded gate |
| Multi-instance (multiple Feishu apps) | Decided 2026-07-26 (`docs/decisions/multi-instance.md`): named instances are fully isolated (config/data/kap home/daemon lease), so this document's semantics hold per-instance; cross-instance coordination remains out of scope |
| Queue blocked by a long prompt, poor UX | Cards explicitly show queue depth; `/abort` (initiator/admin) provides an exit; no automatic timeout cancellation |
| Prompt ownership lost across restarts | Best-effort rebuild via `GET .../prompts` + snapshot; approval cards that cannot be rebuilt expire explicitly (fail-closed; see mvp-scope §4.6) |
| Under loopback, `POST /api/v1/shutdown` is open to any token holder | KITE only allows invocation via the service stop path; the managed shape is responsible for relaunching after an unexpected shutdown |

## When to revisit

Reopen this document when any of the following signals appears:

1. Real users report "someone else is messing with my session" problems;
2. Cross-instance coordination (instances sharing sessions/state) enters the
   roadmap;
3. Upstream introduces ownership/lock primitives (prefer upstream's then; do
   not build our own).
