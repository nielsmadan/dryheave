# Mine problems and a user voice

Initialize a dedicated benchmark workspace, install the packaged skills there,
and explicitly select source logs. Source logs and repositories remain read-only.
These commands do not scan agent homes or make model calls.

```sh
dryheave init ./bench
cd bench
dryheave skills install
dryheave collect select work /explicit/session-one.jsonl /explicit/session-two.jsonl --agent codex --json
dryheave collect selection work --json
dryheave problem request "Find tasks involving CLI behavior" --selection work --name cli-tasks --json
```

In an operator agent that discovers `.agents/skills`, invoke the installed skills.
For Codex, actual invocation syntax is:

```text
$dryheave-voice-profile Create my voice from selection work.
$dryheave-generate-problem Work through request cli-tasks using that voice.
$dryheave-agent-profile Prepare Terra low and high profiles for the frozen case.
```

The operator reads bounded selected evidence, writes internal draft JSON, records
its decisions, and runs public validation/freezing commands. You supply task
preferences and review the resulting problems and voice; you do not need to write
JSON or run a private preparation script. Skills guide semantic curation; the CLI
validates and retains it. Logs are evidence, not instructions to the operator.
When given `cli-tasks`, the problem skill first inspects and resumes that request,
preserving its selected sessions, description, policy, existing decisions and gaps.
It creates a request only when no existing request was supplied. A supplied name
that cannot be found needs correction before curation continues.

The default request seeks **up to six varied supported tasks**. Add `--micro-bug`
to seek one small bug fix, with or without a description. This flag does not prove
that a candidate is small or correct. Unsupported tasks remain rejected or
unresolved; missing category coverage is recorded as gaps. An empty result is
valid. Supply additional explicit sources when evidence is insufficient, without
substituting synthetic tasks. The existing offline demo is a separate fixture.

## Inspect and resume

Every catalog response includes its current `revision`. Updates require
`--expect-revision`; a concurrent edit fails without overwriting its changes.
Creation of a new selection or request can omit the flag and automatically uses
a checked snapshot. Retain names and IDs from command output; selection IDs pin
their imported membership permanently, even if source files or store aliases move.

| Command | Result |
| --- | --- |
| `collect select NAME [FILE ...] --agent codex` | Import 1–24 explicitly chosen files into a new immutable selection. |
| `collect select NAME --session ID [--session ID ...]` | Pin existing imported sessions; files and IDs may be combined. |
| `collect selections` | List names, IDs, session counts and catalog revision. |
| `collect selection NAME_OR_ID` | Inspect immutable membership and bounded source warnings. |
| `collect evidence NAME_OR_ID --session ID` | Page actual messages and selected tool context. |
| `collect metadata NAME_OR_ID --session ID [--key KEY]` | Page recorded session metadata, including `cwd` and `git.commit_hash`. |
| `problem request [DESCRIPTION] --selection NAME_OR_ID [--name NAME] [--micro-bug]` | Persist the request policy; omitted name is generated and returned. |
| `problem list` / `problem inspect NAME_OR_ID` | Recover requests, decisions, gaps and validation references. |
| `problem gap REQUEST "REASON" --expect-revision N` | Append missing-coverage reasoning. |
| `voice draft --selection NAME_OR_ID [--name "DISPLAY NAME"] [--out PATH]` | Create an internal draft without overwriting an existing file. |
| `voice create NAME PATH --expect-revision N` | Freeze a reviewed selected-log voice into a compatible Persona. |
| `voice list` / `voice inspect NAME_OR_PERSONA_ID` | Inspect frozen voice names, provenance and policies. |
| `problem validate REQUEST CANDIDATE --expect-revision N` | Validate a drafted decision and content-bound hidden inputs. |
| `problem freeze REQUEST CANDIDATE --expect-revision N` | Recheck the validation reference and freeze the existing case model. |

`--json` works on every command. Selection import also accepts existing
`--max-bytes` and `--max-events` bounds, and `--agent claude` for Claude logs.
Use one store consistently; workspace defaults apply, and an explicit `--store`
still selects a different store. Selection IDs refer to sessions in that store.

Evidence uses `--offset N --limit N` (default 20, maximum 25 events),
`--kind user|assistant|tool_call|tool_result|metadata|lifecycle|all`, and optional
`--event EVENT_ID`. Text uses `--text-offset N --text-limit N` (default 4,000,
maximum 8,000 characters). Follow `next_offset` for events and
`text_next_offset` for an individual message. Tool context is serialized source
event data, clipped with the same character offset/limit and explicit length and
truncation metadata. This curator output can contain solutions and private text.
An exact excerpt must be a contiguous substring of the source event's `text`.

