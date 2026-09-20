# Task 6 viewer — browser QA

Date: 2026-09-20. Build: working tree at Task 6, pre-commit.
Browser: agent-browser 0.37.1, Chromium, isolated session.
Store: `.cache/terra-effort-20260910/store` — real historical Terra results,
retained unmodified. Run `85893af387b74fd3aad9fdfe5aeea4c3`, experiment `terra-effort`.
Command: `dryheave view --store .cache/terra-effort-20260910/store`.

## Truthfulness against known history

The recorded history is that both Terra trials answered a clarification, then
stalled requesting design approval, and neither completed its task. The viewer
reports that without inflation:

| Group | Scheduled | Attempts | Finished | Scored | Eligible | Eligible passes | Completion rate | Audit-excluded | Subjects claiming completion |
|---|---|---|---|---|---|---|---|---|---|
| high · native | 1 | 2 | 2 | 1 | 1 | 0 | 0.0% | 1 | 0 |
| low · native | 1 | 2 | 2 | 1 | 1 | 0 | 0.0% | 1 | 0 |

Attempt detail reads `completion indeterminate · deterministic indeterminate ·
not eligible · phase finished · excluded: capture_incomplete, unscorable`.
The run carries a `TRUNCATED PREFIX` badge and `durable sequence unknown;
71 event(s) read`, which is the viewer's journal bound reporting itself honestly
rather than presenting a partial read as complete.

## Checks performed

- Response headers on `/`: `Content-Security-Policy: default-src 'none'; script-src
  'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none';
  form-action 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`,
  `Cache-Control: no-store`, `Referrer-Policy: no-referrer`,
  `Cross-Origin-Resource-Policy: same-origin`. Page renders within that CSP.
- All six tabs: each shows exactly one panel (see defect below).
- Attempt detail: identity chips carry a human label beside the full immutable ID
  (`profile terra-low 87c4688…`, `experiment terra-effort d8cf5bc…`).
- Mobile at 390x844: no horizontal overflow (`scrollWidth` 390 = `innerWidth` 390),
  tab strip reflows to a grid, sidebar stacks.
- Server killed mid-session: status becomes "The viewer could not be reached; the
  server may have stopped.", the run list stays populated and the overview keeps
  its data. A failed poll does not blank good data.

## Defect found and fixed

**All six tab panels were visible at once; tabs changed nothing.** `viewer.js` sets
`panel.hidden = !active` correctly, but `.panel { display: grid }` is an author rule
and beats the user-agent `[hidden] { display: none }`, so every panel kept its
layout box. Observed directly: with the Attempts tab selected, the Overview panel
was on screen and `offsetHeight` was non-zero for all six panels.

Fixed by adding `[hidden] { display: none !important }` to `viewer.css`. Re-verified
in the browser: clicking each tab in turn leaves exactly that tab's panel with a
layout box.

No Node test could have caught this — the test shim models the DOM, not the CSS
cascade. `test_hidden_panels_are_not_re_shown_by_a_display_rule` now guards the
mechanism; removing the rule makes it fail.

## Installed-wheel walkthrough (Task 7, non-live portion)

Date: 2026-09-20. Wheel `dist/dryheave-0.1.0-py3-none-any.whl` installed into a
fresh ignored environment at `.cache/task7-install/.venv` via `uv pip install`.

- Installed CLI registers `view`; all 8 viewer assets are present and readable
  from the installed package (81,268 bytes total).
- Installed `dryheave view` serves `/` (200), `viewer.js` (200), `views.mjs` (200),
  `/api/runs` (200) and `/api/labels` (200), and rejects `Host: evil.example` with
  400. So the packaged resources and the Host check both work from the wheel, not
  just from the source tree.
- `offline_demo.py`, written by the installed `dryheave example write`, completed
  end to end with exit 0 and zero model calls: synthetic source repository created,
  `source_preserved: true`, 2 paired results, run `35dad9d68c214a8db4396925fb0f7ca5`,
  portable report `3d2f2f71…`. Command records 001-031 retained under
  `.cache/task7-demo/commands`.
- Export/import round trip: record 030 `export … --output results.tar` exit 0;
  record 031 `import results.tar` into a separate `imported-store` exit 0. Bundle
  `included: ["curated-inputs", "structured-results"]`.

### Portable report as a degraded view

The imported store contains **no runs** (`/api/runs` → `{"runs": [], "total": 0}`),
which is exactly the degraded case the spec requires to stay viewable. Entering the
portable report ID in the sidebar form loads it: `Portable report: yes`, durable
sequence 137, 4 scheduled trials, 4 retained attempts, and an explicit omissions
list naming every withheld sensitive class — raw terminal/native logs and screens,
final files and patches, raw Git metadata, simulator requests and responses, judge
requests and responses, verifier stdout/stderr, full source transcripts, standalone
calibration verifier. Nothing is guessed or silently blank.

Attempt filtering narrows the list from 9 rows to 0 on a non-matching query.
Exactly one tab panel carries a layout box throughout, confirming the `[hidden]`
fix holds in the installed build.

### Not covered here

The live Claude walkthrough — authoring session, Haiku and Sonnet subject trials and
the adaptive simulator — did not run. Authentication is unavailable in this
environment; see the ledger's blocked check for the diagnosis and the resume
commands. Everything above is offline and synthetic or historical, and is labelled
as such; none of it establishes live model behaviour.
