# Collection, authoring and repository contracts

For the selected-log workflow, see [the user guide](../user/selected-log-authoring.md).
`authoring_catalog.py` owns a strict version-1 bounded catalog at the initialized
workspace's authoring directory (`.dryheave/authoring`, catalog state only;
authored voices, cases and reports live in the visible `voices/`, `cases/` and
`reports/` folders, see [workspace setup](../user/workspaces.md)). Directory flock serializes writers; every write
checks the expected catalog revision and atomically publishes the next one.
Malformed, stale or symlinked content is preserved as an error. Selection membership
pins immutable session IDs and cannot be replaced. Requests retain policy, typed
candidate decisions, coverage gaps and immutable case references. It is authoring
metadata, not a runner database; runs and object storage retain their own contracts.

`mining.py` verifies candidate evidence and case provenance inside the selected
user-start/inclusive-end range. Explicit baseline and historical dirty-state
rationales accompany each decision. New selected-log voices require nonempty exact
user-role excerpts, safe policies and a curator safety review. Legacy Personas
remain compatible. Source session membership is curator provenance, not a new
runtime dependency for portable cases/personas.

Voice names default to `default`, which `resolve_voice_name` hands out only while
it is free; afterwards an explicit name is required. `voice_evidence` classifies
every user-role event in the selection by stripping the harness wrapper shapes the
curation flow already recognizes — `user_instructions`/AGENTS.md instruction
blocks, `environment_context`, `skill`, command wrappers and system reminders.
An event whose remainder is conversational is genuine; one that is entirely
wrapper is harness-injected with its reasons recorded; an empty remainder or an
unrecognized markup block is unclassified rather than guessed. The resulting
`VoiceEvidence` counts every class, lists bounded genuine candidates with lengths
and previews, breaks the same counts down per selected session in `sessions`, and
states sufficiency against `VOICE_EVIDENCE_THRESHOLD` genuine messages.

`classified_user_events` yields each user-role event with its classification, its
wrapper reasons and its conversational remainder; `user_message_profile` folds one
session's events into a `UserMessageProfile` of the three counts plus
`genuine_characters` and the lower-median `median_genuine_characters` of the
genuine remainders. `voice_evidence` accumulates the same profile per session and
publishes it as a `SessionEvidence` carrying its `session_id`, so the aggregate
counts are sums of the per-session measurements rather than a separate tally. The
per-session tuple stays optional on `VoiceEvidence` so version-1 voice records
written before it still parse; when present, its counts must total the aggregate
ones and name each session once. A `UserMessageProfile` cannot report characters
without a genuine message, and its median cannot exceed its total.

`create_voice` recomputes the evidence from the selection instead of trusting the
draft and stores it on the immutable voice record, so counts are measurements the
catalog can contradict rather than model prose. It recomputes `triage_evidence`
the same way and stores it beside the evidence, so a voice carries its sampling
frame as well as its surviving excerpts.

`delete_voice` removes one catalog voice entry under the same revision discipline
as every other mutation and returns the removed record. An unknown name is an
`InputError`; a stale revision is the usual `ConflictError` from `edit_catalog`.
Nothing else changes: the frozen persona object is immutable and stays in the
store, so cases holding its `persona_id` still load and experiments that froze it
keep their behavior. Deletion frees the name for reuse, which is why the response
returns the retained `persona_id` — a name no longer identifies one persona across
time, an object ID still does. Voice records are catalog metadata; immutable
objects are never deleted by this command.

## Commit survey and the parent join

`commits.py` surveys a read-only repository and joins its commits back to the sessions
that produced them; `commits_cli.py` registers `collect survey` and `collect commit`
under the existing `collect` group, so no domain service registers itself. The join key
is one recorded fact, not an inference: a Codex `session_meta` carries
`git.commit_hash`, the repository state when the session started, so for a commit `C`
produced during that session, `C`'s parent equals the session's recorded baseline.
`logs.base.recorded_cwd` and `logs.base.recorded_baseline` read those two values and
return `None` when the log holds neither; `mining.session_repository` now delegates to
`recorded_cwd` so triage and the survey read one definition. A Claude log records no
commit hash, so its baseline is always absent rather than guessed. `collect scan`
reports both values per scanned session, which is what makes a commit matchable without
a second parse.

Git access is read-only and bounded. The survey reuses `repositories.Git`, so it
inherits the hardened environment (no system/global config, no hooks, no replace refs,
`GIT_OPTIONAL_LOCKS=0`) and the `SnapshotLimits` time budget, and it issues only
`rev-parse` and `log`. One `git log --numstat` call with an `\x1e`/`\x1f` record format
yields subjects, bodies, parents and per-commit file and line counts together, so
history costs one process. `--max-commits` bounds it to 1-2,000 commits from HEAD
(default 200) and `--max-candidates` to 1-50 reported candidates (default 10); both
bounds are echoed in the response and exceeded bounds raise `LimitError`.

