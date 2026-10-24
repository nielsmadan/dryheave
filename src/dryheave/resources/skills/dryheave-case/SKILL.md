---
name: dryheave-case
description: Curate Dryheave cases as phased facts, calibrate them, pilot them on a weak model, review and iterate, then freeze for the benchmark. Use when asked to prepare a benchmark case, turn an imported session into a reusable task, pilot or review a case, or freeze a subject configuration for comparison.
---

# Prepare, pilot and freeze a case

A case is a task plus the requirements the user knew they wanted. Requirements are
`allowed_facts` grouped into ordered `phases`. A run measures how much of the problem
the agent solves on its own against how much the user stand-in has to point out.
The process is: draft facts, calibrate, pilot, review, iterate, freeze, benchmark.
Work in an initialized workspace; commands find the store themselves.

## Instructions

1. Draft: `dryheave case draft SESSION_ID --repo SOURCE_REPO --commit FULL_SHA --start EVENT_ID --end EVENT_ID --name NAME --json`
   writes `cases/NAME/case.json` (omit `--name` for `case-<session>`; outside a
   workspace `--out PATH` is required). Take EVENT_ID from the `dryheave-collect`
   hand-off or `dryheave collect show SESSION_ID --json` (`data.session.events`);
   for a session in a selection, `dryheave collect evidence SELECTION --session SESSION_ID --kind user --json`
   also lists them. Supply the verified full starting SHA
   explicitly. Use `--initial-patch PATH` only for verified initial uncommitted work.
   Source repositories remain read-only. Offer the checkpoint (see Checkpoints).
2. Review title, `initial_prompt`, event range and `evidence`. Set
   `intent_confirmed` and `facts_reviewed` only after review. Resolve each
   `unresolved_issues` entry before clearing it. Keep solutions, future messages,
   hidden tests and reference patches out of subject-visible content.
3. Voice: resolve the stand-in's voice by name with `dryheave voice inspect NAME --json`
   (curate one with `dryheave-voice-profile` first). Set the draft's `persona: null`
   and `persona_id` to `data.voice.persona_id`.
4. Declare `phases`: an ordered list of names, earliest first (for example
   `["reproduce", "fix", "polish"]`). The runner works on the earliest phase that
   still has an unmet requirement, so order them by what the user would insist on
   first. Every phase needs at least one `requirement` fact.
5. Write `allowed_facts`. Each fact has:
   - `fact_id` and `kind`: `requirement` gates pass/fail and the independence score;
     `optional` is graded and raised but never gates; `context` is background the
     stand-in may share and is never graded (no phase, check or verifier).
   - `phase`: required for requirement and optional facts.
   - `text`: what the user knows, in plain prose. The stand-in sees it and voices it
     in its own words.
   - `check`: the precise pass condition, for the inspector and judge only. Write
     what a person using the result would observe, never a file, type or symbol
     the agent has not mentioned. The stand-in and the subject never see it.
   - `verifier`, instead of or beside a `check`: a hidden command whose result
     decides the fact (step 6).
   - `disclosure`: `on_request` (default; the stand-in shares it when asked) or
     `proactive` (not scored for independence).
   - `evidence` with exact subject-safe excerpts, or `curator_authored: true`.
   A fact needs a `check` or a `verifier`. A fact is one observable thing; split
   a text that bundles several outcomes.
6. A verifier has `command` (its `argv` must contain the exact element
   `{verifier}/ENTRYPOINT`), `entrypoint`, `expected_stdout`, preferably a distinct
   `expected_failure_stdout`, and optional `suite_id`. Map
   `hidden_files["check.py"]` to the script's path relative to the draft (keep it
   inside `cases/NAME/`). Emit success only after all assertions pass; catch only
   expected assertion failures, print the failure marker there and exit 1 (a failure
   marker with exit code 0 is an error, not a failure). Keep imports and setup
   outside that wrapper. During calibration, a missing runtime, syntax/import
   errors or absent markers mean the verifier is broken; once calibrated, a
   verifier that crashes on the agent's tree counts the fact as unmet. Put the
   known-good solution in `reference_patch` (a path relative to the draft) for
   calibration.
7. Look through the session for the author's follow-ups after the agent's work:
   skill invocations such as `/code-review` or `/qa`, docs requests, re-reviews, and
   how they answered a skill's dialogs. Encode them in order as `workflow` prompt
   steps: `step_id`, `prompt`, required `after_phase` (the phase whose requirements
   must all be met before the step is sent; steps must be in phase order) and
   optional `answer_hint` (how the stand-in answers a dialog it triggers). Claude receives the
   prompt verbatim; a Codex variant receives `/name args` as `$name args` and needs
   `skills/name/SKILL.md` in its live Codex config. Write skill steps as `/name`
   for every agent; a prompt starting with `$` or `!` is refused. There are no
   inspect steps: at every done point the inspector judges all requirement and
   optional facts. Ask the author when a follow-up's intent is unclear rather than
   guessing one.
