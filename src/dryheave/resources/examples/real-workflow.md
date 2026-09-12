# Compare one real subject change

## Start with the selected-log operator workflow

Ordinary users can let the packaged skills write internal drafts and specs:

```sh
dryheave init ./bench
cd bench
dryheave skills install
dryheave collect select work /explicit/session.jsonl --agent codex --json
dryheave problem request "Find a small command bug" --selection work --name command-bug --micro-bug --json
```

In Codex invoke `$dryheave-voice-profile` for selection `work`, then
`$dryheave-generate-problem` for request `command-bug`, and
`$dryheave-agent-profile` for the frozen result. Other operator agents use their
own installed-skill invocation syntax. The skills use public commands and write
the internal JSON shown later in this document; users need no preparation script.
The problem skill inspects and resumes the supplied `command-bug` request with
its saved selection, policy, decisions and current revisions. It creates a new
request only when none was supplied; an unknown supplied name needs correction.

Recover names/revisions with `collect selections`, `problem list`,
`problem inspect command-bug`, and `voice list`. Read selected evidence with
`collect evidence work --session ID --limit 20 --offset 0`; follow `next_offset`.
Long messages support `--event EVENT --text-offset N --text-limit N`.
Read recorded `cwd` and `git.commit_hash` with
`collect metadata work --session ID --key cwd` and `--key git`. Codex session
metadata is separate from the event stream. This command returns bounded
`metadata_json`; follow `text_next_offset` with `--text-offset N --text-limit N`
and join chunks before parsing. Missing metadata keys are reported explicitly.
The default problem request seeks up to six varied supported tasks; `--micro-bug`
seeks one and still needs explicit scope/baseline reasoning. Unsupported candidates
remain rejected/unresolved and coverage gaps are retained. Never fill a quota or
substitute synthetic tasks. Changing source membership needs a new selection/request.

New selected-log voice creation requires exact nonempty actual user-role excerpts,
safe policies and curator review of solution leakage and injected instruction
blocks. `problem record` captures source/event boundaries, baseline/dirty-state
reasoning and a drafted case path. `problem validate REQUEST CANDIDATE
--expect-revision N` binds the current draft and hidden files; freeze with the
returned revision using `problem freeze REQUEST CANDIDATE --expect-revision N`.
Any draft/hidden-file edit requires revalidation. Calibrate before subject spend.
Frozen cases/profiles are reusable across repetitions; changed inputs need a new
experiment and retries retain earlier evidence.

For an existing owned skill installation, `skills update --target .agents/skills`
updates intact owned bytes and installs missing bundled skills. Edited/foreign
collisions remain errors. Reload your operator's skill discovery as needed.

## Lower-level authoring and comparison reference

Use an installed `dryheave` executable. Commands below use `bench-store` explicitly;
keep it private and outside any source repository you are benchmarking. JSON
responses place returned IDs under `data.id`. Save those IDs when indicated.
Do not reuse the offline demo's synthetic model names, subject executable or prices.

## 1. Curate and freeze one historical task

```sh
dryheave --store bench-store collect scan --agent codex --root SELECTED_LOG_DIRECTORY --json
dryheave --store bench-store collect import SELECTED_LOG.jsonl --agent codex --json
dryheave --store bench-store collect show SESSION_ID --json
dryheave --store bench-store case draft SESSION_ID --repo SOURCE_REPO \
  --commit VERIFIED_FULL_START_SHA --out case.json --json
dryheave --store bench-store persona draft SESSION_ID --out persona.json --json
```

Replace uppercase arguments with your selected paths and returned IDs. Review the
event range, exact source excerpts, intent, allowed facts and historical SHA.
Never substitute today's HEAD for missing evidence. Complete persona policies,
review any examples for solution text, mark `reviewed_subject_safe: true`, then
`persona create persona.json --json`. Set the case's inline `persona` to null and
its `persona_id` to that returned ID. Review intent/facts, resolve draft issues and
add substantive required criteria and hidden verifier files. A verifier must emit
its success marker after all checks pass, and its separate failure marker only
for known assertion failures. Leave import/setup failures outside that wrapper.
Keep any reference patch and hidden tests curator-only.

```sh
dryheave --store bench-store case validate case.json --json
dryheave --store bench-store case freeze case.json --json
dryheave --store bench-store case inspect CASE_ID --json
```

