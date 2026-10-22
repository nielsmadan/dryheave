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
verified bytes by logical filename. `read_blobs(reference, names=None)` verifies
the closure once per scoped batch, then hashes the actual returned bytes. It does
not cache verification across subject execution. `read_evidence` verifies an
object’s own manifest/blob map and returns an explicit `dependency_error` if its
closure cannot validate; only audit/report code consumes this evidence boundary.
It does not authorize normal loading or grading of invalid dependencies. `verify(id)` checks the entire object closure.
It streams blob content and retains only hashes, so a closure check never
allocates blob bytes. `resolve` only resolves a name; it does not itself prove
integrity or existence.

`read_envelope(reference)` and `read_payload(reference, Model, kind=...)` read,
hash-check and canonicality-check only that object's own manifest, then validate
its payload. They deliberately do **not** verify the closure, so they prove
nothing about referenced inputs; a caller that needs those inputs proven still
calls `verify`, `get`, `load` or `read_blobs`. They exist for bounded catalogue
scans and read-only views where pulling an entire closure would be
disproportionate, and never for loading or grading decisions.
`read_blob_bounded(reference, name, limit=...)` returns one hash-checked blob
under an explicit caller limit without reading the object's other blobs or its
closure; a blob over that limit is a limit error, and a missing or unreadable
blob is an integrity error rather than an I/O failure.

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
`RunStore.inspect` returns a read-only, hash-validated complete event prefix without
locking, repairing a tail or trusting a racing checkpoint. Its handle refuses
writes. Status/report may inspect a run while its writer is active.
`RunStore.list_runs` returns the sorted published run IDs under `runs/`, ignoring
`.pending-*` staging directories and any other name, and an absent directory is an
empty listing rather than an error. `RunStore.read_metadata` reads and validates
only `runs/<run-id>/metadata.json`. `RunStore.summarize(run_id, limit=...)` adds a
bounded, hash-chained prefix of `events.jsonl` without copying events: it stops at
the last complete line inside the limit and reports `bytes_read` and `truncated`,
so a truncated summary's event count is a lower bound and not the durable
sequence. It reads the journal prefix against the full limit first and only reads
`checkpoint.json` — charged against the same limit — when that prefix is truncated,
so a journal that fits is never charged for a checkpoint it will not return. That
checkpoint is identity-checked against the metadata and hash-checked when its
sequence falls inside the prefix; past the prefix its hash cannot be checked, so it
serves only bounded displays. Cheap bounded listings use it; anything needing the complete durable
prefix uses `inspect`, whose optional `limit=` lowers the read ceiling below
`MAX_JOURNAL_BYTES` so a caller refuses an oversized journal with `limit_exceeded`
before parsing it; `report_run(..., journal_limit=...)` passes the same ceiling
down.

`compaction.compact_run` holds the run lock, requires every attempt to be finished
and assessed with its capture (or quarantine) and assessment verifying, refuses
before deleting anything when `attempts/` or an attempt directory is a symlink,
then deletes each attempt's `workspace`, `assessment` and `regrades` directories
plus every `plugins`, `skills` and `cache` directory found at any depth inside
`profile` (never following symlinks) and every `auth.json` and `.credentials.json` there,
whether a regular file or a link (a link is unlinked and its target left alone),
and appends a `compacted` event listing them with the bytes freed. The rest of
`profile` — session logs, file history and backups — is kept. Nothing it deletes
is read after assessment: reports and the viewer use the journal and stored
objects, and a regrade rematerializes its grading workspace from the stored
capture.
`compaction.prune_checks` deletes `checks/<id>` directories (the evidence a failed
`bench` launch check leaves) whose modification time is older than the cutoff, so a
check still in progress is never touched.

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
`sequence` exposes the durable event count without copying events.
`events_since(sequence)` returns defensive copies of the subsequent events and
rejects cursors outside the current prefix. Assessment indexes use that suffix
to observe appends while preserving the read-only report's original prefix.

Process ownership is journaled as `owned-processes` attempt events whose data is
`{"identities": [ProcessIdentity, ...]}`: one to 1,024 identities that the attempt
has not recorded before, so the event count grows with publications rather than
with observed processes. `ProcessOwner.publish` hands every unpublished identity
to its callback as one batch. A forced publication, used when a command's direct
child is claimed, when the terminal reports its launch, before every simulator
call, and before and after cleanup, is written before the caller continues.
Polling publications are throttled to one
batch per second, so an attempt's ownership events stay bounded by its duration
and its forced publications. A descendant observed between throttled polls stays
in its owner and reaches the journal at the next due or forced publication. If
the runner dies in that window, reconciliation reaches it only through a recorded
ancestor that is still running, so a descendant orphaned within the last second
before a crash can be missed. Reconciliation stops every recorded identity whose creation time still
matches. Older journals hold one `owned-process` event per identity, with the
`ProcessIdentity` itself as data; both forms replay into `AttemptState.owned` in
journal order, and either marks the attempt unreconciled until its next cleanup
event. Existing journals are not rewritten.

`ExecutionJournal.save` checkpoints `{"attempts": {attempt_id: stage}}` after each
attempt-state event. Run-list rows whose journal exceeds the row budget take their
attempt counts from that checkpoint, overlaid with later `attempt-state` events in
the prefix, and report the sequence the counts are exact through.

