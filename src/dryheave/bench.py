import re
import shutil
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from threading import Event
from uuid import uuid4

from pydantic import JsonValue, ValidationError

from dryheave.cases import load_frozen_case, needs_judge
from dryheave.codex_policy import CODEX_SANDBOX_ARGUMENTS
from dryheave.controller_models import ClaudeControllerRuntime, ControllerRecipe, RoleBudget
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.auth import RuntimeBindings
from dryheave.drivers.base import TransportError
from dryheave.drivers.dialogs import composer, delivered_prompt, submittable
from dryheave.drivers.log_source import NativeLogSource, native_log_root
from dryheave.drivers.models import DriverLimits, Observation, Screen
from dryheave.drivers.session import DriverSession, normalize_input
from dryheave.drivers.tui_test import TuiTestTerminal
from dryheave.errors import InputError
from dryheave.experiment_models import (
    ExperimentDraft,
    ScoringConfig,
    VariantSpec,
)
from dryheave.experiments import load_experiment
from dryheave.fact_metrics import derived_budget
from dryheave.journals import RunStore
from dryheave.live_config import (
    journaled_manifests,
    live_source,
    require_servers,
    snapshot_directory,
    snapshot_live_config,
)
from dryheave.models import AgentKind, CommandSpec, EnvironmentReference, JournalEvent, TrialStage
from dryheave.pricing import builtin_price_table
from dryheave.profile_launch import (
    LaunchOptions,
    LiveSnapshots,
    launch_environment,
    materialize_profile,
)
from dryheave.profile_models import (
    CaptureSpec,
    LiveConfigManifest,
    NativeRecipe,
    codex_auth_reference,
)
from dryheave.profiles import capture_profile
from dryheave.report_models import AttemptReport, GroupMetrics, RunReport
from dryheave.reports import attempt_eligibility, identities, report_run
from dryheave.result_models import Assessment
from dryheave.runner import (
    WorkflowPlan,
    load_capture,
    require_snapshot_skills,
    run_status,
    selected_workflow_plan,
)
from dryheave.runner_models import RunBudget
from dryheave.runner_state import ExecutionJournal
from dryheave.skills import SKILL_NAMES
from dryheave.storage import ObjectStore
from dryheave.workflow import inspection_records, slash_steps

CLAUDE_AUTH_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"
VARIANT_AGENTS = {"claude": AgentKind.CLAUDE, "codex": AgentKind.CODEX}
AGENT_PREFIX = re.compile(r"[A-Za-z]+")
PROBE = "dryheave launch check"
LAUNCH_CHECK_SECONDS = 180.0
ROLE = re.compile(r"^([^@,\s]+)(?:@([a-z]+))?$")
VARIANT = re.compile(r"^([a-zA-Z0-9][a-zA-Z0-9_.-]{0,127})=([^,]+)((?:,[^,]+)*)$")
STAND_IN_CALL_SECONDS = 180
STAND_IN_BUDGET = RoleBudget(max_calls=100, call_seconds=STAND_IN_CALL_SECONDS, total_seconds=3600)
JUDGE_BUDGET = RoleBudget(
    max_calls=6, call_seconds=300, total_seconds=1200, max_input_bytes=256 * 1024
)


@dataclass(frozen=True)
class RoleChoice:
    model: str
    effort: str | None


@dataclass(frozen=True)
class VariantChoice:
    name: str
    agent: AgentKind
    role: RoleChoice
    removed_mcp_servers: tuple[str, ...]


def parse_role(text: str) -> RoleChoice:
    match = ROLE.fullmatch(text.strip())
    if match is None:
        raise InputError(f"Expected MODEL or MODEL@EFFORT, got {text!r}.")
    return RoleChoice(model=match[1], effort=match[2])


def parse_variant(text: str) -> VariantChoice:
    match = VARIANT.fullmatch(text.strip())
    if match is None:
        raise InputError(f"Expected NAME=[AGENT:]MODEL[@EFFORT][,-mcp:SERVER...], got {text!r}.")
    name = match[1]
    prefix, sep, rest = match[2].partition(":")
    if sep and prefix in VARIANT_AGENTS:
        agent, model = VARIANT_AGENTS[prefix], rest
    elif sep and AGENT_PREFIX.fullmatch(prefix):
        raise InputError(
            f"Variant {name}: unknown agent {prefix!r}; AGENT is {' or '.join(VARIANT_AGENTS)}."
        )
    else:
        agent, model = AgentKind.CLAUDE, match[2]
    removed = []
    for change in filter(None, match[3].split(",")):
        kind, _, value = change.partition(":")
        if kind != "-mcp" or not value:
            raise InputError(f"Unsupported variant change {change!r}; supported: -mcp:SERVER.")
        removed.append(value)
    choice = VariantChoice(
        name=name, agent=agent, role=parse_role(model), removed_mcp_servers=tuple(removed)
    )
    try:
        variant_recipe(choice, agent.value)
    except ValidationError as error:
        raise InputError(f"Variant {name}: {_recipe_refusal(error)}.") from error
    return choice


