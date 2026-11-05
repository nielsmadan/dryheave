# Dryheave

Dryheave turns coding-agent sessions into reusable interactive benchmarks. Import
Codex, Claude or Droid logs, curate a task and user stand-in, freeze its
historical Git repository and selected native configuration, then compare repeated
runs with retained evidence. Subjects use their real terminal UI. Hidden checks
and optional model judgments run after the subject stops; reports keep audit
eligibility, quality, missing observations and spending separate.

Python 3.13+, Git, macOS or Linux are required. Native trials additionally require
a supported agent CLI, explicit authentication and **tui-test 0.1.x** (0.1.0 or
later; `init` offers to install it).
The offline example needs no agent installation or model calls. This is an alpha:
Codex 0.153.4 and Claude 2.1.263 log shapes have fixture coverage; Claude's live
TUI integration is unverified. Codex 0.154.0 has completed a live clarification
exchange at Terra low and high effort. Both subjects then requested design
approval and stopped without editing; successful live task completion remains
unverified. Codex 0.162.1 bench subjects have fixture coverage only: no launch
check or trial has run against a real Codex 0.162.1 yet. See
[native setup and limits](docs/user/native-setup.md) and the
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

Start a dedicated workspace with `dryheave init ./bench2`. It prints a short summary
and the next steps: enter the workspace, start Claude Code, Codex or your coding agent
there and run the `dryheave-voice-profile` skill. Workspace discovery also works from nested directories.
Setup creates private authoring/store/runtime directories under `.dryheave/`, a git
repository and `.gitignore`, installs the operator skills into `.agents/skills`
(Codex) and `.claude/skills` (Claude Code) and, unless commits are `off`, makes the
repository's first commit, without scanning agent homes or making model calls.
Authored files live in visible `voices/`, `cases/NAME/` and `reports/` folders
(`replays/` is scratch, git-ignored); `init --git-commit ask|auto|off` (a menu on
terminals) sets `[git] commit` in `dryheave.toml`: `ask` reports
`commit_paths` for an operator to offer, `auto` commits just the files each
checkpoint command wrote, `off` never commits. Init's first commit is made in
`ask` and `auto` alike. Native runtime paths must fit a 70-byte limit;
`init --runtime-root /short/new-directory` supplies a shorter location when needed.
See [workspace setup](docs/user/workspaces.md) for config, ownership and resume behavior.
When no supported tui-test is found, `init` on a terminal offers to install it or stops
so you can install it yourself ([native setup](docs/user/native-setup.md#install-the-terminal-driver));
`init --tui-test PATH` records one that is not on PATH.
After upgrading dryheave, run `dryheave update` anywhere in the workspace: it
refreshes the skills in both folders and adds missing `.gitignore` lines, then
commits them as your `[git] commit` mode says.

For selected-log mining, use `collect select NAME FILE --agent codex` (or
`--agent claude`, `--agent droid`), then
`problem request [DESCRIPTION] --selection NAME [--micro-bug]`. The packaged
voice/problem/profile skills curate internal drafts and retain supported tasks,
rejections and gaps. Follow [selected-log authoring](docs/user/selected-log-authoring.md)
for the exact commands and Codex/Claude invocation sequences; no hand-authored JSON is
needed for this operator-guided workflow.

## Walkthrough: from logs to a benchmark

This runs the whole process in a fresh workspace. A case is a task plus the
requirements the user knew they wanted, written as facts grouped into ordered
phases. A run measures how much of the problem the agent solves on its own against
how much the user stand-in has to point out. Placeholders in capitals are yours to
fill in. Steps 1 to 3 make no model calls. Calibrating judged facts, the pilot and
the benchmark call models: they need `CLAUDE_CODE_OAUTH_TOKEN` (the stand-in and
judge are Claude), tui-test for the native subject, and Codex authentication for
`codex:` variants; `bench` names what is missing before it spends anything.

1. Create the workspace; it installs the operator skills. `--git-commit` decides
   what the checkpoint commands do with the files they write: `ask` reports
   `commit_paths` for an operator to offer, `auto` commits them, `off` never does.

   ```sh
   dryheave init ./bench2 --git-commit ask
   cd bench2
   ```

2. Select the sessions that show how you work and curate a voice from them. The
   `dryheave-voice-profile` skill fills in `voices/me.json`.

   ```sh
   dryheave collect scan --agent codex --root /path/to/session-logs
   dryheave collect select work /path/to/session.jsonl --agent codex
   dryheave voice draft --selection work --name me --json
   dryheave collect selections --json
   dryheave voice create me voices/me.json --expect-revision REVISION
   ```

3. Draft a case from one session at a verified starting commit. It lands in
   `cases/my-case/case.json`. Set `persona` to null and `persona_id` to the voice's
   `data.voice.persona_id` from `voice inspect`, review the intent, then declare `phases` and facts. Each requirement
   fact names its phase and carries a `check` (the exact pass condition, seen only
   by the inspector and judge) or a hidden `verifier`; its `text` is what the
   stand-in knows. `workflow` prompt steps name the `after_phase` they follow. The
   `dryheave-case` skill walks through every field.

   ```sh
   dryheave collect selection work --json
   dryheave collect evidence work --session SESSION_ID --kind user --json
   dryheave voice inspect me --json
   dryheave case draft SESSION_ID --repo /path/to/source-repo --commit FULL_SHA --start EVENT_ID --end EVENT_ID --name my-case
   ```

   ```json
   {
     "phases": ["fix"],
     "allowed_facts": [
       {"fact_id": "empty-name", "kind": "requirement", "phase": "fix",
        "text": "An empty name should still greet with a trailing space.",
        "check": "Calling greet with an empty name returns 'Hello, ' and does not raise.",
        "curator_authored": true}
     ],
     "workflow": [
       {"step_id": "review", "prompt": "/code-review", "after_phase": "fix"}
     ]
   }
   ```

   Then validate and freeze. Freezing `cases/my-case/case.json` also points the alias
   `my-case` at the new case ID (replacing an earlier target), so the other commands
   take `my-case`. (`dryheave store alias set my-case CASE_ID --replace` re-points
   it by hand.)

   ```sh
   dryheave case validate cases/my-case/case.json
   dryheave case freeze cases/my-case/case.json --json
   ```

4. Calibrate before any benchmark spend. Every requirement and optional fact is
   graded on the baseline (it must be unmet) and on the reference patch (it must be
   met). Facts without a verifier are judged, so name a judge; a case whose facts
   all have verifiers needs none. Inspect `data.flagged` and
   `data.calibration.status`, and fix flagged facts before going on. `unavailable`
   with nothing flagged means some fact got no verdict on a side: `data.next` names
   it, and `facts[].calibration.{baseline,reference}.error` says why for a verifier.
   A failing verifier must print its failure marker and exit nonzero; the marker
   with exit code 0 is an error.

   ```sh
   dryheave case calibrate my-case --judge claude-sonnet-4-6@low --json
   dryheave case calibration CALIBRATION_ID --json
   ```

5. Pilot on a weak model. A pilot is a normal `bench` run with one variant named
   `pilot`, one repetition and the generous default budget (60 turns, 40 stand-in
   replies, 20 inspections). Haiku 4.5 is the default suggestion; for a hard case
   pilot on `pilot=claude-sonnet-4-6@low`, so a failure points at the case rather
   than at a model that cannot do the task. Use the stand-in (`--stand-in`) and
   judge you will benchmark with (and the same `--inspector` and `--no-workflow`
   settings, if you use them).

   ```sh
   dryheave bench my-case --variant pilot=claude-haiku-4-5 --judge claude-sonnet-4-6@low --repetitions 1 --json
   ```

6. Review what happened. `transcript` prints the three lanes (the conversation the
   agent saw, the stand-in's decisions, inspections and corrections behind the
   scenes, and the gaps left at the end) with the call IDs that `replay` takes. The
   viewer shows the same lanes as the Review card in Attempt detail.

   ```sh
   dryheave transcript RUN_ID
   dryheave view RUN_ID --open
   ```

7. Iterate. A pilot that did not pass can be iterated on or re-piloted on a stronger
   model. Voice wording is tested without a new run; a misjudged fact is fixed in its
   `check`, then the inspection is replayed or the run re-graded; a wrong or missing
   fact is edited, re-frozen and re-calibrated before piloting again. Any change to the
   case, including a fixed `check`, makes a new case ID, so the benchmark then needs a
   fresh pilot on it; replay and re-grade only show whether that is worth doing.
   Adopting a replayed voice means setting the draft's `persona_id` to
   `data.voice.persona_id` from `dryheave voice inspect me-2 --json`, then freezing,
   calibrating and piloting again.

   ```sh
   dryheave replay RUN_ID --attempt ATTEMPT_ID --all-stand-in --voice me-2
   dryheave replay RUN_ID --attempt ATTEMPT_ID --call CALL_ID --case NEW_CASE_ID
   dryheave assess RUN_ID --regrade --case NEW_CASE_ID
   ```

8. Freeze and benchmark. The case that passed the pilot is the one to benchmark.
   `--budget-from` derives ceil(used x 1.5) + 5 turns, stand-in replies and
   inspections from the passed pilot, prints them first, and adds the pilot to the
   summary as a floor variant (`pilot (n=1)`). Use the same case, stand-in, judge,
   inspector and workflow as the pilot. `bench` writes `reports/RUN_ID.json` and `.html`.

   ```sh
   dryheave bench my-case --variant base=claude-sonnet-4-6@low --variant changed=claude-sonnet-4-6@high --judge claude-sonnet-4-6@low --budget-from RUN_ID
   dryheave report RUN_ID
   dryheave view RUN_ID --open
   ```

Outcomes read `pass` (every requirement met), `fail` (a requirement unmet, or the
turn or stand-in budget ran out) or `indeterminate` (disputed or unclear verdicts,
or a phase the harness never reached). The independence score is the mean credit
over the requirement facts in reached phases: `own` 1.0, `elicited` 0.75 (the
stand-in voiced it in an ordinary reply before any fix request), `corrected` 0 and `unmet` 0. The
`dryheave-results` skill explains the fact split.

Follow the packaged [real workflow](src/dryheave/resources/examples/real-workflow.md)
(also written by `example write`) for complete JSON shapes and commands:

1. `collect scan/import/show` finds candidate log evidence; `case draft` requires
   an explicitly verified historical SHA. Curate phases and facts (each requirement
   with a `check` or a hidden `verifier`), persona and optional `metrics`, then
   `case validate/freeze` and `case calibrate CASE --judge MODEL[@EFFORT]` before
   subject spend. Inspect the standalone record with `case calibration
   CALIBRATION_ID`; `demonstrated` requires every fact unmet on the baseline and met
   on the reference. The three default rubrics (`code-quality`,
   `codebase-consistency`, `test-quality`) score quality without gating and can be
   overridden by id or dropped via `disabled_rubrics`. See
   [case authoring](docs/tech/authoring.md).
2. `profile create NAME --agent codex|claude --model MODEL` freezes selected
   inputs without a JSON spec; legacy `profile capture` remains supported.
   Both freeze explicitly selected native config, instruction,
   skill and plugin bytes. `profile derive/diff` records one model, skill or
   workflow change while historical inputs remain immutable.
3. `experiment setup NAME --case CASE --profile base=PROFILE --stand-in
   claude --stand-in-model MODEL` pins a bounded adaptive controller without
   a JSON draft. Legacy `experiment validate/create` retains scoring/spec control.
   `run NAME --assess` captures and assesses fresh workspaces after owned cleanup
   (add `--workflow` for the inspection loop; `run` takes no pilot or budget flags);
   `run --resume` reconciles durable
   progress, and explicit retries preserve all earlier attempts.
4. `assess` audits stopped outputs and runs hidden verifiers and optional judges.
   `report` and `compare` show eligible performance, cost by role and total
   (an API-equivalent estimate with its price table version), tokens including
   cache, turns, time, outcome, independence and per-case criterion/metric summaries
   (including cross-repetition agreement) alongside attrition and
   spending for subject, stand-in and judge roles. `assess RUN_ID --regrade
   [--case CASE]` re-grades existing captures without relaunching subjects,
   optionally against a replacement case.
5. `transcript RUN_ID [--attempt ID]` prints an attempt as three lanes: the
   conversation the agent saw, what happened behind the scenes (stand-in
   decisions, inspections, corrections) and the gaps left at the end.
   `replay RUN_ID --attempt ID (--call ID | --all-stand-in) [--voice NAME]
   [--case CASE] [--out DIR]` (default `replays/<id>` in a workspace) re-sends stored stand-in or inspector requests with
   another voice or case and shows old output next to new, writing only to its
   own output directory. See [results](docs/tech/results.md).
6. `view [RUN_OR_REPORT]` serves those results as a read-only local website. It
   binds loopback on an OS-selected port, prints the URL before opening anything,
   and runs in the foreground until Ctrl-C; `--open` also launches a browser.
   The packaged page covers runs, variant groups, attempts and retries, attempt
   detail with dialogue, patches, outcome, independence, the per-fact split,
   criteria, metrics, the Review card, calibration, audits and judge reasoning in a Results tab (per-variant
   comparison) and a Case tab (the frozen case), plus role usage and cost.
   `report RUN_ID --html PATH` writes the same results as one self-contained
   file; several run IDs give one summary table across the runs. Inside a workspace,
   plain `report RUN_ID` writes `reports/RUN_ID.json` and `.html`. Viewing never
   runs, assesses, grades or calls a model.
7. `export/import` transports curated inputs and structured results. Raw captures,
   source sessions and grading artifacts require explicit sensitive-class flags.

A case captures one person's real workflow, not an idealized task: its `workflow`
field records the reviews, manual testing, QA and documentation habits they
actually followed, replayed as `prompt` steps, with facts inspected automatically at each
done point, until every requirement is met or a budget runs out. `bench CASE --variant NAME=MODEL[@EFFORT] [--judge
MODEL[@EFFORT]]` runs that workflow by default; `--no-workflow` sends only the
case's initial prompt, for a first-shot comparison against the full workflow.
`--inspector MODEL[@EFFORT]` runs the fact inspections with a separate model instead
of the judge's. `bench` resumes an unfinished earlier run of the same experiment,
and the experiment includes the built-in price table, so after an update that
changes that table the same command starts and pays for a new run, while the
earlier run stays reportable. Its JSON result's `live_config` maps each agent
with a live snapshot in the run, `claude` and `codex`, to the manifest recorded
in the run journal's first `live-config` event for that agent (`source`,
`entries`, `files`, `bytes`, `tree_sha256`, `settings_sha256`,
`instructions_sha256`, `skills`, `enabled_plugins`, `mcp_servers`,
`hook_events`), even when the run has only Claude variants. The map key names the
agent, so no value carries an `agent` key. A Claude manifest also carries
`state_sha256`, except in runs recorded before that field existed.

A variant is `NAME=[AGENT:]MODEL[@EFFORT][,-mcp:SERVER...]` with `AGENT` either
`claude` (the default) or `codex`, so `luna-high=codex:gpt-5.6-luna@high` runs
Codex (`--codex PATH`, default `codex`) beside Claude variants on the same case.
Any other letters-only prefix, such as `codx:` or `Codex:`, is refused as a
mistyped agent; a prefix with digits, dots or dashes is read as part of a Claude
model name instead, since a model ID such as a Bedrock
`us.anthropic.claude-sonnet-4-5-20250929-v1:0` can itself contain a colon. A
variant whose model, effort or `-mcp:SERVER` removal its agent's profile would
refuse is refused when parsed, with the variant's name.
Each agent's variants start from one snapshot of its live config (`~/.claude` or
`CLAUDE_CONFIG_DIR`; `~/.codex` or `CODEX_HOME`, without credentials or session
state). The Codex snapshot copies only `config.toml`, `AGENTS.md`, `skills`,
`rules`, `prompts`, `hooks` and `hooks.json`; plugins stay referenced by the
absolute paths already in `config.toml` rather than copied. A symlinked
top-level entry refuses the snapshot; links inside a copied entry are copied as
their target content, unless they lead back into the Codex config outside the
copied entries or resolve to a credential file, a credential, history, cache or
trust name, or anything under `.ssh`, `.aws`, `.gnupg`, `.config/gcloud`,
`.docker` or `.kube`, which refuses the snapshot. The Claude snapshot instead keeps every link as a link, and its
hashes cover the link text, not the target's bytes, so a change behind a link
goes undetected. Every copied Codex file is screened for credentials, by file
kind. `config.toml`, `hooks.json`, `hooks` and `rules` get the same check as
selected profile assets: a credential, history, cache or trust path, a literal
value under a credential name (env references such as `env_http_headers`,
`env_vars`, `env_key` and `*_env_var` are fine), a literal
`experimental_bearer_token` or token-named `http_headers` entry, a literal
value under a secret-named key (such as `HF_TOKEN`) in an `env` table or
`[shell_environment_policy.set]` (a `$VAR` value is fine), a URL carrying a
password, a credential-named query parameter in a URL, a literal credential assignment in
a hook or rule file, or token material refuses the snapshot. TOML table and key
names, such as a `[projects."…"]` path, are not scanned. `skills`, `prompts`
and `AGENTS.md` are prose: only realistic-length key material (an `sk-` key
needs at least 32 characters after its prefix) and credential file names
(`.env`, `*.pem`, `id_rsa*`, `credentials*.json`, `credentials`, `auth.json`,
`.git-credentials`, `.npmrc`, `.pypirc`, `.pgpass`) refuse them, so a short
token-shaped secret in prose is copied; a directory with such a name refuses the
snapshot too. `.git` directories in any entry, and `node_modules`, `.venv`,
`venv`, `__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache` and `.tox`
directories under `skills` and `prompts`, are left out of the snapshot and its
hashes.
A resumed run and every attempt refuse a snapshot whose files or shared state no
longer match the hashes in the run journal's `live-config` event. A run
recorded before the manifest carried `state_sha256` is checked against its file
hashes and its MCP server names only.
Codex variants run with `--sandbox danger-full-access` (recorded as launch issue
`codex-sandbox-disabled`) and the live approval settings; a snapshot whose
`config.toml` sets `approval_policy = "never"` adds launch issue
`codex-approvals-disabled`. `workspace-write` is
not used because it silently blocks writes outside the repository, such as
Xcode's DerivedData. Bench variants also trust the case workspace, so the case
repository's own Codex config, hooks and exec policies load without a sandbox;
trust a case repository as you would your own code. A live Codex subject
inherits the operator's environment without `CLAUDE_CODE_OAUTH_TOKEN`,
`ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`. Codex variants bind `auth.json`
by symlink from `CODEX_AUTH_FILE`, or from `auth.json` under `$CODEX_HOME` or
`~/.codex` when it is unset, the same Codex home the live snapshot reads; a
leading `~/` expands against the inherited `HOME`, a relative or `~user/` path
is refused, and so is a file that does not exist. The default comes from the
variant's auth reference (`default_source: "codex-home"`), not from the
variable's name. Bench checks it before the run and each binding resolves it
again, without writing the default into any environment and without reading the
file.
`-mcp:SERVER` removal is Claude-only. The stand-in, judge and
inspector stay Claude, so `CLAUDE_CODE_OAUTH_TOKEN` is always required. A
workflow command `/name args` reaches a Codex variant as the skill mention
`$name args`, and only when the Codex config has a skill of that name: a
directory up to six levels under `skills` (or the bundled `skills/.system`),
outside hidden directories, holding a `SKILL.md` whose frontmatter `name`, or
else the directory name, is `name`. A new run is refused before it is
reserved when the live config lacks it, and a resumed run when its snapshot
does. The launch check types each command in that form and clears it unsent, so
it cannot show that Enter submits a `$name` mention; watch the first paid
trial's review step.

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

