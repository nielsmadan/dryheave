# Collection, authoring and repository contracts

The shipped authoring commands use the same global `--store PATH` and `--json`
options as the storage commands. Development examples below use `.dryheave`,
which is ignored. Draft files are editable JSON; creation refuses to overwrite an
existing file. `validate` and `freeze` return nonzero for unresolved inputs.

```sh
uv run dryheave --store .dryheave collect scan --agent codex --root LOG_DIRECTORY --json
uv run dryheave --store .dryheave collect import LOG_PATH --agent codex --json
uv run dryheave --store .dryheave collect show SESSION_ID --json
uv run dryheave --store .dryheave case draft SESSION_ID --repo SOURCE_REPO --commit FULL_SHA --out .cache/case.json
uv run dryheave --store .dryheave case validate .cache/case.json
uv run dryheave --store .dryheave case freeze .cache/case.json --json
uv run dryheave --store .dryheave case inspect CASE_ID --json
uv run dryheave --store .dryheave persona draft SESSION_ID --out .cache/persona.json
uv run dryheave --store .dryheave persona create .cache/persona.json --json
uv run dryheave --store .dryheave persona inspect PERSONA_ID --json
```

`case draft` accepts `--start EVENT_ID` and `--end EVENT_ID` for an inclusive
message range. Start must contain user text. Omit both repository arguments to
create a visibly unresolved draft; supply both to capture an explicit baseline.
`--initial-patch PATH` freezes selected uncommitted starting work. Current source
working-tree edits are never inferred as the initial patch. Draft suggestions
identify signals such as user messages, tool activity and assistant questions;
the curator still determines intent, task boundaries and grading.

## Authoring and visibility

The draft needs `intent_confirmed: true`, `facts_reviewed: true`, at least one
required grading criterion, and an empty `unresolved_issues` list after those
issues are resolved. The reviewed `allowed_facts` list may be empty when the
initial prompt fully specifies the task. Facts contain stable `fact_id`, text,
`disclosure` (`on_request` or `proactive`) and source evidence, or the explicit
`curator_authored: true` declaration. A persona needs instructions, disclosure
and unknown-answer policies and `reviewed_subject_safe: true`. Persona examples
and fact evidence must be classified `subject` after reviewing for solution text.

A draft selects exactly one inline `persona` or immutable `persona_id`. Standalone
`persona create` freezes a curated JSON file; `persona draft` makes an editable
starting point. Manually authored personas may omit examples. Any examples that
are present must be verified against imported source events at freeze time.

Every `EvidenceExcerpt` retains `session_id`, `event_id`, `visibility` and an exact
nonempty substring `excerpt` from the source event text. Case evidence belongs in
the selected source range. Source session IDs are provenance, not reference-closure
dependencies. Frozen cases reference only their repository and persona objects;
personas contain reviewed excerpts with no full transcript dependencies. Normal
portable copies can replay and validate these three objects without source logs.
Imported session objects remain potentially sensitive curator evidence.

For replay, call `cases.load_frozen_case(store, reference)`,
`personas.load_frozen_persona(store, reference)` and
`repositories.load_repository(store, reference)`. These validate domain invariants,
manifest blob maps and reference closure without consulting original sessions.
`FrozenCase` itself rejects unreviewed facts, unresolved intent and missing or
nonunique required grading criteria. Generic `ObjectStore.get` checks storage
integrity; callers must still use these domain validators for runtime inputs.

`cases.subject_context(case)` returns only initial prompt and allowed facts.
`personas.subject_persona(persona)` returns only communication policies and
reviewed example strings. The full case, snapshot provenance, grading criteria,
reference patch and store paths belong to curator/judge code. These projections
are intentional visibility boundaries; they do not prove semantic non-leakage.

## Setup and grading handoff

`CaseContent.setup` contains ordinary `CommandSpec` values with argv, relative cwd,
timeout/output bounds and inherited environment-name references. It does not run
commands during authoring. `CaseBudgets` separates subject duration, accepted-turn
budget and setup duration. `CapturePolicy` freezes file/byte/depth limits and
explicit relative-path exclusions and ignored-file inclusions for the runner.