def _recipe_refusal(error: ValidationError) -> str:
    return "; ".join(str(item.get("ctx", {}).get("error", item["msg"])) for item in error.errors())


def variant_recipe(choice: VariantChoice, executable: str) -> NativeRecipe:
    codex = choice.agent == AgentKind.CODEX
    return NativeRecipe(
        agent=choice.agent,
        executable=executable,
        model=choice.role.model,
        effort=choice.role.effort,
        config_source="live",
        workspace_trust="trusted",
        disabled_skill_names=SKILL_NAMES,
        removed_mcp_servers=choice.removed_mcp_servers,
        arguments=CODEX_SANDBOX_ARGUMENTS if codex else (),
        runtime_files=(codex_auth_reference(),) if codex else (),
        environment=() if codex else (EnvironmentReference(name=CLAUDE_AUTH_VARIABLE),),
    )


def run_budget(*, max_turns: int, stand_in_replies: int, max_inspections: int) -> RunBudget:
    try:
        return RunBudget(
            max_turns=max_turns,
            stand_in_replies=stand_in_replies,
            max_inspections=max_inspections,
        )
    except ValidationError as error:
        raise InputError(f"Invalid run budget: {_recipe_refusal(error)}.") from error


def claude_role(
    choice: RoleChoice,
    executable: str,
    budget: RoleBudget,
    *,
    conversation_policy: str | None = None,
) -> ControllerRecipe:
    return ControllerRecipe.model_validate(
        {
            "kind": "claude",
            "model": choice.model,
            "effort": choice.effort,
            "isolation": "trusted-native",
            "conversation_policy": conversation_policy,
            "command": CommandSpec(
                argv=(executable,),
                timeout_seconds=budget.call_seconds,
                environment=(EnvironmentReference(name=CLAUDE_AUTH_VARIABLE),),
            ),
            "runtime": ClaudeControllerRuntime(),
            "budget": budget,
        }
    )


def bench_draft(  # noqa: PLR0913
    case_id: str,
    profiles: Mapping[str, str],
    *,
    stand_in: ControllerRecipe,
    judge: ControllerRecipe | None,
    inspector: ControllerRecipe | None = None,
    repetitions: int,
    seed: int,
) -> ExperimentDraft:
    return ExperimentDraft(
        name=f"Bench {case_id[:12]}: " + " vs ".join(profiles),
        cases=(case_id,),
        variants=tuple(
            VariantSpec(name=name, profile=profile) for name, profile in profiles.items()
        ),
        repetitions=repetitions,
        seed=seed,
        stand_in=stand_in,
        scoring=ScoringConfig(judge=judge, inspector=inspector, prices=builtin_price_table()),
    )


def capture_variants(
    store: ObjectStore,
    variants: tuple[VariantChoice, ...],
    executables: Mapping[AgentKind, str],
) -> dict[str, str]:
    names = [variant.name for variant in variants]
    if len(set(names)) != len(names):
        raise InputError("Variant names must be unique.")
    return {
        variant.name: capture_profile(
            store, CaptureSpec(recipe=variant_recipe(variant, executables[variant.agent]))
        )
        for variant in variants
    }


def check_case(store: ObjectStore, reference: str, *, judged: bool) -> str:
    case_id = store.resolve(reference)
    case = load_frozen_case(store, case_id)
    if not judged and needs_judge(case):
        raise InputError(
            "This case has facts without a verifier or judge criteria; pass --judge MODEL[@EFFORT]."
        )
    return case_id


def bench_workflow(store: ObjectStore, experiment_id: str, *, workflow: bool) -> WorkflowPlan:
    return selected_workflow_plan(store, load_experiment(store, experiment_id), enabled=workflow)


def slash_commands(plan: WorkflowPlan) -> tuple[str, ...]:
    return tuple(step.prompt for steps in plan.workflows.values() for step in slash_steps(steps))


