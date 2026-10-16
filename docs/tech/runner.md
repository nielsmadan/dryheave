# Experiments and captured trials

The design decisions behind case workflows, inspections, fix-it replies and dialog
answering are in docs/decisions/0003-case-workflows.md.

The runner freezes an experiment, executes bounded interactive trials, stops its
known writers and captures evidence. A capture has `assessment: "pending"` and
`subject_completion: "unassessed"`. It contains no audit pass, quality score or
fabricated verifier result. Assessment resumes from that immutable capture.

## Public commands

Use the case and profile authoring commands to create the `task` and `subject`
aliases before this example. The simulator below is adaptive through literal
condition matching. Changing the observed question makes it stop on divergence.
The two fixture prompts must equal the frozen case prompt and simulated answer.

```json
{
  "name": "Greeting comparison",
  "cases": ["task"],
  "variants": [{"name": "base", "profile": "subject"}],
  "repetitions": 1,
  "seed": 17,
  "simulator": {
    "kind": "scripted",
    "script": [
      {
        "assistant": "Which punctuation?",
        "decision": {
          "action": "reply",
          "text": "Use a comma after Hello.",
          "fact_ids": ["punctuation"],
          "reason": "The approved punctuation fact answers the question."
        }
      },
      {
        "assistant": "Done.",
        "decision": {"action": "stop", "reason": "The subject reported its implementation."}
      }
    ]
  },
  "fixture": [
    {"prompt": "Fix the greeting; ask me which punctuation.", "assistant": "Which punctuation?"},
    {
      "prompt": "Use a comma after Hello.",
      "assistant": "Done.",
      "files": {"greet.py": "def greet(name):\n    return \"Hello, \" + name\n"}
    }
  ]
}
```

Save it as `.cache/experiment.json`, with the matching reviewed case and aliases:

```sh
uv run dryheave --store .dryheave experiment validate .cache/experiment.json --json
uv run dryheave --store .dryheave experiment create .cache/experiment.json --name greeting --json
uv run dryheave --store .dryheave experiment inspect greeting --json
uv run dryheave --store .dryheave run greeting --mode offline-fixture --json
uv run dryheave --store .dryheave run --status RUN_ID --json
uv run dryheave --store .dryheave capture CAPTURE_ID --json
uv run dryheave --store .dryheave run --resume RUN_ID --json
uv run dryheave --store .dryheave run --resume RUN_ID --retry TRIAL_ID --json
```

`offline-fixture` is explicit and frozen in the run options. It exercises the
same `DriverSession`, journal and capture path, with a deterministic terminal and
native-event fixture. It never invokes the profile executable. Fixture results
remain distinguishable from native runs. Fixture writes are limited to the
owned workspace and cannot name Git metadata. Empty scripts stop when exhausted.

`validate` is read-only. `create` resolves aliases once and publishes immutable
simulator, scoring and experiment objects. Optional variant `model`, `effort`
and `workflow` fields derive a new profile from its pinned parent. No-op overrides
are errors. Cases, variant names and repetitions define at most 10,000 trials.
SHA-256 ordering of the frozen seed and trial identity is deterministic; paired
variants share a case/repetition seed. Moving an alias cannot change saved trials.
Validation, creation and frozen experiment loading reject any scripted judge recipe.
Cases with required judge rubrics need a JSON-command, Codex or Claude judge with
`budget.max_calls` greater than zero before execution can begin. Optional rubrics
can remain without a judge.

## Native execution

```sh
uv run dryheave --store .dryheave run greeting --mode native \
  --tui-test .tools/tui-test/tui-test --runtime-root .cache/rt --json
```

The native run honors the materialized `LaunchPlan` and uses the real pinned
`tui-test` transport. The CLI resolves new-run transport from an explicit option,
workspace config, then PATH; omitted resume options never resolve new defaults.
It requires a configured or explicit short runtime root: each generated
10-character attempt child must keep the absolute `TUI_TEST_HOME` at 70 bytes or
less. The runtime root is recorded and never silently relocated. Keep the runner
process alive until it returns; its terminal controller is persistent. Every
attempt gets a fresh repository, profile directory and terminal runtime.

