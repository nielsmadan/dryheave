# Native runtime setup

Dryheave supports Python 3.13+ on macOS and Linux. Git must be on PATH. The
terminal driver needs **tui-test 0.1.x, from 0.1.0** and refuses any other reported
version (a prerelease such as 0.1.0-beta.3, or 0.2.0). Native Windows execution is
outside v1 support. Install an explicit
Codex or Claude executable independently and retain its exact version in the
profile recipe. A shell function needs an explicit argv launcher; resolving a
same-named binary from PATH does not execute that function.

## Install the terminal driver

`dryheave init` looks for tui-test (`--tui-test`, then the workspace's `tui_test`,
then PATH) and runs the one it finds with `--version`. When it finds none, or one
that reports an unsupported version (such as the old 0.1.0-beta.3), it prints
"Checking GitHub for the newest tui-test release…", shows the install command and
asks on a terminal:

```text
  ❯ Install it for me
    I will install it
  ↑/↓ move · Enter choose · Esc cancel
```

The lookup is one request to GitHub's releases API (5-second connect and 15-second
overall limit). Only plain `X.Y.Z` release tags that are neither drafts nor
prereleases count. The menu names the version and why it was chosen: the newest
supported release; or 0.1.1, the last version tested with dryheave, when the
lookup fails (offline, rate-limited) or lists no supported release. A newer release
than 0.1.1 is marked as untested, with no install-script checksum on record.

"Install it for me" runs once `init`'s other questions are answered. It downloads
that release's `install/install.sh` to a temporary directory, checks it against the
recorded SHA-256 when there is one (0.1.1), prints a progress line and runs it with
`TUI_TEST_VERSION` set and `TUI_TEST_NO_MODIFY_PATH=1`, so your shell profile is
left unchanged. `GITHUB_TOKEN`, `GH_TOKEN` and `PREFIX` are removed from the
script's environment; `TUI_TEST_INSTALL_DIR` is honoured. `init` then checks the
installed version and records the binary's path in `dryheave.toml` unless PATH
already finds it (a stale recorded path is always replaced). `--json` output
reports it under `installed_tui_test` (path, version, and whether the lookup found
it or fell back).

"I will install it" stops before anything is written and prints the command;
install it, then rerun `init`. Esc, q or Ctrl+C cancels `init` (exit 130), also
before anything is written. Once answered, the question collapses to one line
(`✔ tui-test: install 0.1.1`) so the next question takes its place. A terminal
without cursor control (`TERM=dumb`) gets the same choices as a numbered prompt.
Without a terminal (or with `--json`) there is no prompt and no lookup: `init`
creates the workspace and `next` names the tested-version command. Rerunning
`init` on that workspace later, on a terminal or with `--install-tui-test`, installs
and records tui-test. `init --install-tui-test` looks up and installs without
asking. `init` checks the workspace (config, runtime-root length) before any of
this, so those errors come first.

The command for a given version (shown for the tested 0.1.1):

```sh
curl --proto '=https' --tlsv1.2 -LsSf https://raw.githubusercontent.com/microsoft/tui-test/0.1.1/install/install.sh | TUI_TEST_VERSION=0.1.1 sh
```

The script installs to `~/.local/bin` (`/usr/local/bin` as root; set
`TUI_TEST_INSTALL_DIR` to choose). Run by hand, it appends a PATH line to your
shell profile (`~/.zprofile` for zsh) when that directory is not on PATH;
`TUI_TEST_NO_MODIFY_PATH=1` skips that. The script verifies no checksum of the
release archive it downloads. `brew tap microsoft/tui-test
https://github.com/microsoft/tui-test && brew install tui-test` also works while
the latest release is 0.1.x.

