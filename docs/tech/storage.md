# Artifact and run storage

The dependency graph is `errors/constants → models/filesystem → serialization →
storage/journals → cli`. Internal modules never import the package root.
`filesystem.py` imports only standard-library modules and the shared errors.

## Immutable objects

```python
from pathlib import Path

from dryheave.models import ObjectKind, StrictModel
from dryheave.storage import ObjectStore


class Persona(StrictModel):
    instructions: str


store = ObjectStore(Path(".dryheave"))
identifier = store.put(
    ObjectKind.PERSONA,
    Persona(instructions="Ask for missing requirements."),
    files={"examples.txt": b"Can you clarify the input format?"},
)
restored = store.load(identifier, Persona, kind=ObjectKind.PERSONA)
store.set_alias("everyday", identifier)
```

`put(kind, payload, files=..., references=...)` accepts a StrictModel payload and
bytes for logical file paths. Domain services own concrete payload schemas and
use `load(reference, Model, kind=...)` to validate them again when loading.
`get(reference, kind=...)` returns the strict generic envelope. `read_blob` returns
verified bytes by logical filename. `verify(id)` checks the entire object closure.
`resolve` only resolves a name; it does not itself prove integrity or existence.

Every object is `objects/<id>/manifest.json` plus `blobs/<content-sha256>`. The ID
is SHA-256 of canonical manifest bytes: UTF-8 JSON, sorted keys, no whitespace,
finite numeric values and no ASCII escaping. The manifest includes `kind`, integer
`schema_version: 1`, `payload`, a logical filename-to-hash `files` map, and sorted
unique `references`. Blob names and contents affect identity. Equal blobs within
an object share a file. References must be explicit immutable IDs and must already
exist; domain services include every referenced immutable input in this closure.
The generic store does not infer references from arbitrary payload strings.

Unknown envelope fields/versions, duplicate JSON keys, noncanonical encodings,
unexpected object files, malformed hashes and altered/missing blobs are errors.
Logical file paths are normalized relative POSIX names without traversal, control
characters, backslashes, colons or file/ancestor collisions. Object and blob reads
reject symlinks and special files. The current per-manifest bound is 16 MiB and
per-blob bound 256 MiB. These bounds are shared constants and should be considered
when freezing capture policies in domain services.

Publication builds a private sibling directory, fsyncs content and directories,
then atomically renames it while holding the objects lock. An ordinary failure
removes its owned staging directory. A process killed before cleanup can leave
`.pending-*` directories; readers never treat them as published objects. Repeating
an identical put verifies the existing object instead of overwriting it.

`aliases.json` is separately mutable, versioned and atomically replaced under a
stable lock. Updating a different target requires `replace=True` or CLI `--replace`.
Deleting an alias preserves its old objects. Aliases cannot resemble object IDs.
Store reads do not create a missing store. New files/directories use 0600/0700.

## Runs and recovery

```python
from dryheave.journals import RunStore

runs = RunStore(store.root)
run_id = runs.create(experiment_id)
with runs.open(run_id, experiment_id=experiment_id) as journal:
    with store.native_lock():
        journal.append(
            "launch-intent",
            {"session": "unique-owned-terminal-session"},
            trial_id="trial-1",
            attempt_id="attempt-1",
        )
        journal.checkpoint({"stage": "launching"})
```

The run service verifies/freezes the experiment before `RunStore.create`. The
journal layer validates and pins the experiment ID without depending on a domain
service. `runs/<run-id>/metadata.json` contains this ID and an aware creation time.
`RunStore.open` holds an exclusive stable run lock for its entire context and
rejects a conflicting supplied experiment. All writes use the yielded journal.
`ObjectStore.native_lock` additionally serializes native subject execution across
all runs in that store. Acquire the run lock first, then the native lock. Locks
are non-reentrant and fail immediately with `lock_busy`; no stale lockfile deletion
is needed, since OS locks release when the owner exits.

`append(event, data, trial_id=..., attempt_id=...)` writes a canonical JSON line,
with monotonic sequence number, timestamp and the previous event's SHA-256, and
fsyncs before returning. The first event's previous hash is null. Events are bounded
to 1 MiB and journals to 128 MiB. An append I/O failure invalidates the handle;
release and reopen it to reconcile the durable journal before taking more actions.

`checkpoint(state)` and `write_result(state)` atomically replace their respective
JSON files with the run/experiment identity, durable sequence and event hash. Both
can be read back through `read_checkpoint` / `read_result`. Checkpoints may lag the
journal after interruption; the caller replays later events. A checkpoint ahead
of the journal, pointing to another run, or carrying a mismatched hash is corrupt.
The journal does not decide trial-stage transitions or launch/retry policies;
the deterministic runner owns that state machine using the shared TrialStage enum.

Recovery validates every complete line and only tolerates bytes after the final
newline. Those bytes are preserved as `torn-<id>.bin`, exposed as
`journal.recovered_tail`, and removed from the active journal under its run lock
before another append. A complete malformed line is never silently discarded.
Event snapshots returned to callers are copied so mutation cannot alter the
in-memory sequence used for later hash chaining.

These checks detect corruption and serialize cooperating processes. They are not
a security sandbox: a native process with filesystem access can still alter data,
reach source repositories or access other host resources. Journal hashes are not
signatures. Immutable input checks and native audit remain separate concerns.

## CLI registration and errors

Domain CLI modules import `CommandRegistry` from the low-level `commands.py`
module; `cli.py` preserves its existing public imports. Built-in registration
composes storage and authoring modules without domain services importing the CLI.
Add a parser with
`registry.add(name, help_text=...)`, configure argparse options, and associate a
handler with `registry.handler(parser, handler)`. Handlers take `(args, store)` and
return a JSON-compatible dictionary. `main(..., registrars=(register,))` composes
them. Domain services do not import cli.py.

Global `--store PATH` and `--json` options can appear anywhere before `--`. The
separator preserves following literal arguments. A successful response is
`{"ok": true, "data": ...}` on stdout. Failure is `{"ok": false, "error":
{"code": ..., "message": ...}}` on stderr. Input/argparse failures exit 2; integrity,
not-found, conflict and I/O errors exit 1. Pydantic diagnostics expose field
locations and validation codes, excluding supplied values. Help and version use
argparse's text output.

## Filesystem portability

Directory traversal uses descriptor-relative operations with `O_NOFOLLOW` and
`O_DIRECTORY`. Ancestors use `O_SEARCH` on macOS or `O_PATH` on Linux. This permits
traversal without requesting ancestor directory contents. Only selected directories
are opened readably for enumeration or fsync. Missing directories are created only
after an actual not-found result. POSIX flock provides process ownership.

The macOS verification used CPython 3.13.6 and its exposed `os.O_SEARCH`. Linux
coverage runs in CI using the same tests and `os.O_PATH` fallback. Windows is not
a supported platform for this persistence implementation.