Tier 1 derives the vocabulary instead of assuming `feat|fix|chore`. `_prefix_token`
takes the token before a `:` delimiter, allowing a conventional-commit scope and `!`,
lowercases it and folds digit runs to `#`, so `TICKET-123:` and `TICKET-124:` are one
`ticket-#` member rather than two singletons. `_convention` then requires three things
together: at least 20 sampled non-merge commits, at least 60% of subjects carrying such
a token, and the smallest token set covering at least 90% of the prefixed subjects being
at most 12 tokens. The share threshold is deliberately not near 100%: real histories
that keep a convention still contain reverts, initial commits and imports, so demanding
near-total compliance would report "no convention" for repositories that plainly have
one, while 60% cannot be reached by accidental colons because a token may not contain
spaces. The 90%-in-12-tokens test is what separates a classifying vocabulary from free
text: a maintained prefix set is small and closed, so if a long tail is needed to cover
the prefixed subjects, the token is a subject fragment and no convention is declared.
`ConventionReport` publishes the measurements, the thresholds and a `reason` naming the
one that decided the outcome, so the inference can be contradicted rather than trusted.

`_select` applies the tiers in order and stops at the first that yields candidates:
bug-indicating members of the detected vocabulary, then bug-indicating words anywhere in
subject or body, then change shape alone. A detected convention with no bug-indicating
member therefore falls through to tier 2 rather than returning nothing. Every candidate
carries its `tier` and a `signal` naming what selected it, and the tier 3 note and
signal both state that ranking by size is no evidence of a bug fix, because that tier
exists to offer something to read, not to claim a finding.

Merge commits are excluded: a merge records no single starting state, so the parent join
has no defined key, and `collect commit` refuses one outright. Commits touching only
documentation or only configuration are excluded as well. That is a deliberate decision,
not an oversight: a benchmark case needs a behavior change a hidden verifier
can execute and a calibration can show failing at the baseline, and a commit
that only edits Markdown or a settings file leaves nothing to verify. Exclusions are
counted and listed with their reason in `excluded`, so a documentation-heavy window
reads as excluded work rather than as an empty repository. Classification is by path
suffix, filename and documentation directory, and any path outside those classes makes
the whole commit code.

The join reports rather than asserts. `_matches` returns `unique`, `ambiguous`, `none`,
`no_parent` or `not_scanned` with a note explaining it, and each match carries the
session's recorded `cwd`, its `source_id` and `parent_session_id` as the log states
them, and a lexical `repository_match` of `same`, `inside`, `different` or `unknown`.
Ambiguity is common and is reported rather than resolved: against a real Codex corpus
one commit's parent matched three rollout files that were one user session and its two
subagent threads. Those subagent files repeat the parent thread's `session_meta` last,
so the existing parser surfaces the parent identity for all three and records its
`ambiguous_session` warning; the join reports three matches and says the commit cannot
be attributed to one, instead of picking the newest. The cwd is never a filter: the same repository is routinely
checked out in several directories, and the verified splashdown case joins a `dev1`
survey to sessions recorded in `dev2`. Even `unique` is stated as partial, because a
session records only its starting baseline, so the join identifies that session's first
task and not the later commits of the same session. Log scanning tolerates individual
logs that exceed the import bounds or fail to parse: they are skipped, counted in
`sessions_skipped` and reported with bounded reasons instead of failing a whole corpus
scan. `collect commit` accepts only an explicit lowercase hexadecimal SHA of at least
seven characters, so no branch name or `HEAD` can enter, and it hands the resolved
parent to the unchanged `collect select` / `problem request` pipeline. That parent is
recorded evidence, not a verified baseline: the existing rule that a case's starting SHA
is verified against the read-only source repository, and that today's HEAD is never a
substitute, is unchanged by this path.

## Session triage as voice provenance

A voice used to record only the excerpts that survived, which made sampling bias
invisible: three bugfix sessions from one repository looked like a balanced base.
Triage records the frame. `TriageDecision` carries `session_id`, `kind`, an
optional `grade`, `chosen` and a `reason`; its `category` property collapses the
pair into one of the eight vocabulary labels (`config_change`, `feature_small`,
`feature_medium`, `feature_large`, `bugfix_easy`, `bugfix_medium`, `bugfix_hard`,
`extraneous`). Kind plus graded scale is the persisted shape rather than one flat
enum of eight, because the size/difficulty axis belongs to the kind: a validator
requires a grade exactly when the kind has a scale and rejects a grade from the
other kind's scale, so the eight valid pairs are exactly the vocabulary and no
invalid pair can be written. `category` keeps the flat label available for
distributions without parsing names back apart. `mining.triage_decision` raises the
same rules as an `InputError` before the model sees them, because the CLI
validation message deliberately omits model-validator text.

