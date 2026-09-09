# Assessment and retained results

Execution and assessment are explicit commands. `run` stops writers and captures;
`assess RUN_ID` audits, grades and advances each retained attempt to `finished`.
`assess` and execution resume acquire the exclusive run and native locks. Repeating
assessment preserves finished IDs and never relaunches a subject or simulator.
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

`assess` returns `assessment_ids` and `report`. Assessment objects use kind `result`,
schema version 1 and payload kind `assessment`. IDs for the experiment, case,
profile, capture, scoring, raw grading evidence and earlier result are provenance.
They do not pull raw artifacts into structured-result reference closure. Regrading
or reviewing requires independently verified exact inputs and capture bytes.

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
final, baseline and reference copies remain separate. An interrupted shared suite
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
budgets. A JSON-command receives `JudgeInput` on stdin and returns `JudgeResponse`:

```json
{"judgments":[{"criterion_id":"quality","outcome":"pass","score":0.8,
"rationale":"The implementation satisfies the supplied rubric."}],
"usage":null,"observed_model":null,"observed_effort":null}
```

Missing judgments produce individual errors. Raw requests, responses and rationale
remain in optional grading evidence; structured results carry evidence hashes.

Role accounting retains subject, simulator and judge observations independently.
Token categories do not overlap; reasoning is an output subset. Responses are
deduplicated, cumulative observations become deltas, and resets retain observed
segments with partial coverage. Codex exec all-zero fallback usage stays unknown.
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
profile differences are allowed. Compatible partially completed runs retain
unstarted-trial attrition. The latest attempt per paired trial is selected,
including an unassessed retry as attrition, while all earlier spending stays in the
report. Two trials imply no significance.

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
`source-sessions` explicitly adds full source transcripts; curated session-ID
provenance does not automatically include them. Frozen selected input bytes retain
their original object IDs and hashes. Sensitive selection fails on an invalid
required closure; ordinary invalid-result summaries can still be exported.

Bundles are uncompressed tar archives with a strict `bundle.json` inventory,
canonical immutable manifests and exact hash-named blobs. Import rejects unsafe
paths, links, duplicates, unsupported schemas, altered bytes and incomplete or
extra closure content before publication. Existing objects are verified, never
overwritten. Optional exported aliases use `--alias NAME`; import defaults to
collision error, with explicit `--alias-policy skip` or `replace`. Export refuses
an existing destination and publishes only a complete archive.
