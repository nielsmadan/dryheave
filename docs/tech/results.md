# Assessment and retained results

The design decisions behind checks, metrics commands, default rubrics, pricing,
summaries and regrade are in docs/decisions/0002-benchmark-results.md.

Execution and assessment are explicit commands. `run` stops writers and captures;
`assess RUN_ID` audits, grades and advances each retained attempt to `finished`.
`assess` and execution resume acquire the exclusive run and native locks. Repeating
assessment preserves finished IDs and never relaunches a subject or stand-in.
Before loading assessment inputs, recorded native ownership is reconciled across
the store under nonblocking run locks and the native lock. Active interrupted
attempts become stopped; absent or corrupt captures remain unscorable. Successful
fresh cleanup permits later execution but preserves the original capture's failed
cleanup and exclusions.
An interrupted verifier/judge intent becomes an error on assessment resume instead
of repeating an ambiguous external call. SIGINT/SIGTERM cancel the current grader
and stop recorded writers. The CLI exits 130 with a concise `cancelled` error and
an assessment resume command; JSON errors use stderr. Observed verifier/judge
descendants are journaled during
execution; assessment and subject resume reconcile those process identities before
snapshotting grading evidence or launching further native work. Truly unobserved
detached processes remain a native coverage limit. Optional judgments remain distinct
from trusted deterministic outcomes.

```sh
uv run dryheave --store .dryheave run EXPERIMENT --mode offline-fixture --json
uv run dryheave --store .dryheave run --status RUN_ID --json
uv run dryheave --store .dryheave assess RUN_ID --json
uv run dryheave --store .dryheave report RUN_ID --json
uv run dryheave --store .dryheave compare BEFORE_RUN AFTER_RUN --json
uv run dryheave --store .dryheave compare RUN_ID RUN_ID \
  --before-variant base --after-variant changed --json
```

The run ID is emitted to stderr immediately after reservation. JSON stdout remains
a single response. Status and report read a consistent complete event prefix
without acquiring or repairing the writer journal. An incomplete active final
line is ignored by this read-only view; resume repairs it only under its lock.
Each report takes one defensive journal snapshot and indexes assessment events
by attempt. A running assessor extends its local index from newly durable events;
completed judge calls and unmatched intents remain visible without rescanning
the whole journal for each attempt.

## Regrade

```sh
uv run dryheave --store .dryheave assess RUN_ID --regrade --json
uv run dryheave --store .dryheave assess RUN_ID --regrade --case CASE --json
```

`--regrade` is a separate assessment pass with its own `regrade_id`. It grades
every attempt with a published capture in `captured`, `audited`, `grading` or
`finished`, including finished ones, and never relaunches subjects. Each attempt
gets a new assessment whose `previous_id` names the attempt's current assessment;
older assessment IDs stay unchanged and reviews of findings the new audit still
reports carry over. The pass uses the current default rubrics and the price-table
rule (experiment table, else the built-in table). Judge recipe and budget come from
the experiment's scoring; when the graded case has rubric criteria or judged facts
(facts without a verifier) and the experiment has no judge, regrade fails before
changing anything.

`--case CASE` grades against a replacement case: its facts (kinds, phases, checks
and verifiers), rubric criteria, metrics commands, hidden files and reference patch.
It must share the original case's `repository_id` and `initial_prompt`, and the
input-integrity audit, the journaled raises and elicited citations, and `case_id`
still use the capture's own case. A judged fact is compared with the run's last
inspection only when its judge input (`text` and `check`) is unchanged in the
replacement, so correcting a fact's `check` gives the new verdict instead of a
dispute. The assessment records the replacement as `graded_case_id`, and
`criteria_id` reflects the graded case.

