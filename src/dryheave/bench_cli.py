import argparse
import os
import sys
from pathlib import Path
from threading import Event
from uuid import uuid4

from pydantic import JsonValue

from dryheave.bench import (
    CLAUDE_AUTH_VARIABLE,
    JUDGE_BUDGET,
    VariantChoice,
    attempt_progress_lines,
    bench_draft,
    bench_workflow,
    capture_variants,
    check_case,
    check_snapshots,
    check_workflow,
    claude_role,
    discard_check,
    launch_check,
    parse_role,
    parse_variant,
    resumable_run,
    simulator_budget,
    slash_commands,
    summarize,
    summary_live_config,
)
from dryheave.cases import load_frozen_case
from dryheave.commands import CommandRegistry
from dryheave.drivers.auth import check_codex_auth
from dryheave.drivers.base import TransportError
from dryheave.errors import DryheaveError, InputError, MissingSkillError, SnapshotChangedError
from dryheave.experiment_models import ScoringConfig
from dryheave.experiments import create_experiment
from dryheave.host import HostContext
from dryheave.journals import RunStore
from dryheave.models import AgentKind
from dryheave.reports import report_run
from dryheave.runner import WorkflowPlan, cancellation_signals, run_experiment, run_status
from dryheave.runner_cli import assess_captured_run
from dryheave.runner_models import RunOptions
from dryheave.storage import ObjectStore
from dryheave.workspaces import Workspace, resolve_transport, validate_runtime_root

RESUME_ADVICE: dict[type[DryheaveError], str] = {
    MissingSkillError: "If the skill was added to the live Codex config after run {run} "
    "started, pass --new-run instead of resuming it.",
    SnapshotChangedError: "Run {run} cannot reuse its live config snapshot; pass --new-run "
    "to take a fresh one.",
}


def _progress(message: str) -> None:
    print(f"dryheave bench: {message}", file=sys.stderr, flush=True)


def _native_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    workspace: Workspace | None = args.benchmark_workspace
    runtime_root = args.runtime_root or (workspace.path("runtime") if workspace else None)
    transport = resolve_transport(args.tui_test, workspace)
    if runtime_root is None or transport is None:
        raise InputError(
            "Bench needs a runtime root and tui-test: run it inside a dryheave init workspace "
            "or pass --runtime-root and --tui-test."
        )
    validate_runtime_root(runtime_root)
    return Path(runtime_root).absolute(), transport


def _launch_checks(  # noqa: PLR0913
    store: ObjectStore,
    variants: tuple[VariantChoice, ...],
    profiles: dict[str, str],
    paths: tuple[Path, Path],
    *,
    workflow: bool,
    plan: WorkflowPlan,
    host: HostContext,
) -> None:
    root = store.root / "checks" / uuid4().hex
    manifests = check_snapshots(root, variants, plan, environment=host.environment, home=host.home)
    commands = slash_commands(plan)
    for name, profile in profiles.items():
        _progress(f"launch check {name}: starting the subject without a prompt")
        try:
            observation = launch_check(
                store,
                profile,
                root,
                paths=paths,
                environment=host.environment,
                manifests=manifests,
                workflow=workflow,
                commands=commands,
            )
        except TransportError as error:
            raise InputError(
                f"Variant {name} launch check lost its terminal transport ({error}). "
                f"Nothing was sent to a model. Evidence: {root}"
            ) from error
        if observation.state != "ready":
            raise InputError(
                f"Variant {name} did not reach an empty prompt ({observation.state}: "
                f"{observation.reason}). Nothing was sent to a model. Evidence: {root}\n"
                + observation.text[-3000:]
            )
    discard_check(root)


def _execute(
    store: ObjectStore,
    experiment_id: str,
    resume: str | None,
    paths: tuple[Path, Path],
    *,
    workflow: bool,
    host: HostContext,
) -> str:
    cancelled = Event()
    with cancellation_signals(cancelled):
        try:
            summary = run_experiment(
                store,
                None if resume else experiment_id,
                resume=resume,
                options=None
                if resume
                else RunOptions(
                    mode="native",
                    runtime_root=str(paths[0]),
                    transport=str(paths[1]),
                    workflow=workflow,
                ),
                cancelled=cancelled,
                host=host,
            )
        except (MissingSkillError, SnapshotChangedError) as error:
            if resume is None:
                raise
            raise type(error)(f"{error} {RESUME_ADVICE[type(error)].format(run=resume)}") from error
        _progress(f"run {summary.run_id} captured; grading")
        assess_captured_run(store, summary, cancelled)
    return summary.run_id