A deterministic criterion looks like this in draft JSON:

```json
{
  "criterion_id": "greeting",
  "kind": "deterministic",
  "description": "The greeting includes the requested punctuation and name.",
  "required": true,
  "entrypoint": "check.py",
  "command": {
    "argv": ["python3", "{verifier}/check.py"],
    "timeout_seconds": 30,
    "max_output_bytes": 65536
  },
  "expected_stdout": "GREETING_CHECK_EXECUTED",
  "expected_failure_stdout": "GREETING_ASSERTION_FAILED",
  "suite_id": null,
  "calibration": "uncalibrated"
}
```

The draft's `hidden_files` maps verifier-relative targets to explicitly selected
source paths, resolved relative to the draft JSON directory. Frozen cases map
those targets to immutable blob names such as `verifiers/check.py`. A deterministic
command must name its hidden entrypoint as the exact argv element
`{verifier}/ENTRYPOINT`; the grader substitutes its private verifier directory
without shell interpolation. It must execute from an independent final-workspace
copy, use the trusted entrypoint bytes, and require both a successful exit and the
expected stdout sentinel. Positive nonzero exits require `expected_failure_stdout`,
or the common `expected_stdout` marker when no separate failure marker is configured.
Missing evidence and signal termination are errors, so syntax/import/setup failures
cannot become task failures or demonstrated baseline calibration. Emit success only
after all checks pass, and catch only expected assertion failures around the bounded
checks:

```python
import runpy

greet = runpy.run_path("greet.py")["greet"]
try:
    assert greet("Niels") == "Hello, Niels"
    assert greet("") == "Hello, "
except AssertionError:
    print("GREETING_ASSERTION_FAILED", flush=True)
    raise SystemExit(1)
print("GREETING_CHECK_EXECUTED", flush=True)
```

Keep verifier imports and setup outside that assertion wrapper. A success marker
printed before the checks does not establish their execution.

Default `suite_id: null` means an independent copy per criterion. A shared non-null
suite ID declares a suite whose criteria execute in saved order on the same copy.
A judge criterion has `criterion_id`, `kind: "judge"`, `required` and a substantive
`rubric`. `reference_patch` optionally names a curator-only source file; freeze
stores it as `reference.patch`. Setup and grading execution/calibration belong to
later runner/grading services. Authoring validation rejects empty entrypoints,
missing files, blank sentinels, obvious placeholders and empty rubrics; it cannot
prove that a curator's program tests the intended behavior. Criteria remain
explicitly uncalibrated until later execution records establish otherwise.

## Historical repository snapshots

`capture_repository(store, repo, commit, initial_patch=..., limits=...)` requires a
full lowercase commit SHA and creates a `RepositorySnapshot` with exact tree
identity, tracked `tree.tar`, exact ancestor `ancestry.pack`, optional
`initial.patch`, declared limits and curator provenance. It reconstructs and
validates the selected input before publishing the snapshot. Source repositories
and logs are read-only.

Capture creates its own Git metadata and points Git only at the selected source
object directory. Source/global Git configuration, hooks, templates, attributes,
replacement refs and working-tree contents do not drive capture. The archive is
built directly from tree/blob bytes, including `export-ignore` files and literal
`export-subst` placeholders. Source refs, reflogs, remotes and alternate stores
are never copied. Complete reachable ancestry is mandatory in v1; synthetic
roots are not offered. Shallow, partial/promisor and alternate object stores,
submodules, LFS pointers, external/cyclic/dangling symlinks, unsafe paths and
case-colliding tree entries fail visibly.