def snapshot_for_check(
    agent: AgentKind, root: Path, *, environment: Mapping[str, str], home: Path
) -> LiveConfigManifest:
    return snapshot_live_config(
        live_source(agent, environment, home), root / snapshot_directory(agent)
    )


def check_snapshots(
    root: Path,
    variants: tuple[VariantChoice, ...],
    plan: WorkflowPlan,
    *,
    environment: Mapping[str, str],
    home: Path,
) -> dict[AgentKind, LiveConfigManifest]:
    try:
        manifests = {
            agent: snapshot_for_check(agent, root, environment=environment, home=home)
            for agent in sorted({variant.agent for variant in variants})
        }
        if AgentKind.CLAUDE in manifests:
            require_servers(
                manifests[AgentKind.CLAUDE].mcp_servers,
                (name for variant in variants for name in variant.removed_mcp_servers),
            )
        if AgentKind.CODEX in manifests:
            require_snapshot_skills(plan, manifests[AgentKind.CODEX])
    except BaseException:
        discard_check(root)
        raise
    return manifests


def summary_live_config(events: Sequence[JournalEvent]) -> dict[str, JsonValue]:
    return {
        agent.value: manifest.model_dump(mode="json", exclude={"agent"})
        for agent, manifest in sorted(journaled_manifests(events).items())
    }


def launch_check(  # noqa: PLR0913
    store: ObjectStore,
    profile_id: str,
    root: Path,
    *,
    paths: tuple[Path, Path],
    environment: Mapping[str, str],
    manifests: Mapping[AgentKind, LiveConfigManifest],
    workflow: bool,
    commands: tuple[str, ...] = (),
) -> Observation:
    runtime_root, transport = paths
    workspace = root / profile_id[:12] / "workspace"
    workspace.mkdir(parents=True)
    plan = materialize_profile(
        store,
        profile_id,
        workspace.parent / "profile",
        workspace,
        LaunchOptions(probe_version=True, live=LiveSnapshots(root, manifests)),
    )
    if not plan.launchable:
        raise InputError(
            "Variant profile is not launchable: " + ", ".join(item.code for item in plan.issues)
        )
    limits = DriverLimits.for_duration(LAUNCH_CHECK_SECONDS)
    artifacts = ArtifactWriter(workspace.parent / "driver", limits.max_artifact_bytes)
    terminal = TuiTestTerminal(transport, runtime_root / uuid4().hex[:10], limits=limits)
    source = NativeLogSource(
        native_log_root(Path(plan.config_roots["config"]), plan.agent),
        plan.agent,
        artifacts,
        limits=limits,
    )
    session = DriverSession(
        terminal, source, artifacts, agent=plan.agent, limits=limits, cancelled=Event()
    )
    with RuntimeBindings(plan, environment), session:
        observation = session.start(plan, launch_environment(plan, environment))
        if observation.state != "ready":
            return observation
        delivered = tuple(delivered_prompt(plan.agent, command) for command in commands)
        try:
            return _probe(terminal, plan.agent, observation, delivered if workflow else ())
        except TransportError as error:
            return Observation(
                state="unsupported",
                reason=f"The typing probe lost its terminal acknowledgement: {error}",
                text=observation.text,
            )


def _probe(
    terminal: TuiTestTerminal, agent: AgentKind, ready: Observation, commands: tuple[str, ...]
) -> Observation:
    typed = (PROBE, *dict.fromkeys(text for command in commands for text in _typings(command)))
    hint = (
        "check that Claude's command menu offers it."
        if agent == AgentKind.CLAUDE
        else "check that the input box shows it unchanged."
    )
    for text in typed:
        terminal.paste(text)
        if not _settles(terminal, partial(submittable, agent, text=text)):
            return _unsupported(
                terminal,
                "Typed text did not appear in the input box as typed."
                if text == PROBE
                else f"Workflow command {text!r} was not ready to submit as typed; {hint}",
            )
        if not _clears(terminal, agent):
            return _unsupported(terminal, "The input box could not be cleared after typing.")
    return ready


def _typings(command: str) -> tuple[str, ...]:
    normalized = normalize_input(command)
    return normalized.split(maxsplit=1)[0], normalized


def _clears(terminal: TuiTestTerminal, agent: AgentKind) -> bool:
    for _ in range(4):
        terminal.clear_line()
        if _settles(terminal, lambda screen: composer(agent, screen) == ""):
            return True
    return False