Snapshots retain exact reachable Git ancestry through the selected commit. Later
objects, refs, remotes and source working-tree edits are excluded. Source logs and
repositories remain read-only. Native processes can still access the host.

## 2. Select actual installed inputs

Create `capture.json` with the exact installed binary/version and selected model.
The following shape selects specific native files and one complete skill directory.
Replace the values and paths with your selections; do not inventory all of HOME.

```json
{
  "recipe": {
    "agent": "codex",
    "executable": "/absolute/path/to/codex",
    "version": "0.153.4",
    "model": "YOUR_BASE_MODEL",
    "effort": "high",
    "home_policy": "native"
  },
  "include_roots": {"selected": "/absolute/path/to/selected/native/config-root"},
  "assets": [
    {"root": "selected", "path": "config.toml", "kind": "config", "layer": "global", "target_root": "config", "target": "config.toml"},
    {"root": "selected", "path": "AGENTS.md", "kind": "instruction", "layer": "global", "target_root": "config", "target": "AGENTS.md"},
    {"root": "selected", "path": "skills/review", "kind": "skill", "layer": "global", "target_root": "config", "target": "skills/review"}
  ]
}
```

Declare additional include roots for explicitly selected symlink dependencies;
select plugin resources and referenced scripts too. The capture freezes their
actual bytes and hashes. It does not discover the complete effective config or
translate native configuration between harnesses. Project assets use the project
layer and their real native target; choose `overlay: "replace"` explicitly to
replace historical project bytes. Existing project instructions are preserved by
default. The profile disables Dryheave's operator skills for subjects by default.

```sh
dryheave --store bench-store profile capture base --spec capture.json --json
dryheave --store bench-store profile inspect base --json
dryheave doctor --agent codex --tui-test /absolute/path/to/tui-test --runtime-root rt --json
mkdir preview-workspace
dryheave --store bench-store profile preflight base --destination preview-profile \
  --workspace preview-workspace --probe-version --json
```

Doctor invokes only the supplied tui-test executable's bounded `--version` and
locates other binaries. Explicit profile version probing invokes the selected
agent's `--version`, never a model session. Pin tui-test 0.1.0-beta.3. Use a short
runtime root: the absolute root plus `/` and each 10-character attempt child must
fit 70 bytes. Preflight reports exact argv, chosen native roots and fidelity issues.

HOME is retained for normal browser, device and developer-tool state. Fresh
CODEX_HOME/CLAUDE_CONFIG_DIR roots leave ambient influences unresolved. Captured
mode records these limitations; strict mode currently refuses unresolved native
sources/auth/permissions. Native execution is repository-object isolation, not
host isolation. Trust/approval/auth dialogs can stop as `needs_input`.

Authentication stays an explicit runtime binding. Choose supported launcher or
environment-name references; never put credential values in these JSON files.
For an existing Codex file-login binding, `recipe.runtime_files` can contain:

```json
[
  {"name": "subscription-auth", "source_path_environment": {"name": "SUBJECT_AUTH_PATH", "source": "inherited"}, "target": "auth.json", "binding": "symlink", "acknowledge_source_writes": true}
]
```

The operator supplies that path variable privately at execution. Dryheave never
reads or copies the credential bytes. Normal token refresh may write through the
binding to its source; the acknowledgement is required and frozen. Capture and
preflight do not validate login availability. Do not silently switch subscription
authentication to API billing. Shell functions require an explicit argv launcher;
a binary of the same name does not imply the same wrapper or credentials.

## 3. Derive one change and hold the task fixed

Save `derive.json` as `{"recipe_changes":{"model":"YOUR_CHANGED_MODEL"}}`.
This changes one effective model override while retaining the same input bytes.
For a skill or instruction change instead, use `additions` with the selected new
file bytes and `replace` naming its logical frozen blob path, such as
`global/config/skills/review/SKILL.md`; inspect the baseline profile's asset map
first. The additions capture spec must carry the exact derived recipe and explicit
include roots. `workflow` alone is a descriptive comparison label.

```sh
dryheave --store bench-store profile derive base --spec derive.json --name changed --json
dryheave --store bench-store profile diff base changed --json
```

For a concrete single-skill comparison, create `edited-skill/SKILL.md` as a
private copy of your selected review skill and edit just its instructions. Keep
the original installed skill unchanged. For the `skills/review` selection above,
generate this alternative derivation from the exact saved recipe:

```sh
dryheave --store bench-store profile inspect base --json > base-profile.json
python3 - <<'PY'
import json
from pathlib import Path

profile = json.loads(Path("base-profile.json").read_text())["data"]["profile"]
spec = {
    "additions": {
        "recipe": profile["recipe"],
        "include_roots": {"edited": "edited-skill"},
        "assets": [{"root": "edited", "path": "SKILL.md", "kind": "skill",
                    "layer": "global", "target_root": "config",
                    "target": "skills/review/SKILL.md"}]
    },
    "replace": ["global/config/skills/review/SKILL.md"]
}
Path("derive-skill.json").write_text(json.dumps(spec, indent=2) + "\n")
PY
dryheave --store bench-store profile derive base --spec derive-skill.json --name skill-changed --json
dryheave --store bench-store profile diff base skill-changed --json
```

Use that returned profile ID as the changed variant below to compare skill bytes
while holding model/effort constant. Other captured resource files stay frozen
from the parent. If the change also needs scripts or references, select their
new bytes explicitly and list every colliding logical blob in `replace`.
For Codex, replaced or removed skill-source paths remain frozen in
`superseded_skill_paths` and disabled in the generated native rules, so the old
installed copy cannot compete with the selected replacement. Inspect that list
and the profile diff; unrelated ambient skill sources remain a fidelity limit.

Save `experiment.json` using the real case ID and actual profile IDs returned
above. The simulator is a separately authorized role with its own model calls:

```json
{
  "name": "One-model comparison",
  "cases": ["CASE_ID"],
  "variants": [{"name": "base", "profile": "BASE_PROFILE_ID"}, {"name": "changed", "profile": "CHANGED_PROFILE_ID"}],
  "repetitions": 2,
  "seed": 17,
  "simulator": {
    "kind": "codex", "isolation": "trusted-native",
    "command": {"argv": ["/absolute/path/to/codex"]},
    "version": "0.153.4", "model": "YOUR_SIMULATOR_MODEL", "effort": "medium",
    "budget": {"max_calls": 3, "call_seconds": 30, "total_seconds": 90, "max_input_bytes": 131072, "max_output_bytes": 65536}
  }
}
```

The simulator receives only approved facts, reviewed persona strings and current
dialogue. It uses explicit native authentication independently of the subject.
A scripted simulator is suitable only when its literal matching and divergence
behavior are intentional. Optional judge configuration belongs in `scoring.judge`;
it has separate budgets and usage. No current pricing is assumed: add an explicitly
dated/versioned `scoring.prices` table only when you want saved rate estimates.

```sh
dryheave --store bench-store experiment validate experiment.json --json
dryheave --store bench-store experiment create experiment.json --name comparison --json
dryheave --store bench-store run comparison --mode native \
  --tui-test /absolute/path/to/tui-test --runtime-root rt --json
dryheave --store bench-store run --status RUN_ID --json
dryheave --store bench-store assess RUN_ID --json
dryheave --store bench-store report RUN_ID --json
dryheave --store bench-store compare RUN_ID RUN_ID --before-variant base --after-variant changed --json
```

Run IDs appear on stderr when reserved and under `data.run_id` on successful
execution. Resume an interrupted run with `run --resume RUN_ID`; a deliberate
`--retry TRIAL_ID` retains prior attempts and spending. Assessment is separate
and resumes without relaunching the subject.

## 4. Interpret and share retained results

Read `paired_count`, exclusions and all group counts. Compare eligible completion
and duration/token distributions alongside unstarted/failed/retried/excluded
attempts and all-attempt spending. `known_spend_by_currency` is an observed
subtotal; `unknown_cost_records` and `partial_cost_attempts` prevent treating it as
a complete total. Subject, simulator and judge roles remain separate. Missing
telemetry is unknown, and reasoning is an output subset. Two repetitions are an
initial check, not statistical significance or an isolated causal experiment:
browser/device/network/model-service state can differ even with frozen inputs.

```sh
dryheave --store bench-store export RUN_ID --output results.tar --json
dryheave --store imported-store import results.tar --json
dryheave --store imported-store report PORTABLE_ROOT_ID --json
```

Use `data.roots[0]` from import for the portable report. Default exports contain
curated inputs and structured results. Raw terminal/native logs, final files and
grading artifacts require explicit sensitive-class selection. Curated inputs may
still contain private text; review them before sharing.