Every verifier, rubric, fact-judge, metrics and judge event of a regrade carries its
`regrade_id`, so ordinary assessment resume and report reconstruction never see
them, and a regrade never reuses judge calls or evidence from an earlier pass.
Grading files live under `attempts/<id>/regrades/<regrade_id>/assessment`, and the
new assessment's evidence and judge spend cover only that pass. An interrupted
regrade resumes under the same `regrade_id` when the same command is re-run;
interrupted intents become errors as in ordinary resume. The new assessment itself
records the `regrade_id` that produced it, so resume recognizes an attempt as
already regraded in this pass even if the process died after saving that
assessment but before appending its `regrade-assessed` marker, and never chains a
second assessment onto it. While a regrade is
unfinished, plain `assess` and a regrade with another `--case` refuse and print
the resume command. A completed regrade is not repeated by `assess`; another
`--regrade` starts a new pass. The response adds `regrade_id`.

`assess` returns `assessment_ids` and `report`. Assessment objects use kind `result`,
schema version 1 and payload kind `assessment`. IDs for the experiment, case,
profile, capture, scoring, raw grading evidence and earlier result are provenance.
They do not pull raw artifacts into structured-result reference closure. Regrading
or reviewing requires independently verified exact inputs and capture bytes.
`graded_case_id` is present only on regrades against a replacement case.
`regrade_id` is present only on assessments a regrade pass produced.

The audit runs after capture and checks immutable closures, hidden verifier bytes,
known disallowed future Git objects and observed native tool requests for forbidden
source paths. It exempts the owned workspace even when nested beneath the original
source checkout. Command/config provenance and simple path mentions do not establish
reads. Native access and unknown future history coverage remain partial. A matching
solution, ordinary task commit or task-test edit is not a tampering finding.

Hash corruption is retained as an invalid assessment even when ordinary capture
loading fails. Local evidence reads verify the capture's own manifest and blobs,
then label failed dependency verification explicitly. If capture publication fails,
a separate `quarantine` object retains evidence with `input_integrity: unverified`;
it is not a valid normal capture and blocks further subject launches. Old stopped
attempts with no published capture remain unscorable. Corruption, missing capture,
incomplete files/Git/evidence and unresolved writer cleanup cannot be reviewed into
valid inputs. A suspicion can be reviewed without erasing its original finding:

```sh
uv run dryheave --store .dryheave review RUN_ID --attempt ATTEMPT_ID \
  --finding FINDING_ID --decision dismiss --reviewer curator \
  --reason 'Recorded invocation was investigated against the retained evidence.' --json
```

Checks run from frozen hidden entrypoints in independent copies. Criteria with
the same explicit `suite_id` share a workspace in their frozen criteria order;
independent criteria, shared suites and judge calls have separate directory namespaces.
Final, baseline and reference copies remain separate. An interrupted shared suite
does not reconstruct its intermediate mutations; remaining results are errors.
Materialized hidden bytes are checked before and after each execution. Executables are
resolved outside subject-owned code; wrapper/inline-program prefixes are refused.
Each result records exit outcome, expected execution marker, elapsed time and exact
stdout/stderr hashes. Exit zero requires `expected_stdout`; a positive nonzero exit
requires `expected_failure_stdout`, falling back to `expected_stdout` when the separate
failure marker is absent. Missing markers and signal termination are execution errors,
and an inspection run that the inspection deadline cut short is recorded as
`deadline_exhausted` (final grading runs verifiers without that deadline).
Existing retained assessments are immutable.
Baseline/reference calibration is separate: demonstrated means baseline failed and reference passed;
baseline passing is ineffective, and absent or errored calibration is unavailable.
Calibration is stricter than grading: an errored baseline run means the verifier is
broken, not that the baseline failed. This remains trusted native execution, not OS
isolation. Final grading reads each execution with the classifier inspection uses
(`result_models.verifier_verdict`): a pass is `met`; a verifier that started and exited
without its success marker (a failure, crash, import or syntax error, signal, kill at
its output bound, or its own timeout) is `unmet`, and its evidence names the process outcome, exit code and
error. Only a harness-side failure gives the fact no result: the verifier could not be
launched, its copy could not be restored, its cleanup was unconfirmed, its hidden
inputs changed, or grading was interrupted. Because `unmet` from a crash rests on the
verifier running cleanly, an attempt is excluded with `verifier_uncalibrated` when a requirement
verifier crashed on it and, in its own assessment, that verifier's reference run did not
pass (or there was no reference run); a marker-confirmed failure is not affected. Its outcome stays as graded, but it leaves pass rates. The
per-criterion pass rate counts verifier criteria with the same classifier (`met` is a
pass, `unmet` a graded fail, no result is left out). Suspected/confirmed contamination is retained separately and excludes comparisons.