`doctor` checks availability and the supported transport range without downloading,
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

In a workspace, `dryheave update` does the install and update for both skill
folders at once. Installation defaults to the initialized workspace's `.agents/skills`; use
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

Runs keep each attempt's working copies (the agent's workspace, its native profile and
the grading workspaces) next to the journal, about 700 MB per attempt for an Xcode
project. `store compact RUN_ID [--report DIR]` deletes the workspace, assessment and
regrade copies, plus the `plugins`, `skills` and `cache` directories inside the native
profile and every `auth.json` and `.credentials.json` file or link in it (never
a link's target), once every attempt is finished and assessed and its capture and
assessment verify; the profile's session logs, file history and backups stay, and
results, captures and evidence stay in the store, so reports, the viewer and
regrades keep working.
`store prune-checks` deletes failed launch-check evidence older than an hour.

Read the contracts for the [facts model](docs/tech/facts.md), [authoring](docs/tech/authoring.md),
[profiles](docs/tech/profiles.md), [runner](docs/tech/runner.md),
[results and bundles](docs/tech/results.md), [drivers](docs/tech/drivers.md),
[viewer](docs/tech/viewer.md) and [storage](docs/tech/storage.md). Design
rationale lives in [docs/decisions](docs/decisions/).

## Development

Install uv, just, Lefthook and Node, then run `just setup`; `just doctor` reports
any of the four that is missing. Node runs the viewer's development-only module
tests and is needed by `just check`, never at runtime. The checkout uses
`src/dryheave`, argparse, Pydantic 2, uv/Hatchling, Ruff, cyclic-import checks and
strict mypy. `just test` runs the unit suite only; tests that drive a real
process, socket or packaged CLI carry the `integration` marker and run through
`just test-integration`. `just check` runs the full check/test gate, including
both suites, the viewer's Node built-in module tests (`just test-js`, no npm
dependency) and, through `just test-packaging`, a built wheel; `just coverage`
enforces the separate 80% branch-coverage gate over the unit and integration
suites together. `uv build` packages resources and examples.
Use `just install` to install or replace a CLI snapshot from the checkout,
`just install-editable` to link the installed command to source, and `just uninstall`
to remove the tool installation. CI and checkout-local Lefthook hooks use the same checks. Development stores,
fixtures, downloads and caches belong in ignored checkout-local directories.