Profile fidelity issues remain visible. `--strict` refuses unresolved profile
observations, as described in profiles.md. HOME retains the profile's explicit
policy. Auth binding uses only the profile's opaque runtime references. This is
repository-object isolation, with partial native process/access visibility.

Codex profiles may explicitly select `workspace_trust: "trusted"` or
`"untrusted"`. The default `"prompt"` preserves native onboarding. The selected
setting creates a `projects."ABSOLUTE_OWNED_WORKSPACE".trust_level` command-line
override and appears in `LaunchPlan.workspace_trust`. It does not copy a trust
store or change permission/sandbox settings. Only Codex supports these explicit
choices; other harnesses reject them. Permission, trust and other recognized
blockers stop as `needs_input`; the runner never asks the simulator to approve them.
Claude selection dialogs and question-tool screens the parsers cannot read are
answered as described below.

## Controller protocol and budgets

A `simulator` can instead contain:

```json
{
  "kind": "json-command",
  "isolation": "trusted-native",
  "command": {"argv": ["/absolute/path/to/python", "/absolute/path/to/controller.py"]},
  "budget": {"max_calls": 3, "call_seconds": 30, "total_seconds": 90,
             "max_input_bytes": 131072, "max_output_bytes": 65536}
}
```

The command runs in a new owned role directory and receives one `SimulatorInput`
JSON object on stdin. It emits one JSON object on stdout:

```json
{"decision":{"action":"stop","text":"","fact_ids":[],"reason":"No further facts are needed."},
 "usage":null,"observed_model":null,"observed_effort":null}
```

`usage`, when observed, follows `TokenUsage` with uncached input, cache read,
cache write, output, optional reasoning subset and a provenance string. Missing
usage and cost remain null, including interrupted/failed calls. Requested model
and effort live in the frozen recipe; observations remain separate. Unknown
fields, malformed JSON, unavailable fact IDs, control characters and leading
native `/`, `!` or `$` commands are rejected. `reply` needs nonempty text; `stop`
cannot carry subject input. The projection includes only approved facts, reviewed
persona strings and current root dialogue. It excludes source paths/evidence,
hidden files, reference solution and grading criteria. This structural boundary
does not establish semantic leak prevention or OS isolation.

For the verified Codex 0.153.4 exec controller, use `kind: "codex"`, an explicit
`command.argv` launcher prefix such as `["codex"]`, `version: "0.153.4"`, `model`
and `effort`, plus `isolation: "trusted-native"`. The adapter verifies its version
and adds `exec --ephemeral --ignore-user-config --ignore-rules
--skip-git-repo-check --json --color never --output-schema PATH
--output-last-message PATH --model MODEL --config model_reasoning_effort=... -`.
The schema describes `SimulatorDecision`; the final JSON is read from the bounded
output file. Stdout retains native JSONL, including cumulative thread usage.
Codex's all-zero usage fallback remains unknown. This controller can still have
native tools and host access. It preserves explicit launcher/auth/environment
references and adds no permission bypass flags. `CODEX_HOME` can be an explicit
controller command environment reference; otherwise native HOME auth discovery
is left to the selected executable. Credentials are never inspected or logged.

Codex 0.154.0 requires `runtime.discovery: "codex-clean-0.154.0"`. Its owned
CODEX_HOME and TMPDIR are separate from evidence; plugins/startup sync and bundled
skills are disabled. Optional opaque auth.json links require the frozen
source-write acknowledgement. The adapter verifies the exact executable version.
New calls retain cumulative usage even on invalid output or process failure;
model/effort are not inferred from the prompt. Historical 0.153.4 serialization
and launch behavior are preserved.

Claude uses `kind: "claude"`, `version: "2.1.278"`, one executable in
`command.argv`, an explicit model and
`runtime: {"discovery":"claude-tool-free-2.1.278","home_policy":"native"}`.
Its only command environment reference is `CLAUDE_CODE_OAUTH_TOKEN`; it has no
Codex sandbox/auth-file fields. The adapter verifies `2.1.278 (Claude Code)` and
adds print/JSON/schema/model flags, `--tools ''`, empty setting sources,
strict empty MCP configuration, disabled hooks/plugins and session persistence.
Haiku omits effort; supported Sonnet/Opus settings may specify it. Native subjects
remain TUI and receive none of these simulator-only print flags.