def _settles(terminal: TuiTestTerminal, condition: Callable[[Screen], bool]) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if condition(terminal.state()):
            return True
        time.sleep(0.2)
    return False


def _unsupported(terminal: TuiTestTerminal, reason: str) -> Observation:
    return Observation(state="unsupported", reason=reason, text=terminal.state().text)


def discard_check(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)


@dataclass(frozen=True)
class PreviousRun:
    run_id: str
    finished: bool
    configured: bool = True


def resumable_run(
    store: ObjectStore,
    experiment_id: str,
    *,
    workflow: bool,
    budget: RunBudget,
    pilot_run: str | None = None,
) -> PreviousRun | None:
    runs = RunStore(store.root)
    matching = sorted(
        (
            metadata
            for metadata in map(runs.read_metadata, runs.list_runs())
            if metadata.experiment_id == experiment_id
        ),
        key=lambda metadata: metadata.created_at,
    )
    compatible = next(
        (
            (metadata, options)
            for metadata in reversed(matching)
            if (options := ExecutionJournal(runs.inspect(metadata.run_id)).options) is None
            or (options.workflow, options.budget, options.pilot_run)
            == (workflow, budget, pilot_run)
        ),
        None,
    )
    if compatible is None:
        return None
    status = run_status(store, compatible[0].run_id)
    return PreviousRun(
        run_id=status.run_id,
        finished=not status.unstarted_trials
        and all(state.stage == TrialStage.FINISHED for state in status.attempts),
        configured=compatible[1] is not None,
    )


def _price_versions(attempts: Sequence[AttemptReport]) -> list[str]:
    return sorted(
        {
            record.price_version
            for attempt in attempts
            for role in (attempt.roles or (attempt.result.roles if attempt.result else ()))
            for record in role.records
            if record.price_version
        }
    )


def _group_measures(
    group: GroupMetrics | None, attempts: Sequence[AttemptReport]
) -> dict[str, JsonValue]:
    if group is None:
        return {}
    payload = group.model_dump(mode="json", exclude={"variant"})
    payload["price_versions"] = _price_versions(attempts)
    return payload


def _tally(pairs: Sequence[tuple[str, str]]) -> dict[str, JsonValue]:
    return {
        identifier: dict(Counter(value for name, value in pairs if name == identifier))
        for identifier in sorted({name for name, _ in pairs})
    }


def _attempt_summary(
    attempt: AttemptReport, stop_reasons: Mapping[str, str | None]
) -> dict[str, JsonValue]:
    result, topline = attempt.result, attempt.topline
    return {
        "attempt_id": attempt.attempt_id,
        "stop_reason": stop_reasons.get(attempt.attempt_id),
        "outcome": result.outcome if result else None,
        "outcome_reason": result.outcome_reason if result else None,
        "independence": topline.independence if topline else None,
        "corrections": topline.corrections if topline else None,
        "exclusions": list(result.exclusion_reasons) if result else [],
        "first_inspection": topline.first_inspection if topline else None,
        "workflow": topline.workflow if topline else None,
    }


def summarize(report: RunReport, stop_reasons: Mapping[str, str | None]) -> dict[str, JsonValue]:
    def attempts(name: str) -> list[AttemptReport]:
        return [attempt for attempt in report.attempts if (attempt.variant or "unknown") == name]

    def group(name: str) -> GroupMetrics | None:
        return next((item for item in report.groups if item.variant == name), None)

    def results(name: str) -> list[Assessment]:
        return [attempt.result for attempt in attempts(name) if attempt.result is not None]

    summary: dict[str, JsonValue] = {
        name: {
            "attempts": [_attempt_summary(attempt, stop_reasons) for attempt in attempts(name)],
            "facts": _tally(
                [(item.fact_id, item.verdict) for result in results(name) for item in result.facts]
            ),
            "criteria": _tally(
                [
                    (item.criterion_id, item.outcome)
                    for result in results(name)
                    for item in result.criteria
                ]
            ),
            "measures": _group_measures(group(name), attempts(name)),
        }
        for name in sorted({attempt.variant or "unknown" for attempt in report.attempts})
    }
    floor = report.floor
    if floor is not None:
        summary[floor.label] = {
            "floor": True,
            "run_id": floor.run_id,
            "budget": floor.budget.model_dump(mode="json"),
            "measures": floor.group.model_dump(mode="json", exclude={"variant"}),
        }
    return summary