def _bench(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    variants = tuple(map(parse_variant, args.variant))
    judge = parse_role(args.judge) if args.judge else None
    inspector = parse_role(args.inspector) if args.inspector else None
    case_id = check_case(store, args.case, judged=judge is not None)
    host = HostContext(os.environ, Path.home())
    if CLAUDE_AUTH_VARIABLE not in host.environment:
        raise InputError(
            f"{CLAUDE_AUTH_VARIABLE} is not set; subjects, simulator and judge cannot authenticate. "
            "Launch bench from a shell where your Claude credential is injected."
        )
    if any(variant.agent == AgentKind.CODEX for variant in variants):
        check_codex_auth(host.environment)
    judge_recipe = claude_role(judge, args.claude, JUDGE_BUDGET) if judge else None
    inspector_recipe = claude_role(inspector, args.claude, JUDGE_BUDGET) if inspector else None
    check_workflow(
        load_frozen_case(store, case_id),
        workflow=args.workflow,
        scoring=ScoringConfig(judge=judge_recipe, inspector=inspector_recipe),
    )
    paths = _native_paths(args)
    profiles = capture_variants(
        store, variants, {AgentKind.CLAUDE: args.claude, AgentKind.CODEX: args.codex}
    )
    draft = bench_draft(
        case_id,
        profiles,
        simulator=claude_role(
            parse_role(args.simulator),
            args.claude,
            simulator_budget(args.simulator_replies),
            conversation_policy="design-approval",
        ),
        judge=judge_recipe,
        inspector=inspector_recipe,
        repetitions=args.repetitions,
        seed=args.seed,
    )
    experiment_id = create_experiment(store, draft)
    previous = resumable_run(store, experiment_id, workflow=args.workflow)
    if previous is not None and previous.finished and not (args.new_run or args.check_only):
        _progress(f"run {previous.run_id} already finished; pass --new-run to spend on another")
        run_id = previous.run_id
    else:
        if not args.skip_launch_check:
            _launch_checks(
                store,
                variants,
                profiles,
                paths,
                workflow=args.workflow,
                plan=bench_workflow(store, experiment_id, workflow=args.workflow),
                host=host,
            )
        if args.check_only:
            return {
                "experiment_id": experiment_id,
                "profiles": dict(profiles),
                "launch_check": "ready",
            }
        resume = None if previous is None or args.new_run else previous.run_id
        run_id = _execute(
            store,
            experiment_id,
            resume,
            paths,
            workflow=args.workflow,
            host=host,
        )
    status = run_status(store, run_id)
    journal = RunStore(store.root).inspect(run_id)
    report = report_run(store, run_id)
    stop_reasons = {state.attempt_id: state.stop_reason for state in status.attempts}
    for line in attempt_progress_lines(report, stop_reasons):
        _progress(line)
    return {
        "run_id": run_id,
        "experiment_id": experiment_id,
        "profiles": dict(profiles),
        "live_config": summary_live_config(journal.events),
        "results": summarize(report, stop_reasons),
        "next": (f"dryheave view --open   # or: dryheave report {run_id} --html report.html"),
    }


def register_bench(registry: CommandRegistry) -> None:
    parser = registry.add(
        "bench",
        help_text="Benchmark variants of your live Claude or Codex setup on one case: launch check, run, grade and summarize.",
    )
    parser.add_argument("case", help="Frozen case name or ID.")
    parser.add_argument(
        "--variant",
        action="append",
        required=True,
        metavar="NAME=[AGENT:]MODEL[@EFFORT][,-mcp:SERVER]",
        help="Repeat per variant; AGENT is claude (default) or codex. Each starts from the same "
        "snapshot of that agent's live config; -mcp:SERVER is Claude-only.",
    )
    parser.add_argument("--judge", metavar="MODEL[@EFFORT]")
    parser.add_argument(
        "--inspector",
        metavar="MODEL[@EFFORT]",
        help="Workflow inspect steps; defaults to the judge's model when a judge is set.",
    )
    parser.add_argument(
        "--no-workflow",
        dest="workflow",
        action="store_false",
        default=True,
        help="Run the case's initial prompt only, without its declared or default workflow.",
    )
    parser.add_argument("--simulator", default="claude-sonnet-4-6@low", metavar="MODEL[@EFFORT]")
    parser.add_argument(
        "--simulator-replies",
        type=int,
        default=14,
        help="Most replies the simulated user may send; each gets 180 seconds.",
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--claude", default="claude", help="Claude Code executable.")
    parser.add_argument(
        "--codex",
        default="codex",
        help="Codex CLI executable for codex: variants, resolved on PATH.",
    )
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--tui-test", type=Path)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Freeze inputs and run the launch check without sending anything to a model.",
    )
    parser.add_argument("--skip-launch-check", action="store_true")
    parser.add_argument(
        "--new-run", action="store_true", help="Start another run even if one already finished."
    )
    registry.handler(parser, _bench)
