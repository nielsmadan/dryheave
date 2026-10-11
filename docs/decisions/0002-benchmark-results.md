# Generic, case-declared benchmark measures

Status: accepted. Date: 2026-10-08.

Case-specific measures are generic dryheave mechanisms. A case declares yes/no
checks, metrics commands and rubric overrides. No language-, toolchain- or
repository-specific code lives in dryheave. Everything specific to one benchmark
stays in that case's files.

The judge answers checks and rubric criteria in a single call, from the final
files plus a transcript. Each tool action in the transcript is cut to 600
characters, and actions are dropped from the middle when the input bound is
exceeded. The judge sees only a check's id and question, never its expected
answer or its `only_if` gate. A gated check whose prior answer is anything
other than the gate's value reports `not_applicable`. Host paths are redacted
from the transcript. A path containing a literal space leaks its tail after the
space, which is accepted.

A metrics command runs during grading. It works on a copy of the captured final
files, with capture-excluded paths absent, and gets a read-only baseline.
Toolchain failures must make the command fail so the result stays unknown. A
build the subject broke is reported as a metric instead. If a subject replaces
its own metrics folder with a symlink, grading stops with an error rather than
being guarded against.

Three default rubrics (`default-v1`) apply only when an experiment has a judge.
They are not required, and a case overrides one by reusing its id or disables
it with `disabled_rubrics`. `criteria_id` keeps the old criteria-only hash when
a case has no defaults, checks or metrics, so existing comparisons still pair.
Otherwise it covers all three. `compare` pairs only attempts graded against the
same case.

Cost is an estimate of API-equivalent list prices, taken from a dated price
table. Each charge records the price table version it used. Judge calls
recorded before the 1h cache-write split stay priced at the 5m rate. Summaries
count the latest retained attempt per trial. Quality, checks and metrics use
eligible attempts. Cost and tokens count every retained attempt, including
retries. Summaries are keyed by case and measure id, and agreement is computed
across one case's repetitions.

A regrade creates a new assessment that points back to the one it replaces,
under its own regrade id. A replacement case must share the repository and
initial prompt, and it applies to whole runs only. While a regrade is
unfinished, plain `assess` refuses. Human dismissals of audit findings carry
over when the new audit still reports them. Reports sum only the current
grading's judge spend.

Owned processes are journaled in batches of at most 1024, at most once per
second. Forced publishes happen at direct-child claims, launch observation,
cleanup and recovery. This keeps long runs from growing their journals without
bound. Run-list rows come from the checkpoint when the journal prefix is
truncated.
