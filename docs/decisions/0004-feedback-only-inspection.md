# Feedback-only inspection and requirement facts

Status: accepted. Date: 2026-10-23. Supersedes the held-out-correctness rule in
[0003](0003-case-workflows.md).

ADR 0003 kept the case's correctness criterion out of the workflow loop so the
final grade would not be circular. That left the loop with a second, weaker
vocabulary (observable `inspect` criteria) and no way to say which requirements
the user still had to point out. A run could not tell how much of the problem the
agent solved on its own from how much the user stand-in had to raise.

Facts are now the single catalogue of requirements. A fact has a kind
(`requirement`, `optional`, `context`), a phase, a precise `check` and optionally
a deterministic verifier. Inspections judge the same facts that final grading
judges, and what they find drives the loop:

- An inspection is feedback only. It decides which phase is blocking and which
  unmet requirements the stand-in is handed, as text and observation, never the
  `check`. It never yields pass or fail, but it can withhold a verdict: a requirement
  unmet in a phase never reached is indeterminate for stops the subject did not
  cause, and a judged fact that contradicts an inspection of the unchanged final
  snapshot is disputed.
- The inspection snapshot is never graded. Inspector call requests, which embed
  snapshot files, are retained as call evidence only (replay re-sends them) and
  are never graded themselves. Hidden material still lives outside the attempt tree
  and evidence is copied in only at capture, as in 0003.
- Pass and fail come only from final grading of the final capture. A requirement
  met at the end passes; one unmet at the end fails, including when a turn or reply
  budget ran out. Missing, unclear or disputed observations, and unmet
  requirements in unreached phases after a stop the subject did not cause, stay
  indeterminate rather than becoming a verdict.
- Because the loop's feedback now overlaps the grading facts, the run reports how
  each fact was reached (own, elicited, corrected, unmet) and an independence score
  instead of treating the final grade as the only signal. When the final capture is
  the inspected snapshot, a judged fact whose verdict contradicts the inspection is
  re-judged once and otherwise reported as disputed.

0003 no longer governs: `inspect` steps and their repeat-until-pass rule, the single
inspection of required criteria for cases without a workflow (every done point now
inspects all requirement and optional facts), the first inspection as the "right
before feedback" signal (now per-fact first-inspection rates and credits), and
help-needed reply accounting (dialog answers now count toward elicited).

The rest of 0003 stands: workflows are frozen per run, prompt steps are delivered
once and gated by `after_phase`, selection dialogs are answered under the same safety
rules, hidden material stays outside the attempt tree, `inspection_unavailable`
keeps its meaning, and inspection calls are priced and budgeted apart from the
subject.

Earlier decisions call the user stand-in by another name; persisted names use
`stand-in` or `stand_in`.
