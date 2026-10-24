# Facts model: why runs are built this way

This page keeps the reasons. The contracts live in [authoring](authoring.md),
[runner](runner.md), [results](results.md) and [storage](storage.md); the decision
record is [ADR 0004](../decisions/0004-feedback-only-inspection.md).

A run measures how much of the problem the agent solves on its own versus how much
the user stand-in has to point out. That needs one catalogue of requirements that
the stand-in, the inspector, the final judge and the metrics all share.

## Facts

- A fact has a kind: `requirement` gates pass/fail, `optional` is graded but never
  gates, `context` is background the stand-in may mention and is never graded.
- Requirement and optional facts belong to an ordered phase. A fact carries `text`
  (what the user knows; the stand-in sees it) and `check` (the precise pass
  condition; only the inspector and judge see it, so it can be strict without
  leaking into the conversation), or a deterministic `FactVerifier` that replaces
  the judge for that fact.
- Gating lives only on requirement facts. Quality rubric criteria are scored but
  never gate; verifier executions are kept as criteria records beside the facts
  because they carry the calibration and execution evidence.
- Draft cases may have no phases yet so that a skeleton parses; the "at least one
  phase and requirement, every phase has a requirement" rules run in validate,
  freeze and the frozen case.

## Loop: raising the earliest unmet phase

At each done point an inspection judges every requirement and optional fact. The
runner raises the earliest phase with an unmet requirement, so a regression in an
earlier phase is repaired before later work, and a prompt step is delivered only
once no phase up to its `after_phase` blocks. The stand-in is handed that
phase's unmet facts as text and observation, never the `check`, and describes what
it saw in the user's words. A fact counts as raised only when the agent accepted
the resulting fix request, which is why the handed set is journaled. Pacing is the
harness's job: nothing in a voice asks for one fact at a time.

Inspection is feedback only. It never yields pass or fail and its snapshot is
never graded, though it can withhold a verdict: through phase reach for stops the
subject did not cause, and through a dispute when the final capture is the inspected
snapshot. This keeps the loop's vocabulary equal to the grading vocabulary without
making the final grade circular (ADR 0004). Verifier facts read each execution with
one shared classifier in both places: a verifier that ran and exited without its
success marker, crashes and import errors included, is unmet; only a harness-side
failure (not launched, copy not restored, deadline cut it short) gives no result.
An attempt where a requirement verifier crashed (unmet without its failure marker) while
its own reference run did not pass is excluded (`verifier_uncalibrated`), so a broken
verifier cannot fail it; a marker-confirmed failure needs no reference run.

## Credit and independence

Each requirement fact in a reached phase is credited `own` (1.0: met, never raised,
never elicited), `elicited` (0.75: a non-proactive requirement the stand-in cited
in an ordinary reply before it was raised), `corrected` (0: met only after being
raised; wins over elicited) or `unmet` (0). The score is the mean over non-proactive
requirements in reached phases and is unknown, not zero, when nothing is scorable or
when any such requirement ended unclear, disputed or without a result.
Elicited credit is a fixed constant rather than a case field, so scores stay
comparable across cases. Dialog answers count toward elicited; accepted fix requests do not. A fix request
the agent did not accept currently counts as an ordinary reply.

## Outcomes

`pass` means every requirement is met in the final grading. A requirement unmet at
the end is a `fail`, including when turns or stand-in replies ran out, because
running out of budget with work undone is the agent's result. Withholding a verdict
(`indeterminate`) is reserved for missing, unclear or disputed observations and for
stops the subject did not cause. The exact classification is in
[results](results.md#final-grading-and-outcome).

## Budgets and the pilot

Budgets are run options, not case fields: a case describes the work, a run decides
how long to let an agent try. The defaults (60 turns, 40 stand-in replies, 20
inspections) are generous because a pilot is a normal `bench` run with one variant
named `pilot`, one repetition and the full workflow; there is no pilot flag. The
default pilot model is deliberately weak; for hard cases a stronger one is better.
`bench --budget-from PILOT_RUN` derives ceil(used x 1.5) + 5 per figure from a
passed pilot only, clamped to the field maxima, and records the pilot. The stand-in
recipe is frozen with a fixed 100-call ceiling so the reply budget never changes the
experiment ID; that is what lets a pilot and its benchmark share identities and lets
the report show the pilot as a floor variant (`pilot (n=1)`, with its larger budget).

## Reviewing and replaying

The review lanes (Conversation, Behind the scenes, Gaps) are derived from the
journal by one pure builder shared by the viewer and `dryheave transcript`; nothing
extra is journaled for display. `replay` re-sends only stored stand-in and inspector
requests, takes no run lock, writes only to its own output directory (plus the shared native lock file) and refuses
before sending when a stored request is missing. It exists to test wording changes
(`--voice`, a revised fact `check`) against recorded pilots instead of paying for
new runs.

## Workspace git and the store marker

Authored files are plain visible files in a git repository at the workspace root;
`.dryheave/` is ignored state. Checkpoint commits stage only the paths a command
wrote, through a temporary index seeded from HEAD, and touch the user's real index
only after the commit lands, so a failure never disturbs staging. HEAD is updated
compare-and-swap against the HEAD the index was seeded from, so a concurrent commit
fails the checkpoint rather than being reverted. `case freeze`
points the alias named after the case folder at the new case ID; aliases are
mutable pointers and runs record IDs, so history is unaffected. The store carries
`format.json` (format 2) and refuses non-empty stores without it ([storage](storage.md#store-format-marker)).