8. Optional non-gating quality: `criteria` (`criterion_id`, `kind: "judge"`,
   `rubric`) score quality without deciding the outcome. Three default rubrics
   (`code-quality`, `codebase-consistency`, `test-quality`) grade every case once it
   has a judge; override one by reusing its `criterion_id` or drop it via
   `disabled_rubrics`. Add `metrics` entries (`metrics_id`, substantive
   `description`, `entrypoint` mapped in `hidden_files`, `command.argv` containing
   `{verifier}/ENTRYPOINT`) to count things a script can count. The script runs in a
   copy of the final files with `DRYHEAVE_BASELINE_DIR` and `DRYHEAVE_METRICS_OUTPUT`
   in its environment and writes `{"metrics": [{"metric_id": "tests_passed",
   "label": "Tests passed", "value": 12, "unit": "tests", "better": "higher"}]}` to
   that file (at most 64 entries). A failing script is recorded as a metrics error.
9. Validate and freeze: `dryheave case validate cases/NAME/case.json --json`, then
   `dryheave case freeze cases/NAME/case.json --json`. Freezing a draft at
   `cases/NAME/case.json` also points the alias `NAME` at the new case ID (replacing
   an earlier target) and reports it as `data.alias`; the commands take `NAME`.
   If `data.alias_error` appears, the case is frozen but the alias is not. When it
   says `NAME` already points at another kind of object (a profile or experiment
   alias), rename the case folder or move that alias first, as the message says;
   never `--replace` it. Only when the alias write itself failed (an I/O error),
   run `dryheave store alias set NAME CASE_ID --replace`. Offer the checkpoint.
10. Calibrate before any subject spend. Ask which judge model to use, then run
    `dryheave case calibrate NAME --judge MODEL[@EFFORT] --json`. `--judge` is required
    when any requirement or optional fact has no verifier; a case of verifier-only
    facts needs none. Calibration grades every requirement and optional fact on the
    baseline (it must be unmet) and on the reference patch (it must be met).
    `data.flagged` lists facts met on the baseline or unmet on the reference;
    `data.calibration.status` is `demonstrated` only when every fact behaves,
    `ineffective` when any is flagged, and `unavailable` when a fact got no verdict
    on a side with nothing flagged: read `facts[].calibration.{baseline,reference}.error`
    for a verifier (often a failure marker printed with exit code 0; see step 6),
    `facts[].{baseline,reference}_judgment` for a judged fact, and add a
    `reference_patch` when the reference side is missing. `data.next` names those
    facts. Inspect
    `dryheave case calibration CALIBRATION_ID --json` and `--evidence PATH` from its `files`
    map. Offer the checkpoint (in `auto` mode usually `nothing_to_commit`; in `ask`
    mode `commit_paths` lists the case files even when unchanged, so only commit
    them if `git status` shows a change). Fix flagged or unavailable facts (tighten the `check` or verifier), freeze a new case
    (which moves the alias), and calibrate again. Verifier copies do not run `case.setup`:
    required tools and declared environment references must already be available.
11. Pilot. Ask the user which model to pilot on. Suggest Claude Haiku 4.5 with no
    effort, because a weak model leaves the stand-in real work to do and shows
    whether the facts and wording hold up. For a hard case suggest a stronger one
    such as Sonnet at low effort: Haiku may fail for lack of capability, which says
    nothing about the case. A pilot is a normal bench run with one variant named
    `pilot` and one repetition, using the generous default budget:
    `dryheave bench NAME --variant pilot=claude-haiku-4-5 --judge JUDGE_MODEL --stand-in STAND_IN_MODEL --repetitions 1 --json`
    (for Sonnet, `--variant pilot=claude-sonnet-4-6@low`). Remember the `--judge`,
    `--stand-in`, `--inspector` and `--no-workflow` settings you chose: the benchmark
    must use the same ones. This spends
    money and needs the credential bench reports as missing; tell the user before
    running. Save `data.run_id` as PILOT_RUN. Offer the checkpoint for `bench`.
12. Review the pilot. Read `data.results.pilot.attempts[]` for `outcome`,
    `outcome_reason`, `independence`, `corrections` and `stop_reason`, and the
    per-fact `facts` tallies. Then read `dryheave transcript PILOT_RUN` (three lanes:
    the conversation the agent saw, what happened behind the scenes, and the gaps
    left at the end) or `dryheave view PILOT_RUN --open` and the Review card in
    Attempt detail. Check that the stand-in's wording sounds like the user, that
    each fact's verdict matches what you can see in the transcript, and that the
    gaps list holds nothing a missing fact should cover.
