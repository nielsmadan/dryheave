# Installed CLI and offline workflow QA

Run date: 2026-09-09. Scope: clean-wheel package resources, the public offline
benchmark workflow, skill ownership, historical reconstruction and input errors.
Procedure: [CLI and native terminal verification](../README.md).
Environment: macOS arm64, Python 3.13.6, isolated virtual environment inside this
checkout. No model was launched for these checks.

The base revision was `5569f890f14ca660eb5fb318039f04ad19e9a09c`, with Task 7
work in progress. The first wheel SHA-256 was
`f3661bbd641e0135545132658934f03bf1cbd4a06ebab5d5ee4ea44a305e7a7d`.
After the example correction below, the tested wheel SHA-256 was
`1235ce1637b544b40ad3ca2304a02e56cd6cb38efcf15cbaee8eb7918fffecea`.
Exact installed-file/resource hashes and copies of these wheels are retained in
ignored `.cache/qa-installed/`. Subsequent source corrections require their own
verification and are not silently attributed to these wheel hashes.

## Setup and entrypoints

`just build` built the wheel and source distribution successfully. The clean
installation used:

```sh
UV_CACHE_DIR=.cache/uv TMPDIR=.cache/tmp uv venv .cache/qa-installed/venv --python 3.13
UV_CACHE_DIR=.cache/uv TMPDIR=.cache/tmp uv pip install \
  --python .cache/qa-installed/venv/bin/python dist/dryheave-0.1.0-py3-none-any.whl
.cache/qa-installed/venv/bin/python -I -c 'import dryheave; print(dryheave.__file__)'
.cache/qa-installed/venv/bin/dryheave --version
.cache/qa-installed/venv/bin/dryheave --help
.cache/qa-installed/venv/bin/dryheave --store .cache/qa-installed/store --json \
  example write --target .cache/qa-installed/example-fixed
.cache/qa-installed/venv/bin/python .cache/qa-installed/example-fixed/offline_demo.py \
  --dryheave "$PWD/.cache/qa-installed/venv/bin/dryheave" \
  --output "$PWD/.cache/qa-installed/offline fixed ü"
```

The module path pointed into `.cache/qa-installed/venv/lib/python3.13/site-packages`,
not the source tree. All three skill files and both example resources were
readable through `importlib.resources` and hashed. Installed dependencies included
psutil 7.2.2 and Pydantic 2.13.5. The fixed example's store and output paths included
spaces and Unicode.

## Observations

| Scenario | Result | Runtime evidence |
|---|---|---|
| Package entrypoints/resources | Pass | Installed version/help worked; all five resources were available from the wheel. |
| Complete offline loop | Pass after fix | 33 Dryheave CLI commands completed collection, persona/case curation, profile variation, matrix execution, assessment, comparison and portable round trip. |
| Repetitions and comparison | Pass | Four fixture attempts, two eligible passing pairs, source repository preserved. Fixtures and price data were explicitly synthetic. |
| Historical cutoff | Pass | All four workspaces contained the exact two-commit baseline ancestry, had no remotes, and rejected lookup of the known future commit. |
| Existing output protection | Pass | Reusing the example-copy or demo-output destination failed; the prior summary bytes remained unchanged. |
| Frozen configuration | Pass | After changing the selected source instruction, profile inspection remained identical and materialization produced the original captured bytes. |
| One-input derivation | Pass | Deriving medium effort changed exactly the reported effort override while keeping frozen source bytes. |
| Ambiguous case | Pass | Missing repository snapshot and unconfirmed intent returned exit 2 with both specific validation messages. |
| Skills lifecycle | Pass | 19 CLI commands exercised all-skill and selected-skill install/list/doctor/update/uninstall, edited-file refusal, foreign-file/sibling preservation and symlink-target rejection. |
| Synthetic credential field | Pass | Capture rejected the fixture credential without echoing its value or publishing an immutable object. |
| Doctor and error streams | Pass | Pinned real tui-test reported a matching version; absent driver returned `ready: false`. Malformed JSON and unknown ID produced structured stderr errors with nonzero exits. |

The offline run was `7726254b8781436bb324f3e5487ddf98`, experiment
`4a6c17dcfd8ffd99be6f41535cf8aa199401db7e7d44ce307a8b4069ed9c083c`.
Case: `1b8b2b3c960495edb08dc1261ca8e0eef36cbc5338af7a90ff7a16622595a25e`.
Portable report: `3e4caede966c5bccf6a9df7db8def607c97c432e15281758677ac413cafdb635`.
The known future commit was `608d832e752838051dccec1ccd3c622188d1ef22`;
retained ancestry was baseline `7824a79c1a10c10ed5ea2e772f64f7a39dea75b4`
and ancestor `191fe5aab12ba523fb7643c8a89e44660b5346e5`.

## Found defect and retest

The first installed demo called profile preflight with a workspace directory it
had not created. The CLI correctly returned exit 2, `unsafe_path`, with
`Launch workspace must be an existing real directory.` The example consequently
exited 1 before any trial. Its resources and real-workflow instructions were
corrected to create the owned preview workspace first. A rebuilt/reinstalled
wheel completed the full example in a fresh destination.

Two root QA fixture corrections are retained in local evidence: `git cat-file -e`
returns 1 for the missing valid object ID, so the history probe switched to
`cat-file -t` for its expected exit-128 diagnostic; an invalid case draft was moved
back beside its hidden files so relative verifier paths resolved before checking
its intended baseline/intent errors. Neither required a product change.

## Coverage and retained evidence