## Final grading and outcome

Pass and fail come only from final grading; inspection verdicts are feedback.
`Assessment` (schema 2) holds one `FactResult` per graded requirement and optional
fact (`verdict` `met`, `unmet`, `unclear` or `disputed`, an observation, evidence and
`source` `verifier` or `judge`), the judge's `uncovered` problems no fact covers,
`criteria` (verifier executions and non-gating quality rubric scores), and `outcome`
with `outcome_reason`:

- `pass` / `all_met`: every requirement is met.
- `fail` / `unmet`: a requirement is unmet in a reached phase, or in a phase the
  attempt never reached when the attempt ended for a subject-side reason: `timeout`,
  `stand_in_stop`, `workflow_complete` or a budget stop; `budget_exhausted` when
  `turn_budget`, `stand_in_budget` or `controller_budget` ended it.
- `indeterminate` / `grading_error` (a requirement has no result), `disputed`,
  `unclear`, or `phase_unreached` (only unreached-phase requirements are unmet and the
  attempt stopped for any other reason, such as `inspection_budget`,
  `inspection_unavailable`, `exited` or `delivery_uncertain`). Indeterminate attempts are `unscorable`, hence ineligible.

Only stops the subject caused withhold nothing: a harness-side stop leaves unreached
phases unjudged. `exited` is deliberately indeterminate because a CLI exit can be
authentication or harness trouble as easily as an agent crash. A requirement that is
clearly unmet fails the attempt even when another is unclear or disputed. Any stop
reason added later is indeterminate until it is listed as subject-side in
`fact_metrics.SUBJECT_STOPS`.

Facts without a verifier go to one final judge call (`FinalJudgeInput`: the
inspector's fact prompt plus a request for `uncovered`) over the final capture and
transcript. Facts missing from the answer are asked again once on their own; still
missing, they have no result. A judgment without evidence is `unclear`. When the final
capture's snapshot digest equals the last completed inspection's, a judged fact whose
final `met`/`unmet` verdict contradicts that inspection is judged once more; still
contradicting, it is `disputed`. The rubric call follows. Journal events
`fact-judge-intent` and `fact-grade` (the judged `FactResult`s and `uncovered`) and
`rubric-intent` make grading resumable without repeating a judge call; an intent with
no result leaves those facts without a result or the rubrics `interrupted_judge`.

`Assessment.fact_account` (`fact_metrics.py`, pure over the journal and results)
credits each graded fact: `own` (met, never raised, never elicited), `elicited` (a
non-proactive requirement the user stand-in cited in an ordinary reply before it was ever
raised), `corrected` (met after appearing in a `raise` event's handed set; wins over
elicited), `unmet`, or `unreached` (its phase was never reached). A phase is reached
when a completed inspection's earliest blocking phase got that far; without workflows
every phase is. Independence is the mean credit (own 1, elicited 0.75, corrected and
unmet 0) over non-proactive requirements in reached phases, `None` when there are none or when any such requirement ended unclear, disputed or without a result.
The account also keeps stand-in replies (ordinary replies only), fix rounds, reached
and unreached phases, raised facts per phase, and per fact the times raised, the first
completed inspection's verdict and regressions (met then unmet in the next completed
inspection whose snapshot digest changed).

The judge uses the frozen JSON-command, Codex or Claude recipe and finite role
budgets; experiment validation rejects facts without a verifier when there is no
judge, and any explicit scripted judge. For quality rubrics a JSON-command receives
`JudgeInput` on stdin: `initial_prompt`, `criteria`, `files`,
`omitted_binary_files`, `transcript` (`role` `user`, `assistant` or `action`, and
`text`), `transcript_truncated` and `instruction`. It returns `JudgeResponse`:

```json
{"judgments":[{"criterion_id":"quality","outcome":"pass","score":0.8,
"rationale":"The implementation satisfies the supplied rubric."}],
"usage":null,"observed_model":null,"observed_effort":null}
```

For facts it receives `FinalJudgeInput` (`facts` with `fact_id`, `text` and `check`
in place of `criteria`) and returns `{"judgments":[{"fact_id":...,"verdict":...,
"observation":...,"evidence":...}],"uncovered":[...]}`. Rubric scores never gate.
Missing rubric judgments produce individual errors. Raw requests, responses and rationale
remain in optional grading evidence; structured results carry evidence hashes.
For valid JSON with an invalid decision, unavailable fact or invalid criterion IDs,
independently valid usage and observed model/effort survive in the failed call.
Observed settings are bounded to 256 characters without control characters;
invalid fields are omitted independently. Invalid JSON supplies no recovered
accounting fields. Saved-table estimates prefer the retained observed model.
Codex judge settings are recovered from its bounded response file; token usage
comes exclusively from native stdout, even when the response contains usage fields.

Role accounting retains subject, stand-in and judge observations independently.
Token categories do not overlap; reasoning is an output subset. Responses are
deduplicated, cumulative observations become deltas, and resets retain observed
segments with partial coverage. Codex exec all-zero fallback usage stays unknown.
Codex token normalization records its transport policy. The verified exec 0.153.4
protocol defaults an absent cache-write count to zero; explicit null or invalid
counts do not establish zero. Generic native/imported logs lack that guarantee,
so absent cache-write counts and the derived uncached count remain unknown.
Descendants without observed models are not priced at the requested root model.
Requested-root estimates are labeled and use only the saved price version, date,
currency and per-million-token rates. `known_cost` is an observed subtotal; partial
coverage does not establish a complete total. Explicit fixtures/no-call roles can
establish zero; missing telemetry cannot.

`Assessment.workflow` is a `WorkflowOutcome` built from the capture's
`WorkflowProgress` and the journaled `fact-inspection` and `raise` events, present
only when the run had workflows on. It reports declared and delivered prompt steps,
settled inspections (unavailable ones included), fix requests and fallback replies, dialogs answered, the
first and final inspection verdicts and total inspection seconds, plus the events in
order. An inspection is `failed` only when it raised (its blocking phase was not
ahead of the next prompt step), so unmet later-phase facts do not fail it; an
intent with no result is `unknown`. `completed` is true when the attempt stopped
with `workflow_complete`.

Reports show scheduled trials, attempts, completed/scored/eligible counts, reasons
for exclusion and distributions with observed/unknown counts. Spending retains
failed, interrupted, retried and excluded attempts. Unfinished assessments reconstruct
judge charges from their report's durable event prefix: unmatched call intents retain
unknown spending and completed calls retain their observed spending. Comparisons
require matching case, fact, criterion, stand-in, scoring, repetition, seed and execution mode identities;
profile differences are allowed. Attempts graded against different cases, such as
regrades against different replacement cases, are not paired. Compatible partially completed runs retain
unstarted-trial attrition. The latest attempt per paired trial is selected,
including an unassessed retry as attrition, while all earlier spending stays in the
report. Selecting variants also filters each side's metrics and all-attempt spending
to that variant. Saved comparisons retain each supplied input, resolved immutable
report reference (or run ID), durable report sequence and selected variant. Moving
a portable-report alias cannot change the saved reference. Each pair lists the
before and after outcome, fact verdict changes and criterion outcome changes. Two
trials imply no significance.

Each group carries `facts` (`FactGroup`): outcome and reason counts over the latest
finished attempt per trial; pass rate, independence and correction distributions and
per-fact rows (verdict counts, credit split, raised rate, first-inspection met rate,
regressions) over eligible ones. A run started with `bench --budget-from PILOT_RUN`
records the pilot in `RunOptions.pilot_run`; its report adds the pilot as `floor`
(label `VARIANT (n=1)`, its own budget and group) only when its case, stand-in,
scoring and workflow setting match, and the report states the run's own budget.