To verify the download yourself, use the
[0.1.1 release](https://github.com/microsoft/tui-test/releases/tag/0.1.1) assets and
these SHA-256 digests from its release metadata (macOS arm64 is the only one
transport-tested in this project):

| Platform | Asset | SHA-256 |
| --- | --- | --- |
| macOS arm64 | `tui-test-aarch64-apple-darwin.tar.gz` | `a6819562a18031b212565f771d03fe9e5ec7d4f71b389394fd909cc66cee6b81` |
| macOS x86_64 | `tui-test-x86_64-apple-darwin.tar.gz` | `298ae3d37549d4712a7795d388f4e871f3bf8b7863372c3441787870c747408d` |
| Linux arm64 GNU | `tui-test-aarch64-unknown-linux-gnu.tar.gz` | `99588dd2e296f9b2685d7fb71d68a3dde3b1fed8c0e8f1af4d215538e858bd09` |
| Linux arm64 musl | `tui-test-aarch64-unknown-linux-musl.tar.gz` | `f041c826ebf23762613fec969611eb071f7b96bbe65bb2e2404d74445eae9bee` |
| Linux x86_64 GNU | `tui-test-x86_64-unknown-linux-gnu.tar.gz` | `ae1f32afb34c713284b0637ea41d855124fe0ac298defb38a4018cd952c158ca` |
| Linux x86_64 musl | `tui-test-x86_64-unknown-linux-musl.tar.gz` | `7bc4b706abac448b0f7043c93ed66e39c40444da395564d80ac7c0462d377795` |

```sh
mkdir -p .tools/download .tools/tui-test
gh release download 0.1.1 --repo microsoft/tui-test \
  --pattern tui-test-aarch64-apple-darwin.tar.gz --dir .tools/download
printf '%s  %s\n' \
  a6819562a18031b212565f771d03fe9e5ec7d4f71b389394fd909cc66cee6b81 \
  .tools/download/tui-test-aarch64-apple-darwin.tar.gz | shasum -a 256 -c -
tar -xzf .tools/download/tui-test-aarch64-apple-darwin.tar.gz -C .tools/tui-test
.tools/tui-test/tui-test --version
dryheave doctor --tui-test .tools/tui-test/tui-test --agent codex --runtime-root rt --json
```

Linux can use `sha256sum -c -` for the verification step.

Doctor checks the running Python/platform, locates Git and an optional agent, and
runs only the selected tui-test executable's `--version` with five-second/4096-byte
limits. It checks neither auth state nor selected configuration. `data.ready`
means those runtime availability checks passed; agent versions remain unverified.
`data.checks` contains actions for missing/mismatched tools; for tui-test the
action points to `init` for the newest release and gives the tested-version
command above (doctor itself makes no network request). The tui-test check also
reports `supported_versions` as a version specifier (`>=0.1.0,<0.2.0`),
`observed_version` (the raw `--version` output), `outcome` and `exit_code`;
`data.tui_test_install_command` is the tested-version install command. The diagnostic command
itself exits successfully when it can report missing tools, so automation must
check `data.ready`. Use `profile preflight --probe-version` to explicitly verify
the selected agent version and retain its exact generated argv.

## Prepare a native run

Freeze selected settings/instructions/skill resources with `profile capture`,
inspect the saved profile, then preflight. Pin the subject executable/version,
model, effort, native arguments, trust/permission choices and launcher explicitly.
Do the same for user stand-in and optional judge recipes. See the
[packaged real workflow](../../src/dryheave/resources/examples/real-workflow.md).

```sh
mkdir preview-workspace
dryheave --store bench-store profile preflight PROFILE_ID \
  --destination preview-profile --workspace preview-workspace --probe-version --json
dryheave --store bench-store run EXPERIMENT_ID --mode native \
  --tui-test .tools/tui-test/tui-test --runtime-root rt --json
```

The absolute runtime root plus `/` and a 10-character attempt child must fit
70 bytes for Unix sockets. Select a shorter explicit root when doctor reports
`too-long`; Dryheave never silently relocates it. Every attempt creates fresh
repository/profile/runtime directories. Preserve the foreground runner until it
returns; the transport controller owns its terminal and known writers. Unknown
UI, permission, auth and trust dialogs can stop as `needs_input`. The stand-in
cannot approve them. Do not resend a prompt after ambiguous native delivery.

Normal HOME is preserved by default for browser/device/native tool configuration.
Captured inputs use a fresh CODEX_HOME or CLAUDE_CONFIG_DIR. Additional home,
ancestor, plugin, managed and system sources may still affect a run; selected
source skill copies are disabled when frozen elsewhere, but complete discovery
is not established. Strict profile mode currently refuses unresolved native
sources/auth/permission observations. An isolated HOME is an explicit policy
with recorded tool/auth consequences, not a complete host sandbox.

Historical snapshots isolate repository objects and retain selected reachable
ancestry. They do not prevent the subject, stand-in, judge or verifier from
accessing the host, network, original sources, browser or devices. Audit checks
retain observed suspicious access and corruption separately from task quality;
missing telemetry makes coverage partial. Do not present native audit as access
prevention or complete proof of clean execution.

## Authentication and spending

Credentials remain explicit runtime references. Neither capture nor preflight
reads auth files or inventories environment/config state. Choose an existing
supported launcher, named inherited environment references, or an explicitly
acknowledged opaque Codex auth-file symlink. Its source-path environment variable
is looked up only at execution; Dryheave does not read/copy credential bytes.
Only a reference with `default_source: "codex-home"` has a default; `bench`
gives its `CODEX_AUTH_FILE` reference one. It binds `auth.json` under
`$CODEX_HOME` or `~/.codex` (your own Codex login) when its variable is unset,
and accepts a `~/` value. A relative or `~user/` value, or a file that does not
exist, is refused at launch. References from `--auth-file-env` and
`--stand-in-auth-file-env` carry no default, whatever the variable is called:
the variable must hold an absolute path, so they never fall back to your login.
Normal token refresh can write through the link to its original source, so
`acknowledge_source_writes: true` is mandatory in the frozen recipe. Cleanup
removes only a binding whose recorded identity is still owned.

A new CODEX_HOME can change file-auth lookup and keyring identity. Do not extract
subscription tokens or silently switch to API billing to make a trial start.
`CODEX_API_KEY` is for exec/review, not ordinary TUI login; use the supported
authentication mechanism for the selected native version. Consult
[Codex authentication](https://developers.openai.com/codex/auth) and the
[profile authentication contract](../tech/profiles.md#credentials-and-sensitive-artifacts).
Explicit shell launchers preserve their declared semantics, but startup files,
wrapper definitions, permissions and auth availability remain unverified effects.

The subject, adaptive stand-in and optional judge are separate roles that may
each incur charges. Reports retain unknown usage and all-attempt spend, including
failed launches, interruptions, retries and audit exclusions. A saved dated price
table gives an estimate under that version/currency; it is not a current market
quote. Known subtotals do not imply complete totals. Offline fixture/no-call roles
can establish zero model spending; absent native telemetry cannot.

## Compatibility evidence

Friendly setup also has a version-gated Claude 2.1.278 print-mode stand-in and
judge adapter. It uses `--tools ''`, `--strict-mcp-config` with an empty server
set, empty setting sources, explicit disabled hooks/plugins and
`--no-session-persistence`. It never uses `--bare` (which disables OAuth), a
permission bypass, or an API-billing fallback. Only the
`CLAUDE_CODE_OAUTH_TOKEN` environment reference is accepted for that controller;
its value is neither inspected nor saved. Managed policy remains unverified.
Runtime config/tmp/work directories are separate from evidence, and owned
processes must stop before publication. Codex 0.154.0 separately uses its
`codex-clean-0.154.0` policy with optional acknowledged opaque auth binding.

The new `claude-print-2.1.278-synthetic.json` and lifecycle tests are synthetic
offline protocol fixtures, not live recordings. They validate structured-result
boundaries, provider usage on response/process failure, cancellation, version
checks, owned cleanup and conservative native turn handling. They do not prove
live composer/paste compatibility. The existing 2.1.263 source fixtures are
unchanged. Claude usage counts uncached input, cache read/write and output
separately; reasoning and effective effort stay unknown. `modelUsage` identifies
an observed model only when exactly one model has measured tokens; requested
identity is never substituted.

Sanitized source-log fixtures derive from Codex 0.153.4 and Claude 2.1.263; their
text/identifiers are synthetic and do not establish recovered private task intent.
Codex's adapter recognizes its recorded composer, fresh acceptance and root-turn
lifecycle evidence. Claude's driver is fixture-tested only. Unknown versions or
UI shapes may stop conservatively rather than report task completion.

The 2026-09-10 Terra low/high trials on Codex 0.154.0 each accepted the initial
task and scripted clarification answer, retaining model/effort and usage evidence.
Both then requested design approval after loading an automatically supplied
Superpowers plugin. The scripted user stopped there, and the unchanged task code
failed calibrated hidden checks. Successful live task completion remains
unverified. The [Terra QA record](../tests/qa-cli/runs/2026-09-10-terra.md) preserves
these observations and the two earlier scanner failures in that run.

Native log discovery uses only the owned `sessions` directory for Codex and
`projects` directory for Claude, including recovery. Plugins, caches and bundled
skills outside those directories do not consume the bounded log-entry budget.
Fresh Codex configuration roots can still acquire plugins automatically; review
the actual skill/configuration discovery when designing controlled comparisons.

The real non-model transport smoke verifies paste bytes, terminal exit and owned
cleanup with a Python receiver. It proves no model/TUI completion behavior. See
[driver contracts](../tech/drivers.md) and the dated
[manual QA records](../tests/qa-cli/) for actual native attempts and blockers.
Never treat a transport-only smoke or synthetic offline pass as a native agent pass.

Frozen inputs, exact argv and observed/requested versions support repeatable
comparisons. Live model services, browser/device/network state, host tools and
unobserved descendants can still vary. Compare matched cases, scoring, stand-in,
repetition, seed and mode; report sample count/spread without inferring significance
from a pair of trials.
