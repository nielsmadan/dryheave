---
name: dryheave-case
description: Curate Dryheave case and persona JSON, freeze historical repositories, and capture selected native profiles. Use when asked to prepare a benchmark case, turn an imported session into a reusable task, or freeze a subject configuration for comparison.
---

# Prepare a frozen case and profile

## Instructions

1. Read `dryheave case draft --help`, then create an editable draft:
   `dryheave --store STORE case draft SESSION_ID --repo SOURCE_REPO --commit FULL_SHA --start EVENT_ID --end EVENT_ID --out case.json --json`.
   Supply the verified full starting SHA explicitly. Use `--initial-patch PATH`
   only for verified initial uncommitted work. Source repositories remain read-only.
2. Review title, `initial_prompt`, event range and `evidence`. Set
   `intent_confirmed` and `facts_reviewed` only after review. Resolve each
   `unresolved_issues` entry before clearing it. Keep solutions, future messages,
   hidden tests and reference patches out of subject-visible content.
3. Curate `allowed_facts` with `fact_id`, text, disclosure policy and exact
   subject-safe evidence, or explicit `curator_authored: true`. Draft a persona
   with `persona draft SESSION_ID --out persona.json`; fill `instructions`,
   `disclosure_policy`, `unknown_answer_policy`, review examples and set
   `reviewed_subject_safe: true`. Freeze with `persona create persona.json --json`
   and set the case's `persona: null` and `persona_id` to `data.id`.
4. Add at least one required substantive criterion. A deterministic criterion
   needs `entrypoint`, `command.argv` containing the exact element
   `{verifier}/check.py`, `expected_stdout`, and preferably distinct
   `expected_failure_stdout`. Map `hidden_files["check.py"]` to the verifier's
   selected source path, relative to the draft. Emit success only after all
   checks pass; catch only expected assertion failures and emit the failure marker
   there. Keep imports/setup outside that wrapper. Missing runtime, syntax/import
   errors or absent markers must remain errors. A rubric criterion uses
   `kind: "judge"`, `criterion_id`, `required` and a substantive `rubric`.
5. Look through the session for the author's follow-ups after the subject's
   work — skill invocations such as `/code-review` or `/qa`, manual testing they
   reported back (what they actually observed running the result), docs
   requests, re-reviews, and how they answered any skill's dialogs — and
   encode them in order as `workflow`. A `PromptStep` sends a follow-up's
   prompt verbatim, with an `answer_hint` guiding how the simulated user
   answers a dialog it triggers; an `InspectStep` names the criteria the
   author was actually checking at that point. Write a dedicated, observable
   inspect criterion for this — what the user would see using the result,
   never a file, type or symbol the subject has not mentioned — kept separate
   from the correctness criterion in step 4, which stays held out for final
   grading. Ask the author when a follow-up's intent is unclear rather than
   guessing one.
6. Add optional `checks` for binary journey/end-state outcomes a rubric score
   does not capture: each needs a unique `check_id`, a yes/no `question`
   (judged from the final files plus a transcript of the subject's turns and
   tool actions; each action carries at most the first 600 characters of its
   input, and over the judge's input bound actions from the middle, then all
   entries, are dropped), `expect` (`"yes"` or `"no"`), and an optional
   `only_if` naming an earlier check's `check_id` and `answer` so a later check
   only applies when that answer was given (any other answer, including
   `unclear` or none, reports `not_applicable`). The judge receives only each check's `check_id` and
   `question`, never its `expect` or `only_if`. A case with checks (or judge
   criteria) needs a judge: `bench` refuses to run one without
   `--judge MODEL[@EFFORT]`.
   Add `metrics` entries (`MetricsCommand`, each with a unique `metrics_id`)
   to measure things a script can count instead of judging them, e.g. a test
   run or a line count. Give each a substantive `description`, `entrypoint` mapped in
   `hidden_files` like a verifier, and `command.argv` containing the exact
   element `{verifier}/ENTRYPOINT`. The frozen command runs in a copy of the
   captured final files (paths the capture policy excludes are absent) with the
   case's declared environment references, `HOME`, `PATH`,
   `DRYHEAVE_BASELINE_DIR` (a read-only copy of the baseline, for diffing) and
   `DRYHEAVE_METRICS_OUTPUT` (a file path) in its environment. The script writes exactly one JSON
   object to the file at `$DRYHEAVE_METRICS_OUTPUT`:
   `{"metrics": [{"metric_id": "tests_passed", "label": "Tests passed",
   "value": 12, "unit": "tests", "better": "higher"}, ...]}` — at most 64
   entries, labels of at most 200 characters, string values of at most 500,
   metric IDs unique across all of the case's metrics commands; `value` is a number, boolean or string, `unit` and `better`
   (`"higher"`/`"lower"`/omitted) are optional. stdout/stderr are free-form
   logs only. A non-zero exit, timeout, invalid JSON or a contract violation
   is recorded as a metrics error and never crashes the assessment, so a
   failing build can still report `build_succeeded: false` deliberately
   rather than erroring.
   Three default rubrics (`code-quality`, `codebase-consistency`,
   `test-quality`) grade every case once it has a judge, with no entry needed.
   Override one's text for this case by adding a `RubricCriterion` with the
   same `criterion_id`; this case's `required` then applies instead of the
   default. Drop one entirely by listing its id in `disabled_rubrics` (only
   those three ids are accepted there).
