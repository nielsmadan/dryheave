# Development CLI and transport QA

Run date: 2026-09-09. Scope: the first-version CLI, native terminal integration,
retained grading evidence and tampering checks. These are manual runtime
observations from the development installation on macOS arm64. Clean-wheel QA
and actual Codex execution are recorded separately when performed.

The runner revision was `8a5f5bca5a9775600fca3ff72881c1dfc48ead40`. Assessment
checks exercised the in-progress Task 6 changes on that revision; an exact patch
identity was not recorded at execution time. Results below do not identify an
already released package. Python came from the checkout's `.venv/bin/python`.
The terminal driver was tui-test 0.1.0-beta.3.

## Inputs and invocation

All repositories, stores, controllers and deliberately corrupted bytes were
synthetic fixtures under the checkout's ignored `.cache/` directory. No user's
repository, transcript or installed configuration was mutated by these controls.
The subject fixture never launches the profile's executable.

The task fixes `normalize_label`: trim surrounding whitespace, collapse internal
Unicode whitespace, and raise `ValueError` for empty input. Its initial prompt
requires a clarification before editing; the simulator supplies the empty-input
rule. A separate hidden Python verifier checks ordinary, Unicode and empty cases
and emits `DRYHEAVE_LABEL_ALL_CHECKS_EXECUTED` after all checks pass.

The generated source history has one ancestor, a broken baseline and a satisfying
future/reference commit. The frozen case ID is
`e810860ebbf8a43fdb47633121fd57fc7386c8918e735586e6fe9d6cfb5b4fdc`.
The primary fixture run is `d76104c44a014a468e5de8a16d69e200`.

Commands used the public interface:

```sh
.venv/bin/python -m dryheave --store STORE --json assess RUN_ID
.venv/bin/python -m dryheave --store STORE --json report RUN_ID
.venv/bin/python -m dryheave --store STORE --json compare RUN_ID RUN_ID \
  --before-variant base --after-variant changed
.venv/bin/python -m dryheave --store STORE --json export RUN_ID --output results.tar
.venv/bin/python -m dryheave --store IMPORT_STORE --json import results.tar
```

Individual malformed-input tests intentionally expected nonzero exit codes. The
complete local command records retain stdout, stderr and exit status; the table
gives the observations needed to interpret them without those private runtime
directories.

## Observed results

| Scenario | Result | Runtime observation |
|---|---|---|
| Historical reconstruction | Pass | Materialized Git log contained precisely baseline and ancestor; known future lookup failed with exit 128; no remotes were configured. |
| Variants, repetitions and retry | Pass | Two variants × two repetitions produced four captures; explicit retry retained those captures and added a fifth attempt. |
| Assessment and calibration | Pass | All five fixture attempts finished, passed and remained eligible. Baseline failed and reference passed the hidden check, establishing demonstrated calibration. |
| Repeat assessment | Pass | A second `assess` returned the same five result IDs. |
| Paired comparison | Pass | Base versus changed selected two repetition pairs despite the additional retained retry. |
| Required assertion failure | Pass | A hidden script raising `AssertionError` produced task completion `fail`. |
| Missing execution marker | Pass | A script exiting zero with `NO_CHECKS_RAN` produced `indeterminate`, not a passing criterion. |
| Missing verifier runtime | Pass | An explicitly nonexistent interpreter produced `indeterminate`. |
| Future Git objects | Pass | A setup command fetched the generated reference commit into the owned trial. Quality passed, but the audit confirmed contamination and excluded the attempt. The fixture caused this; it is not evidence of agent intent. |
| Corrupted hidden input | Pass | After capture, the frozen verifier blob was actually replaced with a marker-only script. Its hash changed; normal loading rejected it. Assessment retained an invalid, indeterminate, excluded result, and summary export still succeeded. |
| Review boundary | Pass | Trying to dismiss a confirmed future-object finding returned a structured nonzero error. |
| Synthetic pricing | Pass | Two JSON-controller responses each reported 100 uncached input, 20 cache-read, 10 cache-write, 50 output and 15 reasoning tokens. Frozen example rates of 2/1/3/4 USD per million yielded 0.00045 per call and 0.0009 total; reasoning was not charged twice. These are synthetic observations and prices. |
| Partial quality judgment | Pass | A Python judge fixture returned one of two required rubric judgments. The deterministic check and readability judgment passed, the missing interface judgment became an error, and overall completion remained indeterminate. Its synthetic 0.0004 USD spend and 0.8 readability score were retained. |
| Active progress | Pass | While a Python controller slept with the writer lock held, another CLI returned status in 0.206 seconds and report in 0.198 seconds. Both showed the interacting attempt. |
| Ctrl-C | Pass | SIGINT to the owned runner retained one cancelled, cleanly captured attempt and one unstarted trial. The interrupted controller's unknown usage/cost stayed unknown. Grading the unchanged baseline produced failure. |
| Default bundle | Pass | An 81,920-byte archive imported into a fresh store; its portable report retained five attempts and two comparison pairs. Object kinds contained curated inputs and the portable report, with no capture or source-session object. |
| Explicit evidence bundle | Pass | A 747,520-byte archive additionally included captures and assessment evidence; import, report and comparison succeeded. |
| Bundle publication and aliases | Pass | Existing export destinations were refused. Alias collision failed by default; `skip` preserved the old target and `replace` selected the incoming target. |
| Malformed bundles | Pass | Unsafe path, duplicate member, changed blob and missing blob variants failed before immutable-object publication. The escape target was not created. |
| Incompatible comparison | Pass | Comparing the assessed fixture run to an unassessed native run was rejected. |