@dataclass(frozen=True)
class PilotBudget:
    run_id: str
    turns: int
    stand_in_replies: int
    inspections: int
    budget: RunBudget

    def describe(self) -> str:
        budget = self.budget
        return (
            f"budget from pilot {self.run_id}: it used {self.turns} turns, "
            f"{self.stand_in_replies} stand-in replies and {self.inspections} inspections; "
            f"this run allows {budget.max_turns} turns, {budget.stand_in_replies} stand-in "
            f"replies and {budget.max_inspections} inspections"
        )


def _used(store: ObjectStore, run_id: str, attempt: AttemptReport) -> tuple[int, int, int]:
    result = attempt.result
    if result is None or attempt.capture_id is None:
        raise InputError(f"Pilot run {run_id} has no graded capture to derive a budget from.")
    capture = load_capture(store, attempt.capture_id)
    events = RunStore(store.root).inspect(run_id).events
    return (
        capture.accepted_turns,
        sum(call.role == "stand_in" for call in capture.controller_calls),
        len(inspection_records(events, attempt.attempt_id)),
    )


def pilot_budget(store: ObjectStore, run_id: str) -> PilotBudget:
    report = report_run(store, run_id, floor=False)
    if len(report.attempts) != 1:
        raise InputError(
            f"--budget-from needs a pilot run with exactly one attempt; run {run_id} has "
            f"{len(report.attempts)}. Pilot with one variant and --repetitions 1."
        )
    attempt = report.attempts[0]
    result = attempt.result
    if result is None or result.phase != "finished":
        raise InputError(
            f"Pilot run {run_id} has no finished assessment; run `dryheave assess {run_id}` "
            "first, then pass it to --budget-from."
        )
    if result.outcome != "pass":
        raise InputError(
            f"Pilot run {run_id} did not pass (outcome {result.outcome} "
            f"({result.outcome_reason})), so its usage cannot size a budget. "
            "Iterate on the case, or re-pilot on a stronger model (for example "
            "--variant pilot=claude-sonnet-4-6@low) and pass that run to --budget-from."
        )
    if attempt_eligibility(attempt) != "eligible":
        reasons = tuple(dict.fromkeys((*attempt.current_exclusions, *result.exclusion_reasons)))
        named = ", ".join(reasons) or "an unresolved audit finding"
        raise InputError(
            f"Pilot run {run_id} passed but is excluded ({named}), so its usage cannot size a "
            f"budget. Read it with `dryheave report {run_id}`, resolve a finding with "
            "`dryheave review`, or re-pilot."
        )
    turns, replies, inspections = _used(store, run_id, attempt)
    return PilotBudget(
        run_id=run_id,
        turns=turns,
        stand_in_replies=replies,
        inspections=inspections,
        budget=derived_budget(turns=turns, stand_in_replies=replies, inspections=inspections),
    )


def check_pilot_matches(
    store: ObjectStore, run_id: str, experiment_id: str, *, workflow: bool
) -> None:
    pilot = ExecutionJournal(RunStore(store.root).inspect(run_id))
    pilot_experiment = load_experiment(store, pilot.journal.metadata.experiment_id)
    if pilot.options is None or identities(pilot_experiment, pilot.options.workflow) != identities(
        load_experiment(store, experiment_id), workflow
    ):
        raise InputError(
            f"Pilot run {run_id} used a different case, stand-in, scoring or workflow setting "
            "than this benchmark, so it cannot size or floor it. Pilot with the same case, "
            "--stand-in, --judge and --inspector settings, and --no-workflow on both or neither."
        )


def attempt_progress_lines(
    report: RunReport, stop_reasons: Mapping[str, str | None]
) -> tuple[str, ...]:
    def line(attempt: AttemptReport) -> str:
        topline = attempt.topline
        parts = [f"attempt {attempt.attempt_id} stopped ({stop_reasons.get(attempt.attempt_id)})"]
        if topline is not None and topline.outcome is not None:
            parts.append(
                f"outcome {topline.outcome}"
                + (f" ({topline.outcome_reason})" if topline.outcome != "pass" else "")
            )
        if topline is not None and topline.workflow is not None:
            parts.append(f"workflow {topline.workflow}")
            parts.append(f"corrections {topline.corrections}")
            parts.append(f"first check {topline.first_inspection}")
        return "; ".join(parts)

    return tuple(line(attempt) for attempt in report.attempts)
