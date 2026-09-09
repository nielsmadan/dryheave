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
   counts, exclusions, duration/token distributions, `known_spend_by_currency`,
   `unknown_cost_records` and `partial_cost_attempts`. Role records retain subject,
   simulator and judge costs independently, including failures and retries.
5. Compare `compare BEFORE_RUN AFTER_RUN --json`, or compare two variants within
   one run using `compare RUN_ID RUN_ID --before-variant base --after-variant changed --json`.
   Read `paired_count`, `excluded_before/after`, `pairs` and both metric lists.
   Require matching case/criteria/simulator/scoring/repetition/seed/mode identities.
   Present eligible performance beside attrition and spending across every attempt.
6. Export with `export RUN_ID --output results.tar --json`; import with
   `dryheave --store NEW_STORE import results.tar --json`, then inspect
   `report PORTABLE_ROOT_ID --json` using `data.roots[0]` from import. Default
   exports omit raw captures, source transcripts and grading evidence. Select
   `--include-sensitive captures`, `assessment-evidence` or `source-sessions`
   only when those exact bytes are authorized for sharing.

Audit exclusions remain evidence. A reviewed suspected finding can use
`review RUN_ID --attempt ATTEMPT_ID --finding FINDING_ID --decision dismiss --reviewer NAME --reason TEXT`;
review never repairs corrupted inputs. Missing native telemetry is partial coverage,
not evidence of no access or zero spending. Saved price rates are estimates for
their declared version/date/currency. Small samples establish no significance.

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
