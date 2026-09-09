# Dryheave

Dryheave builds reusable interactive coding-agent benchmarks from frozen inputs
and retained evidence. This initial package provides validated artifacts, an
immutable store, aliases, and durable run journals.

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

See [storage contracts](docs/tech/storage.md) for the Python API and recovery rules.
Build an sdist and wheel with `just build`.
