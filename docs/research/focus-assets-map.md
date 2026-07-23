# FOCUS Asset Map for KITE (Phase 2 Borrowing Survey)

> Type: research (evidence material, not a contract). Survey date: 2026-07-23.
> Subject: a three-pass survey of `/home/zlong/llm/focus` (lifecycle/SSOT,
> event-pipeline robustness, group/images/identity) against KITE's four
> admitted Phase 2 features (volatile streaming cards, images in/out, group
> chat, multi-user allowlist) and its current MVP code.
> Purpose: record WHAT is worth borrowing and WHY, so later refactors can
> tell whether each piece should be kept, split, simplified, or removed.

## 0. Cross-cutting Findings on the Current MVP

The survey found real gaps in the *current* code, independent of Phase 2:

1. **kitectl bypasses the daemon** (`kitectl prompt send` writes straight to
   kap REST): the daemon's prompt-ownership map never learns the owner, so an
   approval triggered by a CLI-sent prompt can only take the fail-closed
   "unknown owner → expired card" path. FOCUS routes CLI mutations through a
   loopback control plane into the live service. → `docs/decisions/control-plane.md`.
2. **No terminal reconcile**: terminal text is fetched once at `turn.ended`;
   the normal "turn ends before the final message flushes" race yields empty
   result cards. FOCUS retries the snapshot read and treats it as the
   authority. → hardening in `event_pipeline` (see §3).
3. **No outcome-unknown taxonomy**: kitectl maps connect-failed and
   sent-but-timed-out to the same error; blind retry of a non-idempotent
   submit can double-enqueue. → folded into the control-plane decision.
4. **No preflight/reason-code layer**: `/detach`, `/new` run without checking
   in-flight work; `kitectl service restart` kills the managed kap-server
   (and all in-flight prompts) with no warning. → reason-coded preflights +
   restart preview, ported from `runtime_admin_controller.py` discipline.
5. **State mutation is ad hoc**: `EventPipeline` internals are mutated inline
   at ~15 sites. FOCUS's reducer-message + UNSET-sentinel + frozen-view split
   is what made its persist-before-commit and recovery ordering provable.
   Adopt the discipline when Phase 2 adds streaming/group state, not before.
6. **No store-schema versioning stance** (`binding_store` has no version
   field; cursor store treats corruption as empty). Decide
   fail-closed-vs-migrate deliberately before the first schema change.

## 1. The Map (verdicts per FOCUS module)

### PORT (rename, mostly as-is)

| FOCUS module | What KITE gets | Where it lands |
| --- | --- | --- |
| `service_control_plane.py` (215) | loopback JSON-lines control plane + outcome-unknown error taxonomy | kited ↔ kitectl (see `docs/decisions/control-plane.md`) |
| `runtime_card_publisher.py` (dispatcher part) | latest-wins coalescing patch queue: ≤1 in-flight patch per card, one trailing flush, retry-after honoring; moves Feishu RTT off the RuntimeLoop | streaming (see `docs/contracts/streaming-cards.md`) |
| `execution_transcript.py` | per-prompt transcript: delta accumulation + authoritative full-text reconcile + never-shrink guard + budgeted projection | streaming |
| `thread_image_delivery.py` (108) | upload-once/fan-out-to-attached-chats, per-chat failure isolation | images outbound (see `docs/contracts/images.md`) |
| `pending_attachment_store.py` (173) | TTL'd pending-attachment store with consume-once `take` | images inbound |
| `thread_subscription_registry.py` (55) | first/last-subscriber edge detection for upstream WS subscriptions | adapter, when group broadcasts make multi-chat-per-session common |

### REWORK (port the discipline, rewrite to kap/KITE semantics)

