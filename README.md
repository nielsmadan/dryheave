# Dryheave

Dryheave turns coding-agent sessions into reusable interactive benchmarks. Import
Codex or Claude logs, curate a task and simulated user, freeze its historical Git
repository and selected native configuration, then compare repeated runs with
retained evidence. Subjects use their real terminal UI. Hidden checks and optional
model judgments run after the subject stops; reports keep audit eligibility,
quality, missing observations and spending separate.

Python 3.13+, Git, macOS or Linux are required. Native trials additionally require
a supported agent CLI, explicit authentication and **tui-test 0.1.0-beta.3**.
The offline example needs no agent installation or model calls. This is an alpha:
Codex 0.153.4 and Claude 2.1.263 log shapes have fixture coverage; Claude's live
TUI integration is unverified. Codex 0.154.0 has completed a live clarification
exchange at Terra low and high effort. Both subjects then requested design
approval and stopped without editing; successful live task completion remains
unverified. See [native setup and limits](docs/user/native-setup.md) and the
[dated QA records](docs/tests/qa-cli/).

## Install and try the complete offline loop

From a source checkout with [uv](https://docs.astral.sh/uv/) installed:

```sh
uv tool install .
dryheave --version
dryheave --help
dryheave example write --target .cache/example
python3 .cache/example/offline_demo.py \
  --dryheave "$(command -v dryheave)" --output .cache/demo
```

Both directories must be new. The script uses only the explicit installed CLI
for Dryheave operations. It creates a disposable synthetic repository with a
historical baseline and future solution, imports synthetic logs, curates a persona
and hidden checks, captures configuration and a skill, derives a model variant,
runs two repetitions per variant, assesses, compares and exports/imports results.
All Git mutations are confined to that new disposable repository. Its source
state is then checked unchanged. Generated files are ignored.

Open `.cache/demo/summary.json`, `report.json` and `comparison.json`. The summary
contains immutable IDs, input paths, exact installed executable/version and a
directory of argv/response records. The fixture returns two passing pairs with
zero model calls; synthetic subjects and saved example price rates establish no
real model performance or current market pricing. Repeating the example requires
a fresh output directory. Build a distributable wheel/sdist with `uv build`.

## Run your own benchmark

Start a dedicated workspace with `dryheave init ./bench`, enter it, then run
`dryheave skills install`. Workspace discovery also works from nested directories.
Setup creates private authoring/store/runtime directories without scanning agent
homes or making model calls. Native runtime paths must fit a 70-byte limit;
`init --runtime-root /short/new-directory` supplies a shorter location when needed.
See [workspace setup](docs/user/workspaces.md) for config, ownership and resume behavior.
Record transport once with `init --tui-test /path/to/tui-test`, or install
`tui-test` on PATH. Claude operators install with
`skills install --target .claude/skills`; the default target remains `.agents/skills`.

For selected-log mining, use `collect select NAME FILE --agent codex`, then
`problem request [DESCRIPTION] --selection NAME [--micro-bug]`. The packaged
voice/problem/profile skills curate internal drafts and retain supported tasks,
rejections and gaps. Follow [selected-log authoring](docs/user/selected-log-authoring.md)
for the exact commands and Codex/Claude invocation sequences; no hand-authored JSON is
needed for this operator-guided workflow.

Follow the packaged [real workflow](src/dryheave/resources/examples/real-workflow.md)
(also written by `example write`) for complete JSON shapes and commands:

1. `collect scan/import/show` finds candidate log evidence; `case draft` requires
   an explicitly verified historical SHA. Curate facts, persona and hidden checks,
   then `case validate/freeze` and `case calibrate CASE_ID --json` before subject
   spend. Inspect the standalone record with `case calibration CALIBRATION_ID`;
   `demonstrated` requires observed baseline failure and reference success.
2. `profile create NAME --agent codex|claude --model MODEL` freezes selected
   inputs without a JSON spec; legacy `profile capture` remains supported.
   Both freeze explicitly selected native config, instruction,
   skill and plugin bytes. `profile derive/diff` records one model, skill or
   workflow change while historical inputs remain immutable.
3. `experiment setup NAME --case CASE --profile base=PROFILE --simulator
   claude --simulator-model MODEL` pins a bounded adaptive controller without
   a JSON draft. Legacy `experiment validate/create` retains scoring/spec control.
   `run NAME --assess` captures and assesses fresh workspaces after owned cleanup;
   `run --resume` reconciles durable
   progress, and explicit retries preserve all earlier attempts.
4. `assess` audits stopped outputs and runs hidden verifiers and optional judges.
   `report` and `compare` show eligible performance alongside attrition and
   spending for subject, simulator and judge roles.
5. `view [RUN_OR_REPORT]` serves those results as a read-only local website. It
   binds loopback on an OS-selected port, prints the URL before opening anything,
   and runs in the foreground until Ctrl-C; `--open` also launches a browser.
   The packaged page covers runs, variant groups, attempts and retries, attempt
   detail with dialogue, patches, criteria, calibration and audits, plus role
   usage and cost. Viewing never runs, assesses, grades or calls a model.
6. `export/import` transports curated inputs and structured results. Raw captures,
   source sessions and grading artifacts require explicit sensitive-class flags.

Haiku profiles omit effort. Derive Sonnet with `--model MODEL --effort low`;
derive back to Haiku with `--clear-effort`. This is a configuration comparison,
not effort-only. Claude 2.1.278 controllers use tool-disabled print mode and the
named `CLAUDE_CODE_OAUTH_TOKEN` runtime reference; subjects remain native TUI.
Codex 0.154.0 controllers remain trusted-native, not tool-free. Setup never
inspects credentials or launches models; native prompts are never auto-approved.

```sh
dryheave doctor --agent codex --tui-test /path/to/tui-test --runtime-root rt --json
dryheave --store bench-store run EXPERIMENT_ID --mode native \
  --tui-test /path/to/tui-test --runtime-root rt --json
dryheave --store bench-store assess RUN_ID --json
dryheave --store bench-store compare RUN_ID RUN_ID \
  --before-variant base --after-variant changed --json
dryheave --store bench-store view RUN_ID --open
```

`view` exposes no write, run or grade routes, no CORS, no LAN binding and no
arbitrary filesystem paths. Requests need exactly the loopback `Host`, a supplied
`Origin` must match the viewer origin, and responses use fixed MIME types with a
restrictive CSP, `nosniff`, `no-store`, `no-referrer` and `frame-ancestors 'none'`.
Transcripts, patches and verifier evidence are served as JSON text and never
interpreted. Listings read a bounded journal prefix and mark a row `truncated`
instead of reading whole journals; retained detail is bounded per response and
reports `omitted`, `refused`, `unavailable` or `invalid` rather than guessing.
Bytes that are not valid UTF-8 come back base64-encoded, not lossily decoded.
Calibrations are reachable per run and per case, so an imported portable report
with omitted captures stays a valid degraded view. Local processes that can
already read the store are outside this boundary. See [viewer](docs/tech/viewer.md).

`doctor` checks availability and the pinned transport version without downloading,
launching agents, inspecting config/auth state or making model calls. Inspect
`data.ready` and each diagnostic; a successful diagnostic command can still report
missing tools. Preflight adds exact profile argv, native roots and fidelity issues.

## Optional operator skills

The wheel bundles six agent-neutral skills: `dryheave-collect`, `dryheave-case`,
`dryheave-results`, `dryheave-voice-profile`, `dryheave-generate-problem` and
`dryheave-agent-profile`. They guide evidence collection, voice/task curation,
profile setup and analysis. New CLI captures disable all six names when no
explicit list is supplied; old frozen recipes retain their historical defaults.

```sh
dryheave skills list --json
dryheave skills install --json
dryheave skills install --target .agents/skills --json
dryheave skills doctor --target .agents/skills --json
dryheave skills update --target .agents/skills --json
dryheave skills uninstall --target .agents/skills --json
```

Installation defaults to the initialized workspace's `.agents/skills`; use
`--target` to select another directory your agent discovers. Optional positional
skill names limit an operation; omission selects all six. Repeated installation
skips matching owned skills and refuses foreign, edited or outdated collisions.
Update/uninstall verify the per-skill ownership manifest and exact
bytes first, refusing edited, foreign, incomplete or unsafe content. Update adds
missing bundled skills to an existing target, including upgrades from three to six.
Symlink target
components are rejected. Save and review a conflict manually or select a new
target; no force mode discards it. See [skill ownership](docs/tech/skills.md).

## Evidence and repeatability limits

Source logs and repositories are read-only. Historical snapshots preserve exactly
the selected reachable Git ancestry while omitting later/unreachable objects,
other refs and remotes. Native execution isolates repository objects; it does not
restrict host, network, source-log, browser or device access. Audit detection is
partial and cannot establish the absence of unobserved access.

HOME stays native by default so normal tools can work. Fresh agent config roots
do not establish complete configuration/skill discovery. Captured mode records
unresolved ambient influences; strict mode currently refuses unresolved sources,
auth and permissions. Authentication remains explicit opaque runtime references.
A selected auth-file binding may refresh/write its original source only with the
recorded acknowledgement. No operation silently changes subscription login into
API billing or bypasses permission dialogs.

Task intent and historical baselines cannot always be recovered from a log.
Warnings and unresolved drafts remain visible. Native completion requires fresh
acceptance and turn lifecycle evidence; terminal silence or exit zero is not
task success. Missing usage remains null. Saved dated price tables produce labeled
estimates, observed subtotals can be incomplete, and small samples imply no
statistical significance. Browser/device state, live services, host tools and
unobserved descendants remain repeatability limits even with frozen input bytes.

## CLI and storage

`--store PATH` and `--json` work before or after subcommands. Store selection uses
explicit `--store`, then the nearest `dryheave.toml` workspace, then
`$XDG_DATA_HOME/dryheave` or `~/.local/share/dryheave`. JSON success uses `ok` and
`data`; errors use `ok: false` and `error.code/message` on stderr with nonzero exit.
Help/version remain plain text. Runs announce their ID on stderr at reservation.

Objects are canonical SHA-256 manifests and content-addressed blobs. Aliases point
to immutable IDs; moving one cannot change a frozen experiment. `store inspect`,
`store verify` and `store alias` expose these contracts. Hashes detect accidental
changes; they are not signatures or access control. Stores and even curated
exported inputs can contain sensitive task text and should be reviewed before sharing.

Read the contracts for [authoring](docs/tech/authoring.md),
[profiles](docs/tech/profiles.md), [runner](docs/tech/runner.md),
[results and bundles](docs/tech/results.md), [drivers](docs/tech/drivers.md),
[viewer](docs/tech/viewer.md) and [storage](docs/tech/storage.md).

## Development

Install uv, just, Lefthook and Node, then run `just setup`; `just doctor` reports
any of the four that is missing. Node runs the viewer's development-only module
tests and is needed by `just check`, never at runtime. The checkout uses
`src/dryheave`, argparse, Pydantic 2, uv/Hatchling, Ruff, cyclic-import checks and
strict mypy. `just check` runs the full check/test gate, including the viewer's
Node built-in module tests (`just test-js`, no npm dependency) and, through
`just test-packaging`, a built wheel; `just coverage`
enforces the separate 80% branch-coverage gate. `uv build` packages resources and examples.
Use `just install` to install or replace a CLI snapshot from the checkout,
`just install-editable` to link the installed command to source, and `just uninstall`
to remove the tool installation. CI and checkout-local Lefthook hooks use the same
checks. Development stores, fixtures, downloads and caches belong in ignored
checkout-local directories.
