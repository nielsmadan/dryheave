# Case workflows

Status: accepted. Date: 2026-10-11.

Cases are personal. A case captures one person's real way of working: their
reviews, manual testing, QA and docs habits, along with the task itself. That
workflow lives in the case as ordered `inspect` and `prompt` steps.

Steps advance only at a done point. A done point is a simulator `stop` with
`stop_kind == "done"`, meaning the assistant reports the work done, asks
nothing, and no proactive fact applies. Declined approvals, script exhaustion
and divergence end the run as before.

A `prompt` step is sent once, verbatim. Slash commands go through the
`curated-native-command` policy, and only Claude accepts them.

An `inspect` step stands in for the user checking the work themselves. The
inspector evaluates the listed criteria against a feedback-only snapshot of the
idle workspace, and failures come back as a user-voice fix-it reply. The step
repeats until those criteria pass.

Inspect steps name an observable criterion: what the user would see when using
the result. The case's correctness criterion stays held out for final grading,
because whatever the inspected criteria contain reaches the agent as feedback.
Reusing the grading rubric in the loop would make the final grade mostly circular
and leak requirements the real user never stated. The first inspection's result
is reported as the "right before feedback" signal. `bench --no-workflow` gives
first-shot runs for comparison, and `--inspector` can separate the inspection
model from the grading judge.

Workflows are frozen per run in `RunOptions.workflow`. They are off for plain
`run` unless `--workflow` is passed, and on by default in `bench`. Resuming only
continues a run in the same mode. Without a declared workflow, a case gets one
inspection of its required criteria when an inspector exists.

The snapshot is the one exception to "stop writers before snapshotting". It
never becomes evidence or a grade. Hidden material (verifier files, rubric text
in the judge request) lives in a 0700 directory outside the attempt tree, and
that directory is journaled so recovery can remove it. Evidence is copied in
only at capture.

If the workspace changed during an inspection, or the snapshot is incomplete,
the inspection is retried once and otherwise treated as unavailable. Failures
are classified two ways:

- Infrastructure failures stop the run as `inspection_unavailable`.
- Failures in the subject's code become findings. Verifier findings carry only
  the criterion description and the outcome category, never verifier output.

Inspection calls are judge-role calls with their own index and are priced with
the inspector recipe. Simulator budgets count only simulator calls. Inspection
time is excluded from agent time. Fix-it replies and dialog answers are
excluded from help-needed replies.

When Claude shows a selection dialog, the simulated user answers it, guided by
the current prompt step's `answer_hint`. The driver moves the highlight with the
arrow keys and checks every keypress on screen before acting, and it never
sends Escape. Both the plain and the side-by-side preview layouts are read.
Startup trust and import prompts, and tool-permission approvals, are never
answered.
