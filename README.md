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

Follow the packaged [real workflow](src/dryheave/resources/examples/real-workflow.md)
(also written by `example write`) for complete JSON shapes and commands:

1. `collect scan/import/show` finds candidate log evidence; `case draft` requires
   an explicitly verified historical SHA. Curate facts, persona and hidden checks,
   then `case validate/freeze`.
2. `profile capture` freezes explicitly selected native config, instruction,
   skill and plugin bytes. `profile derive/diff` records one model, skill or
   workflow change while historical inputs remain immutable.
3. `experiment validate/create` pins case/profile/simulator/scoring IDs and
   repetitions. `run` executes fresh workspaces; `run --resume` reconciles durable
   progress, and explicit retries preserve all earlier attempts.
4. `assess` audits stopped outputs and runs hidden verifiers and optional judges.
   `report` and `compare` show eligible performance alongside attrition and
   spending for subject, simulator and judge roles.
5. `export/import` transports curated inputs and structured results. Raw captures,
   source sessions and grading artifacts require explicit sensitive-class flags.

```sh
dryheave doctor --agent codex --tui-test /path/to/tui-test --runtime-root rt --json
dryheave --store bench-store run EXPERIMENT_ID --mode native \
  --tui-test /path/to/tui-test --runtime-root rt --json
dryheave --store bench-store assess RUN_ID --json
dryheave --store bench-store compare RUN_ID RUN_ID \
  --before-variant base --after-variant changed --json
```

`doctor` checks availability and the pinned transport version without downloading,
launching agents, inspecting config/auth state or making model calls. Inspect
`data.ready` and each diagnostic; a successful diagnostic command can still report
missing tools. Preflight adds exact profile argv, native roots and fidelity issues.

## Optional operator skills

The wheel bundles three agent-neutral skills: `dryheave-collect`, `dryheave-case`
and `dryheave-results`. They guide the operator through evidence collection,
curation and analysis. Subject profiles disable these names by default.

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
skill names limit an operation; omission selects all three. Repeated installation
skips matching owned skills and refuses foreign, edited or outdated collisions.
Update/uninstall verify the per-skill ownership manifest and exact
bytes first, refusing edited, foreign, missing or unsafe content. Symlink target
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
[results and bundles](docs/tech/results.md), [drivers](docs/tech/drivers.md) and
[storage](docs/tech/storage.md).

## Development

Install uv, just and Lefthook, then run `just setup`. The checkout uses
`src/dryheave`, argparse, Pydantic 2, uv/Hatchling, Ruff, cyclic-import checks and
strict mypy. `just check` runs the full check/test gate; `just coverage` enforces
the separate 80% branch-coverage gate. `uv build` packages resources and examples.
CI and checkout-local Lefthook hooks use the same checks. Development stores,
fixtures, downloads and caches belong in ignored checkout-local directories.
