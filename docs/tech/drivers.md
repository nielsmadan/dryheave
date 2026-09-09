# Native terminal and event drivers

`drivers/` provides a persistent terminal owner, incremental native-event sources,
submission transactions, usage attribution and separate cleanup observations.
It does not acquire run/store locks, launch simulators, snapshot repositories or
grade tasks. The runner must acquire its run lock followed by the store native
lock and persist attempt/launch identity before constructing a native session.

## Public seams

- `drivers.tui_test.TuiTestTerminal(executable, runtime, limits=...)` implements
  `drivers.base.Terminal`: `start(plan, environment)`, `state()`, `paste(text)`,
  `enter()` and `close()`. Always use it in one Python controller and a `with`
  context for its entire lifetime. A forked controller cannot send further input.
- `drivers.log_source.NativeLogSource(log_root, agent, artifacts, limits)` must
  be constructed **before launch**. It excludes existing JSONL files and tracks
  new files by device/inode, byte offset, line and event sequence. `poll()` returns
  new native events; `cursor()` is a persisted submission boundary. Codex logs
  normally appear beneath the owned `CODEX_HOME/sessions`; Claude logs beneath
  the owned `CLAUDE_CONFIG_DIR/projects`. Supply only the owned native log root.
- `drivers.artifacts.ArtifactWriter(new_directory, max_bytes)` retains bounded
  JSON observations and raw native JSONL bytes with private file modes. Use a
  separate directory from the transport runtime. Artifacts are potentially
  sensitive operator evidence and must not enter default portable exports.
- `drivers.session.DriverSession(terminal, source, artifacts, agent=..., limits=...)`
  owns the exchange in a `with` context. `start(plan, environment)` waits for the
  composer or returns a typed blocker. `prepare(text)` persists an immutable
  `Submission` containing ID, normalized text hash and pre-submit cursor;
  `deliver(submission)` attempts delivery once and waits for completion.
  `reconcile(wait=False)` observes the existing attempt without typing again.
  `close()` returns a separate `CleanupReport`, and is idempotent.
- `session.launch` keeps requested argv/profile/cwd separate from observed native
  session/version/model/effort/cwd. Effective permissions remain unknown.
  `session.events` contains bounded native tool/dialogue/lifecycle evidence.
  `session.usage_snapshot()` returns attributable and unattributed records, the
  observed session graph, missing usage nodes and explicitly partial coverage.
- `drivers.fake.FakeTerminal` and `FakeEventSource` implement the same seams with
  deterministic screens/event batches and injected partial/ambiguous delivery.
  Fake launch observations identify `transport="fake"`; they are offline evidence.

The source, terminal and driver should share the same `DriverLimits`. Defaults are
180 seconds total, five seconds per transport command, 4000 polling operations,
16 MiB native logs, 20,000 events/observed process identities, 256 simultaneously
owned processes, 256 scanned log entries, depth 12, 32,000 input bytes, 1 MiB per
command response and 64 MiB per artifact writer/transport directory. The transport
also has a watchdog for lifetime and recording growth when the caller stops
polling. Limits are finite and validated. Kernel scheduling may delay watchdog
reaction; these bounds are not a native host security sandbox.

`launch_environment(plan, inherited)` remains the profile-owned environment
projection. Pass its result directly to `terminal.start`; the transport adds only
its explicitly owned `TUI_TEST_HOME`. It preserves the exact `LaunchPlan.argv`
and cwd, including an explicit shell launcher. No permission bypasses or silent
HOME relocation are added. Materialization and driver readiness do not establish
model authentication, full profile fidelity or successful task completion.

## Delivery and native completion

Conversation input normalizes CRLF/CR to LF and trims outer whitespace, matching
the pinned Codex composer. Control bytes and generated native command/skill
prefixes `/`, `!` and `$` are rejected. The explicit `curated-native-command`
submission policy allows selected curator commands but grants no simulator
authority; it must be recorded by the runner. The simulator still needs its own
bounded reply/fact validation in the later controller service.

The transport sends bracketed paste via `type -- TEXT`, then the driver polls
the composer before pressing Enter. Short multiline content must match the
visible composer projection, preserving paragraph breaks and accounting for
word/hard wrapping at the configured terminal width; large Codex pastes use the
exact character-count placeholder. Cursor rows bound the input span when known.
Neither a placeholder nor a terminal echo establishes accepted content. Unknown
composer content, failed paste verification, image-path attachment behavior or
unrecognized layouts stop without Enter. Codex's `›` and Ultra `»` prompt markers
are supported. Its exact empty placeholders are recognized only with a cursor
at the empty input position, avoiding confusion with user text containing the
same words. A delayed render is polled within the command/lifetime limits.

An Enter timeout, rejected acknowledgement or malformed response is ambiguous.
The driver reconciles native logs and never resends that submission. Accepted
turns have a separate count from attempted deliveries. A partial or unknown
delivery remains unresolved; the caller cannot turn it into a fresh attempt by
calling `deliver` again. New attempts belong to the runner's explicit retry policy.

Codex 0.153.4 can defer new rollout creation until its first turn. Startup therefore
permits a ready composer before a session ID exists. The initial submission stores
an empty cursor/root identity; later metadata must identify one fresh root session
with the requested cwd. Subsequent submissions pin that identity. Preexisting
files, assistant text, stale user echoes, child sessions and unrelated turn
completions cannot establish success. The driver requires a new accepted user
event with matching normalized text and a started/completed lifecycle pair for
the same root turn. Queued user events may precede their own `task_started`.
Cancellation is distinct from completion. A completed assistant turn ending in
`?` is surfaced as an ordinary question heuristic for the controller; semantic
task completion belongs to the runner.

