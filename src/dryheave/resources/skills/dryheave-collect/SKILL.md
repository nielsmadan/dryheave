---
name: dryheave-collect
description: Discover and import Codex or Claude JSONL sessions as benchmark evidence. Use when asked to collect benchmark tasks, find reusable agent sessions, or import a coding-agent log for Dryheave. Do not use for general log debugging.
---

# Collect benchmark evidence

## Instructions

1. Get an explicit log directory or file and agent kind. Keep source logs and
   repositories read-only. Do not search authentication, shell initialization,
   credential stores or unrelated home directories.
2. Run `dryheave --store STORE collect scan --agent codex --root LOG_DIRECTORY --json`
   (or `--agent claude`). Inspect `data.sessions[].warnings` and `candidates`;
   suggestions indicate useful signals, not recovered task intent.
3. Import selected evidence with
   `dryheave --store STORE collect import LOG_PATH --agent codex --json`.
   Save `data.id`, then run `dryheave --store STORE collect show SESSION_ID --json`.
   Read `data.session.events` and warnings before proposing a task boundary.
4. Propose an initial user event, an inclusive final event, task intent and an
   explicitly verified historical commit. Never infer a missing baseline from
   current HEAD. Preserve ambiguity as an unresolved draft issue.
5. Hand off to `dryheave-case` with session ID, event IDs and baseline evidence.
   Full transcripts stay curator evidence. Review every excerpt separately before
   declaring it safe for a simulated user or subject.

All structured commands return `{ "ok": true, "data": ... }` on stdout;
errors have `ok: false` and `error.code/message` on stderr with nonzero exit.
Use the same explicit store for later commands. Imported usage may be incomplete;
never sum cumulative records or turn missing categories into zero.

## Examples

For “find a reusable task in these Codex logs”, scan the selected directory,
import one candidate and show its events. Return its immutable session ID, a
proposed event range, exact baseline evidence and unresolved questions. An import
success establishes retained source bytes; it does not establish a valid case.

## Troubleshooting

- `baseline_unknown`: locate an explicit historical SHA in trusted task evidence;
  leave the draft unresolved until verified.
- Malformed, truncated or conflicting records: retain the warnings and choose a
  bounded supported source; do not silently fill missing conversation.
- Byte/event limits: select a smaller relevant log or deliberately raise the
  documented `--max-bytes` / `--max-events` bounds. Do not edit the source file.
