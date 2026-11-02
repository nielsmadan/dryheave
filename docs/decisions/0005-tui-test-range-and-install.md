# A supported tui-test range, installed by init

Status: accepted. Date: 2026-11-03.

The terminal driver used to require one exact tui-test build, 0.1.0-beta.3, and
setup told the user to install that build by hand. tui-test publishes a release
every one to three weeks, prereleases changed the config format (beta.5 replaced
`[recording] mode` with `[trace] mode`), and a hand-installed binary was the most
common reason a first native run failed. Dryheave records what it observed rather
than freezing every tool, so the version a run used belongs in its evidence, not
in a pin the user has to match.

Decisions:

- The driver accepts any 0.1.x release from 0.1.0 and refuses prereleases below
  0.1.0 and any other series. A minor release before 1.0 may break the CLI, so the
  range stops at 0.2.0. The reported version is recorded as `transport_version`.
- `init` may make one network request: GitHub's releases API, only when no
  supported tui-test is found and either a terminal can ask or
  `--install-tui-test` was passed. It picks the newest plain `X.Y.Z` release in
  the range and falls back to the last tested version (0.1.1) when the lookup
  fails or finds nothing. `doctor` stays offline and names the tested version.
- Installing runs tui-test's own install script from that release's tag after
  every `init` question is answered. The script is downloaded first and checked
  against a recorded SHA-256 for tested versions; it runs with
  `TUI_TEST_NO_MODIFY_PATH=1` and without `GITHUB_TOKEN`, `GH_TOKEN` or `PREFIX`.
  Dryheave records the installed path itself, so the user's shell profile is never
  edited.

Consequences:

- A newer release than the tested one can be installed without a checksum. The
  menu says it is untested, and `--install-tui-test` opts into it without asking.
  The release archive the script downloads is not checksum-verified by tui-test
  either; the SHA-256 table in the native setup guide covers manual verification
  of the tested release.
- When a tested release is added, `TUI_TEST_TESTED_VERSION` and
  `TUI_TEST_INSTALL_SCRIPT_SHA256` in `tui_test_release` and the digest table in
  the native setup guide change together. A 0.1.x release that breaks dryheave
  raises `TUI_TEST_MINIMUM`.
- Rerunning `init` may set or replace a workspace's `tui_test`, so a stale or
  missing recorded binary can be repaired without editing config by hand.
