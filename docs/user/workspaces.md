# Benchmark workspace setup

Create a dedicated directory for benchmark inputs and results. Source repositories
and selected session logs remain read-only references.

```sh
dryheave init ./bench
cd bench
dryheave skills install --json
dryheave store path
```

`init [PATH]` defaults to the current directory. It writes a private
`dryheave.toml`, a workspace ownership record and private owned authoring, store
and runtime directories. It neither scans agent homes nor launches a model.
`skills install` uses this workspace's `.agents/skills` directory, including from
nested directories. Explicit `--target PATH` remains available outside workspaces.
Select a directory your operator agent actually discovers.

The generated config contains:

```toml
schema_version = 1
authoring = ".dryheave/authoring"
store = ".dryheave/store"
runtime = ".dryheave/rt"
skills = ".agents/skills"
```

Relative paths resolve from the config's directory, so moving a workspace moves
those defaults with it. Absolute paths stay absolute. Paths must be distinct and
cannot overlap, contain parent traversal or use `~`. Unknown keys, wrong types
and unsupported schema versions are errors. The nearest `dryheave.toml` wins;
a malformed nearest config stops the command rather than falling back to a
parent. Help and version do not load workspace config.

Store selection is explicit `--store PATH`, then workspace `store`, then
`$XDG_DATA_HOME/dryheave` or `~/.local/share/dryheave`. An explicit store path
resolves from the invocation directory. A malformed workspace config is still an
error when `--store` is explicit.

Native transport paths have a 70-byte limit including each attempt's slash and
ten-character directory name. `init` checks this before publishing ownership or
data. A long workspace path needs a shorter new runtime directory:

```sh
dryheave init ./bench --runtime-root /short/path/bench-runtime
```

Use a real short location that belongs to your benchmark; symlinks are rejected.
A relative `--runtime-root` is relative to the workspace being initialized.
The command reports the actual byte count and override instructions on failure;
it never relocates files automatically. UTF-8 path bytes count toward the limit.

New native runs use the workspace runtime unless `--runtime-root` is explicit.
`--tui-test PATH` remains required. Offline fixture runs have no native runtime.
Omitted resume/status options keep the run's frozen settings even after config
changes; moving a workspace does not rewrite historical native runtime paths.

Repeated initialization of matching owned state leaves existing bytes untouched.
An interrupted initialization resumes from its ownership record. Foreign or
edited config, mismatched directory ownership and symlinks cause errors and
preserve the conflicting files. Save/review those files or choose a fresh path;
there is no force/adopt mode. Cooperating initializers hold a directory lock; a
concurrent initializer reports `lock_busy` and can be retried after it finishes.
A killed process may leave a private `.dryheave-pending-*` staging directory;
it is never treated as published workspace data or removed by a later init.

You may edit config paths for subsequent commands. `init` then refuses the config
change instead of adopting new directories or overwriting that edit. Updating
launch defaults does not move stored objects. Keep authoring, store and runtime
data private when selecting replacement paths.

Repeated skill installation skips exact current owned skills. Edited or foreign
assets remain errors; intact older owned skills require `skills update --target
PATH`. See [skill ownership](../tech/skills.md) for lifecycle details.
