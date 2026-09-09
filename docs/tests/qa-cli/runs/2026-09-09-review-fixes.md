# Assessment recovery and selection QA

Run date: 2026-09-09. Scope: independent process-level checks of the first Task 6
review fixes. Environment: macOS arm64, Python 3.13.6, development installation.
Build: `8a5f5bca5a9775600fca3ff72881c1dfc48ead40` plus the staged Task 6 work
and its first review-fix wave. An exact patch identity at execution was not
recorded. These are runtime observations through the public CLI, separate from
the implementer's regression suite.

All subjects and graders were synthetic Python fixtures. No model or agent CLI
was launched. Stores, source repositories, helper processes and evidence stayed
under checkout-owned ignored paths.

## Process recovery

A hidden verifier spawned a bounded child in its own process session, with its
stdout/stderr redirected to `/dev/null`, and wrote its parent/child IDs to a fixture marker. The
controller waited until the child's PID and creation identity appeared in the
durable run journal before interrupting the assessor. The helper child had a
20-second natural lifetime; cleanup targeted only the known fixture identities.

| Scenario | Result | Observation |
|---|---|---|
| SIGTERM during verification | Pass | Known parent and child stopped. Resume retained an `interrupted_verifier` error and indeterminate completion; no hidden verifier was executed again. |
| SIGKILL assessor, then parent exit | Pass | The bounded child was confirmed alive after its verifier parent exited. `run --resume` reconciled ownership; assessment resume retained an interrupted error. The child stopped and the verifier was not executed again. |
| Structured cancellation response | Pass after fix | The first SIGTERM probe exposed an uncaught `KeyboardInterrupt` traceback. A targeted repeat returned exit 130, stderr JSON with code `cancelled`, and the exact assessment resume command. Process cleanup and retained outcomes still passed. |

Commands used the public entrypoint:

```sh
.venv/bin/python -I -m dryheave --store STORE --json assess RUN_ID
.venv/bin/python -I -m dryheave --store STORE --json run --resume RUN_ID
.venv/bin/python -I -m dryheave --store STORE --json report RUN_ID
```

The controller sent signals only through its own `subprocess.Popen` handle.
Initial SIGTERM run: `1b751071e91a4b0781ab00b862d9f9cd`; crash-recovery run:
`836886bdd50b4aea9fcecb48fc0a2732`; normalized cancellation repeat:
`963e55ec569e4dbc834d4afd8781aa37`. Local command/summary evidence is in
`.cache/qa-grader-recovery-prep/` and its `normalized-cancellation/` subdirectory.

## Verifier evidence, export and retry selection

The label-normalization case now uses separate success and known-failure markers.
Its hidden script catches assertion/import/syntax failure from the task checks,
prints `DRYHEAVE_LABEL_CHECKS_FAILED`, and reraises. Successful checks print
`DRYHEAVE_LABEL_ALL_CHECKS_EXECUTED`. A verifier's own syntax or setup failure
without the expected evidence is an execution error under the revised contract.

Frozen case:
`22c33182f9777ba547939e1b170426a8da5ed7ad59044096f39ab33fbd661c0f`.
It preserves the earlier source repository and reference patch; the original
case remains intact. Fixture run: `d01adb8e319f472e82ca036dd34f86d1`.

| Scenario | Result | Observation |
|---|---|---|
| Failure-aware calibration | Pass | Both fixture variants passed final verification. The baseline failed with explicit execution evidence and the reference passed, producing demonstrated calibration. |
| Export before assessment | Pass | `export RUN_ID --include-sensitive captures` included both existing captures before any assessment object existed. |
| Latest unassessed retry | Pass | After both original attempts were assessed, an explicit base retry created a third retained attempt. Leaving that retry unassessed yielded zero eligible pairs and one excluded before-side attempt; comparison did not select the older scored result. |

Local inputs, full command responses and summary are in
`.cache/qa-review-fixes-prep/`. The corrected case was also frozen into the final
native-QA store, and the prepared final native experiment now references it.
That preparation did not launch a subject.

## Limits

These checks establish observed-process recovery and CLI selection behavior for
the injected faults. They do not establish ownership of descendants that were
never observed or complete host isolation. Pending judge accounting and shared
suite interruption also have regression tests, but this record does not claim
independent manual execution of those two cases. Clean-wheel and actual model
execution remain separate checks.