The Claude result envelope must be a successful `type: "result"` with a valid
`structured_output`; prose `result` is never treated as a substitute. Unknown
envelope fields, native permission denials and reported server tool use fail the
call. Provider usage is retained independently of decision validity and exit
status. Only a single measured modelUsage identity becomes observed_model;
observed_effort stays unknown. A judge uses the same transport and lifecycle but
validates judgments rather than simulator decisions. Model output cannot
self-report usage or effective settings through the structured decision payload.
New protocol fixtures are explicitly synthetic, not live compatibility evidence.

Each modern controller owns private config/tmp/work directories outside evidence.
Durable binding identities support recovery, and links are removed only after
known writers stop; replaced links/files remain preserved for review. Native HOME,
managed sources and host/network access are recorded limitations, not isolation
guarantees. Only Claude's bounded tool policy is described as tool-disabled;
Codex remains trusted-native.

`conversation_policy: "facts-only"` permits factual clarification;
`"design-approval"` additionally permits ordinary in-scope conversational design
approval. Persona style and dialogue never grant authority for native permissions,
authentication, trust, new scope or spending. Old recipes omit this additive field
and retain their historical projection.

Call time includes native controller startup. Commands, pipes, schema/output
files, role-directory size/count/depth and known process cleanup are bounded.
A native role runtime may hold up to 4096 entries and 64 MiB beyond the call's
byte budget (its own Claude or Codex state, such as Claude's plugin directory
cache); other controller directories up to 256 entries. A call stopped by one of these limits records
the limit on that call; the attempt's merged cleanup keeps only errors that leave
cleanup unresolved. A terminal close is retried once. A close whose
acknowledgement is lost still counts if the daemon exits by itself within the cleanup
bound; a daemon that has to be killed, or whose identity no longer matches the owned
launch, records `terminal_close_failed`.
The subject's overall deadline covers all simulator time, with independent role
call/count/total budgets. A parent deadline can end a call before its own budget.
SIGINT and SIGTERM request cancellation and cleanup. No new subject input follows
a stop, cancellation, budget exhaustion or unresolved delivery.

`scoring` freezes `policy: "required-criteria-v1"`, optional `judge` controller
recipe, and an optional version/date/currency price table with per-million-token
rates. A rate may carry a `long_prompt` tier that applies to a single request whose
total input (uncached input, cache reads and cache writes) exceeds
`above_total_input_tokens`, and the charge's reason says when it applied. A record
not known to cover a single request of such a model, including every simulator,
judge and inspector call, stays unpriced. These are saved
assessment inputs, interpreted by explicit `assess` or
`run --assess`, and are not current market pricing. The convenience flag rejects
status-only requests, incomplete capture and unresolved cleanup. It preserves
the run ID and evidence on failure or cancellation; normal `run` stays
capture-only. See [results.md](results.md).

## Case workflows

`RunOptions.workflow` (default false, dropped from the frozen options when false)
turns on the case's workflow; see authoring.md for steps and the default inspect
step. `run --workflow` sets it for a plain `run`; `bench` sets it by default and
`--no-workflow` turns it off, so the launch check also skips probing slash steps
(bench.py). The setting is not part of the experiment ID, so `bench` resumes or
reports only an earlier run of the same experiment whose frozen
`RunOptions.workflow` matches this invocation. `bench --inspector MODEL[@EFFORT]` sets `ScoringConfig.inspector`;
without it, inspection falls back to the judge. A run with workflows on is
refused before launch when a case has an inspect
step but the scoring config has no inspector or judge with `max_calls` above zero,
or when a slash prompt step targets an agent other than Claude. With workflows off,
or a case with no effective workflow, the loop below never runs.

A decision may carry `stop_kind` (`"done"`, the default, or `"declined"` when the
simulated user will not approve what was asked) and `choices` (dialog answers). Both
are dropped from serialization at their defaults, so existing
scripts and recorded calls keep their bytes. A simulator `stop` with `stop_kind:
"done"` is a done point; script exhaustion and script divergence are not — they
carry `stop_kind: "declined"` and end the run with `stop_reason: "simulator_stop"`
as today. At a done point the runner asks `next_workflow_action` what to do:

- `deliver_prompt`: send the prompt step verbatim; it advances once the subject
  accepts it. Prompts starting with `/` use the `curated-native-command` submission policy; the rest use `conversation`.
- `inspect`: inspect the live workspace against the step's criteria (inspection.py).
  A pass advances and the loop continues at the same done point. A failure stays on
  the step and asks the simulator for a fix-it reply: `SimulatorInput.inspection`
  carries the findings and the instruction replaces "stop when the work is done"
  with the user-voice fix-it rules. A `stop` returned with findings is recorded as a
  failed call with error `stop_with_findings` and retried once with
  `SimulatorInput.correction` saying why the stop was rejected. A second such stop
  does not end the attempt: the runner sends a fixed, neutral fix-it reply
  (`FALLBACK_FIX_REPLY`, which quotes nothing from the inspection) and records it as a
  `simulator-fallback` journal event.
- `complete`: stop with `stop_reason: "workflow_complete"`.

A `declined` stop ends the run as `simulator_stop`. Native events are ingested before
and after each inspection and terminal-owned processes are published. If events
arrived meanwhile, the inspection reruns once and is then unavailable. The judge
transcript comes from the driver's native events so far, falling back to the
dialogue. An unavailable inspection stops with `inspection_unavailable`, unless the
run was cancelled (`cancelled`) or its deadline passed (`timeout`). Inspections per
attempt are capped at the case's `max_turns` (`turn_budget`); every other budget
keeps its existing stop reason and the progress made so far.

Simulator budgets, elapsed time and call indexes count only `role: "simulator"`
calls, so scripted fixtures stay aligned. Inspection judge calls join the attempt's
calls as `role: "judge"`, with their own per-inspection index (shared by an
`events_arrived` rerun, so one inspection stays within `max_calls`) and a deadline of
`min(subject deadline, now + call_seconds)`; assessment prices them with the
inspector (or judge) recipe under the judge role. Private inspection material lives
in a fresh directory under the store's `scratch/`. Recovery removes a kept
directory once dryheave's own judge and verifier cleanup records in it report every
writer stopped or every recorded writer has since exited; it never reads the copied
workspace. Retained judge and verifier artifacts (regular files and directories
only; links and special files are skipped) live under
`runs/RUN_ID/inspections/ATTEMPT_ID/`, outside the attempt tree, and are copied into
the capture's `evidence/inspection/` once the subject has stopped.

Journal events: `workflow-step` (one `WorkflowEvent` and the position after it),
`inspection-intent` and `inspection-result`. They never reuse assessment event
names, so grading after an inspected run still invokes its own judge.
`AttemptState.workflow_position` (dropped when None) is the only workflow field in
the attempt state. Inspect events record `passed`, `failed`, `reused` (unchanged
snapshot, previous failed verdict, no call) or `unavailable`, with the judge call
IDs and inspection wall time; each fix-it reply adds a `fix_requested` event naming
its simulator calls. The capture's `workflow` (`WorkflowProgress`: step IDs,
position, events) is rebuilt from `workflow-step` events at capture, including when
recovery captures an interrupted attempt. Interrupted attempts are never resumed
mid-interaction.

### Selection dialogs

When a Claude turn shows a question dialog (`structured_input`), the runner asks the
simulator to answer it whether or not workflows are on. `SimulatorInput.dialog`
carries the parsed question, option labels, `multi_select` and `allows_text`;
`answer_hint` is the hint of the prompt step being delivered or, failing that, of
the most recent prompt step before the current position. The dialogue ends with the
turn's user prompt. The dialog instruction asks for `choices` with exact option
labels (one for single select, one or more for multi-select), or free single-line
`text` with no choices when `allows_text` is set and nothing fits. A reply may then
carry choices without text. A decision that does not fit the dialog is recorded as
a failed call with error `invalid_dialog_answer` and retried once; a second failure
stops with `needs_input`. A plain reply with empty text is likewise a failed
`empty_reply` call. The driver applies a valid answer and the same turn continues;
a dialog it cannot parse or cannot verify on screen stops with `needs_input`.