13. Iterate without paying for a whole new pilot where a replay is enough:
    - Voice wording is off: edit the voice, create it under a new name, then
      `dryheave replay PILOT_RUN --attempt ATTEMPT_ID --all-stand-in --voice NEW_NAME`
      shows the old reply beside the new one. `--call c-ID` replays one call. To
      adopt the new voice, set the draft's `persona_id` to `data.voice.persona_id`
      from `dryheave voice inspect NEW_NAME --json`, then freeze, calibrate and pilot
      again.
    - A fact is wrong, missing or mis-phased: edit the draft, validate, freeze
      (which moves the alias), calibrate again, then pilot again.
    - The inspector misjudged a fact: fix that fact's `check` and freeze the
      new case, then `dryheave replay PILOT_RUN --attempt ATTEMPT_ID --call j-ID --case NEW_CASE_ID`
      re-sends that inspection with the new check. Re-grade the final verdicts with
      `dryheave assess PILOT_RUN --regrade --case NEW_CASE_ID` when the final grading had it
      wrong.
    Any change to the case makes a new case ID, so the benchmark then needs a fresh
    pilot on the new case (step 11); replay and re-grade only tell you whether that
    is worth doing.
    Replay output goes to `replays/<id>` and is never committed.
14. A pilot that did not pass (`outcome` `fail` or `indeterminate`): say which, with
    its `outcome_reason`, and offer two ways forward. Iterate on the case (step 13)
    when the review shows a wrong fact, check or voice; or pilot again on a stronger
    model when the facts look right and the agent simply could not do the task.
    `--budget-from` accepts only a passed pilot.
15. Freeze: the case that passed the pilot is the one to benchmark; do not edit it
    afterwards (a different case, stand-in, judge, inspector or workflow no longer
    matches the pilot). Benchmark with the pilot as the budget source and floor:
    `dryheave bench NAME --variant base=MODEL --variant changed=MODEL --judge JUDGE_MODEL --stand-in STAND_IN_MODEL --budget-from PILOT_RUN --json`.
    It derives ceil(used x 1.5) + 5 turns, stand-in replies and inspections from the
    pilot, prints the budget before starting, and shows the pilot in the summary as a
    floor variant. Do not pass `--max-turns`, `--stand-in-replies` or
    `--max-inspections` with it. Continue with `dryheave-results`.
16. For profiles, copy the public examples with `dryheave example write --target FRESH_DIRECTORY`
    and follow its `real-workflow.md` to select actual config, instructions and skill
    resources explicitly, then `profile capture NAME --spec capture.json --json`. Derive
    one actual behavior change; the `workflow` field alone is only a label.

## Checkpoints

Commands that write authored files (`voice create`, `case draft`, `case freeze`,
`case calibrate`, `problem freeze`, `bench`, `report`) print `git_commit`
(`ask`, `auto` or `off`) and `commit_paths`. Act on `git_commit`:

- `ask`: offer to commit exactly the listed `commit_paths` with the message the CLI
  uses in `auto` mode: `voice: create NAME`, `case: draft NAME`, `case: freeze NAME`,
  `case: calibrate NAME`, `bench: RUN_ID` or `report: RUN_ID`. On a yes, run
  `git commit -m "MESSAGE" -- PATH PATH ...` (stage new files first with
  `git add -- PATH ...`). Never commit other paths or use `git add -A`.
- `auto`: the CLI already committed; report `commit.state` (`committed` with the
  `commit` hash, `nothing_to_commit`, or `failed` with its `error`; a failed commit
  leaves the command's work done).
- `off`: say nothing about commits.

`replays/` is scratch and is never committed.

## Examples

For “benchmark whether asking first helps”, curate a task with an unspecified
punctuation fact: a `requirement` fact with `disclosure: on_request` and a verifier
for punctuation and empty-name behavior, in one phase. Retain a reference patch for
calibration. Pilot on Haiku, read the transcript to see whether the agent asked
before the stand-in had to volunteer it, then benchmark a baseline and a variant
whose instructions replace only the clarification policy.

For a task that adds a parser function and its tests, use two phases, `parse` and
`tests`. Make `error-path-handled` a `requirement` fact in `parse` with a `check`
("A malformed input returns an error result instead of raising") and
`tests-cover-error-path` a requirement in `tests`. Keep the default
`code-quality` and `test-quality` rubrics, override `codebase-consistency` with this
project's conventions, and add one metrics script reporting `tests_passed`.

## Troubleshooting

- Validation reports unresolved intent/facts/persona: review the actual evidence;
  do not clear flags merely to make validation pass.
- Validation rejects a phase without a requirement, a fact naming an undeclared
  phase, a context fact carrying a phase, check or verifier, a requirement without
  a check or verifier, or a workflow step out of phase order: fix the declaration.
- Snapshot rejects shallow history, alternates or unsupported entries: supply an
  independently prepared complete supported source; never repair the original
  repository as an implicit authoring step.
- `case calibrate` refuses without `--judge`: some fact has no verifier. Pass
  `--judge MODEL[@EFFORT]` rather than adding placeholder verifiers.
- `bench` refuses a case with judge-graded facts or rubrics when no `--judge` was
  given: pass `--judge MODEL[@EFFORT]`.
- `bench --budget-from` refuses a pilot that did not pass, has more than one
  attempt, or used a different case, stand-in, judge, inspector or workflow setting: pilot
  again with the exact options the benchmark will use.
- Preflight reports ambient sources or auth/permission uncertainty: retain those
  fidelity limits. `--strict` refuses them; it cannot prove full host isolation.
- Credentials remain opaque runtime references; never capture auth bytes. Operator
  skills are disabled for subjects by default; do not re-enable them accidentally.
