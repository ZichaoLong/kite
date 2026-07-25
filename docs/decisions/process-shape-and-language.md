# Decision: Process shape, language, and the local TUI wrapper

> Status: Decided (open alignment items in the final section).

## Question 1: Reuse FOCUS assets in Python, or embed kap-server in TypeScript?

kap-server is exported as a library with `startServer()`, so in theory KITE
could be a TS process, putting the engine, the API, and the bridge into the
same process.

**Decision: Python, with kap-server as a managed subprocess.**

Rationale:

1. **Asset reuse differs by an order of magnitude.** Of the roughly 40k lines
   in FOCUS `bot/`, the upstream-agnostic parts are the majority — the Feishu
   transport layer, cards, RuntimeLoop, binding/stores, the group-chat domain,
   service_manager, the install system. What is truly bound to codex is only
   the adapter layer, about 1.5k lines (977 + 559, measured 2026-07-21;
   `adapters/codex_app_server.py` +
   `codex_protocol/client.py`). Choosing Python = replacing those ~1.5k lines +
   revising contracts to kap semantics; choosing TS = rewriting all the other
   38k lines.
2. **The fork-and-embed route has been disproven.** Embedding an agent's
   internals costs merging with upstream forever; and since kimi-code is TS,
   fork-and-embed physically has no interface to begin with.
3. **Process isolation is itself an advantage.** A kap-server crash does not
   take down the bridge's state machine; a bridge crash does not take down the
   session runtime. FOCUS's managed mode has already validated this shape.
4. kap-server's lack of a daemon mode is exactly compensated for by the
   managed subprocess shape.

Cost: one extra layer of process management (port, token, lifecycle); the
corresponding FOCUS code can be ported directly, so the cost is prepaid.

## Question 2: Why defer the local TUI wrapper (FOCUS's `focus`/`fcodex` counterpart)?

One of FOCUS's core selling points is "the local terminal continues the same
live thread that Feishu is operating on". **This capability is currently not
implementable on kimi-code; MVP does not do it, and the command names
`kite`/`kcode` are reserved.**

Basis (details in `docs/research/kap-server-usability.md` §6):

- The interactive `kimi` TUI cannot connect to kap-server; there is no
  `codex --remote`-style mode; the TUI and the server engines are even
  different generations (v1 vs v2).
- `kimi -S` resumes an on-disk session, and there is no cross-process session
  lock: continuing a session in the TUI while kap-server holds it live = two
  processes writing the same session directory without a lock. If KITE
  provided a wrapper, it would be product-level encouragement of this data
  corruption path.

### Alternative stance: bare kimi is out of the shared contract

Consistent with FOCUS's stance on bare codex, written into the README and
user docs:

> While a session is held by KITE's kap-server (busy or recently active), do
> not use `kimi -S` / `kimi -c` to continue the same session locally; to
> operate locally, first `/detach` on the Feishu side and confirm the session
> is idle, then take on the cold continuation at your own risk.

If upstream gains a remote attach capability in the future (the wire protocol
returns, or the TUI learns to connect to kap-server), add the wrapper back per
the FOCUS design: a thin shell + local proxy + exec of the upstream TUI.

## Question 3: With the TUI wrapper deferred, who carries local continuation? (Supplementary note after the 2026-07-21 check)

**The upstream web UI carries it.** The check confirms (evidence:
supplementary check in `docs/research/kap-server-usability.md`):

- kimi-web is a pure /api/v1 client of kap-server, with no privileged channel
  and no exclusivity assumptions — a fully equal peer of the KITE bridge;
- Concurrency is backed by the server-side FIFO + converging broadcasts +
  approval idempotency; the Feishu side and the web side operating the same
  session simultaneously is a first-class scenario within upstream's design;
- The web UI has genuinely invested mobile adaptation; `kimi web --host` +
  the token-bearing Network URL natively supports phone access.

Therefore KITE's "continue the same session locally" story = the same
`kimi web --no-open` supervised by `kited`, with no self-built wrapper at all.
This also reinforces the choice of `kimi web --no-open` over a pure API shim
for the launch method (see `kite-design.md`, Aligned item 1).

Product-positioning corollary: when the phone is on the same LAN, the web UI
and the Feishu bot overlap in functionality; KITE's unique value on the Feishu
side lies where the web UI cannot reach — cross-network reachability (Feishu
push needs no VPN/tunnel), group-chat sharing, and IM-native asynchronous
approvals.

## Aligned (2026-07-21)

1. Launch command: `kimi web --no-open` (see `kite-design.md`, Aligned item 1).
2. Version policy: no hard pinning (see `kite-design.md` §10); perform version
   detection at install/startup time; warn but do not block when the version
   differs from the verified version.
