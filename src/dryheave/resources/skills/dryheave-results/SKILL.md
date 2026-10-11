---
name: dryheave-results
description: Assess, report and compare saved Dryheave runs with retained audit evidence and role spending. Use when asked to analyze benchmark results, compare a model or skill change, resume assessment, or export a portable benchmark report.
---

# Analyze retained benchmark results

## Instructions

1. Inspect saved inputs with `dryheave --store STORE experiment inspect EXPERIMENT_ID --json`.
   Frozen case/profile/simulator/scoring IDs determine the comparison. An alias
   move cannot change historical runs. Repetitions require fresh workspaces.
2. For an authorized new trial, run
   `dryheave --store STORE run EXPERIMENT_ID --mode native --tui-test TUI_TEST_PATH --runtime-root SHORT_ROOT --json`.
   Inspect `doctor` and profile preflight first; native agent calls can spend money.
   Explicit `--mode offline-fixture` requires saved synthetic fixture turns and
   never measures a real model. Keep execution modes separate.
3. Inspect `run --status RUN_ID --json`; use `run --resume RUN_ID --json` to
   reconcile retained progress. `--retry TRIAL_ID` creates a new retained attempt
   and may spend again. Never resend ambiguous native input manually.
4. Run `dryheave --store STORE assess RUN_ID --json`, then `report RUN_ID --json`.
   `data.attempts[].result` carries assessment evidence; missing results remain
   pending. Read `data.groups` for scheduled/attempt/completed/scored/eligible
   counts, exclusions, duration/token (including cache read/write) distributions,
   `known_spend_by_currency`, `cost_by_role`, `unknown_cost_records` and
   `partial_cost_attempts`. Role records retain subject, simulator and judge
   costs independently, including failures and retries, each with the price
   table version/date it used. `groups[].criteria`, `.checks` (with
   `agreement`, the share of a case's repetitions matching the majority
   outcome) and `.metrics` summarize case-declared checks and metrics commands
   per case; `help_needed`/`simulator_replies` summarize how much the subject
   asked for versus was told. Three default rubrics (`code-quality`,
   `codebase-consistency`, `test-quality`) grade every case with a judge
   unless a case overrode or disabled one; `rubric_set` on each assessment
   records the default version used.
5. Compare `compare BEFORE_RUN AFTER_RUN --json`, or compare two variants within
   one run using `compare RUN_ID RUN_ID --before-variant base --after-variant changed --json`.
   Read `paired_count`, `excluded_before/after`, `pairs` and both metric lists.
   Require matching case/criteria/simulator/scoring/repetition/seed/mode identities.
   Present eligible performance beside attrition and spending across every attempt.
6. To re-grade existing captures without relaunching subjects — after a rubric,
   check or metrics-script change — run
   `dryheave --store STORE assess RUN_ID --regrade --json`, or
   `--regrade --case REPLACEMENT_CASE_ID --json` to grade against a
   replacement case that shares the original's `repository_id` and
   `initial_prompt`. Each regraded attempt gets a new assessment chained to
   its prior one via `previous_id`; `graded_case_id` is set only when a
   replacement case was used. An interrupted regrade resumes by re-running the
   same command; plain `assess` refuses while a regrade is unfinished.
7. Serve the results interactively with `dryheave --store STORE view RUN_ID --open`
   (Results tab for cost/tokens/turns/help-needed/criteria/checks/metrics per
   variant, Case tab for the frozen case, attempt detail for judge reasoning),
   or write one shareable file with
   `dryheave --store STORE report RUN_ID --html report.html`. Add
   `--include-sensitive assessment-evidence` to include judge rationale in the
   file, and `--force` to overwrite an existing path.
8. Export with `export RUN_ID --output results.tar --json`; import with
   `dryheave --store NEW_STORE import results.tar --json`, then inspect
   `report PORTABLE_ROOT_ID --json` using `data.roots[0]` from import. Default
   exports omit raw captures, source transcripts and grading evidence. Select
   `--include-sensitive captures`, `assessment-evidence` or `source-sessions`
   only when those exact bytes are authorized for sharing.

Audit exclusions remain evidence. A reviewed suspected finding can use
`review RUN_ID --attempt ATTEMPT_ID --finding FINDING_ID --decision dismiss --reviewer NAME --reason TEXT`;
review never repairs corrupted inputs. Missing native telemetry is partial coverage,
not evidence of no access or zero spending. Saved price rates are estimates for
their declared version/date/currency, not current market quotes. Small samples
establish no significance.

## Examples

For “did my new review skill help?”, capture the changed skill bytes as a derived
profile, hold all other inputs fixed, run repeated paired trials, and assess.
Report the paired sample count and quality changes, excluded/unstarted trials,
duration spread and each role's known spend plus unknown coverage. A synthetic
offline example demonstrates this workflow but supports no claim about model quality.

## Troubleshooting

- `assess` is interrupted: repeat assessment on the same run. It preserves
  finished IDs and marks ambiguous external calls as errors without relaunching them.
- No eligible pairs: inspect exclusion reasons and input compatibility; do not
  combine differing cases/scoring or dismiss tampering without evidence.
- Unknown/partial cost: retain nulls and observed subtotals. Do not substitute
  current web prices or count cached input twice.
- `assess` or a regrade with a different `--case` refuses while a regrade pass
  is unfinished: finish or resume that regrade first rather than starting a
  second grading pass on the same attempts.
- A regrade with `--case` rejects a replacement whose `repository_id` or
  `initial_prompt` differs from the original: pick a case that shares both,
  or regrade without `--case` to keep the original case.
