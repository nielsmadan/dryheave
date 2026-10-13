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

Each `type` command may take `command_seconds` plus 0.05 seconds per typed
character, bounded by the terminal lifetime; key presses keep `command_seconds`.
A `type` or Shift+Enter that loses its acknowledgement has submitted nothing, so
the driver reads the composer instead of failing: an exact match continues to
Enter; anything else is cleared with Ctrl+U, pressed until the composer reads
empty (Claude 2.1.296 kills one wrapped row, then its newline, per press), and
the whole prompt is retyped once. A composer that still does not match, or does
not clear, stops as `partial_input` without Enter. Screen reads that lose their
acknowledgement while the composer is verified count as unread polls.

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
continuation remain unresolved. Auto-compaction mid-turn writes a
`compact_boundary` system record with a null `parentUuid`; it joins the turn of
its `logicalParentUuid`, or the current turn when that UUID is not yet known
(2.1.296 points it at a record written after the boundary). The following
`isCompactSummary`/`isVisibleInTranscriptOnly` user record continues that turn
and is never human acceptance. Hook metadata is retained and invalidates a
pending completion until later native evidence establishes another terminal
response. This deliberately conservative contract can stop unsupported Claude
versions rather than claiming their turns completed.

Background agents also hold completion. Claude 2.1.2xx records a background launch
as the tool result's `toolUseResult`: `isAsync: true` with `agentId` for Agent,
`resumedAgentId` for a SendMessage resume, `backgroundTaskId` for Bash, and
`taskId` with `timeoutMs` for Monitor. A TaskStop result (`task_id` with
`task_type`) ends that task. When a task finishes during a turn, Claude records a
`queued_command` attachment whose `commandMode` is `task-notification`. After a turn
end it instead writes a non-meta user record of `<task-notification>` blocks
(`origin.kind` `task-notification`) parented on the `turn_duration` record, then
continues without user input. A block names one or more `<task-id>` tags, usually
its launching `<tool-use-id>`, and a `<status>`. Only a terminal status
(`completed`, `failed`, `killed` or `stopped`) finishes the listed tasks; Monitor
events carry no status and finish nothing.

A `turn_duration` record's `pendingBackgroundAgentCount` decides whether the turn
is held: an integer holds it only while greater than zero. Older records without
the field fall back to the adapter's tracked Agent launches that no notification or
TaskStop has finished. Background Bash and Monitor tasks never hold a turn, since
Claude does not wait for them; a dev server that never exits leaves the turn free
to complete. A new human turn clears every tracked task. Notification records are
never human acceptance: they and the continuation they start stay in the turn they
are parented on, so a held turn completes once Claude ends a turn with no agents
pending, or reaches the driver timeout. A notification with no tracked parent and no
tracked task belongs to no turn.

Curated Claude slash commands are typed like other Claude input. Claude 2.1.296
shows its command menu above the composer, highlighting one `❯ /name` entry, and
closes it once arguments are typed. Enter is sent only when the highlighted entry is
exactly the typed name, or when the menu is closed and the command has arguments; an
unmatched or differently highlighted menu stops as `partial_input`. Escape is never
sent. Claude records the command as a user record of `<command-message>`,
`<command-name>` and optional `<command-args>` tags, accepted as `/name args`; the
expanded skill text follows as an `isMeta` user record in the same turn. A curated
local command that produces no model turn is instead followed by a non-meta
`<local-command-stdout>` (or `<local-command-stderr>`) user record parented on the
command; that record completes the command's own turn with its text as the
assistant text, rather than starting a new human turn. The bench launch check
probes slash steps only when the caller says workflows are enabled, and types each
offered workflow command name and full text, then clears it unsent.

Native Codex logs omit approval, request-user-input and MCP elicitation dialogs.
Pinned terminal anchors therefore distinguish `approval`, `structured_input` and
`unsupported` observations; auth/trust/hooks/permission dialogs never become
ordinary simulator questions. Approval and unsupported states stop for caller
policy; Codex `structured_input` and unrecognized dialogs can instead be answered
through the screen seam below. Unknown UI or missing completion
reaches a bounded visible blocker/timeout. Terminal exit code `0` means exited,
never still running. Terminal readiness/silence is not model completion.

