# Contract: Group Chat (Phase 2)

> Status: admitted (2026-07-23, passed the carrying-capacity gate below);
> turns active with its implementation.
> Evidence: `docs/research/focus-assets-map.md` (FOCUS group domain survey),
> `docs/decisions/concurrency-model.md` (queue semantics + prompt-level
> ownership), `docs/contracts/mvp-scope.md` §3 (multi-chat broadcast rule).

## 1. Carrying-Capacity Gate

1. **Which layer?** Application (ingress classification, command guards,
   actor checks at card-click) + local state (one new store for group
   config). Transport already normalizes chat type and @mentions.
2. **Which state axis?** One NEW axis, registered in kite-design §4 before
   code: **group config** — persistent, chat-keyed `{activated,
   activated_by, activated_at, mode}`. Everything else rides existing axes:
   a group is an ordinary `chat_id` binding (axis 1) — FOCUS needed a
   shared-binding hack that KITE's chat-keyed store gets for free — and
   prompt ownership (axis 4) extended with `sender_open_id`.
3. **Crash/restart recovery?** Group config is in the store (loaded like
   bindings); mention detection is stateless; prompt ownership rebuilds per
   the existing §4.6 path (unrebuildable approvals expire explicitly,
   unchanged).
4. **Which tests?** §5 below.

## 2. Scope (First Cut)

In: **`mention_only` groups only.** An admin activates a group once
(`/group activate`); afterwards **any member** may prompt by @bot + text.
Slash commands in groups stay admin-only. Deactivated or stranger content is
silently ignored (one denial hint on @/slash, no spam).

Out (explicit non-goals for this cut): `assistant` mode (per-chat log +
history context — needs the log/boundary axis, deferred), `all` mode
(every message triggers — floods the FIFO and needs an exclusivity rule),
merge_forward, per-member ACL beyond the actor rule, group creation/admin
via kitectl.

## 3. Behavior Contract

1. **Activation**: `/group activate|deactivate` (admin only, in the group)
   writes the config; `/status` shows it. Activation requires the chat to be
   bound first (unbound group → the first activation also creates+binds the
   session, same first-use rule as p2p).
2. **Ingress matrix**: in an activated group, only @bot+text from members
   enters the prompt path; non-@ messages are ignored entirely (no logging,
   no context). In a non-activated group, everything is ignored except admin
   slash commands. P2P behavior is unchanged (admin-only for now).
3. **Approvals/questions in groups**: the card posts to the group chat
   (broadcast per mvp-scope §3); the click handler verifies
   `clicker_open_id == initiator_open_id || admin` — bystander clicks get a
   denial toast and change nothing. The initiator's `sender_open_id` rides
   the prompt-ownership record (axis 4, no new axis).
4. **/abort in groups**: initiator or admin, same rule as p2p — enforced by
   the same actor check.
5. **Broadcast**: normal output (execution/terminal cards) posts to the
   group chat like any attached chat; the existing multi-chat rule applies
   unchanged when a group and a p2p chat share one session.
6. **Multi-user identity (allowlist)**: falls out of activation — group
   membership (maintained by Feishu) is the user list; no separate p2p
   allowlist is admitted in this cut (FOCUS's evidence: groups cover the
   need; p2p stays admin-only).

## 4. Fail-Closed List

1. Member message in a non-activated group → ignored (denial hint only on
   @/slash); never prompts.
2. Bystander approval/question click → denial toast, no state change, no
   upstream response from that click (the card stays live for the actor).
3. Group config store corrupt → that group reads as non-activated (fail
   closed to silence, never to open).
4. Sender identity missing from an event → treat as non-member.

## 5. Tests That Lock the Behavior

- Ingress matrix: p2p/group × admin/activated member/stranger × @/no-@ ×
  slash/text (every cell has an explicit outcome).
- Activation: admin-only; persists across restart; deactivate stops all
  member prompting immediately.
- First activation of an unbound group creates+binds (cwd =
  `default_working_dir`).
- Approval card: initiator click resolves; admin click resolves; bystander
  click → toast, card untouched, no REST call.
- `/abort`: initiator/admin only in groups.
- Broadcast: group + p2p bound to the same session both get cards; approval
  goes to the initiator's chat only (existing §3 rule).
- Config corruption → group treated as non-activated.

## 6. Deferred With Pointers

- `assistant`/`all` modes: need the log/boundary axis and (for `all`) an
  exclusivity rule; FOCUS's `group_chat_store` log half and
  `group_history_recovery.py` are the ready-made designs (asset map §1).
- Merge-forward aggregation: `forward_aggregator.py` (2s window) is filed.
- Rich question form in groups: same actor rule, no new contract needed.

## Aligned Additions (2026-07-24)

1. **Sender display names**: group-facing notices (approval/question routing
   hints) resolve the initiator's display name via the contact API
   (`contact:user.base:readonly`) through a TTL'd read-through cache
   (fail-soft fallback to a shortened open_id; no state axis — the cache is
   disposable and rebuilt on demand). Layer: application; recovery: nothing
   to rebuild; tests: cache hit/TTL/negative-cache/fallback, notice wording
   with and without a resolvable name.
