# Frozen profiles and native launch preparation

Profiles capture explicitly selected Codex or Claude inputs and prepare a native
TUI launch. They do not start a subject, bind authentication, run a controller or
install skills. The dependency graph is `models → profile_models/profile_security
→ profile_capture → profiles → profile_launch → profile_cli`; CLI composition
registers these commands in `cli.py`.

## CLI

```sh
uv run dryheave --store .dryheave profile capture everyday --spec .cache/capture.json --json
uv run dryheave --store .dryheave profile inspect everyday --json
uv run dryheave --store .dryheave profile derive everyday --spec .cache/derive.json --name changed --json
uv run dryheave --store .dryheave profile diff everyday changed --json
uv run dryheave --store .dryheave profile preflight changed --destination .cache/trial/profile --workspace .cache/trial/repo --json
uv run dryheave --store .dryheave profile materialize changed --destination .cache/trial/profile --workspace .cache/trial/repo --json
```

`capture` accepts an optional alias name and optional `--agent`, which must agree
with the recipe. `--replace-alias` explicitly moves an existing alias. `derive`
accepts `--name` and the same alias replacement flag. All historical IDs remain
unchanged. `preflight` is read-only by default. Its optional `--probe-version`
runs only the selected executable's `--version`, bounded to 10 seconds and 4096
output bytes; it never runs the launcher or a model call. A shell wrapper's
version remains unverified even if the separately declared binary matches.

`materialize` requires a fresh destination separate from an existing workspace.
Use the repository materializer to create that workspace before preparing a real
trial. `launch-plan.json` records the exact prepared invocation. Preflight lists
missing executables and version mismatches with `launchable: false`; preparing
files does not imply the subject can start successfully.

## Capture specification

`profile create NAME --agent codex|claude --model MODEL` provides selected-file
capture without a JSON spec. Codex defaults to 0.154.0 (0.162.1 is also
accepted; `codex_policy.CODEX_PROFILE_VERSIONS` lists both), Terra low and an
explicit disabled-plugin/bundled-skill policy. Claude requires an explicit model, pins
2.1.278, uses settings.json/CLAUDE.md targets and adds only the named OAuth token
reference. New Claude recipes reject Codex flags, alternate billing references,
print/bare/bypass native arguments and unsupported effort. Only `--verbose` is
accepted through its native-argument helper. Selected native config still needs
permission and managed-discovery review.

`profile derive BASE --model MODEL --effort LEVEL --name NAME` can change either
or both settings. `--clear-effort` removes the override; Haiku requires this when
deriving from an effort-enabled profile. Mixing these options with `--spec` is
an error. New discovery fields serialize only when explicitly set, preserving
historical payloads and object IDs. The old spec commands and recipe defaults
retain their previous behavior.

Codex disabled-plugin mode emits `--disable plugins` and
`skills.bundled.enabled=false`, without `--ignore-user-config` on subjects. Both
flags were checked against the 0.154.0 source only; a 0.162.1 recipe uses them
unverified, and its launch plan's discovery mechanisms say so.
Selected-plugin mode preserves selected bytes but cannot freeze local plugin
discovery, startup sync or upgrades. HOME, project, managed and system influences
remain unverified. Its `codex-discovery-policy` issue says "Codex 0.154.0" even
for a 0.162.1 recipe. Keep that text: `load_profile` recomputes the issues and
compares them with the stored ones, so rewording it would stop stored profiles
from loading.

Paths in `include_roots` resolve relative to the specification file. Individual
asset paths are relative to their named root, and targets are relative to their
native destination root. This example selects three specific inputs; it never
walks the rest of the home directory.

```json
{
  "schema_version": 1,
  "recipe": {
    "agent": "codex",
    "executable": "codex",
    "version": "0.153.4",
    "model": "YOUR_SELECTED_MODEL",
    "effort": "high",
    "home_policy": "native",
    "environment": []
  },
  "include_roots": {
    "user": "/absolute/path/to/selected-config",
    "project": "/absolute/path/to/project-inputs"
  },
  "assets": [
    {
      "root": "user", "path": "config.toml", "kind": "config",
      "layer": "global", "target_root": "config", "target": "config.toml"
    },
    {
      "root": "user", "path": "AGENTS.md", "kind": "instruction",
      "layer": "global", "target_root": "config", "target": "AGENTS.md"
    },
    {
      "root": "project", "path": "skills/review", "kind": "skill",
      "layer": "project", "target_root": "project",
      "target": ".agents/skills/review", "overlay": "preserve"
    }
  ]
}
```

