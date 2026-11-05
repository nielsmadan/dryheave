# Benchmark workspace setup

Create a dedicated directory for benchmark inputs and results. Source repositories
and selected session logs remain read-only references.

```sh
dryheave init ./bench
cd bench
dryheave store path
```

`init [PATH]` defaults to the current directory. It writes a private
`dryheave.toml`, a workspace ownership record and private owned authoring, store
and runtime directories, runs `git init` (unless it is already the top level of a
git repository; a workspace nested inside another repository gets its own) and writes a `.gitignore`. It neither scans agent homes nor
launches a model.
It then installs the packaged skills into the configured `.agents/skills` (Codex)
and into `.claude/skills` (Claude Code), installing missing ones and updating
outdated owned ones. A skill directory it cannot install, such as a foreign or
edited one, is reported and left alone; the other skills are installed and init
still succeeds. When
`[git] commit` is `ask` or `auto` and the repository has no commits yet, init
commits `dryheave.toml`, `.gitignore` and the installed skills as
`workspace: initialize`, the same way an `auto` checkpoint commits; this first
commit is the one commit `ask` makes without asking. A rerun in a repository
that already has commits treats the skills it changed like `dryheave update`
does, as a `workspace: update` checkpoint. Paths git ignores (for example a
`.claude/` line in your own `.gitignore`) are left out of either commit and
listed as `ignored_paths`.

Without `--json`, init prints a summary (repository and first commit, skill
folders, tui-test) and the next steps: enter the workspace, start Claude Code, Codex
or your coding agent and run the `dryheave-voice-profile` skill, with the Claude Code
(`/dryheave-voice-profile`) and Codex (`$dryheave-voice-profile`) invocations and the
folders other agents can load the skills from. `--json` returns the paths,
`skill_installs` (one entry per skill folder: its `target`, the skills it wrote in
`changed`, the skills it left alone in `conflicts` with a `reason`, and an `error`
when the whole folder failed), `git` (repository state, `gitignore`,
`gitignore_added`, `git_commit`), the checkpoint fields `commit_paths`,
`git_commit`, `message` and `commit` at the top level, and the same steps as
`next`.
`skills install` uses this workspace's `.agents/skills` directory, including from
nested directories. Explicit `--target PATH` remains available outside workspaces.
Select a directory your operator agent actually discovers.

The generated config contains:

```toml
schema_version = 2
authoring = ".dryheave/authoring"
store = ".dryheave/store"
runtime = ".dryheave/rt"
skills = ".agents/skills"

[git]
commit = "ask"
```

## Updating a workspace

After upgrading dryheave, run `dryheave update` anywhere inside the workspace. It
brings the scaffolding up to the installed version without touching the config,
store or your files:

- installs missing and updates outdated owned skills in `.agents/skills` and
  `.claude/skills`, leaving foreign or edited skill directories alone and naming
  them;
- appends missing `.gitignore` lines.

It then treats the files it changed like a checkpoint, with the message
`workspace: update`: `ask` prints the `git add`/`git commit` command for exactly
those paths, runnable from where you ran `update` (`--json` lists them as
`commit_paths`), `auto` commits them, `off` does neither. A workspace that is
already current prints `Workspace already up to date` and commits nothing. When a
skill folder is refused or fails, the header says the workspace was only partly
updated (or not updated) and names each refused skill. `--json` returns `path`,
`updated`, `gitignore_added`, `skill_installs` and the checkpoint fields.

## Layout and git

`.dryheave/` holds only the store, runtime and the authoring catalog. Everything
you or an agent author is visible at the workspace top level and meant to be
committed:

| Folder | Content |
|---|---|
| `voices/` | Voice drafts (`voices/NAME.json`; a taken name gets `-2`, `-3`, ...). |
| `cases/NAME/` | `case.json`, `reference.patch` and metric scripts. `case draft` writes `cases/NAME/case.json` by default (`--name NAME`, default `case-<session>`; `--out PATH` overrides). |
| `reports/` | `report RUN` writes `reports/RUN.json` and `reports/RUN.html`; `bench` writes the same for its run. `report --html PATH` keeps writing only that file. |
| `replays/` | Default `replay` output (`replays/<id>`). Scratch: git-ignored, never committed. |

`.gitignore` lists `.dryheave/`, the workspace ownership record
`.dryheave-workspace.json`, `__pycache__/`, tool caches and `replays/`;
re-running `init` appends missing lines without touching your own. Problem
decisions record `--draft` paths relative to the workspace root.