`SelectionTriage` holds one decision per session, keyed by session ID under the
selection's name, so the catalog's existing key-matches-name and known-selection
checks cover it. Triage attaches to a selection because a selection is the only
thing that pins an immutable examined membership; `record_triage` refuses a session
outside it and replaces one decision at a checked revision while preserving the
rest. Selection membership stays immutable: triage records a judgement about the
sessions, never a change to them.

`triage_evidence` folds the recorded decisions against the selection into a
`TriageEvidence`: `entries` in selection order, each with the session's
`repository` — its recorded `cwd`, the same metadata `collect metadata --key cwd`
pages, with no resolution to a repository root — the `untriaged` remainder, and a
`TriageVariety`. Variety is computed over the **chosen** sessions only: the `kinds`
distribution, the `repositories` spread with `unknown_repositories` for chosen
sessions whose log recorded no `cwd`, and a `varied` flag that is true only when
the chosen sessions span more than one kind *and* more than one repository, since
either axis alone still describes one slice of work. Both models check their own
arithmetic the way `VoiceEvidence` does: the counts must total the selection, the
distributions must total the chosen sessions, `varied` must follow the spread, and
each session may appear once. `TriageEvidence` stays optional on `VoiceRecord`, so
version-1 records written before triage still parse.

Sampling warnings are CLI presentation, next to the existing below-threshold one:
`mining_cli._sampling_warnings` reports untriaged sessions, a triaged set with
nothing chosen, and a chosen set that is not varied. `mining_cli._merged_warnings`
appends them to the below-threshold evidence warning, so every response carries
one `warnings` list of plain strings in a fixed order: evidence warnings first,
then sampling ones. `collect selection`, `collect triage` and `voice inspect`
compute no evidence warning, so their list holds sampling ones only. Neither
`voice create` nor `collect triage` refuses for thin or lopsided sampling; a
curator may proceed knowingly, and the persisted triage keeps the frame readable
afterwards.

`collect scan` reports the same `UserMessageProfile` per discovered log under
`user_messages`, computed from the session it already parsed, and orders the
scanned sessions by descending genuine count and then descending genuine
characters. Classification adds regex passes over user-event text only, inside
the existing `ImportLimits` bounds, so a scan still reads each file once and
gains no new filesystem work. No composite score is published: the curator reads
the measured components and chooses. A selection normally spans several sessions;
the per-session evidence exists so thin coverage is visible instead of averaged
away.

`collect metadata SELECTION --session ID` reads bounded serialized session metadata
with selected-membership checks. `--key` targets a top-level value such as `cwd` or
`git`; a missing key is an error. Character offsets, total length, next offset and
truncation are explicit, with a 4,000-character default and 8,000-character maximum.
Codex `session_meta` contributes to `Session.metadata`, separately from events.
The legacy `collect show` still returns the full session unchanged.

Problem draft paths inside the authoring root are stored lexically relative to
it; outside drafts keep absolute paths. Reads retain no-follow checks for every
directory component and the final file, and reject parent traversal. No path
normalization resolves away symlinks before those checks.

Problem validation references bind request revision, curated decision, canonical
draft content and hashes of every hidden verifier and reference patch. Temporary
owned copies provide the exact bytes used by existing case validation/freezing.
Request edits clear prior validations; freeze detects draft or file changes and
refuses stale references. The catalog records a frozen case ID only after success.
Frozen decisions cannot be replaced. Import/freeze may leave harmless immutable
objects after a later catalog publication error; the catalog never claims them
as completed work before its atomic publication succeeds.

The shipped authoring commands use the same global `--store PATH` and `--json`
options as the storage commands. Development examples below use `.dryheave`,
which is ignored. Draft files are editable JSON; creation refuses to overwrite an
existing file. `validate` and `freeze` return nonzero for unresolved inputs.
In a workspace, freezing a draft at `cases/NAME/case.json` (by `case freeze` or
`problem freeze`) then sets store alias `NAME` to the new case ID with
`ObjectStore.set_alias(..., replace=True)` and reports `alias`; a failed alias write
is reported as `alias_error` and leaves the frozen case in place. Other drafts set
no alias.

