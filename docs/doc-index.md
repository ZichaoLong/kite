# Docs Index

This directory is the source of truth for repository architecture, runtime
boundaries, and feature contracts.

## Reading Rule

When code and docs disagree, treat that as a contract gap. Tighten the code,
the docs, or both.

## Current Status

The repository is in the planning phase; all documents are **drafts**. The
first alignment round completed on 2026-07-21, and each document's "open
questions" have been converted into "aligned" records; the next step is spike
validation (`docs/verification/spike-checklist.md`). Once it passes, the
documents turn active and code work begins. After the transition to active,
contracts/, architecture/, and decisions/ are treated as active repository
facts.

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
  facts (after the transition to active)
- treat `verification/` as validation support
- treat `research/` as evidence; if its facts conflict with the latest
  upstream code, the upstream code wins
- treat local notes under `docs/_work/` as working material

## Read By Type

### User-Facing Entry

- [README.md](../README.md)

### Contracts

- [`mvp-scope.md`](./contracts/mvp-scope.md) (draft)
  - MVP feature scope, command surface, approval behavior, fail-closed list, non-goals, carrying-capacity gate

### Architecture

- [`kite-design.md`](./architecture/kite-design.md) (draft)
  - overall architecture, process shape, layering, adapter layer boundaries, state axes, event consumption strategy, card model, persistence, service management

### Decisions

- [`backend-selection.md`](./decisions/backend-selection.md)
  - why kap-server was chosen as the shared backend; the trade-off between the two integration paths; countermeasures against upstream drift
- [`process-shape-and-language.md`](./decisions/process-shape-and-language.md)
  - why Python + managed subprocess; why the local TUI wrapper is deferred; the stance on bare kimi
- [`concurrency-model.md`](./decisions/concurrency-model.md)
  - why server-side queue semantics + prompt-level ownership; why no interaction owner is implemented; cross-process tearing risk and the single-instance premise

### Verification

- [`spike-checklist.md`](./verification/spike-checklist.md)
  - Milestone 0: the upstream hands-on validation checklist that must pass before business code work starts

### Research

- [`kap-server-usability.md`](./research/kap-server-usability.md)
  - kap-server usability survey (lifecycle, authentication, API coverage, event model, concurrency semantics, maturity, gaps)
- [`okbot-vs-focus.md`](./research/okbot-vs-focus.md)
  - comparison of the benchmark project OKbot with FOCUS; lessons from the OKbot route

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
| Why no memory / ASR / device control? | [`okbot-vs-focus.md`](./research/okbot-vs-focus.md), [`mvp-scope.md`](./contracts/mvp-scope.md) |

## Language

Bilingual Chinese-English is mandatory (decided 2026-07-21): English is the
canonical version (`<name>.md`), Chinese is `<name>.zh-CN.md`; the two
versions are paired and kept in sync; enforced by `scripts/check-docs.sh` in
CI (following the FOCUS convention). README and AGENTS.md follow the FOCUS
practice: README stays Chinese-only, and AGENTS.md ships with a zh-CN
translation.
