---
name: dryheave-agent-profile
description: Prepare named Dryheave Codex or Claude subject profiles and controlled model or effort comparisons from explicitly selected native configuration. Use when asked to create an agent profile or prepare native benchmark inputs. Do not inspect credentials or run subjects implicitly.
---

# Prepare subject profiles

## Instructions

1. Inspect `dryheave profile --help` and the workspace's selected problem/case.
   Agree on executable/version, model/effort, selected configuration/instructions/
   skills/plugins and explicit runtime references. Read only selected paths;
   never scan environment inventories, shell startup or authentication contents.
2. Use `profile create NAME --agent codex|claude --model FULL_MODEL_ID`, adding
   only selected `--config FILE`, `--instruction FILE`, `--skill DIRECTORY`
   and `--plugin DIRECTORY` inputs. Friendly setup pins Codex 0.154.0 or Claude
   2.1.278. Claude maps config/instructions to settings.json/CLAUDE.md; Codex
   uses config.toml/AGENTS.md. Legacy `capture --spec` and `derive --spec` remain
   supported for other recipes; write any internal JSON yourself.
   Claude uses only the named `CLAUDE_CODE_OAUTH_TOKEN` runtime credential
   reference. Never read its value or use `--bare`, API-key fallback, or a
   permission-bypass flag. Codex opaque auth binding requires both
   `--auth-file-env NAME` and `--acknowledge-auth-source-writes`.
3. New capture recipes disable all six operator names: `dryheave-collect`,
   `dryheave-case`, `dryheave-results`, `dryheave-voice-profile`,
   `dryheave-generate-problem`, `dryheave-agent-profile`. `profile capture` fills
   that list when omitted. Explicit lists are honored, and old frozen recipes
   retain their historical three-name default. Do not accidentally capture or
   enable these operator instructions for benchmark subjects.
4. Derive with `profile derive BASE --model MODEL --effort LEVEL --name NAME`.
   For Haiku, omit effort; when deriving from an effort-enabled profile, supply
   `--clear-effort`. Never describe Haiku as high-effort. Haiku versus Sonnet
   low is a model-and-configuration comparison, not effort-only. For a Terra
   low/high comparison, keep the same model and derive only `--effort high`.
   Run `profile diff BASE VARIANT --json` to verify the intended differences.
5. Inspect `profile preflight --help`; preflight records exact argv, unresolved
   ambient influences and authentication/permission requirements. Native HOME
   and fresh config roots do not prove host or discovery isolation. Preserve
   these limitations. Neither voice style nor benchmark authoring authorizes
   trust/authentication/permission dialogs.
6. Use `experiment setup NAME --case CASE --profile base=BASE --profile
   changed=VARIANT --simulator claude --simulator-model FULL_MODEL_ID`.
   Omit simulator effort for Haiku; otherwise select a supported effort
   explicitly. Default budgets are three replies, 30 seconds per call and 90
   seconds total, separately from the subject. Freeze one simulator for both
   subjects. `--design-approval` grants only ordinary in-scope conversational
   approval; facts-only is the default, and persona style grants no authority.
   Claude simulation uses tool-disabled print mode; native subjects remain TUI.
   Codex simulation is trusted-native, not tool-free.
7. Calibrate each frozen case, then report `dryheave run NAME --assess --json`.
   Execution remains a separate explicit user action; setup makes no model calls.
   Record transport once with `init --tui-test PATH` or install tui-test on PATH.
   `run --assess` requires captured evidence and resolved owned cleanup before
   assessment. Retain the announced run ID for separate `assess RUN_ID` or
   `run --resume RUN_ID`; retries preserve earlier evidence, never force success.

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
