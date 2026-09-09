# CLI and native terminal verification

Run this procedure after changing packaging, the public workflow, terminal
integration, or retained-result contracts. It checks an installed package and
records occasional runtime observations separately from the automated suite.

Use macOS or Linux with Python 3.13+, uv and Git. All generated inputs below live
inside an ignored directory in this checkout. Choose a fresh suffix for each run;
the example refuses to reuse its destination. No real model is required for the
offline procedure, and no global skills are installed.

## Clean installation and offline workflow

From the repository root:

```sh
just build
uv venv .cache/qa-installed/venv --python 3.13
uv pip install --python .cache/qa-installed/venv/bin/python dist/dryheave-0.1.0-py3-none-any.whl
.cache/qa-installed/venv/bin/python -I -c 'import dryheave; print(dryheave.__file__)'
.cache/qa-installed/venv/bin/dryheave --version
.cache/qa-installed/venv/bin/dryheave --help
.cache/qa-installed/venv/bin/dryheave doctor
.cache/qa-installed/venv/bin/dryheave example write --target .cache/qa-example
.cache/qa-installed/venv/bin/python .cache/qa-example/offline_demo.py \
  --dryheave "$PWD/.cache/qa-installed/venv/bin/dryheave" \
  --output "$PWD/.cache/qa-offline"
```

Configure `UV_CACHE_DIR` and `TMPDIR` to checkout-local directories if your
execution environment restricts writes elsewhere. `just` already supplies those
settings for its recipes. The printed module path must identify the clean
installation, and the generated example must invoke that same executable.

Inspect the example's JSON outputs and command record. Collection must retain
ordered evidence; frozen inputs must have stable IDs. Trials must be explicitly
labeled `offline-fixture`. Assessment must retain baseline/reference calibration,
comparison must show sample counts and attrition, and the imported portable
report must preserve the comparison. Synthetic subject behavior and prices do
not establish real model performance or current provider pricing.

Run the example a second time with its existing destination. Expect a nonzero
error and preserved prior outputs. Also exercise root help, a malformed JSON
input, an unknown object ID, and a store path containing spaces and Unicode.
Read complete stdout, stderr and exit status, including every reported failure.

## Skill ownership

Use a fresh target below `.cache/`; never use a real agent skill directory.
Run each command separately and inspect its complete response:

```sh
qa_cli="$PWD/.cache/qa-installed/venv/bin/dryheave"
qa_skill_root="$PWD/.cache/qa-skills"
mkdir "$qa_skill_root"
printf 'preserve this sibling\n' > "$qa_skill_root/foreign.txt"
"$qa_cli" --json skills list
"$qa_cli" --json skills install --target "$qa_skill_root"
"$qa_cli" --json skills list --target "$qa_skill_root"
"$qa_cli" --json skills doctor --target "$qa_skill_root"
"$qa_cli" --json skills update --target "$qa_skill_root"
"$qa_cli" --json skills uninstall --target "$qa_skill_root"
cat "$qa_skill_root/foreign.txt"
"$qa_cli" --json skills install --target "$qa_skill_root" dryheave-collect
printf '\nUser-owned addition.\n' >> "$qa_skill_root/dryheave-collect/SKILL.md"
"$qa_cli" --json skills update --target "$qa_skill_root" dryheave-collect
"$qa_cli" --json skills uninstall --target "$qa_skill_root" dryheave-collect
"$qa_cli" --json skills doctor --target "$qa_skill_root"
cat "$qa_skill_root/dryheave-collect/SKILL.md"
```

The first installation must list all three skills as `current`; its uninstall
must retain `foreign.txt`. The later update and uninstall must return nonzero and
retain the appended text. Skill doctor should report `conflict`; its own zero
exit status means the diagnostic ran, so inspect every skill status.

Repeat with a foreign file inside an otherwise unchanged skill directory, and
with a target reached through a symlink. Expect refusal with both the foreign
file and symlink destination preserved. The durable fixture for these variants is
[the skill lifecycle test module](../../../tests/test_skills.py). Save complete
responses and before/after hashes of bytes that must survive. Leave intentionally
edited fixtures for inspection; a user must resolve the conflict before an
ownership-aware uninstall can remove them.

