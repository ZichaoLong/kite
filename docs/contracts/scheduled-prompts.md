# Contract: Scheduled Prompts (Phase 3)

> Status: admitted (2026-07-25, passed the carrying-capacity gate below);
> turns active with its implementation.
> Blueprint: FOCUS `docs/contracts/scheduled-prompts.md` (the battle-tested
> shape: no built-in scheduler subsystem; systemd timers route back through
> the local CLI into the daemon).

## 1. Carrying-Capacity Gate

1. **Which layer?** Admin surface (`kitectl schedule`) + OS timers; execution
   rides the loopback control plane into the daemon (`prompt/submit`), i.e.
   the application layer. The daemon gains no new subsystem.
2. **Which state axis?** None new. A scheduled prompt is an ordinary prompt:
   ownership is recorded to the bound chat via the control plane (axis 4),
   approvals/questions behave identically to Feishu-originated prompts.
   Schedule metadata lives in systemd unit files, not in KITE stores.
3. **Crash/restart recovery?** Timers are systemd's business (persistent
   across kited restarts and reboots, subject to `Persistent=`). A fired
   prompt recovers per mvp-scope §4.6 like any other prompt. `kitectl` is
   stateless about schedules beyond reading the unit files.
4. **Which tests?** §4 below.

## 2. Product Shape

Explicitly NOT a built-in scheduler subsystem and NOT a persistent job
queue. The supported shape is: **safely synthesize one new prompt for an
existing binding at a future time**, executed by the same KITE instance
through the normal prompt path.

- Trigger path: systemd --user timer → service unit runs
  `kitectl [--instance <name>] prompt send --chat <chat_id> --text <text>`
  → loopback control plane → daemon submit (ownership recorded to the chat,
  modes carried from the binding). A schedule created for a named instance
  fires with `--instance <name>`, so it always routes back to the instance
  that created it (§3.1).
- Conflicts ride kap's server-side FIFO (a busy session queues; no local
  in-memory FIFO exists or is needed).
- Platform boundary: the managed timer helper dispatches like
  `service_manager` — Linux `systemd --user` timers, macOS `launchd`
  (`StartCalendarInterval`), Windows Task Scheduler calendar triggers
  (admitted 2026-07-25).

## 3. `kitectl schedule` Surface

- `kitectl schedule create --chat <id> --text <text> (--at <ISO timestamp> | --cron <expr>) [--display silent|announce]`
  - writes `kite-schedule-<hash>.timer` + `.service` (default instance) or
    `kite-schedule-<instance>-<hash>.timer` + `.service` (named instance)
    under `~/.config/systemd/user/` and enables the timer; never starts it
    manually for the future time (systemd owns firing).
  - `--at` produces a one-shot timer (`OnCalendar=<ts>`); `--cron` a
    recurring timer. A past timestamp and an unparseable cron are rejected
    before writing anything (fail-closed).
  - kitectl path resolution for the generated service unit: explicit
    `--ctl-path` > `KITE_BIN_DIR/kitectl` or `~/.local/bin/kitectl` >
    `<data root>/.venv/bin/kitectl`. The resolved path is stored in the unit.
- `kitectl schedule list` — enumerate the current instance's
  `kite-schedule-*.timer` units with their schedule + next elapse (parsed
  from `systemctl --user list-timers` when available, unit file as
  fallback).
- `kitectl schedule show <name>` — print the timer + service definitions.
- `kitectl schedule remove <name>` — disable + delete the unit pair
  (confirmation-free only with `--yes`).
- `kitectl schedule run-now <name>` — fire the service unit once
  immediately (via `systemctl --user start`).

### 3.1 Instance scoping (multi-instance)

The OS timer store is a single shared namespace per host user, so schedules
are namespaced per instance (docs/decisions/multi-instance.md):

- A named instance's unit name carries the instance segment:
  `kite-schedule-<instance>-<hash>` (the default instance keeps
  `kite-schedule-<hash>`). The hash itself stays chat + schedule + text, so
  two instances can never collide or overwrite each other's timers.
- The generated ExecStart of a named instance fires
  `kitectl --instance <name> prompt send ...`, so the prompt is submitted to
  the instance that created the schedule; the default instance omits the
  flag (kitectl's normal resolution ladder then applies).
- `list`, `show`, `remove`, and `run-now` see and accept only the current
  instance's schedules: a bare hash resolves inside the current namespace,
  and a full name from another namespace is rejected fail-closed.

## 4. Behavior and Safety Contract

1. A scheduled task is only "start one new prompt at a future time"; it may
   not bypass binding/attach/actor admission (the control plane enforces the
   same rules as for CLI prompts).
2. `display_mode`: `silent` (default) submits without a trigger notice;
   `announce` makes the daemon send one short "scheduled trigger" notice to
   the target chat before submitting. No richer choreography.
3. The target binding must exist at fire time; when it does not, the
   control plane's existing `no_binding` error is the fail-closed outcome
   (no implicit binding creation).
4. Recurring timers must carry an explicit termination strategy, same as
   FOCUS: a one-shot timestamp, a self-removal condition + removal command
   inside the prompt, or a deterministic one-shot cleanup prompt at a known
   deadline. The helper warns when `--cron` is created without one.
5. The daemon being down at fire time yields the control-plane "not
   running" error in the service unit's log (visible via
  `systemctl --user status kite-schedule-<name>`); nothing is retried
   blindly (outcome-unknown is visible, not hidden).

## 5. Tests That Lock the Behavior

- Unit-file rendering: one-shot vs recurring, `OnCalendar` values, the
  resolved ctl-path inside the service unit, `Persistent=` on.
- ctl-path resolution order (explicit > env/bin-dir > venv).
- create validation: past `--at`, bad `--cron`, unknown chat (no binding) →
  rejected, nothing written.
- list/show/remove/run-now flows over a mocked systemctl (no real systemd
  calls in tests); remove requires `--yes`.
- instance scoping (§3.1): a named instance's unit name carries the
  instance segment and its ExecStart carries `--instance <name>`; two
  instances cannot collide; list filters to the current instance;
  show/remove reject another instance's schedule fail-closed.
- `announce` submits a trigger notice before the prompt (daemon-side, via
  the control plane prompt/submit path with a display flag).
- Daemon down at fire time: the service unit surfaces the refusal (logged),
  exit non-zero.