Each verified answer journals a `workflow-step` event with kind `dialog`, outcome
`answered`, the accepted turn count and its simulator call IDs. Its `step_id` is the
hint's prompt step, or `dialog` when there is none. Recording does not move
`workflow_position`. Without a workflow the capture keeps `workflow` None unless a
dialog was answered; then it holds a `WorkflowProgress` with no steps, position 0 and
the dialog events.

### Screen answers

When a turn stops on a Claude AskUserQuestion or Codex `request_user_input`
screen that the driver cannot parse (`DriverSession.screen()` is not `None`; the
allow-list is in drivers.md), the runner enters a screen loop instead of stopping.
Any other unrecognised screen, and every Droid screen, stops with `needs_input`.
Each step sends one simulator
request with `SimulatorInput.screen` (the raw screen, trailing whitespace trimmed;
when the request exceeds `max_input_bytes` only the bottom lines that fit are
kept), the persona, the dialogue so far and the same `answer_hint` as a selection
dialog, under `SCREEN_INSTRUCTION`. Only these requests carry the screen fields in
the Codex/Claude output schema; other requests keep their schema and bytes. The
decision returns `screen_kind` (`question`, `approval` or `busy`), for a question
exactly one `screen_action` (`{"key": ...}` or `{"text": ...}`, no control
characters, no leading `/`, `!` or `$`, at most 2000 characters) and `screen_target`
(the option or answer it is working toward). A question is a `reply` with empty
text and choices; `approval` and `busy` are a `stop` without an action. Text whose
first non-space character is a digit is rejected, because Claude types text as
keystrokes and a digit there selects an option. Anything else is a failed
`invalid_screen_answer` call, retried once, then `needs_input`. The three fields
are dropped from serialization when absent.

Mode and permission prompts are kept from the simulator by the allow-list: such a
prompt does not carry the question-tool chrome, so it never enters the loop. Two
backstops sit behind it. The normalised `permission_like` wording check rejects a
qualifying screen that also shows permission or mode wording. The option guard
refuses any `Enter`, `Space` or digit whose chosen option or target names a mode
or permission choice from its fixed vocabulary, whatever the simulator judged; it
does not recognise every such option (one-shot "Yes"/"Allow" rows or session
grants, for example). A refusal stops with `needs_input` and an `approval`
observation (`SCREEN_ACTION_REFUSED`).

The driver applies one action per call (`act`) to the screen the simulator saw;
if the screen changed while the simulator was deciding, nothing is sent and the
loop asks again with the new screen. `busy` waits for the screen to change or the
turn to end (`await_change`), bounded by `SCREEN_WAIT_SECONDS` (60 s) in total per
screen; on expiry the loop stops with `needs_input` (`SCREEN_WAIT_EXPIRED`).
`approval` stops with `needs_input` and an `approval` observation; no key is sent.
A subject that has exited stops as `exited` before any request or key. The loop
ends when the screen no longer qualifies (the composer never does): the turn then
continues as usual, or a parsed question takes the selection-dialog path.

Screens are told apart by `screen_identity`, the normalised lines other than
option, marker and typed-answer rows, so moving the highlight or typing keeps a
screen while a new question starts another. Each screen gets `SCREEN_ACTIONS` (8)
actions and `SCREEN_WAITS` (4) busy waits, and the whole loop at most
`SCREEN_STEPS` (32) steps; exceeding either stops with `needs_input`. Each step
journals a `screen-step` event (call ID, kind, action, target, resulting state and
reason). A screen left after at least one sent key records its own `dialog`
`answered` workflow event with that screen's simulator call IDs, like a selection
dialog, as soon as the next screen appears, even if that one is then judged an
approval; leaving through cancellation, timeout, subject exit or a lost key
acknowledgement records none, and a lost acknowledgement stops with `needs_input`.
Help-needed never counts screen decisions as conversational replies, answered or
not. Calls count against the simulator budget as usual. Screens that appear before
Enter was sent, or at startup, still stop as before.

## Durable recovery and assessment integration