A minimal spec needs only `recipe.agent` and `recipe.executable`; an empty asset
selection is explicit and its native influences remain visible. It requires no
auth-file reads or environment inventory. A missing version is an unresolved
issue. `config`, `instruction`, `skill`, `plugin` and `resource` classify selected
assets. Resource dependencies must be selected separately. Config assets require
syntactically valid native TOML or JSON. Cross-agent configuration/instruction
translation is rejected.

Global files use the fresh `config` root: Codex `config.toml`, optional named
`NAME.config.toml`, `AGENTS.md` and `skills/NAME/...`; Claude `settings.json`,
`CLAUDE.md` and `skills/NAME/...`. Project settings use `.codex/config.toml` or
`.claude/settings[.local].json`; project instructions use their native names.
`home` targets are limited to Codex `.agents/skills/NAME/...` and require explicit
`home_policy: "isolated"` at materialization. Plugin selections use `plugins/NAME`
and preserve their whole selected directory, including resources and scripts.
Claude receives a `--plugin-dir` for each selected plugin root. Codex plugin
activation and external dependencies remain reported limitations.

Each `FrozenAsset` saves exact bytes as a hashed blob, original source root/path,
resolved source path, layer, native destination, overlay policy and executable
bit. Symlinks resolve component by component under explicitly declared roots;
`link/..` follows the link before applying the parent traversal. External targets,
cycles, special files and unsafe paths are rejected. Other roots can be declared
for intentional symlink dependencies. Default bounds are 2048 total selected
entries, 4 MiB per file, 32 MiB total, depth 32 and 40 symlink hops per resolution.
Empty directories have no runtime content and are not frozen. No absolute
reference inside a captured file is silently rewritten.

Duplicate targets, file/ancestor overlaps, case-insensitive and Unicode-normalized
collisions are rejected before publication. Imported objects undergo the same
native target checks; an expanded file must equal or descend from its recorded
selection target. Byte hashes, manifest files, parent references, saved limits
and generated fidelity diagnostics are validated again on load. Expanded depth
is measured from each recorded selection target: a directly selected file has
depth zero, and a direct child of a selected directory has depth one. Derivation
retains and enforces the parent's limits even when additions use looser limits.

## Overlays and variants

