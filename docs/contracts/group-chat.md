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

In: **`mention_only`, `assistant`, and `all` group modes.** An admin
activates a group once (`/group activate`); the mode is switchable via
`/group-mode 〈mention_only|assistant|all〉` (default `mention_only`).

- `mention_only`: only @bot + text from members triggers; everything else is
  ignored (no logging, no context).
- `assistant`: every member message is appended to a per-chat log; @bot +
  text triggers with the log since the last trigger boundary injected as
  context. History fetch failure blocks the prompt with an explicit notice
  (fail-closed — never answer silently without the context).
- `all`: every member message triggers a prompt directly (no context
  injection). **Exclusivity rule**: an all-mode group's session may not be
  bound to any other chat (noise pollution across chats); switching to
  `all` is denied with a remediation text when the session is shared, and
  `/switch`/`/new` into a shared session while in `all` mode is denied the
  same way (FOCUS's thread-access rule).

Slash commands in groups stay admin-only. Deactivated or stranger content is
silently ignored (one denial hint on @/slash, no spam).

Out (explicit non-goals for now): merge_forward in groups, per-member ACL
beyond the actor rule, group creation/admin via kitectl.

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
   message, not recite history. Limits: 50 messages / 24h lookback (both
   config-overridable via `group_history_fetch_limit` /
   `group_history_fetch_lookback_seconds`) / 5s boundary slack (a fixed
   constant, not wired to config); history fetch failure blocks the
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
6. All-mode exclusivity violation (the group's session is or would be
   shared with another chat) → mode switch / rebind denied with the
   remediation text; never silently allowed.

## 5. Tests That Lock the Behavior

- Ingress matrix: p2p/group × admin/activated member/stranger × @/no-@ ×
  slash/text × mode(mention_only/assistant) (every cell has an explicit
  outcome).
- Activation: admin-only; persists across restart; deactivate stops all
  member prompting immediately.
- Mode switching: `/group-mode` admin-only; assistant → every member
  message logged (bot's own excluded); mention_only → nothing logged;
  all → every member message triggers a plain prompt.
- All-mode exclusivity: mode switch to `all` denied when the session is
  shared with another chat (remediation text); `/switch`/`/new` into a
  shared session while in `all` mode denied; allowed when exclusive.
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

- Nothing currently deferred. Earlier deferrals are now admitted:
  merge-forward in groups (§3.7), the all-mode reverse exclusivity (§3.8),
  and the rich question form (§3.9).

## 7. Later Admissions (2026-07-25)

### 3.7 Merge-forward in groups

Trigger semantics per mode (FOCUS parity): `mention_only` → dropped
silently (forwards never carry @mention); `assistant` → appended to the
group log as context material, never a trigger; `all` → aggregated and
processed like a member text message. The 2s aggregation window and the
recursive expansion are shared with the p2p path.

Claim-merge semantics (also shared with p2p; audit M12): a buffered
transcript waits out its ~2s window for the sender's next plain text,
which *claims* the stash — the two merge into ONE prompt, transcript first
(`<forwarded_messages>` block, then the comment), so the instruction never
runs ahead of the content it refers to. The claim is keyed on
(sender, chat), so it never leaks across members or chats. Only an
unclaimed window flushes the transcript as its own prompt. Slash commands
never claim, and interaction replies (approval/question answers) take
claim precedence by design. A group claim re-checks the current mode first
(fail-closed mirror of the window flush): only an activated all-mode group
claims; after a mid-window mode flip the stash is left for the flush,
which drops it explicitly.

### 3.8 All-mode reverse exclusivity

The exclusivity rule applies in both directions: any other chat
(p2p or group) rebinding (`/switch`, first-bind) into a session an
all-mode group already occupies is denied with the same remediation text.

### 3.9 Rich question form

`question.requested` renders an option-button card per question item
(numbered reply as fallback, actor rule identical to approvals); answering
or timeout dismisses (patches) the card. Fulfills the original mvp-scope
question row.

## Aligned Additions (2026-07-24)

1. **Sender display names**: group-facing notices (approval/question routing
   hints) resolve the initiator's display name via the contact API
   (`contact:user.base:readonly`) through a TTL'd read-through cache. The
   chain is FOCUS-isomorphic: `name` or `nickname` → fallback `open_id[:8]`
   (bot senders `机器人:{id[:8]}`); fail-soft, no state axis. Tests: cache
   hit/TTL/negative-cache/fallback chain, notice wording with and without a
   resolvable name.

## Aligned Additions (2026-07-25, audit C2)

1. **Bot removed / group disbanded**: on the chat-unavailable lifecycle
   events the group's activation config is deactivated (fail closed to
   silence, same stance as §4.3); the mode preference, the binding, and the
   group log file are kept. Re-adding the bot later does NOT silently
   revive the old activation — coming back requires an explicit admin
   `/group activate` again.
2. **Feishu topic (thread) replies join the main-stream context** (scope
   cut, documented after audit L16): FOCUS models per-thread scopes and
   keeps topic replies out of the main-stream context; this cut has one
   boundary triple per chat and no thread scopes, so a member message
   replied in a Feishu topic is logged and backfilled like any ordinary
   group message. The message wire carries `thread_id`, so a future cut
   can filter client-side; the history list API itself offers no
   server-side thread filter.
