# Assessment and retained results

The design decisions behind checks, metrics commands, default rubrics, pricing,
summaries and regrade are in docs/decisions/0002-benchmark-results.md.

Execution and assessment are explicit commands. `run` stops writers and captures;
`assess RUN_ID` audits, grades and advances each retained attempt to `finished`.
`assess` and execution resume acquire the exclusive run and native locks. Repeating
assessment preserves finished IDs and never relaunches a subject or simulator.
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
the experiment's scoring; when the graded case has rubric criteria or checks and
the experiment has no judge, regrade fails before changing anything.

`--case CASE` grades against a replacement case: its criteria, checks, metrics
commands, hidden files and reference patch. It must share the original case's
`repository_id` and `initial_prompt`, and the input-integrity audit, help needed
and `case_id` still use the capture's own case. The assessment records the
replacement as `graded_case_id`, and `criteria_id` reflects the graded case.

Every criterion, check, metrics and judge event of a regrade carries its
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
failure marker is absent. Missing markers and signal termination are errors. Trusted
verifiers emit failure evidence only for known check failures; syntax/import/setup
failures do not establish a failed check. Existing retained assessments are immutable.
Baseline/reference calibration is separate: demonstrated means baseline failed and reference passed;
baseline passing is ineffective, and absent or errored calibration is unavailable.
This remains trusted native execution, not OS isolation. Required check failures
produce task failure; missing/error results produce indeterminate completion.
Suspected/confirmed contamination is retained separately and excludes comparisons.

The optional judge uses the frozen JSON-command/Codex recipe and finite role
budgets. Required rubrics need a judge with a positive call budget; experiment
validation and loading reject missing or disabled required judges and any explicit
scripted judge. Optional rubrics can omit the judge. A JSON-command receives
`JudgeInput` on stdin: `initial_prompt`, `criteria`, `files`,
`omitted_binary_files`, `checks` (each case check's `check_id` and `question`),
`transcript` (`role` `user`, `assistant` or `action`, and `text`),
`transcript_truncated` and `instruction`. It returns `JudgeResponse`, with one
`checks` entry per supplied check:

```json
{"judgments":[{"criterion_id":"quality","outcome":"pass","score":0.8,
"rationale":"The implementation satisfies the supplied rubric."}],
"checks":[{"check_id":"error-path-handled","answer":"yes",
"rationale":"The parser returns an error value for malformed input."}],
"usage":null,"observed_model":null,"observed_effort":null}
```

`answer` is `yes`, `no` or `unclear`. Missing judgments and missing check
answers produce individual errors. Raw requests, responses and rationale
remain in optional grading evidence; structured results carry evidence hashes.
For valid JSON with an invalid decision, unavailable fact or invalid criterion IDs,
independently valid usage and observed model/effort survive in the failed call.
Observed settings are bounded to 256 characters without control characters;
invalid fields are omitted independently. Invalid JSON supplies no recovered
accounting fields. Saved-table estimates prefer the retained observed model.
Codex judge settings are recovered from its bounded response file; token usage
comes exclusively from native stdout, even when the response contains usage fields.

Role accounting retains subject, simulator and judge observations independently.
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

Reports show scheduled trials, attempts, completed/scored/eligible counts, reasons
for exclusion and distributions with observed/unknown counts. Spending retains
failed, interrupted, retried and excluded attempts. Unfinished assessments reconstruct
judge charges from their report's durable event prefix: unmatched call intents retain
unknown spending and completed calls retain their observed spending. Comparisons
require matching case, criterion, simulator, scoring, repetition, seed and execution mode identities;
profile differences are allowed. Attempts graded against different cases, such as
regrades against different replacement cases, are not paired. Compatible partially completed runs retain
unstarted-trial attrition. The latest attempt per paired trial is selected,
including an unassessed retry as attrition, while all earlier spending stays in the
report. Selecting variants also filters each side's metrics and all-attempt spending
to that variant. Saved comparisons retain each supplied input, resolved immutable
report reference (or run ID), durable report sequence and selected variant. Moving
a portable-report alias cannot change the saved reference. Two trials imply no significance.

## HTML report

```sh
uv run dryheave --store .dryheave report RUN_ID --html report.html
uv run dryheave --store .dryheave report RUN_ID --html report.html --force
uv run dryheave --store .dryheave report RUN_ID --html report.html \
  --include-sensitive assessment-evidence
```

`--html PATH` renders `report_run`'s `RunReport`, each referenced case's
`ViewerService.case()` summary and, when requested, each attempt's judge
reasoning into one self-contained HTML file: inline CSS with light/dark support
via `prefers-color-scheme`, no `<script>`, no external resource references, and
every value HTML-escaped. `render_report_html` in `report_html.py` is a pure
function from that data to the page string; the CLI does the I/O (resolving the
reference, loading cases, optionally loading reasoning, writing the file).
Both open with a Summary table: one row per variant, then one row per latest
attempt of each trial, giving the agent's accepted turns, working time, total tokens
(cache reads and writes included) and API-equivalent cost, plus one column per case
check (✓ wanted outcome, ✗ the opposite, – not applicable, ? no clear answer; variant
rows show passes over answered). Variant rows average the attempts listed under them,
so retried attempts are excluded there, though their spend stays in the sections below.
These come from each attempt's and each group's `topline`, derived in `reports.py`.
Sections mirror the viewer's Results and Case tabs: per-variant cost (API-equivalent
estimate, with price basis), tokens, turns, duration and help needed, each labelled
with its population (every retained attempt including retries, or eligible attempts
only); per-case criterion/check/metric summaries keyed by `(case_id, id)`. The
report itself carries each group's `price_basis` and `help_needed` summary and each attempt's `eligibility`, computed once in
`reports.py`, so the HTML export and the viewer render them rather than
re-deriving them from attempts. The page then has an
`Attempts` table with a collapsible detail per attempt; and a `Cases` section per
referenced case (prompt, facts with disclosure, effective rubrics with origin,
deterministic criteria by description/required/argv, checks, metrics commands,
budgets, tool requirements, baseline and reference-patch presence — hidden
entrypoint content is never shown). A case referenced by a result (e.g. a
regrade's replacement case dropped from a portable bundle's closure) that
cannot be loaded from the store renders its section as "case not available
in this store" rather than aborting the export.

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
into final files/patches, Git metadata, terminal/native logs/screens and simulator
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