Claude 2.1.296 startup prompts ("Accessing workspace:" folder trust and "Allow
external CLAUDE.md file imports?") render unnumbered `❯` option lists with an
"Enter to confirm · Esc to cancel" footer. They are `approval` observations, so a
run stops with `needs_input` before any simulator call. Any other unnumbered
Claude list with that footer is `unsupported`, never a composer.

## Claude question dialogs

Claude's AskUserQuestion dialog is the one `structured_input` the driver answers.
`dialogs.question_dialog(screen)` parses it into a `QuestionDialog`: question
text, rows (`option`, the free-text `text` row, and the multi-select `submit`
row), the focused row, checkbox state and the multi-select flag. The layout is
taken from the 2.1.296 bundled UI source, read statically and never observed
live: a rule, a tab bar of `☐`/`☒` headers (`← … ✔ Submit →` for several
questions or multi-select), the question, rows `❯ N. Label` with an indented
description line (multi-select rows read `N. [✔] Label`), a final
`Type something.` row, a `Next`/`Submit` row for multi-select, then a rule,
`N. Chat about this` and the footer `Enter to select · … · Esc to cancel` as the
last line. Options are located by column relative to the chat row, so unnumbered
lists also parse. A trailing committed `✔` is not part of a label. A
single-select question whose options carry previews renders side by side: a
30-column option list, a 4-column gap, then a boxed preview and a `Notes:` line,
an unnumbered `Chat about this` row and `n to add notes` in the footer. Options
are read from the left column only, wrapped labels are joined, and the preview
pane is ignored. Anything that does not fit stays unrecognized. The
final "Review your answers" screen parses as a `review` dialog with
`Submit answers`/`Cancel` rows.

`DriverSession.question()` returns the visible question for Claude, or `None`.
`answer(asked, DialogSelection)` works on the same submission and only
on the question it was given:

- Single select: Up/Down until the screen shows the highlight on the chosen
  row, then Enter.
- Free text: move to the `Type something` row, type the single line and wait
  for the row to show it, then Enter.
- Multi-select: Space on each row whose checkbox differs from the choices,
  waiting for each toggle, then move to the `Submit`/`Next` row and press Enter.

Every key waits up to `command_seconds` for the expected screen. If the screen
does not show it, nothing more is sent and the call returns `unsupported`
(`cancelled` when cancellation was requested). After Enter the driver waits for
the question to change. A following question returns `structured_input`, so
several questions are answered one at a time. A review screen is submitted only
when it reports every question answered. Closure must be seen on two
consecutive screens. The turn is then observed as usual through
`reconcile(wait=True)`. `session.answered` counts verified answers. Keys go
through `Terminal.press`, which accepts only `Up`, `Down`, `Space`, `Enter` and
the digits `1`-`9`, and sends them with tui-test `key press` (verified against
0.1.0-beta.3: a digit name sends that one byte); Escape is never sent.

Usage keeps response/session/thread/root-turn identity, raw categories and source
provenance. Responses deduplicate by identity; cumulative records are marked
superseded only when response records exist for that same thread. Cumulative
series are never summed. Invalid categories and absent usage remain unknown;
unattributed/orphan records are retained separately. The graph and usage coverage
remain partial because native logs do not enumerate every detached agent.

## Screen answers

Some question layouts the parsers do not know reach a model-driven fallback
instead of stopping. The fallback is an allow-list: `dialogs.answerable_screen(agent,
screen)` is true only when all of these hold:

- `dialog()` reports `structured_input` or the `UNRECOGNIZED` unsupported reason,
  and the screen is not a Claude question `question_dialog` parses.
- `question_chrome(agent, text)` finds the agent's own question-tool chrome:
  - Claude (AskUserQuestion): a row reading "Type something.", "Type something" (the
    multi-select placeholder) or "Chat about this" among the last six visible
    lines, compared after normalisation and with any highlight marker, number,
    checkbox and indentation removed, with no other numbered option row below the
    last such row (so agent text printed above a short permission prompt never
    qualifies); or its "Submit answers" row there under a "Review your answers" title.
  - Codex (`request_user_input`, wording from the 0.162.1 binary): a
    `Question N/M` header line, and in the last two visible lines a footer with a
    `<key> to submit answer` or `<key> to submit all` hint.
  - Droid: no question tool is known, so a Droid screen never qualifies.
- `permission_like(text)` finds no permission or mode wording.

Any other unrecognised screen, including every permission, mode, approval, trust
and tool-call prompt that lacks that chrome, keeps its `unsupported` observation
and stops the turn with `needs_input`. `permission_like` first normalises the text
(`normalized`: NFKC, zero-width characters removed, curly quotes and apostrophes
mapped to ASCII) and matches each line, minus any row marker and number, against
wording pinned from the shipped binaries: "don't ask (me) again", "do not ask again", "what to do
differently", "allow all edits", "grant permissions", trust prompts, "Do you want
to proceed/make/allow/approve/use/download…", "Allow …?", "Allow and …" / "Allow
for this session", "Approve …?", Claude's plan-mode exit ("Ready to code?", "Would
you like to proceed?", "Yes, … auto mode / auto-accept edits / bypass permissions",
"Enter/Exit plan mode?"), `Use skill "…"?`, "Use this skill?", "Run a dynamic
workflow?", and login phrases. It is a second layer behind the allow-list, not a
complete list of permission wording. Anchored approval, trust, startup, auth and
disabled-input screens never qualify. The live Claude 2.1.296 question in
`tests/fixtures/claude-2.1.296-fix-scope.txt` qualifies: its `☐` tab line sits
one column right of the question body, which `question_dialog` rejects, and it
shows the "Type something." and "Chat about this" rows.

