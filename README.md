# Dryheave

Dryheave builds reusable interactive coding-agent benchmarks from frozen inputs
and retained evidence. Import Codex/Claude logs, curate cases and personas, and
freeze exact historical repositories with their reachable Git ancestry. The core
also provides immutable storage, aliases and durable run journals. Capture selected
agent profiles, derive immutable variants, and prepare exact native launch plans
with explicit discovery and authentication limitations.

Requires Python 3.13 or newer. For development, install uv, just and Lefthook,
then run `just setup`, `just check`, and `just coverage`.

```sh
uv run dryheave --help
uv run dryheave --version
uv run dryheave --store .dryheave store path --json
uv run dryheave --store .dryheave store inspect <object-id> --json
uv run dryheave --store .dryheave store verify <object-id> --json
uv run dryheave --store .dryheave store alias set everyday <object-id>
uv run dryheave --store .dryheave store alias list --json
```

`--store PATH` and `--json` work before or after subcommands. The default store is
`$XDG_DATA_HOME/dryheave`, or `~/.local/share/dryheave` when XDG_DATA_HOME is unset.
JSON responses have `ok` and `data` fields. Errors have `ok: false` and an `error`
with a stable code and message. Errors go to stderr with a nonzero exit code.
Help and version stay plain text.

Objects are canonical SHA-256 manifests plus content-addressed blobs. Aliases
point to immutable IDs. Loading an object checks its content and references.
An object ID detects accidental changes; it is not a signature or access control.
Store artifacts can contain sensitive task evidence and should be kept private.

See [authoring](docs/tech/authoring.md) for collection commands, editable drafts,
historical snapshots and replay APIs. See [storage contracts](docs/tech/storage.md)
for immutable artifacts and recovery rules. See [profiles](docs/tech/profiles.md)
for capture/derive specifications, project overlays and native preflight. Build an sdist and wheel with
`just build`.

```sh
uv run dryheave --store .dryheave profile capture everyday --spec .cache/capture.json --json
uv run dryheave --store .dryheave profile inspect everyday --json
uv run dryheave --store .dryheave profile preflight everyday --destination .cache/trial/profile --workspace .cache/trial/repo --json
```

Profile preparation preserves native HOME by default and creates a fresh agent
config root. Captured mode reports unresolved ambient sources; `--strict` refuses
them. Preparation does not start an agent or bind authentication.

Freeze and execute experiment matrices with `experiment validate/create/inspect`
and `run`. Native execution uses the selected TUI and an explicit short runtime
root; `--mode offline-fixture` exercises the same runner without an agent launch.
`run --resume` preserves attempts, and `--retry TRIAL_ID` requests a new one.
See [runner contracts](docs/tech/runner.md) for controller JSON, budgets, native
setup, final captures and the pending-assessment recovery seam.
