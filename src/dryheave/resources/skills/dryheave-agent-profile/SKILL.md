---
name: dryheave-agent-profile
description: Prepare named Dryheave subject profiles and controlled model or effort comparisons from explicitly selected native configuration. Use when asked to create an agent profile, compare Terra low and high, or prepare native launch inputs for a benchmark. Do not inspect credentials or run subjects implicitly.
---

# Prepare subject profiles

## Instructions

1. Inspect `dryheave profile --help` and the workspace's selected problem/case.
   Agree on executable/version, model/effort, selected configuration/instructions/
   skills/plugins and explicit runtime references. Read only selected paths;
   never scan environment inventories, shell startup or authentication contents.
2. Prefer the available public profile helpers shown in help. Otherwise use the
   shipped `profile capture NAME --spec PATH --json` and
   `profile derive BASE --spec PATH --name NAME --json`. Write internal specs
   yourself using the packaged real workflow from `example write --target FRESH_DIRECTORY`.
   The user supplies preferences, not JSON. Capture freezes selected bytes;
   reference authentication opaquely with the existing runtime binding mechanism.
3. New capture recipes disable all six operator names: `dryheave-collect`,
   `dryheave-case`, `dryheave-results`, `dryheave-voice-profile`,
   `dryheave-generate-problem`, `dryheave-agent-profile`. `profile capture` fills
   that list when omitted. Explicit lists are honored, and old frozen recipes
   retain their historical three-name default. Do not accidentally capture or
   enable these operator instructions for benchmark subjects.
4. For a Terra comparison, create one captured base with the requested supported
   executable/version, `model: "gpt-5.6-terra"` and `effort: "low"`. Derive high
   using `{"recipe_changes":{"effort":"high"}}`. Inspect both immutable IDs
   and run `profile diff LOW HIGH --json`; only effort should differ.
5. Inspect `profile preflight --help`; preflight records exact argv, unresolved
   ambient influences and authentication/permission requirements. Native HOME
   and fresh config roots do not prove host or discovery isolation. Preserve
   these limitations. Neither voice style nor benchmark authoring authorizes
   trust/authentication/permission dialogs.
6. Use available public experiment helpers, or write the internal experiment
   spec yourself from the packaged example, pinning frozen case/profile IDs,
   independent simulator limits and repetitions. Validate before create. Report
   the experiment reference and exact next run command; execution remains a
   separate explicit user action. More repetitions create fresh trial workspaces;
   retries preserve previous evidence and should not be used to force success.

## Examples

For “compare this task with Terra low and high”, capture the selected native
configuration once, derive only the effort change, inspect the diff, and pin the
two profiles with the same calibrated case and simulator into an experiment.
Return its name and IDs, fidelity limits and the supported run command.

## Troubleshooting

- Unsupported native version or flag: retain the preflight error and consult
  installed public help; do not claim launch compatibility or invent an adapter.
- Edited/foreign config or skills: preserve source bytes and choose explicit
  intended inputs. Installing operator skills never authorizes global changes.
- Missing authentication or trust: retain the runtime-reference requirement for
  user action. Never read auth contents or automate permission dialogs.