## Recovery, verifier and contamination controls

The dated development records preserve the process-level fault injections used
in this build. Their local raw stores are investigative artifacts. The durable
fixtures for repeating those behaviors are in:

- [assessment recovery tests](../../../tests/test_assessment_recovery.py): observed detached child cleanup after an assessor exits, recovery before any new subject launch, and PID reuse protection.
- [native recovery tests](../../../tests/test_native_recovery.py): cleanup before corrupted input validation, cross-run ownership reconciliation and setup descendant persistence.
- [grading tests](../../../tests/test_grading.py): success/failure execution evidence, missing markers and signal exits.
- [integrity tests](../../../tests/test_integrity.py): suspected access review and immutable corruption boundaries.
- [assessment tests](../../../tests/test_assessments.py): retained calibration, future-object contamination and interrupted grading suites.
- [bundle tests](../../../tests/test_bundles.py): malformed input, explicit evidence selection, atomic publication and alias collisions.
- [report tests](../../../tests/test_reports.py): latest-attempt selection, unassessed retries, unknown spending and attrition.
- [role accounting tests](../../../tests/test_role_accounting.py): independently retained settings and native usage when simulator or judge responses fail validation.

Run these through `uv run pytest PATH -q` as needed; use `just check` for the
required complete check. Do not run suites concurrently: they share the configured
pytest temporary directory. Automated fixture results remain separate from
manual CLI and live terminal observations.

## Native Codex trial

Use the native setup and profile instructions in [the runner reference](../../tech/runner.md)
and [profile reference](../../tech/profiles.md). Pin the installed Codex version,
tui-test version, frozen case/profile/experiment IDs, and the exact native argv.
Use a small generated repository with a known baseline and future reference,
a calibrated hidden verifier, and an initial prompt requiring one clarification.
The scripted simulator may answer only that reviewed fact.

Native execution consumes model usage. Set a finite subject deadline and
simulator budget in the frozen inputs before launching, and keep an explicit
attempt allocation. A failed launch counts; retry is a new attempt. This build's
allocation was two attempts, 180 seconds and three simulated replies each.

Supply existing login only through the profile's explicit runtime reference.
Record the reference name and acknowledgement of possible native refresh writes;
never inspect or copy authentication contents. Keep ordinary native permission
settings. A permission or unsupported dialog is a retained stop, not a prompt for
the simulator to approve.

```sh
dryheave --store STORE --json run EXPERIMENT_ID --mode native \
  --tui-test /absolute/path/to/tui-test --runtime-root /short/owned/runtime
dryheave --store STORE --json assess RUN_ID
dryheave --store STORE --json report RUN_ID
```

Expect fresh acceptance of the initial message, an ordinary clarification,
acceptance of the approved simulated answer, and matching native completion.
After terminal and known-writer cleanup, inspect deterministic check outcomes,
audit eligibility, requested versus observed model/effort, per-role usage and
unknown costs. A successful driver exit alone does not establish task completion.

Containerless runs retain host access. Historical object isolation and tampering
detection do not establish complete isolation, complete telemetry, or ownership
of every unobserved detached process. Do not infer an A/B performance ranking from
a couple of smoke attempts or pair incompatible cases/execution modes.

## Recording and cleanup

Save a dated record under `runs/` with the wheel hash, revision and relevant local
changes, exact versions/commands/inputs, scenario results, failure evidence, and
cleanup observations. Keep raw transcripts, screenshots, stores and auth bindings
out of Git. Verify owned terminal and process cleanup; retain failed captures.
Remove only the fixture skills through their ownership-aware uninstall command.
Generated stores and virtual environments can remain ignored for investigation.

Recorded runs:

- [Development CLI and transport](runs/2026-09-09-development.md)
- [Assessment recovery and review fixes](runs/2026-09-09-review-fixes.md)

- [Installed CLI and offline workflow](runs/2026-09-09-installed.md)
- [Final allocated native Codex smoke](runs/2026-09-09-native.md)