`DriverSession.screen()` returns the current screen when it qualifies, else `None`.
A screen that `composer_ready()` classifies as the composer (a Claude `❯` row
directly under a rule, or a Codex composer row followed by `? for shortcuts` or a
context-left line) never qualifies, and `prepare()` clears the footer remembered
from the previous turn's screen.

`act(ScreenAction, shown, target)` applies one action: a key (`Up`, `Down`, `Space`,
`Enter`, `1`-`9`) or one bounded line of text typed with `paste`. It re-reads the
screen and sends nothing when the screen no longer qualifies, when the subject has
exited (an `exited` observation, or the completed turn), or when the normalised
screen differs from `shown`, the text the simulator judged; the stale case records
a `screen-stale` artifact and returns the new screen's classification so the
caller can ask again. Before `Enter`, `Space` or a digit, `escalation()` reads the
option the key would choose (the highlighted row and its continuation lines, the
row numbered by the digit, and for `Enter` any checked row) and the action's
`target`; if either names a mode or permission choice ("auto mode", "auto-accept",
"accept edits", "bypass", "don't ask again", "always allow", "allow all", "allow
and", "and allow", "trust", "dangerously", "skip permissions", "yolo", and the bare
words "allow", "deny" and "approve", matched
case-insensitively after normalisation) no key is sent, a `screen-refused`
artifact is recorded and the result is an `approval` observation with
`SCREEN_ACTION_REFUSED`. This guard is independent of `permission_like` and is a
backstop: its vocabulary does not cover every mode or permission option (one-shot
"Yes" rows, for example), so the
allow-list is what keeps those prompts away from the simulator. After a
sent action it waits up to `command_seconds` for the screen to change and then
read the same on two polls, records `screen-intent` (screen before, action) and
`screen-result` (changed flag, screen after; both the last 8000 characters)
artifacts, counts the action in `screen_actions`, and returns the classification
of the new screen: `exited` when the subject has exited, the dialog observation
while one is shown, an `unsupported` `UNRECOGNIZED` observation while the screen
keeps the footer it had when it qualified (Codex's free-text question stops parsing
once text is typed), otherwise `reconcile()` without waiting, so the caller can
account for the closed screen before observing the rest of the turn with
`reconcile(wait=True)`. `await_change(text, until)` waits within the driver deadline
and the monotonic `until` for the screen to differ or the turn to end, with the same
classification; reaching `until` on an unchanged screen returns `unsupported` with
`SCREEN_WAIT_EXPIRED`. A lost key acknowledgement returns `unsupported` with the
screen state unknown. `exit_observation(screen)` gives the completed turn or an
`exited` observation for an exited subject.

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