`materialize_repository(store, reference, destination)` requires a new destination
and returns the snapshot model. It creates independent Git objects and files,
verifies that the pack contains exactly the selected reachable closure and verifies
the tree/archive identity. An optional patch is applied in the index and its
resulting tree validated before working-tree files are written. Symlinks resolve
components in filesystem order, including intermediate links before `..`, and
must stay inside the workspace. Materialization writes exact bytes without
checkout filters and leaves only `refs/heads/baseline` and an index matching that
baseline. Normal `git log`, `git blame` and `git diff` remain useful. No source path,
remote, reflog or alternate is installed in operational Git metadata. Errors remove
only the new owned destination. Capture scratch directories live under the chosen
store and are removed when their owning operation ends.

Snapshot `source_path` and `source_git_path` are curator-only audit provenance.
`known_disallowed_commits` inventories bounded locally present commits outside
baseline ancestry, including unreachable objects. `audit_coverage` is
`complete_at_capture` or `partial`, with `audit_limit` and warnings. This inventory
cannot cover commits created later. `FrozenCase.source_path_hash` copies SHA-256 of
the imported transcript's absolute path string, so later observed tool paths can
be compared without requiring the original Session. Preserve matching-path evidence
in later audit results; a hash is not an access-prevention mechanism.

The default capture bounds are 20,000 files, 64 MiB tracked bytes, 128 MiB packed
history, 100,000 objects, 10,000 known disallowed commits and 120 seconds per Git
operation sequence. The object-store per-blob limit still applies. Native replay
isolates repository objects; it does not restrict host, source-log or network
access by an agent.

## Log and process APIs

`logs.service.import_session(store, path, AgentKind, limits)` freezes a `Session`.
`logs.codex.parse` and `logs.claude.parse` return the same model without writing.
`logs.base.discover` scans non-symlink JSONL files with depth/entry limits.
`ImportLimits` defaults to 16 MiB, 20,000 source records, 10,000 scan entries and
12 directory levels; CLI import/scan expose byte and event limits. A malformed
record is skipped with a source-line warning; unterminated final records, missing
metadata, conflicting identities and baseline ambiguity remain visible.

Events retain source-line/event IDs, timestamps, native IDs, turn IDs, useful text
and selected structured tool/lifecycle metadata. Encrypted reasoning, reasoning
blocks, base instructions, hook bodies and compaction replacement histories are
excluded. The adapter reads both Codex legacy response/event messages and 0.153.4
paginated capitalized item completions; duplicate message representations collapse
while distinct turns remain distinct. Claude text/tool blocks remain ordered.

Codex dialogue deduplication indexes ordered candidates by native ID, text and
turn. It consumes the earliest compatible event representation, including
missing-turn fallbacks, without rescanning all previous candidates per message.

Usage keeps raw category records and provenance. Codex response IDs take precedence
over legacy cumulative token counts; the latter remain marked `superseded` when
response records exist. Without response records, cumulative records remain
cumulative and must never be summed as separate calls. Claude snapshots deduplicate
by message ID. Input categories are non-overlapping: Codex uncached input subtracts
cached and cache-write input only when both categories are observed; otherwise it
remains unknown. Claude input is already uncached. Unknown categories remain null.
Claude `cost_state` metadata is preserved separately from
per-message usage. Mixed or incomplete recorded coverage does not establish a
complete session cost.

Sanitized real-format fixtures and public version/shape provenance live in
`tests/fixtures/`. They derive from installed Codex 0.153.4 and Claude 2.1.263
JSONL records. Free text, identifiers and paths are synthetic replacements; the
fixtures establish protocol-shape compatibility, not recovery of private task
intent. Private source path/line mappings remain ignored under `.cache`.

`processes.run_command(CommandSpec, root, input_bytes=..., environment=...)`
returns `CommandResult` with stdout/stderr bytes, exit code, `exited`/`timeout`/
`output_limit` outcome and cleanup-coverage text. It drains both pipes, writes
stdin without deadlock, bounds total output and wall time, and stops its owned
process group before reaping on failure. A normally reaped PID is never signaled.
Descendants retaining inherited pipes remain bounded; detached children or children
that close those pipes are unobserved. Later native runner code must retain its
own writer/session ownership and cannot infer complete quiescence from this helper.