## Reviewing an attempt: transcript and replay

```sh
uv run dryheave --store .dryheave transcript RUN_ID [--attempt ATTEMPT_ID] [--json]
uv run dryheave --store .dryheave replay RUN_ID --attempt ATTEMPT_ID \
  (--call CALL_ID | --all-stand-in) [--voice NAME] [--case CASE] [--out DIR]
```

`transcript` prints each attempt (or only `--attempt`) as three lanes, built by the
pure `transcript.review_lanes` from loaded data; the viewer's `review` route calls the
same builder. **Conversation** is what the agent saw: every accepted user message
with its turn and author (the initial prompt, the stand-in call that wrote it, the
fallback fix reply, a workflow prompt step, or `unknown` when nothing in the journal
matches it) and each agent reply. **Behind the scenes** lists, in journal order,
every stand-in call with its purpose (`reply`, `fix_request`, `dialog`, `screen`),
action, text, reason and citations; for a fix request also the set it was handed,
recomputed from the failed inspection exactly as the runner raises it; rejected
calls with their rejection code; fallback fix replies; every inspection with each
fact's verdict, observation and evidence plus the inspector call IDs (`j-…`) to pass to
`replay --call` (an inspection or stand-in call intent without a result is `unknown`); corrections from the journaled `raise` events (handed set, the
stand-in's own citations, cause); workflow prompt steps; and the stop reason.
**Gaps** lists the facts that ended unmet, unclear or disputed (with the fact text,
observation, evidence and grader), the judge's `uncovered` problems and unreached
phases, or says the attempt is not assessed yet. The text form is for reading;
`--json` returns `RunTranscript` (schema 1) with one `ReviewLanes` per attempt.
Reading takes no lock. The case is read from its manifest only, never from its
hidden verifier files. Without a run journal (a portable report) the lanes come
from the capture: conversation from its dialogue, stand-in calls without turns and
inspections without per-fact verdicts, and the lanes say so in `notes`; anything
that cannot be read is named in `notes` rather than guessed.

`replay` re-sends stored requests of one attempt: one stand-in (`c-…`) or inspector
(`j-…`) call with `--call`, or every stand-in call with `--all-stand-in`. Requests are
read from the attempt's capture in the store (`evidence/controller/CALL/` and
`evidence/inspection/*/judge/CALL/`), falling back to the run directory
(`attempts/ATTEMPT/controller/CALL/`, `inspections/ATTEMPT/*/judge/CALL/`) for an
attempt not yet captured; a call whose request is in neither is refused with an
input error before anything is sent. `--voice` (a workspace voice name, persona ID
or alias) replaces the stand-in's persona; `--case` (case ID or alias) replaces the
stand-in's initial prompt, allowed facts and the text of handed facts, and for an
inspector call rebuilds each fact's `text` and `check` from that case, refusing a
fact the case lacks. The experiment's frozen stand-in and inspector recipes send
the request (a scripted stand-in keeps its original call index); the old output
(the stand-in call's recorded decision or error, the inspector's stored response)
is shown next to the new one. A re-send that fails is reported as `failed` with its
error, never filled in. The old inspector output goes through the same settling as a
run (a verdict without evidence is `unclear`). Replay takes no run lock and writes
nothing into the run or the store except the shared native lock file: `--out`
(default `<workspace>/replays/<id>`, else `./replay-<id>`) must be a new directory outside the store, and
receives each call's request, response and controller artifacts plus `replay.json` (`ReplayReport`, schema 1). Controller processes run under the
store's native lock and handle SIGINT/SIGTERM: the call in flight is stopped, replay exits 130 with a
`cancelled` error and writes no `replay.json` (the output directory is created only
once the lock is held; a SIGKILL or crash can leave a child process and credential
symlinks under `--out`, so do not point `--out` into a repository); a capture blob that fails its hash check is an
error, not a reason to fall back to the run directory; replay does not
reconcile other operations' ownership because that writes into run journals.

