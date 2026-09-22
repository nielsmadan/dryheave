# Mine problems and a user voice

Initialize a dedicated benchmark workspace, install the packaged skills there,
and explicitly select source logs. Source logs and repositories remain read-only.
These commands do not scan agent homes or make model calls.

```sh
dryheave init ./bench
cd bench
dryheave skills install
dryheave collect scan --agent codex --root /explicit/log-directory --json
dryheave collect select work /explicit/session-one.jsonl /explicit/session-two.jsonl /explicit/session-three.jsonl --agent codex --json
dryheave collect selection work --json
dryheave problem request "Find tasks involving CLI behavior" --selection work --name cli-tasks --json
```

`collect scan` reads each log under the same bounds as import and reports, per
session, how much of it is the user actually writing: `user_messages` counts
`user_events`, how many are `genuine` conversation, how many are
`harness_injected` wrapper blocks, how many stayed `unclassified`, and the
`genuine_characters` total with the `median_genuine_characters` of those genuine
messages. Sessions are returned richest first — by genuine count, then by total
genuine characters — so a session of ten `continue` messages sorts below one
carrying real paragraphs. Nothing is scored for you; the components are
measurements and the choice stays yours.

Each scanned session also reports the `cwd` and `baseline_commit` its log recorded:
the working directory Codex wrote into `session_meta`, and the `git.commit_hash` of the
repository when the session started. Both are `null` when the log records neither.
Neither is inferred or substituted, and a Claude log records no commit hash at all, so
its `baseline_commit` is always `null`. That recorded baseline is the join key the
commit survey below uses.

A voice built on one session describes that session. Select several, spanning
different kinds of work, up to the 24 a selection holds. Selection membership is
immutable, so widening one means creating a new selection with more files. Both
`--agent codex` and `--agent claude` are accepted, for scanning and for selecting.

## Start from Git history

Choosing which session to mine by reading conversations is guesswork. Commit messages
are curated summaries and the diff shows whether anything verifiable changed, so survey
the repository first and let the commit lead back to its session:

```sh
dryheave collect survey --repo /explicit/source-repo --root /explicit/log-directory \
  --agent codex --max-commits 200 --max-candidates 10 --json
dryheave collect commit feb4d909 --repo /explicit/source-repo \
  --root /explicit/log-directory --agent codex --json
```

Both commands read the repository read-only through `git log` and `git rev-parse`; they
write nothing and create no objects. `--max-commits` bounds the survey to the newest N
commits reachable from HEAD (default 200, maximum 2,000) and `--max-candidates` bounds
the reported candidates (default 10, maximum 50). The response repeats both bounds.
`--root` and `--agent` go together: `--root` without `--agent` and `--agent` without
`--root` are both refused, and omitting them surveys commits with every candidate's
`join` reported as `not_scanned`.

`collect survey` assumes no commit convention. It first measures the repository's own
subjects: the token before a `:` delimiter, with digit runs folded to `#` so
`TICKET-123:` and `TICKET-124:` count as one `ticket-#` scheme. A convention is declared
only when at least 20 commits were sampled, at least 60% of them carry such a token, and
at most 12 tokens cover at least 90% of the prefixed subjects. `convention` reports
`detected`, the measured `prefixed_share`, the inferred `vocabulary` with each token's
count and whether it is bug-indicating, `vocabulary_coverage` and a `reason` naming the
threshold that decided it, so the inference is visible rather than assumed.

Candidate selection then uses the first tier that produces anything, and every candidate
carries the `tier` and the `signal` that selected it:

| Tier | Selection | What the signal shows |
| --- | --- | --- |
| 1 | Bug-indicating members of the detected vocabulary (`fix`, `bugfix`, `hotfix`, and similar) | Which token matched, and the vocabulary it belongs to |
| 2 | Bug-indicating words anywhere in the subject or body, case-insensitive | Which words matched |
| 3 | Change shape alone: fewest files first, then fewest changed lines, at most five files | The file and line counts, and an explicit statement that this is no evidence of a bug fix |

Merge commits are excluded. So are commits touching only documentation or only
configuration: a benchmark case needs a behavior change a hidden verifier can execute,
and a commit that only edits Markdown or a settings file leaves nothing to verify. They
are not dropped silently — `merges_excluded`, `change_class_excluded` and a bounded
`excluded` list report each one with its reason, so a docs-heavy window is visible
instead of looking like an empty repository.

The join uses one fact: a session records the commit hash of the repository when it
started, so for a commit `C` produced in that session, `C`'s parent is the baseline the
session recorded. Each candidate reports its `parent` and every scanned session whose
recorded baseline equals it, with that session's `cwd` so the repository can be
confirmed — the cwd is reported, never used to filter, because the same repository is
often checked out in several directories. `join` is one of:

| `join` | Meaning |
| --- | --- |
| `unique` | Exactly one scanned session records that parent as its baseline. |
| `ambiguous` | Several scanned logs record that parent; the commit cannot be attributed to one of them. Each match repeats the `source_id` and `parent_session_id` its log states, which is how a subagent or resumed rollout shows up where the log records one. |
| `none` | No scanned log records that parent. The commit may be hand-written, or its session deleted, or outside the scanned root. |
| `no_parent` | A root commit, which has no prior state for any session to record. |
| `not_scanned` | No log root was given, so no join was attempted. |

Even a `unique` match is partial: a session records only the baseline it *started* from,
so the join finds that session's first task, not every commit it produced. The
`join_note` says so on every candidate. Sessions that could not be parsed within the
import bounds are skipped rather than failing the scan, and are counted in
`sessions_skipped` with bounded `skipped` reasons.

`collect commit SHA` resolves one remembered commit the same way and hands over to the
unchanged pipeline. It accepts a lowercase hexadecimal SHA of at least seven characters;
branch names and `HEAD` are refused, and a merge commit is refused because it records no
single starting baseline. The response reports the full commit, its `parent` and the
matching sessions, and its `next` names the ordinary commands:

```sh
dryheave collect select from-commit /explicit/session.jsonl --agent codex --json
dryheave problem request "Fix detected in commit feb4d909" --selection from-commit --json
```

The reported parent is a claim from the log, not a verified baseline. It still has to be
checked against the read-only source repository before `case draft --commit FULL_SHA`,
and today's HEAD is never a substitute.

## Collect, triage, derive

Voice curation runs in three stages. **Collect** candidate sessions with
`collect scan` and pin the ones you mean to examine with `collect select`; that
selection is the examined frame, not yet the chosen one. **Triage** each pinned
session — read it, judge what kind of session it is, and record whether it was
chosen or rejected and why. **Derive** the voice from the chosen sessions with
`voice draft` and `voice create`.

```sh
dryheave collect selection work --json
dryheave collect triage work --session SESSION_ID \
  --kind feature --grade medium --decision chosen \
  --reason "A two-turn feature request in the parser repository" \
  --expect-revision 1 --json
```

The vocabulary is exactly eight categories, expressed as a kind and, where the
kind has one, a grade on that kind's own scale:

| `--kind` | `--grade` | Category |
| --- | --- | --- |
| `config_change` | none | config change |
| `feature` | `small` / `medium` / `large` | feature small/medium/large |
| `bugfix` | `easy` / `medium` / `hard` | bugfix easy/medium/hard |
| `extraneous` | none | extraneous |

A feature or bugfix without its grade is rejected, and so is a grade from the
other kind's scale or a grade on a kind that has none. Triage is a catalog
mutation like any other: it requires `--expect-revision N`, and re-recording one
session replaces that session's decision while preserving the rest.