| FOCUS module | Discipline to port | Where it lands |
| --- | --- | --- |
| `execution_recovery_controller.py` | terminal reconcile: retry-on-empty, snapshot-authoritative, delivery dedup; watchdog generation counter; degraded-mode classification | `event_pipeline` terminal path (hardening) |
| `runtime_admin_controller.py` | `ReasonedCheck` preflights with reason codes; destructive-op preview (unverifiable ⇒ force-only, never available) | `/detach` `/new` preflights; `kitectl service restart` preview (hardening) |
| `binding_runtime_manager.py` | persist-before-commit with staged rollback for multi-binding mutations | binding write path, before group chat makes batch ops real |
| `runtime_state.py` / `runtime_view.py` | reducer messages + UNSET sentinel + frozen read views | adopt as Phase 2 state grows (streaming/group) |
| `prompt_turn_entry_controller.py` | card-before-submit fail-closed ordering; start-failure card rendering; stuck-running watchdog reconcile on user poke | `app_handler` / pipeline (hardening) |
| `interaction_request_controller.py` | pending→processing click guard (no double-submit); fail-close sweep entry points (unbind/shutdown sweep pending approvals) | `event_pipeline` approval slice (hardening) |
| `adapter_notification_controller.py` | target-match every event; delta→authoritative reconcile; heartbeat-driven watchdog; terminal-ordering hazards | streaming consumer (see `docs/contracts/streaming-cards.md`) |
| `inbound_surface_controller.py` | route-table + group-guard taxonomy (`group_admin` / `request_actor_or_admin`) with actor check at click time | group chat (see `docs/contracts/group-chat.md`) |
| `codex_group_domain.py` | activation commands + admin-at-click gating | group chat |
| `stores/group_chat_store.py` (config half) | per-chat group config `{mode, activated, activated_by, activated_at}` | group chat (log/boundary half deferred with assistant mode) |
| `file_message_domain.py` | stage-into-session-cwd pipeline: type validation, filename discipline, TTL + lazy sweep, cwd-mismatch block, consume-once with restore | images inbound (see `docs/contracts/images.md`) |
| `group_history_recovery.py` | boundary-triple dedup (seq + created_at + message_ids), self-app filtering, fail-closed history fetch | deferred until assistant mode is admitted |
| `generated_image_delivery.py` (+store) | claim→deliver→commit idempotency for event-driven outbound delivery | back pocket; the scanning half is codex-specific (text-to-image is a KITE non-goal) |

### SKIP (with rationale)

| FOCUS module | Why |
| --- | --- |
| `thread_access_policy.py`, `thread_runtime_coordination.py` | interaction-owner lease — contradicts KITE's decided concurrency model (`docs/decisions/concurrency-model.md`); the reserved-axis template if ever admitted |
| `instance_resolution.py`, `instance_layout.py`, `legacy_migration.py` | multi-instance = Phase 3; no legacy install to migrate |
| `thread_resolution.py` | KITE's `/sessions` + `/switch` already cover it |
| `permissions_profile.py`, `approval_policy.py` | codex enums; KITE's `/mode` `/plan` already shipped |
| `forward_aggregator.py` | merge-forward not admitted; the 2s aggregation-window trick is filed for group chat later |
| `card_text_projection.py` | port the terminal-card marker + projector together with `/last` when it arrives |

## 2. Verified-Upstream Fact Used by the Images Contract

kap's prompt `content` is a discriminated union including `image` / `video` /
`file` parts (`packages/protocol/src/message.ts:70-78`), so inbound images
can be submitted natively; the staged-path reference remains as composition
context and as the fallback for oversized/unsupported files.

## 3. Where Each Finding Landed

- `docs/decisions/control-plane.md` — finding 1 + 3 (dual-writer, outcome-unknown).
- Hardening batch on the existing pipeline — findings 2, 4 and the click-guard/sweep
  parts of §1 (terminal reconcile, reason-coded preflights, restart preview).
- `docs/contracts/streaming-cards.md` — the streaming mechanisms (§1 PORT rows + checklist).
- `docs/contracts/images.md` — inbound/outbound pipelines + the new attachment-staging axis.
- `docs/contracts/group-chat.md` — group config axis, actor-at-click, allowlist fallout.
- `docs/architecture/kite-design.md` §4 — two new state axes (group config,
  attachment staging) registered before any code, per the carrying-capacity gate.