`init --git-commit ask|auto|off` (and `[git] commit`) controls the checkpoint
commands (`voice create`, `case draft`, `case calibrate`, `case freeze`,
`problem freeze`, `bench`, `report`, `update`):

- `ask` (default): nothing is committed (except init's first commit, above); the
  result lists `commit_paths` (workspace-relative POSIX paths) and the `message`
  so an operator can offer a commit.
- `auto`: the command commits only those explicit paths, taken literally, with a fixed message
  (`voice: create NAME`, `case: draft NAME`, `case: calibrate NAME`,
  `case: freeze NAME`, `bench: RUN`, `report: RUN`, `workspace: update`). The commit is built in a
  temporary index seeded from HEAD, so unrelated staged, intent-to-add or unstaged
  changes are left alone and your index is only touched, for those paths, after the
  commit lands (a failed index sync is reported as `commit.index_sync_error`).
  The commit's parent is the HEAD recorded before the index was seeded, and HEAD
  moves only if it still points there, so a commit you make from another terminal
  meanwhile is kept and the checkpoint fails instead of reverting it. The
  `pre-commit`, `prepare-commit-msg`, `commit-msg` and `post-commit` hooks run
  through `git hook run` with `GIT_EDITOR=:`; `post-commit` runs after your index
  is synced. This needs git 2.36 or later: on an older git every checkpoint fails
  with a message naming the version found. A pre-commit hook that unstages the
  paths leaves nothing to commit, and a commit-msg hook that empties the message
  aborts the commit.
  A merge in progress makes the commit fail,
  `case freeze` commits the draft plus the hidden files and reference patch it
  names (and, for a draft at `cases/NAME/case.json`, points the store alias `NAME`
  at the new case ID, replacing an earlier target and reporting `alias`; the alias
  lives in the store, is never committed, and `store alias set --replace` re-points it
  by hand), `case calibrate` commits the `case.json` and referenced files of `cases/<REF>/` only when REF is a case folder
  name (calibrating by case ID commits nothing), and nothing new to commit is a no-op. A failed commit
  (hook rejection, missing identity) is reported under `commit.state = "failed"`
  with the error; the command's own work stays done.
- `off`: nothing is offered or committed.

Without `--git-commit`, `init` asks when stdin and stdout are terminals (and
`--json` is absent); otherwise it uses `ask`. The question is an arrow-key menu
(↑/↓ or j/k move, a digit jumps, Enter chooses, Esc, q or Ctrl+C cancels init
before anything is written); once answered it collapses to one line such as
`✔ git commits: ask`. When stderr is not a terminal or `TERM=dumb`, it
falls back to a numbered prompt where end of input cancels; the same summary line
follows the answer. Re-running `init` on an existing workspace adds the repository,
`.gitignore`, the chosen mode and a new tui-test path (from `--tui-test` or an
install) without touching the store; a workspace with the old v1 config is not migrated.
A missing `git` executable is an error unless the mode is `off`.

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
When no supported tui-test is found, `init` on a terminal offers to install it or
stops ([native setup](native-setup.md#install-the-terminal-driver)). Otherwise set
`init ./bench --tui-test /path/to/tui-test` once, or set
`tui_test = "tools/tui-test"` in the workspace config. Relative transport paths
resolve from the config directory. New runs prefer explicit `run --tui-test`,
then workspace `tui_test`, then an installed `tui-test` on PATH. Without
`--tui-test`, `init` runs the found transport's `--version` to check it is
supported; an explicit `--tui-test` is recorded unchecked, and
`doctor --tui-test PATH` checks its version.
Offline fixture runs have no native runtime.
Omitted resume/status options keep the run's frozen settings even after config
changes; moving a workspace does not rewrite historical native runtime paths.

Claude Code finds the skills init installed in `.claude/skills` (invoke them as
`/dryheave-agent-profile`); Codex finds them in `.agents/skills` (`$dryheave-agent-profile`).

Repeated initialization of matching owned state leaves existing bytes untouched,
except outdated owned skills, which it updates.
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

`skills install` skips exact current owned skills. Edited or foreign
assets remain errors; intact older owned skills require `skills update --target
PATH`, or `dryheave update` for both workspace skill folders. See [skill ownership](../tech/skills.md) for lifecycle details.
