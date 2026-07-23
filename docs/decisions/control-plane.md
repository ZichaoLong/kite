# Decision: Loopback Control Plane Between kitectl and kited

> Status: Decided (2026-07-23, driven by a live bug found in the FOCUS asset
> survey; see `docs/research/focus-assets-map.md` §0.1).

## Problem

`kitectl prompt send` (shipped in the MVP as the control-plane entry for
later scheduled capabilities) talks directly to kap-server REST. That makes
it a **second writer** on the prompt-ownership state axis (kite-design §4
axis 4): the daemon's in-memory ownership map never learns who initiated a
CLI-sent prompt, so an approval triggered by one has no certain owner and
must take the fail-closed path — the approval card is posted as **expired**.
CLI-sent prompts are effectively unapprovable from Feishu.

Secondary issue: kitectl's REST error handling cannot distinguish "request
never reached the server" from "request sent, response lost". Blindly
retrying a non-idempotent prompt submit can double-enqueue.

## Decision

Add a **loopback control plane** inside `kited`, modeled on FOCUS's
`service_control_plane.py` (ported with renames):

- JSON-lines over loopback TCP, single-line request/response, 1 MB cap,
  auth-token checked (a daemon-issued token, stored with 0600 alongside the
  other instance secrets), loopback only — no TLS because it never leaves
  the host.
- The endpoint is published via a small metadata file (port + token path),
  so kitectl discovers the live daemon instead of trusting a recorded port
  ("live outranks recorded", same idea as kap's instance registry).
- `kitectl prompt send` becomes a **client of the daemon**: the submit is
  serialized through the RuntimeLoop, ownership is recorded exactly as for a
  Feishu-originated prompt, and the resulting cards/approvals behave
  identically regardless of entry surface.
- Error taxonomy is three-way: connection refused → "definitely not
  delivered" (safe to retry); sent but no valid response → **outcome
  unknown** (do NOT retry blindly; tell the operator to verify via
  `kitectl session status`); business error → surface upstream msg.

Read-only kitectl queries (`session list/status`, `binding list`) keep
talking to kap REST / the stores directly — only mutations that must
serialize through the daemon move to the control plane. Live introspection
(pending approvals, ownership view) may later be exposed over the same
channel; not in this batch.

## Why not the alternatives

- **Direct REST + idempotency key**: kap's prompt submit has no idempotency
  key, and it would still not record ownership in the daemon. Solves neither
  problem.
- **File/queue-based IPC**: cannot answer "did the daemon accept it"
  synchronously, and gives no access to live in-memory state.
- **Make kitectl send Feishu messages to itself**: circular, fragile, and
  breaks the admin-surface separation.

## Consequences

- The control plane is the only kitectl→kited mutation channel; anything
  that mutates daemon-owned state must go through it (single-writer
  discipline restored on the ownership axis).
- `kitectl prompt send` semantics change: the reply becomes "accepted by
  the daemon" (with prompt id when known), and outcome-unknown is reported
  as such. The MVP contract row for kitectl stays valid; the behavior note
  is recorded in mvp-scope's aligned section when implemented.
- Scheduled prompts (Phase 3) ride this same channel — that was the
  original intent of `kitectl prompt send`; this decision completes it.
