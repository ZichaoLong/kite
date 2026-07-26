# Docs Index

This directory is the source of truth for repository architecture, runtime
boundaries, and feature contracts.

## Reading Rule

When code and docs disagree, treat that as a contract gap. Tighten the code,
the docs, or both.

## Current Status

M1 (the MVP) is complete and live-verified (2026-07-22); the daemon is
deployed (systemd --user + autostart). Milestone 0 (spike validation) passed
on 2026-07-21 against **kimi 0.28.1** (results:
`docs/verification/spike-results.md`). `contracts/`, `architecture/`, and
`decisions/` are **active** repository facts — any inconsistency between
them and the code is a contract gap. Phase 2 is admitted: volatile
streaming cards, images in/out, group chat (see the Phase 2 contracts);
implementation starts with a hardening batch from the FOCUS asset survey
(`docs/research/focus-assets-map.md`).

## Document Types

- `docs/contracts/`
  - normative feature and runtime behavior contracts
- `docs/architecture/`
  - current architecture, layering, module split, and implementation shape
- `docs/decisions/`
  - decision records that explain why a design boundary exists
- `docs/verification/`
  - spike validation checklist and manual test checklist; validation support, not product/runtime semantics
- `docs/research/`
  - survey reports on upstream and benchmark projects; evidence material for decisions, not contracts themselves
- `docs/_work/`
  - local working notes; not repository facts

Status guidance:

- treat `contracts/`, `architecture/`, and `decisions/` as active repository
  facts
- treat `verification/` as validation support
- treat `research/` as evidence; if its facts conflict with the latest
  upstream code, the upstream code wins
- treat local notes under `docs/_work/` as working material

## Read By Type

### User-Facing Entry

- [README.md](../README.md)

### Contracts

- [`mvp-scope.md`](./contracts/mvp-scope.md) (active)
  - MVP feature scope, command surface, approval behavior, fail-closed list, non-goals, carrying-capacity gate
- [`streaming-cards.md`](./contracts/streaming-cards.md) (admitted, Phase 2)
  - volatile streaming into the execution card: coalescing/throttle/retry discipline, gap→rebuild, fail-closed list
- [`images.md`](./contracts/images.md) (admitted, Phase 2)
  - image inbound (staged, TTL'd) and outbound (upload+fan-out) pipelines; the attachment-staging state axis
- [`group-chat.md`](./contracts/group-chat.md) (admitted, Phase 2)
  - mention_only groups: activation, ingress matrix, actor-at-click approvals, allowlist fallout; the group-config state axis
- [`scheduled-prompts.md`](./contracts/scheduled-prompts.md) (admitted, Phase 3)
  - scheduled prompts: systemd --user timers routing back through `kitectl prompt send` into the control plane; display modes; termination strategy

### Architecture

- [`kite-design.md`](./architecture/kite-design.md) (active)
  - overall architecture, process shape, layering, adapter layer boundaries, state axes, event consumption strategy, card model, persistence, service management

### Decisions

- [`backend-selection.md`](./decisions/backend-selection.md)
  - why kap-server was chosen as the shared backend; the trade-off between the two integration paths; countermeasures against upstream drift
- [`process-shape-and-language.md`](./decisions/process-shape-and-language.md)
  - why Python + managed subprocess; why the local TUI wrapper is deferred; the stance on bare kimi
- [`concurrency-model.md`](./decisions/concurrency-model.md)
  - why server-side queue semantics + prompt-level ownership; why no interaction owner is implemented; cross-process tearing risk and the single-instance premise
- [`control-plane.md`](./decisions/control-plane.md)
  - why kitectl mutates daemon state through a loopback control plane; the dual-writer bug it fixed; the outcome-unknown error taxonomy
- [`multi-instance.md`](./decisions/multi-instance.md)
  - the multi-tenant shape: instance layout, isolated per-instance kap homes, resolution ladder, daemon instance lease; why interaction owner is still reserved

### Verification

- [`spike-checklist.md`](./verification/spike-checklist.md)
  - Milestone 0: the upstream hands-on validation checklist that must pass before business code work starts
- [`spike-results.md`](./verification/spike-results.md)
  - Milestone 0 results (passed 2026-07-21, kimi 0.28.1): per-item observations, upstream drift corrections, and the design adjustments folded into contracts/architecture

### Research

- [`kap-server-usability.md`](./research/kap-server-usability.md)
  - kap-server usability survey (lifecycle, authentication, API coverage, event model, concurrency semantics, maturity, gaps)
- [`focus-assets-map.md`](./research/focus-assets-map.md)
  - what to borrow from FOCUS and why (lifecycle, robustness, SSOT assets), mapped to the admitted Phase 2 features

## Read By Question

| Question | Read |
| --- | --- |
| What is KITE's overall architecture and process shape? | [`kite-design.md`](./architecture/kite-design.md) |
| Why bridge kap-server instead of ACP / node-sdk / wire? | [`backend-selection.md`](./decisions/backend-selection.md), [`kap-server-usability.md`](./research/kap-server-usability.md) |
| Why not embed kap-server with TypeScript? Why reuse FOCUS's Python assets? | [`process-shape-and-language.md`](./decisions/process-shape-and-language.md) |
| Why is there no `kite` local TUI wrapper (the counterpart of FOCUS's fcodex)? | [`process-shape-and-language.md`](./decisions/process-shape-and-language.md) |
| What happens when multiple Feishu chats operate on the same session at once? Who holds exclusive write? | [`concurrency-model.md`](./decisions/concurrency-model.md) |
| What does the MVP do and not do? How does a new feature get admitted? | [`mvp-scope.md`](./contracts/mvp-scope.md) |
| Which upstream behaviors must be validated hands-on before work starts? | [`spike-checklist.md`](./verification/spike-checklist.md) |
| What did the spike observe, and which doc facts did it correct? | [`spike-results.md`](./verification/spike-results.md) |
| Which Phase 2 features are admitted, and under what contracts? | [`streaming-cards.md`](./contracts/streaming-cards.md), [`images.md`](./contracts/images.md), [`group-chat.md`](./contracts/group-chat.md) |
| Why does kitectl mutate through a loopback control plane? | [`control-plane.md`](./decisions/control-plane.md), [`focus-assets-map.md`](./research/focus-assets-map.md) |
| Why no memory / ASR / device control? | [`mvp-scope.md`](./contracts/mvp-scope.md) |

## Language

Bilingual Chinese-English is mandatory (decided 2026-07-21): English is the
canonical version (`<name>.md`), Chinese is `<name>.zh-CN.md`; the two
versions are paired and kept in sync; enforced by `scripts/check-docs.sh` in
CI (following the FOCUS convention). README and AGENTS.md follow the FOCUS
practice: README stays Chinese-only, and AGENTS.md ships with a zh-CN
translation.
