# Experiments and captured trials

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
Cases with required judge rubrics need a JSON-command or Codex judge with
`budget.max_calls` greater than zero before execution can begin. Optional rubrics
can remain without a judge.

## Native execution

```sh
uv run dryheave --store .dryheave run greeting --mode native \
  --tui-test .tools/tui-test/tui-test --runtime-root .cache/rt --json
```

The native run honors the materialized `LaunchPlan` and uses the real pinned
`tui-test` transport. It requires an explicit short runtime root: each generated
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
choices; other harnesses reject them. Permission, structured-input and unsupported
dialogs stop as `needs_input`; the runner never asks the simulator to approve them.

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

Call time includes native controller startup. Commands, pipes, schema/output
files, role-directory size/count/depth and known process cleanup are bounded.
The subject's overall deadline covers all simulator time, with independent role
call/count/total budgets. A parent deadline can end a call before its own budget.
SIGINT and SIGTERM request cancellation and cleanup. No new subject input follows
a stop, cancellation, budget exhaustion or unresolved delivery.

`scoring` freezes `policy: "required-criteria-v1"`, optional `judge` controller
recipe, and an optional version/date/currency price table with per-million-token
rates. These are saved assessment inputs, interpreted only by the explicit `assess`
command, and are not current market pricing. See [results.md](results.md).

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
commands publish discovered descendants during polling and cleanup, so observed
children remain recoverable after their direct parent exits. Unknown launch
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