Assessment events (`criterion-intent`, `criterion-result`, `check-result`,
`metrics-intent`, `metrics-result`, `judge-intent`, `judge-result`) written by a
regrade add a `regrade_id` key to their data. `ExecutionJournal` indexes them by
attempt, regrade scope and name; an unscoped journal never returns regrade events,
and a scoped one returns only its own regrade's events with that key removed.
Cleanup and ownership events are never scoped. Run-level `regrade-start`
(`regrade_id`, optional replacement `case_id`) and `regrade-finished` events
bracket a pass; an attempt-level `regrade-assessed` event records each new
`assessment_id` so a resumed pass skips attempts it already finished. Each
assessment a regrade produces also records that pass's `regrade_id` on itself, so
a resumed pass still recognizes an attempt as done from the attempt's current
assessment alone when the process died after saving it but before the
`regrade-assessed` event was appended, instead of chaining a second assessment
onto the first.

These checks detect corruption and serialize cooperating processes. They are not
a security sandbox: a native process with filesystem access can still alter data,
reach source repositories or access other host resources. Journal hashes are not
signatures. Immutable input checks and native audit remain separate concerns.

### Live config snapshots

A run with a live-config profile keeps one snapshot per agent beside its journal:
`runs/<run-id>/live-config/` for Claude and `runs/<run-id>/live-config-codex/` for
Codex. A `bench` launch check writes the same layout under `checks/<id>/`. Each
snapshot holds the copied entries, a `manifest.json` (`LiveConfigManifest`) and,
for Claude only, `state.json` with the shareable part of `.claude.json`. The
snapshot is staged as a `.partial` sibling and renamed into place once complete;
nothing rewrites it afterwards, including `manifest.json`.

When a run first takes a snapshot, it appends a run-level `live-config` event
whose data is the manifest's own serialization, the same form as `manifest.json`:
a Codex manifest carries `"agent": "codex"`, a Claude manifest omits `agent`, and
both omit `state_sha256` when it is unset. A manifest without `agent` parses as
Claude.
The fields are `schema_version: 1`, `source`, `entries`, `files`, `bytes`,
`tree_sha256`, `settings_sha256`, `instructions_sha256`, `skills`,
`enabled_plugins`, `mcp_servers`, `hook_events` and the optional `state_sha256`
(the hash of Claude's `state.json`). The first `live-config` event per agent is
the authoritative manifest: resume, every attempt's materialization and the
`bench` summary read it, never `manifest.json`. A snapshot directory found with
no event (a crash after the copy) is verified against its own `manifest.json`
before that manifest is journaled. Verification recomputes `tree_sha256` over the
entries, where each file contributes its content hash and each link its link
text, never its target's bytes (only Claude snapshots keep links). It also
checks `state.json` against `state_sha256`; a manifest written before
`state_sha256` existed is checked by its MCP server names only. Any mismatch,
missing file or swapped link is `snapshot_changed`.

Codex keys hook trust by the absolute path of the hooks file, so after copying a
Codex snapshot into an attempt, materialization moves each
`[hooks.state."<source>/…"]` table in the copied `config.toml` to the copy's path
with its `trusted_hash` unchanged. Hooks trusted live stay trusted and the rest stay
untrusted. Trust written as anything other than one table per entry is refused,
since the subject would otherwise stop at Codex's "Hooks need review" dialog.

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
not-found, conflict and I/O errors exit 1. Subclasses keep their parent's exit
code with their own `code`: `snapshot_changed` (a live config snapshot no longer
matches its journaled manifest) is an integrity error and exits 1, while
`missing_skill` (a Codex workflow skill absent from the config), `unsafe_path`
and `limit_exceeded` are input errors and exit 2. Pydantic diagnostics expose field
locations and validation codes, excluding supplied values. Help and version use
argparse's text output.

CLI store selection uses an explicit `--store`, then the nearest workspace's
`dryheave.toml` store path, then the XDG fallback. Configuration is a strict
versioned TOML boundary; relative paths resolve from that file's directory.
Workspace setup and directory ownership are separate from immutable object
storage. See [workspace setup](../user/workspaces.md).

## Filesystem portability

Directory traversal uses descriptor-relative operations with `O_NOFOLLOW` and
`O_DIRECTORY`. Ancestors use `O_SEARCH` on macOS or `O_PATH` on Linux. This permits
traversal without requesting ancestor directory contents. Only selected directories
are opened readably for enumeration or fsync. Missing directories are created only
after an actual not-found result. POSIX flock provides process ownership.

The macOS verification used CPython 3.13.6 and its exposed `os.O_SEARCH`. Linux
coverage runs in CI using the same tests and `os.O_PATH` fallback. Windows is not
a supported platform for this persistence implementation.

## Standalone calibrations

`ObjectKind.CALIBRATION` has a strict version-1 `CaseCalibration` payload and a
manifest reference to its exact frozen case. Its payload `files` map must match
the retained evidence blobs. The calibration loader checks ordered criteria and
verifier identities, baseline/reference results against retained output, and
resolved cleanup. These records do not create run journals or subject captures.
Exporting one requires `--include-sensitive calibration-evidence`; import verifies
the declared sensitive class and the full exact input/evidence closure before
publishing objects. Existing object kinds, frozen schemas and IDs are unchanged.
