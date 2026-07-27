# Decision: Multi-Instance (Multi-Tenant) Shape

> Status: Decided (2026-07-26, superseding the "Phase 3 candidate" registration
> in kite-design.md §9 and complementing `docs/decisions/concurrency-model.md`).
> Blueprint: FOCUS's `bot/instance_layout.py` and `bot/instance_resolution.py`.

## Problem

KITE must run one bot per enterprise (Feishu tenant): multiple independent
instances on one host, each with its own Feishu app, config, bindings, and
kap-server. The MVP premise was single-instance; this decision defines the
multi-instance shape and what concurrency machinery it actually requires.

## Decision

### 1. Instance layout (FOCUS's shape, migration-free)

```
<config root>/instances/<name>/   (config: system.yaml, env, init.token, control.token)
<data root>/instances/<name>/     (data: stores, logs, kap home, runtime status)
```

The **default instance keeps today's paths byte-identically**
(`~/.config/kite` + `~/.local/share/kite`) — the current deployment becomes
the default instance with zero migration. Named instances live under
`instances/<name>/`. A name is `[a-z0-9][a-z0-9._-]*`, at most 64
characters (FOCUS parity; fail-closed on anything else; no `default`,
`instances`, `..`).

### 2. Per-instance isolated kap home (the tearing killer)

Each instance's kited spawns its kap child with an **isolated
`KIMI_CODE_HOME`** at `<data>/kap-home/` (instead of the shared
`~/.kimi-code`):

- Sessions are physically per-tenant — the correct semantics for
  multi-enterprise bots.
- No two kap-server processes can ever write the same session directory:
  the "no cross-process session lock" hazard from
  `docs/research/kap-server-usability.md` §4/§7 disappears by construction.
- Provider config comes from the instance's own env file (the
  `KIMI_MODEL_*` overlay, spike-proven), so no per-home kimi config is
  needed; each tenant may also point `kap.home` at a real, per-tenant kimi
  home if it wants shared state with a local kimi CLI (its own risk, per
  the bare-kimi stance).

### 3. Instance resolution (kitectl)

`kitectl [--instance <name>] <command>`; resolution ladder
(FOCUS's `instance_resolution.py`):

1. explicit `--instance` (or `KITE_INSTANCE` env),
2. the single running instance when exactly one is live (discovered via
   per-instance `control_plane.json` metadata, stale pid filtered),
3. the default instance.

Ambiguity (multiple running, none explicit) fails closed with the list of
candidates. `kitectl service` commands always take an explicit-or-default
instance (no "single running" convenience for destructive ops).

### 4. Daemon instance lease (the real cross-instance guard)

Two kited processes must never drive the same instance (Feishu would
load-balance bot events between them → inconsistent behavior). kited takes
an **exclusive advisory file lock on `<instance data>/kited.lock`** at
startup; a second kited on the same data dir exits immediately with a clear
message naming the holder pid. The lease lives in the data dir — not the
config dir — because every mutable shared surface is there
(`control_plane.json`, the stores, `runtime_status.json`,
`<data>/kap-home`), and a per-axis explicit `--config-dir`/`--data-dir`
override can point two different config dirs at one data dir (FOCUS locks
the data dir for the same reason). This replaces nothing else — it is the
only cross-process coordination multi-instance actually needs.

### 5. Interaction owner: still NOT implemented (deliberate)

The user's direction included "interaction owner and other supporting
machinery". Analysis, recorded here so the decision is revisitable:

- The owner lease in FOCUS exists because codex app-server's interaction
  requests have a single exit and need a deterministic route. kap made
  approvals/questions queryable REST resources with idempotent responses,
  so in-instance multi-chat is already safe via prompt-ownership routing
  (see `docs/decisions/concurrency-model.md` — unchanged).
- The only cross-process session-sharing vector an owner would guard is
  eliminated by §2 (isolated kap homes — no two processes share sessions).
- The remaining shared surfaces (Feishu bot identity per tenant, kap
  per-prompt FIFO, approval idempotency) are covered by §4 and upstream
  semantics respectively.

Therefore the owner lease remains a **reserved concept** (kite-design.md §4)
with the same revisit signals as before (real users hit a conflict it would
solve; upstream grows a locking primitive). If either fires, this section
is where the implementation starts (FOCUS's
`stores/interaction_lease_store.py` + `thread_runtime_coordination.py`).

## Consequences

- kite-design.md §9's "multi-instance is a Phase 3 candidate requiring a
  cross-instance concurrency contract first" is resolved by this document.
- New config/env var: `KITE_INSTANCE`; new kitectl global flag `--instance`.
- The `kap.home` default changes from `~/.kimi-code` to the instance's own
  `<data>/kap-home` **only for instances**; the default instance keeps
  `~/.kimi-code` (its live state is there).
- Fail-closed throughout: bad instance names, lease conflicts, ambiguous
  resolution, and missing per-instance provider config all produce explicit
  errors, never silent fallbacks.
