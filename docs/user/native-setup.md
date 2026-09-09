# Native runtime setup

Dryheave supports Python 3.13+ on macOS and Linux. Git must be on PATH. The
terminal driver requires **tui-test 0.1.0-beta.3** and refuses a different reported
version. Native Windows execution is outside v1 support. Install an explicit
Codex or Claude executable independently and retain its exact version in the
profile recipe. A shell function needs an explicit argv launcher; resolving a
same-named binary from PATH does not execute that function.

## Install the pinned terminal driver manually

Dryheave never downloads tools automatically. These release assets and SHA-256
digests were obtained from the tagged Microsoft release metadata on 2026-09-09.
Only the macOS arm64 asset has been downloaded and transport-tested in this
project; the remaining rows are published artifacts, not tested-platform claims.

| Platform | Asset | SHA-256 |
| --- | --- | --- |
| macOS arm64 | `tui-test-aarch64-apple-darwin.tar.gz` | `c1902b9388d5e6c48eb6675efeca2305afd4cd6c518455f93860441f28bdcd7d` |
| macOS x86_64 | `tui-test-x86_64-apple-darwin.tar.gz` | `f7208c9b14d5d6a3678afbf9685c83312fb46edebe4036eb5f080325e9e7986d` |
| Linux arm64 GNU | `tui-test-aarch64-unknown-linux-gnu.tar.gz` | `31261346af3542cbf425b1f661743db629b8f9daa2edeb7544dd4db4bae19e7a` |
| Linux arm64 musl | `tui-test-aarch64-unknown-linux-musl.tar.gz` | `2a3705c90e8ec33634f2d7bcbf0a282c3ee2353d3cc6ea1178eb202e1970e1bc` |
| Linux x86_64 GNU | `tui-test-x86_64-unknown-linux-gnu.tar.gz` | `35d0cfff1b3d6cfeba3abb0eeae7537c5722c36414106ca28b142e5e48235a37` |
| Linux x86_64 musl | `tui-test-x86_64-unknown-linux-musl.tar.gz` | `4400119a886efc627c9959b9444706a75ffc46241fae21b6cb021bb1c2a5b894` |

For macOS arm64, use fresh download/extraction directories:

```sh
mkdir -p .tools/download .tools/tui-test
gh release download 0.1.0-beta.3 --repo microsoft/tui-test \
  --pattern tui-test-aarch64-apple-darwin.tar.gz --dir .tools/download
printf '%s  %s\n' \
  c1902b9388d5e6c48eb6675efeca2305afd4cd6c518455f93860441f28bdcd7d \
  .tools/download/tui-test-aarch64-apple-darwin.tar.gz | shasum -a 256 -c -
tar -xzf .tools/download/tui-test-aarch64-apple-darwin.tar.gz -C .tools/tui-test
.tools/tui-test/tui-test --version
dryheave doctor --tui-test .tools/tui-test/tui-test --agent codex --runtime-root rt --json
```

Continue to extraction only after the digest check passes. For another platform,
select its exact asset and digest above. Linux can use `sha256sum -c -` for the
verification step. The expected version line is `tui-test 0.1.0-beta.3`.
The [pinned release](https://github.com/microsoft/tui-test/releases/tag/0.1.0-beta.3)
also provides direct download links.

Doctor checks the running Python/platform, locates Git and an optional agent, and
runs only the selected tui-test executable's `--version` with five-second/4096-byte
limits. It checks neither auth state nor selected configuration. `data.ready`
means those runtime availability checks passed; agent versions remain unverified.
`data.checks` contains actions for missing/mismatched tools. The diagnostic command
itself exits successfully when it can report missing tools, so automation must
check `data.ready`. Use `profile preflight --probe-version` to explicitly verify
the selected agent version and retain its exact generated argv.

## Prepare a native run

Freeze selected settings/instructions/skill resources with `profile capture`,
inspect the saved profile, then preflight. Pin the subject executable/version,
model, effort, native arguments, trust/permission choices and launcher explicitly.
Do the same for simulator and optional judge recipes. See the
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
UI, permission, auth and trust dialogs can stop as `needs_input`. The simulator
cannot approve them. Do not resend a prompt after ambiguous native delivery.

Normal HOME is preserved by default for browser/device/native tool configuration.
Captured inputs use a fresh CODEX_HOME or CLAUDE_CONFIG_DIR. Additional home,
ancestor, plugin, managed and system sources may still affect a run; selected
source skill copies are disabled when frozen elsewhere, but complete discovery
is not established. Strict profile mode currently refuses unresolved native
sources/auth/permission observations. An isolated HOME is an explicit policy
with recorded tool/auth consequences, not a complete host sandbox.

Historical snapshots isolate repository objects and retain selected reachable
ancestry. They do not prevent the subject, simulator, judge or verifier from
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

The subject, adaptive simulator and optional judge are separate roles that may
each incur charges. Reports retain unknown usage and all-attempt spend, including
failed launches, interruptions, retries and audit exclusions. A saved dated price
table gives an estimate under that version/currency; it is not a current market
quote. Known subtotals do not imply complete totals. Offline fixture/no-call roles
can establish zero model spending; absent native telemetry cannot.

## Compatibility evidence

Sanitized source-log fixtures derive from Codex 0.153.4 and Claude 2.1.263; their
text/identifiers are synthetic and do not establish recovered private task intent.
Codex's adapter recognizes its recorded composer, fresh acceptance and root-turn
lifecycle evidence. Claude's driver is fixture-tested only. Unknown versions or
UI shapes may stop conservatively rather than report task completion.

The final bounded Codex attempt on 2026-09-09 reached directory trust onboarding
and stopped before task delivery, with zero accepted turns. The recorded argv
revealed a dotted-key trust-override encoding bug; its correction is tested
offline, with no further paid launch in that allocation. Full live task completion
remains unverified. The dated QA record retains exact attempt and cleanup evidence.

The real non-model transport smoke verifies paste bytes, terminal exit and owned
cleanup with a Python receiver. It proves no model/TUI completion behavior. See
[driver contracts](../tech/drivers.md) and the dated
[manual QA records](../tests/qa-cli/) for actual native attempts and blockers.
Never treat a transport-only smoke or synthetic offline pass as a native agent pass.

Frozen inputs, exact argv and observed/requested versions support repeatable
comparisons. Live model services, browser/device/network state, host tools and
unobserved descendants can still vary. Compare matched cases, scoring, simulator,
repetition, seed and mode; report sample count/spread without inferring significance
from a pair of trials.