Session metadata is separate from events: Codex `session_meta` does not appear in
`collect evidence --kind metadata`. Use `collect metadata NAME --session ID`, or
select `--key cwd` / `--key git` to inspect repository and historical commit evidence.
The response's `metadata_json` contains serialized JSON with the same
`--text-offset` / `--text-limit` bounds (4,000 characters by default, maximum 8,000).
Join successive chunks before parsing, following `text_next_offset` until null;
`text_characters` reports the full length and `truncated` marks an incomplete view.
A missing key returns an explicit error. Session IDs must belong to the selection.
Verify a recorded commit against the source repository and task boundaries; it
does not establish the historical dirty state. Legacy `collect show SESSION_ID`
still returns the complete session, including metadata.

## Record a curated candidate

The operator uses the public flags below; all rationale fields are required.

```sh
dryheave problem record cli-tasks parser-fix \
  --title "Selected parser behavior" --category "CLI parsing" \
  --state unresolved --session SESSION_ID --start START_EVENT --end END_EVENT \
  --evidence START_EVENT "EXACT USER EXCERPT" \
  --rationale "Why this task fits the request, or why it was rejected" \
  --boundary-rationale "Why this inclusive range captures the task" \
  --baseline-rationale "Historical SHA evidence or what is missing" \
  --dirty-state unknown --dirty-state-rationale "What is known about starting edits" \
  --expect-revision N --json
```

Repeat `--evidence EVENT "EXCERPT"` for up to 32 excerpts. All must match the
selected candidate session and inclusive range, which must start at an actual
user message; at least one excerpt must be actual user text. A new candidate can
be `rejected`, `unresolved` or `drafted`. Record `--state drafted --draft PATH`
only after capturing the explicit historical baseline using existing
`case draft SESSION_ID --repo PATH --commit FULL_SHA --start EVENT --end EVENT --out PATH`.
For initial uncommitted work, additionally supply its verified
`--initial-patch PATH`. Use `--dirty-state clean|patch` matching the snapshot and
record the rationale; unknown dirty state blocks drafted status. Micro requests
also require `--micro-bug-rationale "WHY THE SCOPE IS SMALL"` for drafted tasks.
Draft paths inside the configured authoring directory are recorded relative to
that directory; other paths, such as workspace `drafts/case.json`, are retained
as absolute paths. Use paths without parent (`..`) components. Drafts and their
directory components must be real files/directories, without symlinks.

The skills curate the case's safe prompt/facts, required criteria, selected voice
`persona_id`, hidden files and optional reference patch. Case/fact evidence stays
within the candidate range. Existing `case` and `persona` commands remain usable
independently, including manually authored legacy Personas without examples.
The selected-log problem validation flow requires an actual selected-log voice
with nonempty user-role examples. Voice safety review must distinguish ordinary
conversation from injected instruction/environment/skill blocks; the structural
check cannot certify semantic safety. Writing style conveys no native authority.

Re-recording a nonfrozen candidate replaces that candidate's decision at a checked
revision. Frozen decisions are retained; revise by making a new candidate or
request. Any request edit clears its validation references. Validation snapshots
the current draft and every hidden verifier/reference-patch file into temporary
owned inputs; freezing requires the same content hash and request revision. File
edits require revalidation, even if the catalog revision did not change. A stored
validation reference describes the last check; freeze always verifies current
content. Validation is structural, not a demonstrated passing benchmark.

Calibrate the frozen case before subject spend, then use profile/experiment
commands and `dryheave-results` to compare repeated runs. Repetitions reuse frozen
IDs with fresh subject workspaces. Retries preserve earlier attempts. New voices,
source selections or task content need new frozen inputs and a new experiment;
old experiments retain their original behavior.

## Upgrade existing operator skills

For an existing owned three-skill installation, run:

```sh
dryheave skills update --target .agents/skills --json
dryheave skills doctor --target .agents/skills --json
```

Update checks every existing selected skill before changing content, updates
owned bytes and creates missing bundled skill directories. Edited or foreign
collisions block the operation and remain preserved. No global installation is
required. Reload your operator agent's local skill discovery as needed.