Historical project instructions are preserved by default. On a collision,
`overlay: "preserve"` leaves the historical bytes and records both the selected
blob and previous hash. An explicit `"replace"` records and applies replacement.
Matching bytes and executable state record an `identical` action. Replacement
applies the captured executable state in both directions; preserve keeps the
historical bytes and executable state. Global instructions always remain a
separate global layer. Runtime permissions and approval settings come from the
selected native configuration and the recipe's native arguments; Dryheave
generates no permission or sandbox bypass flags of its own. A recipe can still
carry one: `bench` records `--sandbox danger-full-access` as every Codex
variant's native `arguments` and keeps the live approval settings. It does not
use `workspace-write`, which silently blocks writes outside the repository, such
as Xcode's DerivedData, and would break those cases. A Codex launch plan whose
arguments contain `danger-full-access`, `--dangerously-bypass-approvals-and-sandbox`
or `--yolo` reports launch issue `codex-sandbox-disabled`; the bypass flags,
`-a never`/`--ask-for-approval never` and an `approval_policy="never"` override
report `codex-approvals-disabled` (`codex_policy.argument_bypasses`). A live
Codex launch also reads the per-attempt copy of the snapshot's `config.toml`
(never the operator's live directory) and adds either issue when its effective
`sandbox_mode` is `danger-full-access` or `approval_policy` is `never`, taking a
selected `profile` table's value over the top level
(`codex_policy.config_bypasses`). Those issues appear in the materialized
`launch-plan.json`, not in a plain preflight, which has no snapshot. When the
recipe also trusts the workspace, as every bench variant does, the sandbox issue
adds that the case repository's own Codex config, hooks and exec policies load
without a sandbox, so a case repository must be trusted like your own code. A live Codex profile that disables skills gets a
generated `--config skills.config=[...]` override, which replaces rather than
merges into the live `config.toml`'s own `[[skills.config]]` array and
re-enables any skill the operator disabled there; this is reported as launch
issue `codex-skills-config-override`.

A simple derivation spec is:

```json
{"schema_version": 1, "recipe_changes": {"model": "OTHER_MODEL", "effort": "medium", "workflow": "ask-first"}}
```

`workflow` is a comparison label. Actual workflow behavior belongs in selected
instructions, skills, config or native arguments; the label alone does not inject
a prompt. Recipe changes are fully revalidated. `additions` can contain a capture
spec with the exact derived recipe; `replace` must list precisely its colliding
logical blob paths, such as `global/config/config.toml`. `remove` lists existing
logical blob paths. Unknown removals, implicit replacements and no-op derivations
are errors. Derivation reads old blobs from the immutable parent, never from its
source paths. Its new ID references the previous profile ID; moving an alias cannot
change earlier experiments.
Loading, deriving and materializing profiles each read asset bytes in a scoped
batch, verifying the closure once per batch and hashing every returned asset.
Verification is repeated at each operation boundary; no persistent cache can hide
later changes to frozen bytes.

## LaunchPlan and runtime integration

```python
from dryheave.profile_launch import materialize_profile, launch_environment
from dryheave.profiles import load_profile

profile = load_profile(store, profile_id)
plan = materialize_profile(store, profile_id, runtime_directory, trial_workspace)
# The driver resolves references immediately before launch, under run/native locks.
environment = launch_environment(plan, inherited_environment)
```

`capture_profile(store, spec, base=...)`, `load_profile(store, reference)`,
`derive_profile(store, reference, spec, base=...)` and
`diff_profiles(store, before, after)` are domain APIs in `profiles.py`.
`preflight_profile` and `materialize_profile` share arguments
`(store, reference, destination, workspace, *, strict=False, probe_version=False)`
and return the strict `LaunchPlan` model from `profile_models.py`.

The plan includes exact argv/cwd, requested and resolved executable, requested and
observed version, launcher executable, native roots, generated environment and
files, named environment references, deferred runtime file references, layered
project overlay mappings, discovery roots and issues. Downstream execution must
honor this plan, check `launchable`, preserve the frozen profile ID, acquire run
then store native locks, own the process lifetime and record native observations.
Plan preparation is not authorization to start a subject. Materialization writes
selected blobs and the plan; a failed write can leave an owned partial directory
for inspection. Retry requires a fresh destination.

`recipe.launcher` is an explicit argv prefix that itself invokes the native agent;
the declared executable is **not** appended to it. Native selected arguments and
harness-owned model/effort/skill overrides follow the prefix. For example,
`["/bin/zsh", "-lc", "exec my_wrapper \"$@\"", "dryheave"]` expresses a shell
launcher that receives the native flags. Its startup files, function definition,
authentication and executable selection are unverified external effects. An
executable named `codex` is resolved with PATH; Dryheave never assumes it calls a
shell function of the same name. Relative executable paths and relative PATH
entries resolve from the preflight caller's working directory. Resolved native
and launcher executables are recorded as absolute paths so they remain valid
after changing to the launch workspace.

Native `HOME` is preserved by default for browser/device/developer tooling.
A fresh `CODEX_HOME` or `CLAUDE_CONFIG_DIR` is always generated. The launch
environment inherits only explicit reference names plus `PATH`, terminal/locale
basics, native HOME and native XDG root references. Missing explicit references
fail by name only. `TMPDIR` is private to the runtime directory. Optional isolated
HOME also generates private XDG roots and reports the changed tool/auth behavior.
`launch_environment` selects values by name without enumerating or serializing
the supplied mapping. Treat its returned values as sensitive process input.
A live-config profile's plan instead uses the `inherited` environment policy:
the subject gets the operator's environment minus Claude's nested-session
markers, with the generated roots and variables on top, and a Codex plan also
drops `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`.

## Fidelity and supported discovery controls

Default captured mode reports unresolved influences. `--strict` refuses every
unresolved issue. Current adapters cannot establish full host fidelity: managed
config, auth availability, effective permissions and native tool effects remain
unverified, so strict mode currently refuses such plans even with a pinned binary.
It does not silently downgrade to captured mode. Missing observations are not
proof that a source is absent.

Codex 0.153.4 discovers config-layer skills, `CODEX_HOME/skills`,
`HOME/.agents/skills`, bundled/system roots, plugin/extra roots and repository
`.agents/skills`. A fresh CODEX_HOME alone leaves home skills in scope. Session
`skills.config` rules can disable a canonical SKILL.md path or a skill name.
Dryheave automatically disables each captured skill's original source document
when the frozen destination differs. Thus a source edit or ambient copy cannot
compete with the frozen copy of that selected skill. Additional known sources can
be listed in `disabled_skill_paths`; unrelated home/ancestor roots remain visible
and are never inventoried. The rules do not prevent arbitrary filesystem reads.
See the pinned [discovery implementation](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/ext/skills/src/host_roots.rs)
and [skill rules](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/config/src/skills_config.rs).

The historical `NativeRecipe` defaults disable `dryheave-collect`, `dryheave-case`
and `dryheave-results`; these defaults remain unchanged for old frozen profiles.
New CLI captures whose spec omits `disabled_skill_names` explicitly add those
three plus `dryheave-voice-profile`, `dryheave-generate-problem` and
`dryheave-agent-profile`. Codex disables them through session rules; Claude uses
`skillOverrides` in generated settings. Explicitly changing
`disabled_skill_names` changes that policy and its immutable profile ID.
Claude's generated `claudeMdExcludes` omits ancestor instruction files/rules,
`CLAUDE_CONFIG_DIR` redirects user resources, and auto memory is disabled through
`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`. Claude plugin skills do not obey
`skillOverrides`, so managed/ambient plugins remain explicitly unverified.
See [Claude skill visibility](https://code.claude.com/docs/en/skills#override-skill-visibility-from-settings),
[settings sources](https://code.claude.com/docs/en/cli-reference), and
[instruction exclusions](https://code.claude.com/docs/en/memory#exclude-specific-claude-md-files).

## Credentials and sensitive artifacts

Capture rejects recognized credential/state files and directories, credential
fields, common token/private-key patterns and credential-bearing argv entries.
Persisted trust fields are also rejected. Diagnostics identify the selected target
or argv position without printing values. Config is never silently redacted and
then described as faithful. These checks are deliberately conservative and are
not an exhaustive secret detector; transcripts, arbitrary argv and final output
can still be sensitive. All checks complete before publishing profile objects.
`profile_security` holds the shared detector, which `load_profile` reruns and
the live Codex snapshot also uses. A config file is judged on its parsed structure: token-shaped material
anywhere, a non-empty string or number under a recognized credential field, or a
credential-named URL query parameter refuses it, while env-reference keys
(`env_http_headers`, `env_vars`, `env_key`, `*_env_var`) are skipped. Other files
are scanned for token material and literal credential-field assignments; a
`$VAR` value is not a literal. Recognized fields are a fixed list
(`_credential_field`), so a literal under another secret-sounding name, such as
`GITHUB_TOKEN`, is refused by the shared detector only when the value looks like
a token. `load_profile` reruns this detector on stored profiles, so it must never
become stricter than what earlier profiles passed. Capture of a Codex profile,
and the assets a derivation adds, additionally run the live snapshot's Codex
check (`reject_codex_config_secrets`, described below) on each selected config;
`load_profile` does not rerun it, so it may grow stricter.

The live Codex snapshot screens by file kind with functions that
`load_profile` never runs, so they may differ from the shared detector.
`config.toml` and `hooks.json` use `credential_in_live_config`: the shared
config check, except that token material inside a TOML table or key name is
ignored, plus the Codex check: literal Codex token fields
(`experimental_bearer_token`, `bearer_token`, token-named `http_headers`
entries), a literal under a key whose last word is secret-like (`TOKEN`,
`SECRET`, `PASSWORD`, `KEY`, `AUTH`, `PAT`, …) in any `env` table or
`shell_environment_policy.set` unless it starts with `$`, and any string that
is a URL with a password. Files under `hooks` and `rules` use the shared text
check and `sensitive_path`. Prose under `skills`, `prompts` and `AGENTS.md`
uses `credential_in_prose`, which matches only realistic key lengths
(`PROSE_KEY_PATTERN`), and `credential_file` for file names; a directory with a
credential file name refuses the snapshot. `.git` directories in every entry,
and dependency and cache directories (`node_modules`, `.venv`, `venv`,
`__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.tox`) under
`skills` and `prompts`, are skipped by the walk, so they are neither copied nor
hashed. A nested link is checked again at its resolved target: a target, or
anything inside a linked directory, under `SECRET_DIRECTORIES` (`.ssh`, `.aws`,
`.gnupg`, `.config/gcloud`, `.docker`, `.kube`), with a credential file name or
named exactly like a `SENSITIVE_PARTS` entry refuses the snapshot. The walk
itself lives in `tree_copy`, which takes the screen, admission and link-target
checks as a policy; `live_config` keeps the layouts and policies.

Environment credentials are explicit name references, never values. Codex may
also declare an opaque deferred file recipe:

```json
{
  "name": "subscription-auth",
  "source_path_environment": {"name": "SUBJECT_AUTH_PATH", "source": "inherited"},
  "target": "auth.json",
  "binding": "symlink",
  "acknowledge_source_writes": true
}
```

An optional `"default_source": "codex-home"` gives the reference a default; it
is omitted from the payload when absent, so older references keep their IDs.

Put this entry in `recipe.runtime_files`. Neither capture, preflight nor
materialization reads that variable or its file, copies credentials, creates the
link, or validates login availability. A later driver must explicitly bind it
immediately before launch. The binding does not expose the source-path variable
to the subject, though a live profile's inherited environment carries it when
the operator set it. A reference with `default_source: "codex-home"`
(`profile_models.codex_auth_reference`, which `bench` uses) has a default
whatever its variable is named: when the variable is unset, the binding uses
`auth.json` under the Codex home that `live_config.live_source` resolves
(`$CODEX_HOME`, else `~/.codex`), and a `~/` value expands against the inherited
`HOME`. A relative or `~user/` value and a missing file are refused at binding
time, before the subject starts. References without it, including every one
from `--auth-file-env`, have no default and their files are not checked. Codex token refresh can write through the link to its
source; this is not a read-only credential mount. The caller must preserve that
acknowledgement and record binding/cleanup ownership.

A changed CODEX_HOME changes both file auth lookup and keyring identity.
`CODEX_API_KEY` is for exec/review, not ordinary TUI authentication.
`CODEX_ACCESS_TOKEN` accepts supported personal-access/agent-identity tokens;
ordinary ChatGPT login tokens cannot simply be extracted into it. No profile
operation changes subscription authentication into API billing. See
[Codex authentication](https://developers.openai.com/codex/auth) and the pinned
[auth storage implementation](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/login/src/auth/storage.rs).

A Codex recipe may explicitly set `workspace_trust` to `trusted` or `untrusted`.
The default `prompt` preserves native onboarding. Materialization adds the exact
owned workspace as a `projects={"PATH"={trust_level="trusted"}}` argv override and records the
choice in `LaunchPlan`. It preserves captured config bytes and existing native
permission/sandbox settings. Non-prompt trust choices currently reject Claude;
no global trust store is copied or modified. See runner.md for native execution.

The whole `projects` value keeps workspace paths as literal TOML keys. Codex
0.153.4's [CLI override parser](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/config/src/overrides.rs)
splits dotted override keys without interpreting quotes. A quoted path in the
override key therefore does not establish the intended trust setting. The final
native smoke exposed this and stopped at trust onboarding; the corrected value
encoding has offline regression coverage. A complete live task after this
correction remains unverified; see [the native QA record](../tests/qa-cli/runs/2026-09-09-native.md).

Derived profiles retain `superseded_skill_paths` when a selected `SKILL.md` is
removed or replaced from another source. Codex launch rules disable those original
paths as well as the current selected source, so a scratch replacement does not
reactivate the older installed skill. The provenance survives later derivations
and is shown by profile diff. Frozen copies remain enabled. Claude's ambient
skill discovery retains the separate native-fidelity limitations described above.
