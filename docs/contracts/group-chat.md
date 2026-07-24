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
2. **Which state axis?** Two NEW axes, registered in kite-design §4 before
   code: **group config** — persistent, chat-keyed `{activated,
   activated_by, activated_at, mode}`; and **assistant log** — a per-chat
   JSONL message log with a monotonic `seq` plus the trigger **boundary
   triple** `{seq, created_at, message_ids}` (timestamp alone is not a
   cursor: multiple messages can share one millisecond). Everything else
   rides existing axes: a group is an ordinary `chat_id` binding (axis 1)
   and prompt ownership (axis 4) extended with `sender_open_id`.
3. **Crash/restart recovery?** Group config and the log/boundary are in
   stores (loaded like bindings); mention detection is stateless; prompt
   ownership rebuilds per the existing §4.6 path. After a restart the log
   covers recent history and the boundary triple dedups the Feishu REST
   history backfill exactly (FOCUS's crash-tested design).
4. **Which tests?** §5 below.

## 2. Scope

In: **`mention_only` and `assistant` group modes.** An admin activates a
group once (`/group activate`); the mode is switchable via
`/group-mode 〈mention_only|assistant〉` (default `mention_only`).

- `mention_only`: only @bot + text from members triggers; everything else is
  ignored (no logging, no context).
- `assistant`: every member message is appended to a per-chat log; @bot +
  text triggers with the log since the last trigger boundary injected as
  context. History fetch failure blocks the prompt with an explicit notice
  (fail-closed — never answer silently without the context).

Slash commands in groups stay admin-only. Deactivated or stranger content is
silently ignored (one denial hint on @/slash, no spam).

Out (explicit non-goals for now): `all` mode (every message triggers —
floods the FIFO and needs an exclusivity rule), merge_forward in groups,
per-member ACL beyond the actor rule, group creation/admin via kitectl.

## 3. Behavior Contract

1. **Activation**: `/group activate|deactivate` (admin only, in the group)
   writes the config; `/status` shows it. Activation requires the chat to be
   bound first (unbound group → the first activation also creates+binds the
   session, same first-use rule as p2p).
2. **Ingress matrix**: in an activated group, only @bot+text from members
   enters the prompt path; in `mention_only` mode, non-@ messages are
   ignored entirely (no logging, no context); in `assistant` mode, non-@
   member messages are appended to the per-chat log (bot's own messages and
   non-member messages never enter the log). In a non-activated group,
   everything is ignored except admin slash commands. P2P behavior is
   unchanged (admin-only for now).
3. **Assistant context composition**: an assistant-mode trigger composes the
   prompt as `<group_chat_scope>/<group_chat_context>/<group_chat_current_turn>`
   — the log since the boundary (merged with a Feishu REST history backfill,
   deduped via the boundary triple, self-app messages filtered) plus the
   current message; the envelope tells the model to answer the current
   message, not recite history. Limits: 50 messages / 24h lookback /
   5s boundary slack (config-overridable); history fetch failure blocks the
   prompt with an explicit notice (fail-closed).
4. **Approvals/questions in groups**: the card posts to the group chat
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
5. Assistant-mode history fetch failure → the prompt is blocked with an
   explicit notice; never answer without the context (no silent fallback).

## 5. Tests That Lock the Behavior

- Ingress matrix: p2p/group × admin/activated member/stranger × @/no-@ ×
  slash/text × mode(mention_only/assistant) (every cell has an explicit
  outcome).
- Activation: admin-only; persists across restart; deactivate stops all
  member prompting immediately.
- Mode switching: `/group-mode` admin-only; assistant → every member
  message logged (bot's own excluded); mention_only → nothing logged.
- Assistant context: log/boundary merge with REST backfill, boundary-triple
  dedup (same-millisecond messages), self-app filtering, envelope shape,
  limits (50/24h/5s), fetch failure blocks with the notice.
- Log axis: JSONL append with monotonic seq, boundary set/get, corruption
  reads as non-activated/empty log (fail-closed), restart reload.
- First activation of an unbound group creates+binds (cwd =
  `default_working_dir`).
- Approval card: initiator click resolves; admin click resolves; bystander
  click → toast, card untouched, no REST call.
- `/abort`: initiator/admin only in groups.
- Broadcast: group + p2p bound to the same session both get cards; approval
  goes to the initiator's chat only (existing §3 rule).
- Config corruption → group treated as non-activated.

## 6. Deferred With Pointers

- `all` mode (every message triggers): needs an exclusivity rule against
  noisy cross-chat thread sharing; FOCUS's `thread_access_policy.py` is the
  reference design (asset map §1).
- Merge-forward in groups: the aggregator is p2p-only today (forwards never
  carry @mention, so mention_only groups drop them); admitting them in
  groups needs its own trigger-semantics decision.
- Rich question form in groups: same actor rule, no new contract needed.

## Aligned Additions (2026-07-24)

1. **Sender display names**: group-facing notices (approval/question routing
   hints) resolve the initiator's display name via the contact API
   (`contact:user.base:readonly`) through a TTL'd read-through cache. The
   chain is FOCUS-isomorphic: `name` or `nickname` → fallback `open_id[:8]`
   (bot senders `机器人:{id[:8]}`); fail-soft, no state axis. Tests: cache
   hit/TTL/negative-cache/fallback chain, notice wording with and without a
   resolvable name.