The assessment and bundle round-trip script completed 33 CLI commands; the bundle
error and review script completed 18. The interruption script used a Python
controller, with no model call. Detailed local evidence remains in
`.cache/qa-final-prep/{results,bundle-error}-{commands,summary}.json` and
`.cache/qa-interrupt-prep/{commands,summary}.json`. The partial judgment's inputs
and command evidence remain in `.cache/qa-judge-prep/`.

## Native launch failure and transport retest

The first allocated native attempt ran during Task 5 development, before the
runner commit above. Codex was requested at version 0.153.4 and low effort with
the normal model default, a trusted generated workspace and an opaque runtime
authentication-file binding. No credential contents were read or copied.

```sh
DRYHEAVE_QA_AUTH_PATH=/Users/nielsmadan/.codex/auth.json \
.venv/bin/python -m dryheave --store .cache/qa-final-prep/store --json \
  run native-qa-first --mode native \
  --tui-test .tools/tui-test/tui-test --runtime-root .cache/rt
```

Run `221ed46a911f43699cb97d9ce85d6300` retained a failed attempt. The driver
mistook subject arguments for its own flags and exited 2 with
`the argument '--config <PATH>' cannot be used multiple times`. The transport
then reported `Expected valid UTF-8 JSON.` Codex itself never started; there were
zero submitted or accepted turns and no observed model usage. Terminal closure,
known-writer stop and log drain succeeded; the runtime auth symlink was removed.

The argument boundary was fixed by inserting `--` before the subject executable.
A real tui-test retest delivered repeated `--config`, `--profile`, `--json` and
`--rows` arguments, including spaces and Unicode, exactly to a Python receiver.
It exited zero and stopped the terminal and known writers. A separate integrated
driver retest with synthetic native events observed ready → question → completed,
two accepted turns and complete known-writer cleanup/log drain.

These retests establish transport behavior with non-model subjects. The failed
start conservatively consumed one of the two allocated native launch attempts;
one remains for final installed-product QA.

## Limits

Same-day review correction: the initial grader accepted any nonzero verifier exit
as check failure, even without execution evidence. That could misclassify a
verifier syntax or dependency error. The implementation is being corrected to
require explicit evidence for failure as well as success. The table preserves
the behavior observed before that correction. A new verifier authoring file
prints `DRYHEAVE_LABEL_CHECKS_FAILED` only for a caught assertion/import/syntax
failure in its task checks; direct baseline/reference probes returned exit 1
with that failure marker and exit 0 with the existing success marker. Its case
has not yet been frozen or run through the corrected grader at this record's
current endpoint.

During this development pass, root found incorrect omission labels for individual
sensitive export options and a currency calculation that treated a proven-zero
unconfigured role as an additional unknown currency. After correction, public
captures-only and assessment-evidence-only exports each retained the appropriate
other-role omissions, and the synthetic priced report showed one observed cost,
zero unknown costs and a mean of 0.0009 USD. A local schema inspection also verified
that every property in the native judge response, nested judgment and token-usage
objects is required, with nullable fields representing missing observations.
No model was called for that schema inspection.

This record establishes development CLI behavior on this host. It does not
establish live model performance, Linux runtime compatibility, complete native
host isolation, or absence of unobserved detached processes. Raw fixture stores
remain ignored for investigation. No global agent session was killed or global
skill installation changed.
