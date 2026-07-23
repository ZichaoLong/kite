# Contract: Volatile Streaming Cards (Phase 2)

> Status: admitted (2026-07-23, passed the carrying-capacity gate below);
> turns active with its implementation. On conflict with code behavior after
> activation, this document is the contract.
> Evidence: `docs/research/focus-assets-map.md` (FOCUS streaming mechanisms),
> `docs/architecture/kite-design.md` §5 (volatile strategy pre-registered).

## 1. Carrying-Capacity Gate

1. **Which layer?** Adapter (normalize `assistant.delta` + offset watermark)
   → application (per-prompt transcript + patch scheduling) → a coalescing
   patch dispatcher at the transport edge (Feishu RTT never blocks the
   RuntimeLoop).
2. **Which state axis?** None new. Streaming state is in-memory per-prompt
   transcript, rebuilt from the REST snapshot after restart/resync exactly
   like prompt ownership (§4.6); the execution card anchor already pins
   `{chat_id, session_id, prompt_id, card_message_id}`.
3. **Crash/restart recovery?** Durable events remain the only authoritative
   driver: a streaming gap (offset jump), a resync, or a restart all fall
   into the existing snapshot-rebuild path; the terminal card is always
   produced from durable signals. Volatile text is enhancement, never
   evidence.
4. **Which tests?** §5 below.

## 2. Scope

In: `assistant.delta` per-token streaming into the current execution card,
for every attached chat's own card (each attached chat keeps its own anchor;
patches fan out per anchor).

Out (explicit non-goals): `thinking.delta`, `tool.call.delta`, `shell.*`,
`agent.status.updated`; streaming into question/approval cards; any
volatile-derived state that survives into durable decisions.

## 3. Behavior Contract

1. **Full-snapshot patch invariant**: every card patch re-renders the whole
   card from the accumulated transcript. Patches are never diffs, so a lost
   or coalesced patch loses nothing.
2. **Coalescing**: latest-wins per card message — at most one in-flight
   patch per card and exactly one trailing flush; a flood of deltas becomes
   ~2 patches per card per burst.
3. **Throttle**: per-card minimum patch interval (default 700 ms) with a
   single trailing timer; the final state is never dropped.
4. **Patch failures**: retryable (Feishu 230020 rate limit, transport
   timeouts) → requeue after `retry_after` (default 2 s), newer render wins;
   non-retryable → drop (safe by invariant 1). Terminal-card patch failure →
   one-time plain-text content rescue.
5. **Delta healing**: deltas carry a cumulative offset; an offset gap jumps
   straight to snapshot rebuild (never guess the missing text). A completed
   turn's authoritative text always reconciles over delta-accumulated text,
   monotonically — never shrink.
6. **Markdown**: sanitize at render time on the full accumulated text only
   (a delta may split a token). Execution card = runtime variant (tolerant
   of unclosed fences); terminal card = json2 variant (fence normalization).
7. **Size caps**: reply projection is char-budgeted with a truncation
   notice; the terminal card enforces the utf-8 byte budget → plain-text
   fallback (ported from FOCUS's terminal budget discipline).
8. **Timer hygiene**: shutdown and terminal transitions cancel trailing and
   retry timers; a stale timer firing after its prompt ended is a no-op
   (generation-guarded).
9. **Target matching**: prompt-scoped deltas mutate only the card whose
   anchor matches the prompt id (same rule as durable events).

## 4. Fail-Closed List

1. Offset gap / volatile overflow → snapshot rebuild of the session's card
   content (same path as `resync_required`).
2. Rebuild failure → freeze the card as "状态未知" with the `kitectl session
   status` hint (unchanged from MVP §4.2).
3. Streaming disabled or kap stops sending deltas → the durable path still
   produces correct cards (degraded smoothness, never degraded correctness).

## 5. Tests That Lock the Behavior

- Coalescing: N rapid submits → ≤1 in-flight per card, exactly one trailing
  flush, order preserved; submit during in-flight replaces pending render.
- Throttle: immediate patch when idle, single trailing timer when within
  the interval; timers cancelled on shutdown/terminal.
- Retry-after: 230020/timeout → requeued and eventually applied; newer
  render supersedes; non-retryable dropped without crash.
- Gap: offset jump → exactly one rebuild; rebuild failure → frozen-unknown.
- Reconcile: authoritative text replaces deltas; shorter stale text never
  shrinks longer current content.
- Rendering: unclosed fence mid-stream renders tolerantly; terminal card
  normalizes fences; over-budget terminal → plain-text fallback.
- Fan-out: two attached chats each get their own streamed card.

## 6. Interaction With Existing Contracts

- mvp-scope §3 concurrency display is unchanged (queue length still comes
  from durable state).
- The durable-event list in kite-design §5 stays the sole driver of state
  transitions; this contract adds a volatile side-channel for presentation
  only — exactly the "volatile later" clause already registered there.
