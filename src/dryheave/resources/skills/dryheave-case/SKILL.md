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
5. Run `dryheave --store STORE case validate case.json --json`, then
   `case freeze case.json --json` and `case inspect CASE_ID --json`. Save immutable
   IDs. Validation establishes structure and evidence consistency; later assessment
   must demonstrate that baseline fails and reference passes.
6. Copy the public examples with `dryheave example write --target FRESH_DIRECTORY`.
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

## Troubleshooting

- Validation reports unresolved intent/facts/persona: review the actual evidence;
  do not clear flags merely to make validation pass.
- Snapshot rejects shallow history, alternates or unsupported entries: supply an
  independently prepared complete supported source; never repair the original
  repository as an implicit authoring step.
- Preflight reports ambient sources or auth/permission uncertainty: retain those
  fidelity limits. `--strict` refuses them; it cannot prove full host isolation.