## HTML report

Inside a workspace, `report RUN [RUN ...]` without `--html` also writes
`reports/RUN.json` (the report; the printed `--json` output adds the checkpoint keys) and `reports/RUN.html` (several runs use
`combined-<hash>`), replacing earlier copies, and `bench` writes the same pair for
its run. Both are checkpoint paths (`commit_paths`); see
[workspace setup](../user/workspaces.md). Explicit `--html PATH` writes only that
file.

```sh
uv run dryheave --store .dryheave report RUN_ID --html report.html
uv run dryheave --store .dryheave report RUN_ID --html report.html --force
uv run dryheave --store .dryheave report RUN_ID --html report.html \
  --include-sensitive assessment-evidence
uv run dryheave --store .dryheave report RUN_ID OTHER_RUN_ID --html combined.html
```

`--html PATH` renders `report_run`'s `RunReport`, each referenced case's
`ViewerService.case()` summary and, when requested, each attempt's judge
reasoning into one self-contained HTML file: inline CSS with light/dark support
via `prefers-color-scheme`, no `<script>`, no external resource references, and
every value HTML-escaped. `render_report_html` in `report_html.py` is a pure
function from a list of `(RunReport, RunSetup | None)` entries, the cases and the
reasoning to the page string; the CLI does the I/O (resolving each reference, loading
run settings through `run_setups` when there are several, loading cases, optionally
loading reasoning, writing the file).
Both open with a Summary table: one row per variant, then one row per latest
attempt of each trial, giving the outcome (✓ pass, ✗ fail with its reason,
? indeterminate with its reason; variant rows show passes, independence and corrections over eligible attempts only, the same population as the Facts table),
independence, accepted turns, first inspection (✓ passed, ✗ failed,
? unavailable, – no inspection), corrections (fix rounds), workflow progress
(`delivered/steps`, or – without workflows), working time (agent time excludes
inspection seconds), total tokens (cache reads and writes included) and
API-equivalent cost, and a final floor row for the pilot with its budget. Variant
rows show first-check passes and workflow completions as passes over the attempts
below them that have an inspection or a workflow respectively, and mean
independence and corrections; retried attempts are excluded from every variant row, though
their spend stays in the sections below. These come from each attempt's and
each group's `topline`, derived in `reports.py`. An attempt's detail adds a
Facts card (outcome, independence, corrections and corrections by phase with the
facts each raised, ordinary stand-in replies, phases, and per fact the final
verdict, credit, times raised, first check, observation and evidence, then the
uncovered problems) and a Workflow card with its step/inspection/dialog counts and its events in order
(step, kind, outcome, turn, unmet facts); an attempt without a retained
workflow shows that plainly instead.
Sections mirror the viewer's Results and Case tabs: per-variant cost (API-equivalent
estimate, with price basis), tokens, turns, duration and stand-in replies, each labelled
with its population (every retained attempt including retries, or eligible attempts
only); a Facts section from each group's `facts`; per-case criterion/metric summaries
keyed by `(case_id, id)`. The
report itself carries each group's `price_basis` and `facts` summary and each attempt's `eligibility`, computed once in
`reports.py`, so the HTML export and the viewer render them rather than
re-deriving them from attempts. The page then has an
`Attempts` table with a collapsible detail per attempt; and a `Cases` section per
referenced case (prompt, phases in order, facts with kind, phase, disclosure, text
and check or verifier argv, prompt steps with their `after_phase`, effective
rubrics with origin, metrics commands, budgets, tool requirements, baseline and
reference-patch presence — hidden entrypoint content is never shown). A case referenced by a result (e.g. a
regrade's replacement case dropped from a portable bundle's closure) that
cannot be loaded from the store renders its section as "case not available
in this store" rather than aborting the export.

