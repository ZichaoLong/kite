# OKbot vs FOCUS: Lessons from Two Approaches

> Type: research (evidence material, not a contract). Investigation date: 2026-07-21.
> Subjects: `~/llm/kimi/OKbot` (kimi-cli fork, HEAD 38e305e) and
> `~/llm/focus` (FOCUS, Feishu ↔ Codex bridge).
> Purpose: answer "which path should KITE take, and which pitfalls to avoid".

## Core Difference: Fork-Embedded vs Protocol Bridging

| Dimension | FOCUS | OKbot |
| --- | --- | --- |
| Relationship to upstream | Independent daemon, protocol-bridging the unmodified `codex app-server` (WS JSON-RPC); upgrading upstream = swapping the CLI version | A **fork** of `MoonshotAI/kimi-cli` v1.9.0, with Feishu welded into the source tree (`src/kimi_cli/feishu/`, single file `sdk_server.py` at 4593 lines); upgrading upstream = continuous merging |
| Session model | Explicit contract over a five-state axis (binding/attached/loaded/running/interaction owner), JSON store | `chat_id:user_id` → KimiSoul in an in-memory dict; persistence reuses kimi-cli session files |
| Multi-client / multi-instance | Multi-subscription broadcast + interaction owner exclusive write; cross-instance loaded gate + machine-level lease, **fail-closed** | Multiple Feishu accounts (one WS thread per account), but no multi-process/multi-machine coordination; concurrent writes fall back on file locks only |
| Approval model | approval_policy + permissions_profile persisted and explicitly applied per turn, **fail-closed** | YOLO auto-approve (on by default) + three-button approval card, **fail-open auto-approve on error** |
| Message rendering | Single-anchor execution card with streaming patch (at most one card per session at any moment) + a separate terminal-state card | Segmented multi-message: thinking in a collapsed card, body text one card per ~100 characters, each tool call its own card (the old StreamingCard path is dead code) |
| Feature scope | Restrained: thread lifecycle, permissions, group-chat contract, images, scheduled prompts | Expansionist: scheduled tasks, long-term memory (SQLite+embedding), background-task wakeups, voice ASR, text-to-image, browser/Android device control, MCP/Skills hot reload, Plan Mode |
| Tests | 1137 tests, roughly 1:1 with source; heavy coverage on core paths | 639 tests, but the Feishu core `sdk_server.py` (4593 lines) has almost no direct tests (`tests/okbot/` is only 509 lines) |
| Docs | Contract system spanning contracts/architecture/decisions; code-doc inconsistency is treated as a contract gap | Its own docs drifted from the code (webhook mode, `card.py` no longer exists but is still in the README), inconsistent version numbers, `print` debugging |

## Divergence in Design Philosophy

- **FOCUS treats "the safety of sharing the same live thread" as the core problem**: complexity is spent on the state axis, leases, and cross-instance admission; it would rather refuse than take a risk. The feature surface is deliberately narrow.
- **OKbot treats "making all of Kimi's capabilities usable inside Feishu" as the core problem**: approvals pass by default and auto-approve on error, trading away protocol-layer overhead for rapid feature stacking. Concurrency safety, multi-instance support, and state contracts basically do not exist.

## Lessons for KITE

1. **The OKbot route is physically infeasible on kimi-code**: kimi-code is a TypeScript rewrite; the "import the agent's internal APIs inside a Python process" interface does not exist. Fork-embedding ends together with kimi-cli.
2. **The cost of feature stacking is maintainability**: OKbot's Feishu core is 4593 lines with almost no tests, docs drifted from code, and fail-open approvals — once feature growth outruns the architecture's carrying capacity, what users experience is not features but bugs (the same shape of problem as OpenClaw).
3. **OKbot's positive value is a validated feature-demand list**: scheduled tasks, background-task wakeups, memory, voice, and rich media all have verified real demand. KITE should selectively absorb them through its own carrying-capacity gate (scheduled prompts already have a ready-made contract in FOCUS) — **absorb the demand, not the architecture**.
4. **The FOCUS route becomes possible on kimi-code for the first time**: kap-server provides a long-lived backend comparable to `codex app-server` (see `docs/research/kap-server-usability.md` for details); the entire FOCUS design can be ported over.

## Disposition of OKbot's Incremental Features

| OKbot feature | KITE disposition | Rationale |
| --- | --- | --- |
| Scheduled tasks (natural-language cron) | Absorb in Phase 3 | The FOCUS scheduled-prompts contract is portable |
| Background-task session wakeups | Defer; watch upstream `task.*` events | kap-server already has a task event stream; get the MVP working first |
| Long-term memory | Not doing | Beyond a bridge's responsibility; an agent-side capability |
| Voice ASR | Not doing (Non-goal) | Introduces an external ASR dependency; unrelated to bridging |
| Text-to-image / device control | Not doing (Non-goal) | An agent tool capability, not part of the bridge |
| MCP/Skills hot reload | Not doing | Belongs to the agent configuration surface; go through `kitectl` or the upstream CLI |
| Plan Mode | Absorb in MVP (pass-through mapping) | kap-server natively supports per-prompt `plan_mode`; the cost is minimal |