```sh
uv run dryheave --store .dryheave collect scan --agent codex --root LOG_DIRECTORY --json
uv run dryheave --store .dryheave collect import LOG_PATH --agent codex --json
uv run dryheave --store .dryheave collect show SESSION_ID --json
uv run dryheave --store .dryheave case draft SESSION_ID --repo SOURCE_REPO --commit FULL_SHA --out .cache/case.json
uv run dryheave --store .dryheave case validate .cache/case.json
uv run dryheave --store .dryheave case freeze .cache/case.json --json
uv run dryheave --store .dryheave case inspect CASE_ID --json
uv run dryheave --store .dryheave persona draft SESSION_ID --out .cache/persona.json
uv run dryheave --store .dryheave persona create .cache/persona.json --json
uv run dryheave --store .dryheave persona inspect PERSONA_ID --json
```

`case draft` accepts `--start EVENT_ID` and `--end EVENT_ID` for an inclusive
message range. Start must contain user text. Omit both repository arguments to
create a visibly unresolved draft; supply both to capture an explicit baseline.
`--initial-patch PATH` freezes selected uncommitted starting work. Current source
working-tree edits are never inferred as the initial patch. Draft suggestions
identify signals such as user messages, tool activity and assistant questions;
the curator still determines intent, task boundaries and grading.

## Authoring and visibility