Claude is fixture-tested only. Human acceptance needs a UUID and excludes
tool-result, meta and sidechain user records. Assistant events retain parent UUID
lineage; completion requires linked `end_turn`/`stop_sequence` followed by the
native `turn_duration` lifecycle marker. Tool stops, missing lineage and hook
continuation remain unresolved. Hook metadata is retained and invalidates a
pending completion until later native evidence establishes another terminal
response. This deliberately conservative contract can stop unsupported Claude
versions rather than claiming their turns completed.

Native Codex logs omit approval, request-user-input and MCP elicitation dialogs.
Pinned terminal anchors therefore distinguish `approval`, `structured_input` and
`unsupported` observations; auth/trust/hooks/permission dialogs never become
ordinary simulator questions. V1 stops these states for caller policy; it does
not implement arbitrary dialog replies. Unknown UI or missing completion reaches
a bounded visible blocker/timeout. Terminal exit code `0` means exited, never
still running. Terminal readiness/silence is not model completion.

Usage keeps response/session/thread/root-turn identity, raw categories and source
provenance. Responses deduplicate by identity; cumulative records are marked
superseded only when response records exist for that same thread. Cumulative
series are never summed. Invalid categories and absent usage remain unknown;
unattributed/orphan records are retained separately. The graph and usage coverage
remain partial because native logs do not enumerate every detached agent.

## Transport protocol and cleanup

The configurable executable must report exactly `tui-test 0.1.0-beta.3`. The
checkout's verified binary is `.tools/tui-test/tui-test`; no package-manager
launcher or global install is used. `runtime` must be a new owned directory with
an absolute path of at most 70 bytes for portable Unix sockets. A unique short
session name and always-on asciicast recording directory are generated there.
Use a different runtime for every attempt and profile.

Ordinary replies use `{ok,data?,message?,kind?}`. Measured running `daemon status`
returns `{ok:true,data:{pid,shell_pid,version,...}}` with exit 0; absent status
returns bare `{pid:null,running:false,session}` with exit 3. The adapter normalizes
these internally and preserves raw responses. Screens contain nullable integer
`exited`; `null` is running. Raw status fixtures and pinned source provenance are
in `tests/fixtures/driver-provenance.json`.

`process_ownership.ProcessOwner` uses pinned **psutil 7.2.2** for portable process
identity and descendant tracking. This is the additional runtime dependency;
standard-library subprocess groups alone cannot retain detached children after
reparenting. The [versioned psutil documentation](https://psutil.io/7.2/) documents
PID-plus-creation-time identity, safe `is_running`/signal checks, and the fact that
`children()` loses descendants after intermediate parents disappear. Observed
children are retained independently before this happens. Only owned identities
are signaled; terminal close verifies the recorded daemon creation time before
addressing its session. Reused/reaped PIDs are never signaled as old processes.

The controller closes its owned terminal, then terminates/escalates retained
writers and checks survivors. The bounded command helper owns and reaps its
direct subprocesses and drains their pipes; process tracking deliberately does
not compete with `Popen.wait()` to reap those children. The driver drains remaining
native logs only after cleanup. A successful tui-test close by itself does not
prove a detached child stopped. Tests include a real bounded Python writer that
survives parent exit, plus deterministic PID mismatch and limit failures.

`CleanupReport` separates terminal closure, known-writer stop confirmation, log
drain, survivors and errors from turn completion. Any cleanup failure must make
comparison/grading ineligible. Native coverage is always `partial-native`:
descendants that detach between scans can remain unobserved. SIGINT/exception
unwinding and cancellation must exit the session context; the runner owns SIGTERM
handling and recovery journals. An uncatchable controller crash cannot promise
cleanup and must be reconciled as interrupted by the runner.

## Opaque runtime auth binding

`drivers.auth.RuntimeBindings(plan, inherited)` is an optional explicit context
around the driver context. It binds only frozen runtime-file references, looks up
only their named path variables, and creates the acknowledged `auth.json` symlink
inside the owned config root. It never reads, copies, inventories or logs credential
bytes. Source existence/login validity are intentionally unverified. The source
path variable is not automatically exposed to the subject.

Exit removes only the symlink with the recorded filesystem identity. A replaced
binding is preserved and reported as `runtime_binding_replaced`; do not delete
unknown replacement content as cleanup. Codex may write through a binding during
token refresh, as acknowledged in the frozen profile. Tests use only nonexistent
or synthetic fixture paths. This task never bound the user's actual credentials.

## Non-model transport smoke

This API runs only a bounded Python raw-terminal receiver. It verifies exact
bracketed paste bytes for a leading hyphen, newline and Unicode, then terminal
exit and owned cleanup. It does not launch an agent or consume model usage.
Use a new short ignored directory on every invocation:

```sh
UV_CACHE_DIR=.cache/uv TMPDIR=.cache/tmp uv run python - <<'PY'
from pathlib import Path
from dryheave.drivers.smoke import smoke_transport

result = smoke_transport(Path('.tools/tui-test/tui-test'), Path('.cache/transport-smoke'))
print(result.model_dump_json(indent=2))
PY
```

The implementation is `src/dryheave/drivers/smoke.py`; its receiver script and
recordings stay under the supplied runtime. `logs_drained` is false in this
transport-only smoke because no native agent-log adapter ran. On 2026-09-09 the
implementation smoke at `.cache/ts2` observed exact bytes, exit 0, terminal close,
and every known writer stopped. Codex live composer/model validation and all
Claude live validation remain unexecuted in Task 4.