7. Run `dryheave --store STORE case validate case.json --json`, then
   `case freeze case.json --json` and `case inspect CASE_ID --json`. Save immutable
   IDs. Run `case calibrate CASE_ID --json` before subject spend. Inspect
   `data.calibration.status` and save `data.id` as CALIBRATION_ID. `demonstrated`
   means every deterministic criterion observed baseline failure and reference
   success; `ineffective` means a baseline passed; `unavailable` includes verifier
   errors or a missing reference; judge-only cases are `not_applicable`.
   Use `case calibration CALIBRATION_ID --json` and `--evidence PATH` from its
   `files` map to inspect outputs. Fix errors or ineffective criteria, freeze a
   new case and calibrate again. Verifier copies do not execute `case.setup`:
   required tools and declared environment references must already be available.
8. Copy the public examples with `dryheave example write --target FRESH_DIRECTORY`.
   Follow its `real-workflow.md` to select actual config, instructions and skill
   resources explicitly, then `profile capture NAME --spec capture.json --json`.
   Inspect frozen hashes and preflight issues. Derive one actual behavior change;
   the `workflow` field alone is only a label.

Use finite command and subject budgets. Credentials remain opaque runtime references;
never capture auth bytes. Native HOME normally stays intact for browser/device/tool
state. Fresh agent config roots do not isolate all native discovery; captured mode
reports unresolved sources and strict mode currently refuses them. Operator skills
are disabled for subjects by default; do not re-enable them accidentally.

## Examples

For “benchmark whether asking first helps”, curate a task with an unspecified
punctuation fact. Give the persona permission to disclose that fact on request.
Grade punctuation and empty-name behavior through a hidden verifier; retain a
separate reference patch for calibration. Capture the baseline instructions and
derive a variant replacing only their clarification policy, then freeze both
profile IDs into one experiment.

For a task that adds a parser function and its tests, keep the default
`code-quality` and `test-quality` rubrics, override `codebase-consistency`
with this project's specific naming/layout conventions, and add two checks:
`error-path-handled` ("Does the final code handle a malformed input without
raising?", expect `"yes"`) and `tests-cover-error-path` (`only_if`
`error-path-handled` = `"yes"`, "Do the added tests exercise the malformed
input?", expect `"yes"`). Add one `metrics` entry whose hidden script runs the
project's test command and reports `tests_passed`/`tests_failed` counts parsed
from its output, with `better: "higher"`/`"lower"` set accordingly; freeze and
calibrate, then run `bench CASE --variant base=MODEL --judge MODEL` so the
checks and rubrics are graded.

## Troubleshooting

- Validation reports unresolved intent/facts/persona: review the actual evidence;
  do not clear flags merely to make validation pass.
- Snapshot rejects shallow history, alternates or unsupported entries: supply an
  independently prepared complete supported source; never repair the original
  repository as an implicit authoring step.
- Preflight reports ambient sources or auth/permission uncertainty: retain those
  fidelity limits. `--strict` refuses them; it cannot prove full host isolation.
- Validation rejects a duplicate check id, an `only_if` referencing a check not
  declared earlier, or a `disabled_rubrics` id outside `code-quality`,
  `codebase-consistency`, `test-quality`: fix the declaration rather than
  removing the check or rubric. A metrics command that does not invoke its
  `entrypoint` with `{verifier}/ENTRYPOINT`, or whose description reads as a
  placeholder, is rejected the same way.
- `bench` refuses a case with checks or judge criteria when no `--judge` was
  given: pass `--judge MODEL[@EFFORT]` rather than dropping the checks, so
  they are not silently skipped.
- `bench` refuses a workflow-enabled run whose workflow has an inspect step
  but no judge or `--inspector MODEL[@EFFORT]`: pass one, or drop the step,
  rather than running with `--no-workflow` to sidestep the check.