The draft needs `intent_confirmed: true`, `facts_reviewed: true`, at least one
declared phase, at least one requirement fact, a requirement fact in every phase,
and an empty `unresolved_issues` list after those issues are resolved. These rules
run in `case validate`, `case freeze` and `FrozenCase`; a draft with no phases yet
still parses. Facts contain stable `fact_id`, `kind`, `phase`, `text`, `check`,
`disclosure` (`on_request` or `proactive`), an optional `verifier` and source
evidence, or the explicit `curator_authored: true` declaration. See
[Facts, phases, checks and verifiers](#facts-phases-checks-and-verifiers). A persona
needs instructions, disclosure and unknown-answer policies and
`reviewed_subject_safe: true`. Persona examples and fact evidence must be classified
`subject` after reviewing for solution text.

A draft selects exactly one inline `persona` or immutable `persona_id`. Standalone
`persona create` freezes a curated JSON file; `persona draft` makes an editable
starting point. Manually authored personas may omit examples. Any examples that
are present must be verified against imported source events at freeze time.

Every `EvidenceExcerpt` retains `session_id`, `event_id`, `visibility` and an exact
nonempty substring `excerpt` from the source event text. Case evidence belongs in
the selected source range. Source session IDs are provenance, not reference-closure
dependencies. Frozen cases reference only their repository and persona objects;
personas contain reviewed excerpts with no full transcript dependencies. Normal
portable copies can replay and validate these three objects without source logs.
Imported session objects remain potentially sensitive curator evidence.

For replay, call `cases.load_frozen_case(store, reference)`,
`personas.load_frozen_persona(store, reference)` and
`repositories.load_repository(store, reference)`. These validate domain invariants,
manifest blob maps and reference closure without consulting original sessions.
`FrozenCase` itself rejects unreviewed facts, unresolved intent, undeclared phases,
phases without a requirement and duplicate fact IDs. Generic `ObjectStore.get` checks storage
integrity; callers must still use these domain validators for runtime inputs.

`cases.subject_context(case)` returns only initial prompt and each fact's ID, text and
disclosure; `check` and `verifier` never leave curator, inspector and judge code.
`personas.subject_persona(persona)` returns only communication policies and
reviewed example strings. The full case, snapshot provenance, fact checks, rubrics,
reference patch and store paths belong to curator/judge code. These projections
are intentional visibility boundaries; they do not prove semantic non-leakage.

## Facts, phases, checks and verifiers

A case declares `phases`, an ordered tuple of names, and `allowed_facts`, the
requirements the user knew they wanted:

```json
{
  "phases": ["fix"],
  "allowed_facts": [
    {
      "fact_id": "empty-name",
      "kind": "requirement",
      "phase": "fix",
      "text": "An empty name should still greet with a trailing space.",
      "check": "Calling greet with an empty name returns 'Hello, ' and does not raise.",
      "disclosure": "on_request",
      "curator_authored": true
    },
    {
      "fact_id": "greeting-punctuation",
      "kind": "requirement",
      "phase": "fix",
      "text": "The greeting uses a comma after Hello.",
      "verifier": {
        "entrypoint": "check.py",
        "command": {
          "argv": ["python3", "{verifier}/check.py"],
          "timeout_seconds": 30,
          "max_output_bytes": 65536
        },
        "expected_stdout": "GREETING_CHECK_EXECUTED",
        "expected_failure_stdout": "GREETING_ASSERTION_FAILED"
      },
      "curator_authored": true
    }
  ]
}
```

`kind` is `requirement` (gates the outcome and the independence score), `optional`
(graded and raised in its phase, never gating) or `context` (background the user
user stand-in may share; no phase, check or verifier). Requirement and optional facts name
a declared phase and carry a `check`, a `verifier`, or both. `text` is what the user
knows and is the only part the stand-in sees. `check` is the precise pass condition
for the inspector and judge; it must be substantive (placeholders are rejected) and
is never shown to the stand-in or the subject. `disclosure: "proactive"` marks a
fact the user would volunteer; it is graded but excluded from the independence score.
Fact IDs must be unique and cannot collide with a rubric criterion ID.

A fact without a verifier is judged: the judge answers `met`, `unmet` or `unclear`
with an observation of what a user would see (at most 600 characters) and evidence
(a file and lines, or command output; empty evidence counts as unclear), from the
final files and the transcript. A fact with a verifier is decided by running the
hidden command instead; a model never overrides it.

The draft's `hidden_files` maps verifier-relative targets to explicitly selected
source paths, resolved relative to the draft JSON directory (so keep them in
`cases/NAME/`). Frozen cases map those targets to immutable blob names such as
`verifiers/check.py`. A verifier command must name its hidden entrypoint as the
exact argv element `{verifier}/ENTRYPOINT`; the grader substitutes its private
verifier directory without shell interpolation. It must execute from an independent
final-workspace copy, use the trusted entrypoint bytes, and require both a
successful exit and the expected stdout sentinel. Positive nonzero exits require
`expected_failure_stdout`, or the common `expected_stdout` marker when no separate
failure marker is configured. Missing markers and signal termination are execution
errors. Calibration is stricter than grading: only a marker-confirmed failure
demonstrates the baseline, so a verifier that crashes during calibration is broken
and calibration is `unavailable`. On an agent's tree, inspection and final grading
share one classifier (`result_models.verifier_verdict`): a verifier that started and
exited without its success marker (a crash, an import or syntax error, a signal, a
kill at its output bound, or a timeout short of the inspection deadline) is `unmet`,
because calibration already showed it runs on the baseline and the reference. Only a
harness-side failure gives no result (`unclear` during inspection, `grading_error` at
the end): the verifier could not be launched, its workspace copy could not be
restored, its cleanup could not be confirmed, its hidden inputs changed, or the
inspection deadline cut it short. An attempt where a requirement
verifier crashed while its assessment's own reference run did not pass is excluded as
`verifier_uncalibrated`, so a broken verifier cannot produce false fails. Emit success only after all checks pass, and catch only
expected assertion failures around the bounded checks:

```python
import runpy

greet = runpy.run_path("greet.py")["greet"]
try:
    assert greet("Niels") == "Hello, Niels"
    assert greet("") == "Hello, "
except AssertionError:
    print("GREETING_ASSERTION_FAILED", flush=True)
    raise SystemExit(1)
print("GREETING_CHECK_EXECUTED", flush=True)
```

Keep verifier imports and setup outside that assertion wrapper. A success marker
printed before the checks does not establish their execution. Default
`suite_id: null` means an independent copy per fact; a shared non-null suite ID runs
those verifier facts in saved order on the same copy. `reference_patch` optionally
names a curator-only source file; freeze stores it as `reference.patch`. Authoring
validation rejects empty entrypoints, missing files, blank sentinels and obvious
placeholders; it cannot prove that a curator's program tests the intended behavior.

`CaseContent.setup` contains ordinary `CommandSpec` values with argv, relative cwd,
timeout/output bounds and inherited environment-name references. It does not run
commands during authoring. `CaseBudgets` separates subject duration and setup
duration; turn, stand-in reply and inspection budgets are run options (`bench
--max-turns`, `--stand-in-replies`, `--max-inspections`, or `--budget-from`), not
case fields. `CapturePolicy` freezes file/byte/depth limits and explicit
relative-path exclusions and ignored-file inclusions for the runner.

`criteria` holds non-gating quality rubrics: a `RubricCriterion` has `criterion_id`,
`kind: "judge"` and a substantive `rubric`. They are scored when the experiment has
a judge and never change the outcome. `metrics` commands count what a script can.

A `MetricsCommand` measures things a script can count instead of judging
them, e.g. a test run, a coverage diff or a line count. `metrics_id` must be
unique among metrics entries, `description` must be substantive (not a
placeholder), `entrypoint` is mapped in `hidden_files` exactly like a
fact verifier's, and `command.argv` must contain the exact element
`{verifier}/ENTRYPOINT`. The frozen command runs once, on its own copy of the
captured final files with a separate read-only baseline copy. Paths the case's
`capture_policy` excludes are absent from that copy, so a script that needs
them must restore them from the baseline itself. It never runs through
`check_copy`/`execute_check`'s fixed verifier environment: it receives `HOME`,
`PATH`, `DRYHEAVE_BASELINE_DIR` (the baseline copy's path, for diffing) and
`DRYHEAVE_METRICS_OUTPUT` (a file path) alongside the case's own declared
environment references. The script must write exactly one JSON object to the
file at `$DRYHEAVE_METRICS_OUTPUT`:

```json
{"metrics": [
  {"metric_id": "tests_passed", "label": "Tests passed", "value": 12, "unit": "tests", "better": "higher"},
  {"metric_id": "build_succeeded", "label": "Build succeeded", "value": true}
]}
```

At most 64 entries, each with a unique `metric_id`, a `label` of at most 200
characters, a `value` that is a number, boolean or string (strings at most
500 characters), and optional `unit` and `better` (`"higher"`, `"lower"` or
omitted). Reports summarise metrics per case and `metric_id` across all of
the case's metrics commands, taking the label and unit from the first value
seen, so a `metric_id` must also be unique across those commands and keep the
same meaning, label and unit. stdout/stderr are kept as free-form evidence,
not parsed. A
non-zero exit, timeout, invalid JSON or any contract violation produces a
`MetricsResult` with `outcome: "error"` and a reason instead of raising, so a
script can deliberately report a failed build as a metric value rather than
erroring, and a crash never aborts the rest of the assessment. Metrics never
affect the outcome or eligibility.

Three default rubrics — `code-quality`, `codebase-consistency` and
`test-quality` (`rubrics.DEFAULT_RUBRIC_SET`, currently `default-v1`) — are
applied to every case automatically, but only when the experiment has a
judge; without one, no default criteria are added. A case overrides one by
declaring a `RubricCriterion` whose `criterion_id` matches a default id: its
`rubric` text replaces the default's. `disabled_rubrics` lists default ids to drop
entirely for this case; validation rejects any id there that is not one of
the three default ids. `Assessment.rubric_set` records which default version
graded a given attempt.

`calibrations.calibrate_case` grades every requirement and optional fact on the
baseline (it must be unmet) and on the reference patch (it must be met) using the
neutral `VerifierContext` and shared `grading.check_copy`/`execute_check`
operations. Each side is an independent repository copy; verifier facts sharing a
`suite_id` share a copy within that side, in frozen order. Facts without a verifier
are judged together, one judge call per side, so `case calibrate --judge
MODEL[@EFFORT]` is required when such facts exist (a case of verifier-only facts
needs none). Neither calibration nor assessment runs `case.setup` on verifier
copies; dependencies must be available through trusted executables and explicit
inherited environment references. A fact is `ineffective` (flagged
`met_on_baseline` or `unmet_on_reference`), `demonstrated` (unmet then met) or
`unavailable` (unclear verdicts, a missing reference, verifier errors); the case
status is `demonstrated` only when every fact is, `ineffective` when any is flagged,
`unavailable` otherwise, and `not_applicable` without graded facts. `flagged` in the
command result lists each flagged `fact_id` with its flags. A judge verdict is not
an executed check, and calibration records say so in their limitations.

The standalone `case-calibration` object stores the exact `case_id`, a `facts_id`
digest over the graded facts and hidden byte hashes, each fact's verdicts,
verifier executions or judgments, the judge calls, evidence hashes and cleanup. It
references only the exact case; no run, capture or Assessment is synthesized.
`load_calibration` checks these identities against the frozen case, verifies
retained result/output hashes and observed pass/fail markers, and requires stopped
owned writers. Explicit omissions prevent demonstrated status. Export/import
requires `calibration-evidence` and preserves the exact record/input closure;
source sessions are still optional.

`case calibration ID [--evidence PATH --offset N --limit N]` validates the record
and reads bounded output slices. The service holds the store native lock and an
operation lock. Atomic `STORE/calibrations/UUID/operation.json` records owned process
identities and cleanup; interrupted working evidence stays in that directory.
Recovery signals only matching recorded PID/create-time pairs and their observed
descendants. Calibration, native run and assessment reconcile this ownership before
execution. Failed cleanup blocks snapshot/publication. Retrying calibration starts
fresh copies; it does not infer results from interrupted work.

## Workflows

A case is personal: it captures one person's real workflow, including the
reviews, QA and documentation habits they actually followed, not an idealized task
stripped of those habits. `CaseContent.workflow` is an ordered tuple of
`PromptStep` entries, each with a unique `step_id`, a `prompt` and an `after_phase`
naming a declared phase; steps must be ordered by phase. A step sends its `prompt`
once: a skill invocation such as `/code-review --quick`, or a plain instruction such
as "update the docs". Claude gets it verbatim; a Codex variant on its live config
gets a `/name args` prompt as the skill mention `$name args`, and the run is refused
unless its Codex config has `skills/name/SKILL.md`. A step may carry an
`answer_hint` guiding how the stand-in answers any dialog that step triggers. A
prompt cannot be one of the built-in commands that produce no model turn
(`cases.NO_TURN_COMMANDS`: `/clear`, `/compact`, `/exit`, `/quit`, `/resume`,
`/rewind`, `/login`, `/logout`), and cannot start with `$` or `!`: a skill step is
written `/name` for every agent, and Codex receives it as `$name`.

The runner inspects at each done point (the stand-in says the work is done, or a
prompt step comes due). One inspector call judges every requirement and optional
fact on the current snapshot; verifier facts run their verifier instead. Facts that
come back `unclear` are re-judged once, and unclear facts that are the only blockers
do not stop the run advancing. An earlier inspection is reused only when the digest
of snapshot, transcript and fact set matches. Every inspection is journaled with its
intent first and its result after, and only feeds feedback and workflow metrics: it
never decides pass or fail, and its snapshot is never graded.

`workflow.next_workflow_action(case, position, verdicts)` is the pure transition
function. The blocking phase is the earliest phase with an unmet requirement
(`blocking_phase`, which also catches regressions in earlier phases). A pending
prompt step is delivered once its `after_phase` precedes the blocking phase, that
is, once every requirement of phases up to and including `after_phase` is met;
otherwise a blocker means `raise`, and no blocker with no pending step means
`complete`. To raise, the stand-in is handed that phase's unmet requirement facts
(plus its unmet optional facts), as `text` and the observation, never the `check`,
and asked to describe what it saw in the user's own words. When the agent accepts
the resulting fix-request turn the run journals a `raise` event with the handed set;
a fallback reply is journaled the same way with cause `fallback`. After the last
prompt step a closing inspection always runs. All requirements met with no pending
steps ends the attempt with `stop_reason="workflow_complete"`.

Budgets come from `RunOptions.budget` (`RunBudget`): `max_turns` (default 60) caps
accepted subject turns, `stand_in_replies` (40) caps stand-in calls and
`max_inspections` (20) caps inspections, which no longer count against turns or
subject time. Exhaustion ends the attempt with its own stop reason and grades as
`fail` with reason `budget_exhausted`. `bench --budget-from PILOT_RUN` derives
ceil(used x 1.5) + 5 for each figure from a passed single-attempt pilot and records
the pilot in `RunOptions.pilot_run`; the pilot must share the benchmark's case,
stand-in, scoring and workflow identities.

`CaseContent.workflow` is dropped from serialization when empty. `dryheave run
--workflow` turns the workflow on for a plain `run`. `bench` turns it on by default;
`--no-workflow` sends only the initial prompt and disables inspection, raising and
prompt steps, and also skips the launch check's slash-step probe. `bench --inspector
MODEL[@EFFORT]` sets `ScoringConfig.inspector` for the inspections, defaulting to the
judge's model when omitted.

## Historical repository snapshots

`capture_repository(store, repo, commit, initial_patch=..., limits=...)` requires a
full lowercase commit SHA and creates a `RepositorySnapshot` with exact tree
identity, tracked `tree.tar`, exact ancestor `ancestry.pack`, optional
`initial.patch`, declared limits and curator provenance. It reconstructs and
validates the selected input before publishing the snapshot. Source repositories
and logs are read-only.

Capture creates its own Git metadata and points Git only at the selected source
object directory. Source/global Git configuration, hooks, templates, attributes,
replacement refs and working-tree contents do not drive capture. The archive is
built directly from tree/blob bytes, including `export-ignore` files and literal
`export-subst` placeholders. Source refs, reflogs, remotes and alternate stores
are never copied. Complete reachable ancestry is mandatory in v1; synthetic
roots are not offered. Shallow, partial/promisor and alternate object stores,
submodules, LFS pointers, external/cyclic/dangling symlinks, unsafe paths and
case-colliding tree entries fail visibly.

`materialize_repository(store, reference, destination)` requires a new destination
and returns the snapshot model. It creates independent Git objects and files,
verifies that the pack contains exactly the selected reachable closure and verifies
the tree/archive identity. An optional patch is applied in the index and its
resulting tree validated before working-tree files are written. Symlinks resolve
components in filesystem order, including intermediate links before `..`, and
must stay inside the workspace. Materialization writes exact bytes without
checkout filters and leaves only `refs/heads/baseline` and an index matching that
baseline. Normal `git log`, `git blame` and `git diff` remain useful. No source path,
remote, reflog or alternate is installed in operational Git metadata. Errors remove
only the new owned destination. Capture scratch directories live under the chosen
store and are removed when their owning operation ends.

Snapshot `source_path` and `source_git_path` are curator-only audit provenance.
`known_disallowed_commits` inventories bounded locally present commits outside
baseline ancestry, including unreachable objects. `audit_coverage` is
`complete_at_capture` or `partial`, with `audit_limit` and warnings. This inventory
cannot cover commits created later. `FrozenCase.source_path_hash` copies SHA-256 of
the imported transcript's absolute path string, so later observed tool paths can
be compared without requiring the original Session. Preserve matching-path evidence
in later audit results; a hash is not an access-prevention mechanism.

The default capture bounds are 20,000 files, 64 MiB tracked bytes, 128 MiB packed
history, 100,000 objects, 10,000 known disallowed commits and 120 seconds per Git
operation sequence. The object-store per-blob limit still applies. Native replay
isolates repository objects; it does not restrict host, source-log or network
access by an agent.

## Log and process APIs

`logs.service.import_session(store, path, AgentKind, limits)` freezes a `Session`.
`logs.service.parser_for(AgentKind)` selects the adapter; `logs.codex.parse`,
`logs.claude.parse` and `logs.droid.parse` return the same model without writing.
`logs.base.discover` scans non-symlink JSONL files with depth/entry limits.
`ImportLimits` defaults to 16 MiB, 20,000 source records, 10,000 scan entries and
12 directory levels; CLI import/scan expose byte and event limits. A malformed
record is skipped with a source-line warning; unterminated final records, missing
metadata, conflicting identities and baseline ambiguity remain visible.

Events retain source-line/event IDs, timestamps, native IDs, turn IDs, useful text
and selected structured tool/lifecycle metadata. Encrypted reasoning, reasoning
blocks, base instructions, hook bodies and compaction replacement histories are
excluded. The adapter reads both Codex legacy response/event messages and 0.153.4
paginated capitalized item completions; duplicate message representations collapse
while distinct turns remain distinct. Claude text/tool blocks remain ordered.

Droid reads `session_start` for the session ID, log version, `cwd` and title, message
records for text/tool blocks, `todo_state` as metadata and `agent_turn_outcome` as a
turn lifecycle event; `compaction_state` records a `compacted` warning instead of
replaying replacement summaries. Droid writes hook and system traffic as `user`
messages, marked `visibility: user_only` for hook and interface notices and
`visibility: llm_only` for injected system text. Those events are retained, not
filtered, and their text is carried in a `<harness-event>` wrapper so the shared
`mining.classify_user_event` patterns report them as `harness_injected` rather than as
the human speaking. A Droid log carries no Git metadata, so `recorded_baseline` is
always `None` and every Droid session records a `baseline_unknown` warning.

Codex dialogue deduplication indexes ordered candidates by native ID, text and
turn. It consumes the earliest compatible event representation, including
missing-turn fallbacks, without rescanning all previous candidates per message.

Usage keeps raw category records and provenance. Codex response IDs take precedence
over legacy cumulative token counts; the latter remain marked `superseded` when
response records exist. Without response records, cumulative records remain
cumulative and must never be summed as separate calls. Claude snapshots deduplicate
by message ID. Input categories are non-overlapping: Codex uncached input subtracts
cached and cache-write input only when both categories are observed; otherwise it
remains unknown. Claude input is already uncached. Unknown categories remain null.
Claude `cost_state` metadata is preserved separately from
per-message usage. Mixed or incomplete recorded coverage does not establish a
complete session cost.

Sanitized real-format fixtures and public version/shape provenance live in
`tests/fixtures/`. They derive from installed Codex 0.153.4 and Claude 2.1.263
JSONL records. Free text, identifiers and paths are synthetic replacements; the
fixtures establish protocol-shape compatibility, not recovery of private task
intent. Private source path/line mappings remain ignored under `.cache`.

`processes.run_command(CommandSpec, root, input_bytes=..., environment=...)`
returns `CommandResult` with stdout/stderr bytes, exit code, `exited`/`timeout`/
`output_limit` outcome and cleanup-coverage text. It drains both pipes, writes
stdin without deadlock, bounds total output and wall time, and stops its owned
process group before reaping on failure. A normally reaped PID is never signaled.
Descendants retaining inherited pipes remain bounded; detached children or children
that close those pipes are unobserved. Later native runner code must retain its
own writer/session ownership and cannot infer complete quiescence from this helper.