`run_experiment`, `run_status` and `load_capture` in `runner.py` are reusable
services. `experiments.py` supplies validate/create/load/expand services. Runner
state is in `runner_models.py`, and `ExecutionJournal` in `runner_state.py` owns
transitions through reserved → preparing → launching → interacting → stopping →
captured. It always replays durable events, including events ahead of a checkpoint.
A stable run lock is acquired before the store native lock. Both remain held
through execution, cleanup and capture.

Before execution, the native-lock holder reconciles recorded ownership in every
run in the store. Other run locks are acquired nonblocking; contention refuses
the operation instead of waiting with the native lock held. Status remains a
read-only journal view. Recovery uses only durable run/process identities before
loading benchmark dependencies, so corrupt frozen inputs cannot prevent recorded
writers from being stopped. Dependency failures remain journaled and block new
subject calls; stopped invalid attempts can be assessed as ineligible.

A launch intent saves the attempt, workspace/runtime/session and exact LaunchPlan
before subject launch. Each submission saves its driver ID, prompt hash and
pre-submit cursor before typing. Call intents precede external controller calls.
The driver reconciles ambiguous Enter delivery against native acceptance without
resending. Accepted turns and delivery attempts are distinct observations.

Resume preserves captured attempts and marks active interrupted attempts. It
stops only recorded PID/creation-time identities, drains retained native logs and
captures the remaining evidence. It does not relaunch an interrupted subject or
repeat an ambiguous controller call. Recovery durations may be unknown and its
interaction coverage is explicitly partial. A terminal launch whose ownership
was never observed remains an unresolved cleanup failure; automatic continuation
is refused. Explicit `--retry TRIAL_ID` creates another retained attempt and never
erases the prior attempt's possible spending. Unstarted trials can continue after
reconciliation. Resume pins execution options and rejects a conflicting experiment.

Fresh `ownership-reconciled` events determine whether later execution is safe.
They preserve historical capture cleanup failures and assessment exclusions; they
do not rewrite an earlier capture into eligible evidence. A newly recorded setup,
subject or controller/grader process invalidates earlier cleanup success. Setup
commands publish discovered descendants in batches during polling (at most one
batch per second) and cleanup, so observed children remain recoverable after
their direct parent exits; see storage.md for the ownership event format. Unknown launch
ownership and newly unresolved writers still block continuation.

The `capture` object kind holds `CapturedAttempt` and exact blob maps:

- `workspace/` files, symlink targets and executable state; directories are explicit.
- `final.patch`, relative to the historical baseline, including untracked files.
- `git/` raw Git metadata and `git-inventory.txt`, separate from reconstructed code.
- `evidence/` driver/native events, transport recordings, setup and role-call artifacts.

Capture rejects special files before opening, never follows symlinks, bounds
files/bytes/depth and records omissions. Frozen exclusions and unselected ignored
files are intentional omissions. Escapes, unsupported content, byte limits and
capture failures make completeness false. Empty directories are retained. Relative
symlinks that resolve to the workspace root, such as `.` or `sub/..`, retain their
exact target bytes and count as complete; escaping or cyclic links remain omissions.
Independent patch generation uses a disposable repository without subject Git
configuration, hooks or filters. External alternates are retained as evidence
but never followed. A successful final turn, stopped writers, complete files and
later assessment eligibility are separate observations.

Assessment services can obtain `run_status(...).pending_assessment`, load each capture and
restore its files with `final_capture.materialize_files(destination,
capture.workspace.files, blobs, directories=capture.workspace.directories)`.
Supply `store.read_blobs(capture_id)` bytes in `blobs` keyed by blob path, and use
a fresh destination. The batch verifies the complete closure once and hashes each
returned blob, avoiding repeated closure traversal per captured file. `load_capture` validates the domain
identities, hashes, sizes and exact file/reference maps. For tampered input audit,
retain validation failures as evidence rather than declaring the capture valid.

After auditing/grading, the assessment service opens `RunStore.open(run_id)`, constructs
`ExecutionJournal(journal)` and saves the selected `AttemptState` through audited,
grading and finished. `capture_id` is immutable across these transitions;
`assessment_id` names the retained result object. Its service must validate its own
assessment objects before saving the result ID. `run --resume` skips all those
stages, and pending assessment excludes finished attempts. These transitions
require no subject or simulator call. The journal remains the source of truth;
checkpoint/result files are derived snapshots.
