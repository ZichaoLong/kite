# AGENTS.md

This file only records the repo owner's design and development preferences.

Do not treat this file as the source of truth for repository architecture, module boundaries, or feature semantics. Read the relevant documents under `docs/` on demand.

本文件移植自 FOCUS 的同名文件，工程立场完全一致；KITE 特有约定见末节。

## Core Preference

Default toward:

- clear architecture
- easy maintenance
- unambiguous behavior

Do not default toward:

- preserving compatibility for its own sake
- keeping weak abstractions because they already exist
- encoding fuzzy product behavior directly into code

## Default Engineering Stance

When making changes:

- prefer explicit contracts over implicit conventions
- prefer one clear path over multiple half-supported paths
- prefer removing ambiguity over preserving legacy shape
- prefer simple control flow over clever layering
- prefer fail-closed behavior over ambiguous best-effort behavior

If a feature contract is unclear, surface the ambiguity and tighten the contract in code, naming, validation, or docs.

## Compatibility

Compatibility is not a default goal in this repo.

Unless the user explicitly asks otherwise:

- internal APIs may be changed freely
- stale branches and compatibility shims may be removed
- behavior may be simplified if the result is cleaner and easier to reason about

例外：对上游 kimi-code 的 kap-server API 依赖属于**外部**契约，不适用本条——上游演进不受本仓库控制，须以 CI 快照 diff 感知漂移、在适配层内显式适配（版本策略见 `docs/architecture/kite-design.md` §10：跟随，不钉死）。

## Refactoring Bias

Refactoring is encouraged when it improves clarity.

Good refactors usually:

- make ownership clearer
- reduce hidden coupling
- remove duplicate paths
- reduce ambiguity in runtime state or behavior

Bad refactors usually:

- move complexity without clarifying ownership
- add abstraction without simplifying the code
- preserve confusing structure just to avoid change

## Review Priorities

Prioritize, in order:

1. ambiguous or incorrect behavior
2. unclear ownership of state, events, or responsibilities
3. hidden coupling across modules
4. concurrency or lifecycle risk
5. missing regression coverage for high-risk flows
6. naming or structure that obscures intent

## Testing Preference

Do not stop at "tests pass".

When practical, add or update tests that lock down the intended behavior of the change, especially for bugs, state transitions, ownership transfer, and other high-risk flows.

测试从第一天起进 CI（FOCUS 缺测试 CI 的教训不重复）。

## Docs Policy

Keep repository facts out of this file.

- Architecture, boundaries, and runtime design belong in dedicated docs.
- Feature contracts and behavior semantics belong in dedicated docs.
- When adding or changing an important feature, command, concept, or abstraction for a concrete scenario, prefer recording its design intent in the relevant doc under `docs/`, not just its surface behavior.
- Prefer documenting three points whenever practical:
  - what problem or scenario it is meant to solve
  - which layer of state or abstraction boundary it operates on
  - why existing mechanisms were not sufficient
- This is mainly to preserve the reason something exists, so later refactors can still tell whether it should be kept, split, simplified, or removed.
- Read those docs only when the task needs them.
- Technical docs are bilingual: English is canonical (`<name>.md`), Simplified Chinese is the paired translation (`<name>.zh-CN.md`); the pair is enforced in CI by `scripts/check-docs.sh`.

**功能承载力门槛**：任何功能进入开发前，必须先在其合同文档中回答四个问题——归哪一层？动哪条状态轴？崩溃/重启后怎么恢复？用什么测试锁住行为？答不出来就砍需求，不加例外。

## 克制原则

功能增速不得超过架构承载力。宁可少做、做透，不做多、做漏。

- 新功能默认拒绝，除非通过承载力门槛
- 每个功能必须有明确的失败模式（fail-closed），不允许"出错时尽力而为"
- Non-goals 与 goals 同等重要，写进合同文档

## KITE 特有约定

- **上游 source of truth**:kap-server 行为以 `~/llm/kimi/kimi-code` 的实际代码为准；二手描述不可信时先读上游代码。
- **架构蓝本**:FOCUS(`~/llm/focus`)。复用其上游无关资产（飞书传输层、卡片、RuntimeLoop、stores、service 管理）时，按 KITE 词汇（session/agent/prompt/approval/question）重命名，不保留 codex 术语。
- **上游版本跟随，不钉死**（2026-07-21 对齐）：不指望长期停留在一个旧 kimi-code 版本上；CI 必须有 kap-server OpenAPI 快照 diff 护栏感知漂移；安装/启动做版本检测，与已验证版本不符时警告但不阻止运行。
- **安装纪律**（沿用 FOCUS）：不使用 `pip install .` / `pip install -e .`；唯一支持的安装路径是仓库提供的 install 脚本。