Several references produce one page with a single Summary table across every
run: a Run column, and each variant row labelled with its variant name and the
model and effort its frozen profile requested. A single-run page carries no model
labels, so its Summary matches the viewer's. A Runs section lists each run's frozen
settings (`run_setups` in `reports.py`, which loads each distinct experiment once:
cases, variant profiles, user stand-in, judge, inspector as `inspection_recipe`
selects it, and the price table versions the run's charges cite). Cases, stand-in,
judge and inspector are compared by frozen identity (case IDs, stand-in object
ID, recipe digest), never by label: a differing one is highlighted, a setting
unavailable for any run is reported as not comparable instead of equal, and only when
every compared setting is known and equal does the page say the runs match. Variants
and price tables are listed but not compared. Settings that failed to load are listed
as issues with their reason. Each run's full report follows under its own heading.
Without `--html`, several references return `{"reports": [...], "setups": [...]}`,
each setup a versioned `run-setup`. A report takes at most 16 references, and each
run may appear only once, including through two references that resolve to it. Judge
reasoning is keyed by run and attempt, so each run's section shows its own.

Judge rationale per criterion and check is included only with
`--include-sensitive assessment-evidence` — the only accepted value for
`report --html` — the same flag and semantics as `export`'s sensitive-artifact
selection; without it, or when no reasoning was retained, attempt detail shows
"reasoning not included". Reasoning is built from the attempts already loaded
for the report, not a second per-attempt journal read. `--force` and
`--include-sensitive` are rejected without `--html`. The CLI refuses to
overwrite an existing destination unless `--force` is given; the write itself
is exclusive (create-only) so a file that appears at the destination between
the refusal check and the write is never overwritten.

# Portable bundles

```sh
uv run dryheave --store .dryheave export RUN_ID --output results.tar --json
uv run dryheave --store .dryheave export RUN_ID --output evidence.tar \
  --include-sensitive captures --include-sensitive assessment-evidence --json
uv run dryheave --store .another-store import results.tar --json
uv run dryheave --store .another-store report PORTABLE_REPORT_ROOT_ID --json
```

Import returns `roots` and a `bundle` manifest. For run exports the first root is a
`portable-report` object; remaining roots include the exact experiment input closure
when valid and any explicitly selected sensitive artifacts. The portable report
is inspectable/comparable without raw capture objects. Its omitted raw classes
and unavailable audit/regrade evidence are explicit. Original input/result IDs
remain provenance and are not claims that omitted bytes were exported faithfully.

Default bundles contain curated inputs and structured results. `captures` opts
into final files/patches, Git metadata, terminal/native logs/screens and stand-in
artifacts from durable capture/quarantine IDs, including captures published before
assessment. `assessment-evidence` adds raw verifier and judge artifacts. Selected
classes enter the included inventory only when their objects exist; unavailable
artifacts retain omission labels and identify the affected attempts.
`source-sessions` explicitly adds full source transcripts named by case origins,
case evidence, allowed-fact evidence and persona examples, including standalone
personas. Sources are deduplicated. Missing transcripts are identified in omissions
and retain the incomplete source-transcript label; corrupt available sources fail
export. Curated session-ID provenance does not automatically include transcripts.
Frozen selected input bytes retain
their original object IDs and hashes. Sensitive selection fails on an invalid
required closure; ordinary invalid-result summaries can still be exported.

Bundles are uncompressed tar archives with a strict `bundle.json` inventory,
canonical immutable manifests and exact hash-named blobs. Import rejects unsafe
paths, links, duplicates, unsupported schemas, altered bytes and incomplete or
extra closure content before publication. Existing objects are verified, never
overwritten. Optional exported aliases use `--alias NAME`; import defaults to
collision error, with explicit `--alias-policy skip` or `replace`. Export refuses
an existing destination and publishes only a complete archive.
Before the tar parser runs, import scans physical headers on the same open file.
PAX/GNU extensions are limited to 64 KiB each and 16 consecutive headers. Normal
PAX long paths remain supported. PAX size overrides, sparse metadata and physical
sparse entries are rejected so extension interpretation cannot change the checked
member boundaries.