`collect triage`, `collect selection`, `voice draft`, `voice create` and
`voice inspect` all return `triage`: each judged session with its `repository`
(the session's recorded `cwd`, or null when the log recorded none), the
`untriaged` list, and a `variety` summary. `variety` counts the examined
`sessions`, how many are `triaged` and `untriaged`, how many were `chosen` and
`rejected`, the `kinds` distribution of the chosen sessions, their
`repositories` spread with `unknown_repositories` for chosen sessions with no
recorded `cwd`, and a `varied` verdict that is true only when the chosen sessions
span more than one kind *and* more than one repository. `triage_warnings` names
sessions that still carry no decision, says when every triaged session was
rejected, and says when the chosen set comes from one kind of work or one
repository. Nothing is refused for thin or lopsided sampling; the warning travels
with the voice instead. `collect selection` also reports each session's
`repository` and its own triage decision, so the repository spread is visible
before any reading starts.

`voice create` recomputes the triage from the selection and stores it on the
voice record beside the computed evidence, so `voice inspect` reports later what
the sampling frame was: how many sessions were examined, what kinds they were and
which were rejected. Voices created before triage existed keep parsing; their
`triage` is null.

In an operator agent that discovers `.agents/skills`, invoke the installed skills.
For Codex, actual invocation syntax is:

```text
$dryheave-voice-profile Create my voice from selection work.
$dryheave-generate-problem Work through request cli-tasks using that voice.
$dryheave-agent-profile Prepare Terra low and high profiles for the frozen case.
```

For Claude Code, install into its project-local discovery directory:

```sh
dryheave skills install --target .claude/skills
```

Then invoke `/dryheave-voice-profile`, `/dryheave-generate-problem` and
`/dryheave-agent-profile` with the same selection/request names. Use
`collect select ... --agent claude` when the selected source logs are Claude
logs; the operator's agent need not match the source-log agent.

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
| `collect scan --agent codex --root DIR` | List bounded candidate logs with per-session user-message signal, recorded `cwd` and `baseline_commit`, richest first. |
| `collect survey --repo PATH [--root LOG_DIR --agent AGENT]` | Survey bounded read-only history for candidate commits with their tier, signal and session join. |
| `collect commit SHA --repo PATH [--root LOG_DIR --agent AGENT]` | Resolve one remembered commit to its baseline parent and the sessions recording it. |
| `collect select NAME [FILE ...] --agent codex` | Import 1–24 explicitly chosen files into a new immutable selection. |
| `collect select NAME --session ID [--session ID ...]` | Pin existing imported sessions; files and IDs may be combined. |
| `collect selections` | List names, IDs, session counts and catalog revision. |
| `collect selection NAME_OR_ID` | Inspect immutable membership, each session's recorded repository and triage, and bounded source warnings. |
| `collect evidence NAME_OR_ID --session ID` | Page actual messages and selected tool context. |
| `collect metadata NAME_OR_ID --session ID [--key KEY]` | Page recorded session metadata, including `cwd` and `git.commit_hash`. |
| `collect triage NAME_OR_ID --session ID --kind KIND [--grade GRADE] --decision chosen\|rejected --reason "REASON" --expect-revision N` | Record one examined session's judged kind and sampling decision. |
| `problem request [DESCRIPTION] --selection NAME_OR_ID [--name NAME] [--micro-bug]` | Persist the request policy; omitted name is generated and returned. |
| `problem list` / `problem inspect NAME_OR_ID` | Recover requests, decisions, gaps and validation references. |
| `problem gap REQUEST "REASON" --expect-revision N` | Append missing-coverage reasoning. |
| `voice draft --selection NAME_OR_ID [--name NAME] [--out PATH]` | Create an internal draft and report computed selection evidence, without overwriting an existing file. |
| `voice create [NAME] PATH --expect-revision N` | Freeze a reviewed selected-log voice into a compatible Persona; an omitted name uses `default`. |
| `voice list` / `voice inspect NAME_OR_PERSONA_ID` | Inspect frozen voice names, provenance, policies, recorded evidence and triage. |
| `voice delete NAME --expect-revision N` | Discard a catalog voice entry; its frozen persona object is retained. |
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

## Voice names and computed evidence

An omitted voice name means `default`. `voice draft` and `voice create` use it
while it is free and fail with an explicit error once a voice holds it, so a
second voice must be named deliberately. Names stay immutable; there is no rename.

A voice curated from the wrong draft or the wrong selection is discarded with
`voice delete NAME --expect-revision N`, which removes the catalog entry and
nothing else. An unknown name is an explicit error and a stale revision is a
conflict, as with every other catalog mutation. The frozen persona object stays in
the store: cases that reference its `persona_id` still load, and every experiment
that froze it keeps its original behavior. The response repeats that persona ID so
the retained object stays identifiable. Deleting frees the name, so a later voice
may reuse it — a report must therefore say which voice it describes, since the
name alone no longer identifies one persona.

`voice draft` and `voice create` return an `evidence` object computed from the
selection itself, not supplied by the caller. It counts `user_events`, reports
which were `harness_injected` and the wrapper `reasons` that classified each one
(AGENTS.md/`user_instructions` blocks, `environment_context`, `skill`, command
wrappers and system reminders), which remain `genuine`, and which stayed
`unclassified` because no recognized shape explains them. Each genuine candidate
carries its `event_id`, `characters` and a short `preview`. `sessions` repeats the
same counts per selected session, with that session's `genuine_characters` and
`median_genuine_characters`, so coverage is visible: a selection of six sessions
whose genuine messages all come from one of them is still a one-session voice.
Sessions that contributed nothing are listed with zero counts rather than omitted.
A selection is `sufficient` only at or above `threshold` genuine conversational
messages; below it, both commands return a `warnings` entry naming both numbers,
the genuine count of every selected session, and the `collect select` invocation
for a wider selection. Creation is never refused for thin evidence. `voice create` recomputes these figures and
persists them on the voice record, together with the triage, so `voice inspect`
reports the same measured provenance and the same sampling frame later.

Re-recording a nonfrozen candidate replaces that candidate's decision at a checked
revision. Frozen decisions are retained; revise by making a new candidate or
request. Any request edit clears its validation references. Validation snapshots
the current draft and every hidden verifier/reference-patch file into temporary
owned inputs; freezing requires the same content hash and request revision. File
edits require revalidation, even if the catalog revision did not change. A stored
validation reference describes the last check; freeze always verifies current
content. Validation is structural, not a demonstrated passing benchmark.

Calibrate the frozen case before subject spend:

```sh
dryheave case calibrate CASE_ID --json
dryheave case calibration CALIBRATION_ID --json
dryheave case calibration CALIBRATION_ID --evidence PATH_FROM_FILES --limit 4000 --json
```

The first command returns `data.id` for a standalone immutable calibration and
`data.calibration.status`. `demonstrated` means every deterministic criterion
observed an assertion-failing baseline and successful reference. `ineffective`
means a baseline passed. `unavailable` includes verifier errors, missing reference
patches or incomplete retained evidence. Judge-only cases are `not_applicable`;
no judge/model calls run during calibration. Inspect each criterion's baseline
and reference execution, including `outcome`, `error`, `returncode` and markers.
The `files` map lists retained output paths. Evidence reads use byte offsets and
return `next_offset`, byte length and truncation; invalid UTF-8 displays replacement
characters while the immutable bytes and hashes remain exact.

Verifier copies use the same command/environment rules as assessment. They do
not execute `case.setup`: required trusted tools and declared environment-name
references must already be available. Suite criteria execute in frozen order on
one copy per side; other criteria get independent copies. Source repositories
remain read-only. Commands have frozen finite time/output limits. Cancellation
stops recorded owned processes and retains working evidence under
`STORE/calibrations/OPERATION_ID`; retrying starts a fresh calibration after
reconciliation. Native run and assessment also reconcile interrupted calibration
ownership before proceeding. Unresolved writers block evidence publication.

Correct the draft/reference/verifier when calibration is unavailable or
ineffective, validate/freeze a new case and calibrate its new ID. Old calibrations
keep describing their original frozen bytes even when aliases or drafts change.
A zero command exit means the record was saved; inspect its status before spend.
Share the exact standalone record and input closure explicitly:

```sh
dryheave export CALIBRATION_ID --include-sensitive calibration-evidence --output calibration.tar --json
dryheave --store imported-store import calibration.tar --json
dryheave --store imported-store case calibration CALIBRATION_ID --json
```

This export contains curator-only verifier output and hidden case inputs; it
requires the sensitive selection. Then use profile/experiment commands and
`dryheave-results` to compare repeated runs. Repetitions reuse frozen
IDs with fresh subject workspaces. Retries preserve earlier attempts. New voices,
source selections or task content need new frozen inputs and a new experiment;
old experiments retain their original behavior.

## Freeze profiles and adaptive simulation

Friendly setup does not launch agents. For the approved Haiku-default versus
Sonnet-low configuration comparison, select explicit provider model IDs:

```sh
dryheave profile create haiku --agent claude --model claude-haiku-4-5-20251001
dryheave profile derive haiku --model claude-sonnet-4-6 --effort low --name sonnet-low
dryheave profile diff haiku sonnet-low --json
dryheave experiment setup comparison --case CASE_ID \
  --profile haiku=haiku --profile sonnet-low=sonnet-low \
  --simulator claude --simulator-model claude-haiku-4-5-20251001 \
  --simulator-max-calls 3 --simulator-call-seconds 30 --simulator-total-seconds 90 \
  --design-approval
dryheave run comparison --assess --json
```

These model IDs are explicit example choices, not auto-discovered current defaults.
Calibrate CASE_ID first. Add only explicitly selected `--config`, `--instruction`,
`--skill` and `--plugin` inputs to profile creation. Both profiles retain the same
selected bytes and runtime references. Haiku has no effort flag; deriving back
from Sonnet needs `--model claude-haiku-4-5-20251001 --clear-effort`.
Do not describe this as an effort-only comparison.

Claude setup requires the runtime reference `CLAUDE_CODE_OAUTH_TOKEN`; no helper
reads its value. Subjects use their native TUI and stop on native trust/auth/
permission dialogs. The simulator uses print mode with built-in tools disabled,
strict empty MCP config and bounded separately owned runtime. Managed settings
remain a recorded fidelity limit. `--design-approval` authorizes ordinary
in-scope design conversation only; without it the policy is facts-only.
Persona writing style never authorizes actions. `--bare` and permission bypasses
are not supported.

For Codex 0.154.0, use `profile create terra-low --agent codex --model
gpt-5.6-terra --effort low`, then derive only `--effort high`. New profiles disable
plugins and bundled skills; selected config still loads. Selecting plugins needs
`--plugins selected-plugins` and records unresolved discovery/sync limitations.
`experiment setup ... --simulator codex` uses a versioned trusted-native adapter,
not a tool-free controller. `--simulator none` explicitly freezes zero replies.
Legacy JSON spec commands remain supported.

New native runs resolve transport from `run --tui-test`, workspace `tui_test`,
then PATH. `run --assess` starts assessment only after complete capture and owned
cleanup; it can run the experiment's frozen judge. Capture-only `run` remains
available. A failure retains the announced run ID and evidence; use
`run --resume RUN_ID` or `assess RUN_ID` separately. Viewing never starts models.

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