Local evidence is under `.cache/qa-installed/`: the example's `summary.json` and
`commands/`; `wheel-lifecycle/{commands,summary}.json`; the final CLI/error/history
records; and the profile-immutability records. Deliberately edited skill fixtures
remain for inspection; clean owned installs were uninstalled. No global skills,
source repository history, real user configuration, or agent sessions were changed
by these offline checks.

Interrupted-process, verifier-failure, tampering, malformed-bundle and retry
controls have separate [development](2026-09-09-development.md) and
[review-fix](2026-09-09-review-fixes.md) runtime records, plus durable regression
fixtures linked from the procedure. They were not all repeated from this wheel.
The [native record](2026-09-09-native.md) reports the allocated real Codex attempt
and its trust-onboarding blocker. Linux execution and full native task completion
remain outside this record's observed coverage.

## Same-day corrected-package verification

After the trust encoding, superseded skill provenance, ownership-manifest and
final packaging changes, root refreshed the clean installation from wheel
`1d3ec50430ecffb4beae258e64ee2b75e91f55fbe0260ea1e733dd63c85959e1`.
The same installed-example command used fresh `example-complete` and
`offline complete ü` directories. All 33 CLI commands passed again: run
`07367c6e512b4422981197141ca47652`, experiment
`6c2bd5d1df28678dba0212db0e6e9015f309b31af5ca99811cc84aaf8778ec6f`,
four eligible passing fixture attempts and two pairs. The imported portable
report was `4037ae3b62222a077e58dcd25b6a4cec0259715977a5eea57af37066bf438117`.
The 19-command installed skill/error/doctor check also passed in
`complete-lifecycle`. Exact installed-file hashes and the wheel are retained in
`complete-identity.json` and `complete-wheel.whl` under `.cache/qa-installed/`.
This repeat launched no model and does not change the separate native result.

## Whole-project review corrections

After the recovery, comparison, profile, journal, accounting and archive fixes,
root built and installed wheel
`ae413d7eb495a7189cad15a247e4a1e040b20f77cdb8b1d2e2594c2dd34b7e1c`.
All 81 packaged files matched source, sdist and the non-editable installation;
the five bundled resources were present. The installed example completed all
33 commands in `offline final ü`: run `fcf30db5db4848ee97b10c2c5c91a2a4`,
four eligible passing fixture attempts, two pairs and preserved source state.
Portable report: `4ee857fc5ffc21f105962291cac6c4719927dff621091c7abcd386f5e80eca79`.

Additional installed CLI checks passed:

- The 19-command skills/error/doctor lifecycle check, with edited and foreign
  files preserved and symlink targets refused.
- A three-command regression selecting only `base` metrics before and `changed`
  metrics after, then rejecting an experiment with a required but absent judge.
- Two archive imports containing 1,200 empty PAX headers or a 1,536-byte file
  declaring 2 GiB of metadata. Both returned bounded structured errors and
  published zero objects.

Local evidence is in `.cache/qa-installed/` under `offline final ü`,
`wheel-final-lifecycle`, `wheel-final-regressions` and `wheel-final-tar`.
The exact wheel and package identity are retained as `pre-observation-wheel.whl`
and `pre-observation-package.json`. These checks precede a final focused Codex
judge response-metadata correction; its verification is recorded separately below.
No model was launched and the two-attempt native allocation remains exhausted.

## Final package verification

The focused Codex judge correction passed independent re-review. Its final wheel
SHA-256 is `4fb9abbb2f44d664022209f1a23d470c6e20b6f6d524ad2bf496f1f35ac66627`.
Root refreshed the same clean installation and verified all 81 package files
against source, wheel and sdist. `final-package.json` and `final-wheel.whl` retain
that identity under `.cache/qa-installed/`.

The installed example was copied to `example-release` and run with fresh output
`offline release ü`. All 33 commands passed: run
`3956b16bd71d4978938b981eedb7c1fc`, experiment
`3cb4d78681a5c4c41b1a02ed7098f0d6502d90e41f3ecd658c37645bccfa220f`,
four eligible passing fixture attempts, two pairs and unchanged synthetic source.
The imported portable report was
`08f0cf87ba701548168a756a493c757330d73d6eb2f4384af2d670afab452433`.

The 19-command lifecycle check, three-command comparison/required-judge check
and two malformed-archive CLI checks passed again in `release-lifecycle`,
`release-regressions` and `release-tar`. The archive checks published zero objects.
The exact helper commands used were:

```sh
.cache/qa-installed/venv/bin/python .cache/qa-installed/example-release/offline_demo.py \
  --dryheave "$PWD/.cache/qa-installed/venv/bin/dryheave" \
  --output "$PWD/.cache/qa-installed/offline release ü"
.cache/qa-installed/venv/bin/python .cache/qa-installed/lifecycle-smoke.py \
  "$PWD/.cache/qa-installed/venv/bin/dryheave" .cache/qa-installed/release-lifecycle
.cache/qa-installed/venv/bin/python .cache/qa-installed/final-regressions.py \
  "$PWD/.cache/qa-installed/venv/bin/dryheave" .cache/qa-installed/release-regressions \
  '.cache/qa-installed/offline release ü/summary.json'
.cache/qa-installed/venv/bin/python .cache/qa-installed/tar-boundary-cli.py \
  "$PWD/.cache/qa-installed/venv/bin/dryheave" .cache/qa-installed/release-tar
```

The helpers are retained local QA artifacts; the reusable public procedure and
shipped regression modules are linked above. No real model or agent executable
was launched by these checks. Full native completion remains unverified.
