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

from dryheave.cases import FrozenCase, InspectStep, effective_workflow, load_frozen_case
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
    inspection_recipe,
)
from dryheave.experiments import load_experiment
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
from dryheave.runner import (
    WorkflowPlan,
    require_snapshot_skills,
    run_status,
    selected_workflow_plan,
)
from dryheave.runner_state import ExecutionJournal
from dryheave.skills import SKILL_NAMES
from dryheave.storage import ObjectStore
from dryheave.workflow import slash_steps

CLAUDE_AUTH_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"
VARIANT_AGENTS = {"claude": AgentKind.CLAUDE, "codex": AgentKind.CODEX}
AGENT_PREFIX = re.compile(r"[A-Za-z]+")
PROBE = "dryheave launch check"
LAUNCH_CHECK_SECONDS = 180.0
ROLE = re.compile(r"^([^@,\s]+)(?:@([a-z]+))?$")
VARIANT = re.compile(r"^([a-zA-Z0-9][a-zA-Z0-9_.-]{0,127})=([^,]+)((?:,[^,]+)*)$")
SIMULATOR_CALL_SECONDS = 180
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


def simulator_budget(replies: int) -> RoleBudget:
    return RoleBudget(
        max_calls=replies,
        call_seconds=SIMULATOR_CALL_SECONDS,
        total_seconds=min(3600, max(1, replies) * SIMULATOR_CALL_SECONDS),
    )


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
    simulator: ControllerRecipe,
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
        simulator=simulator,
        scoring=ScoringConfig(judge=judge, inspector=inspector, prices=builtin_price_table()),
    )


def check_workflow(case: FrozenCase, *, workflow: bool, scoring: ScoringConfig) -> None:
    if not workflow:
        return
    inspected = inspection_recipe(scoring) is not None
    steps = effective_workflow(case, enabled=True, inspected=inspected)
    if not inspected and any(isinstance(step, InspectStep) for step in steps):
        raise InputError(
            "This case's workflow has an inspect step but no judge or --inspector "
            "MODEL[@EFFORT]; pass one, or run with --no-workflow."
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
    needs_judge = case.checks or any(criterion.kind == "judge" for criterion in case.criteria)
    if not judged and needs_judge:
        raise InputError("This case has checks or judge criteria; pass --judge MODEL[@EFFORT].")
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


def resumable_run(store: ObjectStore, experiment_id: str, *, workflow: bool) -> PreviousRun | None:
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
            metadata
            for metadata in reversed(matching)
            if (options := ExecutionJournal(runs.inspect(metadata.run_id)).options) is None
            or options.workflow == workflow
        ),
        None,
    )
    if compatible is None:
        return None
    status = run_status(store, compatible.run_id)
    return PreviousRun(
        run_id=status.run_id,
        finished=not status.unstarted_trials
        and all(state.stage == TrialStage.FINISHED for state in status.attempts),
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


def _help_needed_summary(attempts: Sequence[AttemptReport]) -> dict[str, JsonValue]:
    disclosed: Counter[str] = Counter()
    proactive: Counter[str] = Counter()
    on_request: Counter[str] = Counter()
    measured = 0
    latest = {attempt.trial_id: attempt for attempt in attempts}
    for attempt in latest.values():
        result = attempt.result
        if result is None or not result.eligible or attempt.current_exclusions:
            continue
        help_needed = result.help_needed
        if help_needed is None:
            continue
        measured += 1
        disclosed.update(help_needed.facts_disclosed)
        proactive.update(help_needed.proactive_facts)
        on_request.update(help_needed.on_request_facts)
    return {
        "measured_attempts": measured,
        "facts_disclosed": dict(disclosed),
        "disclosed_proactively": dict(proactive),
        "disclosed_on_request": dict(on_request),
    }


def _group_measures(
    group: GroupMetrics | None, attempts: Sequence[AttemptReport]
) -> dict[str, JsonValue]:
    if group is None:
        return {}
    payload = group.model_dump(mode="json", exclude={"variant"})
    payload["price_versions"] = _price_versions(attempts)
    payload["help_needed"] = _help_needed_summary(attempts)
    return payload


def summarize(report: RunReport, stop_reasons: Mapping[str, str | None]) -> dict[str, JsonValue]:
    def attempts(name: str) -> list[AttemptReport]:
        return [attempt for attempt in report.attempts if (attempt.variant or "unknown") == name]

    def group(name: str) -> GroupMetrics | None:
        return next((item for item in report.groups if item.variant == name), None)

    def outcomes(name: str) -> dict[str, JsonValue]:
        results = [
            criterion
            for attempt in attempts(name)
            if attempt.result is not None
            for criterion in attempt.result.criteria
        ]
        return {
            identifier: dict(
                Counter(item.outcome for item in results if item.criterion_id == identifier)
            )
            for identifier in sorted({item.criterion_id for item in results})
        }

    return {
        name: {
            "attempts": [
                {
                    "attempt_id": attempt.attempt_id,
                    "stop_reason": stop_reasons.get(attempt.attempt_id),
                    "completion": attempt.result.completion if attempt.result else None,
                    "exclusions": list(attempt.result.exclusion_reasons) if attempt.result else [],
                    "first_inspection": attempt.topline.first_inspection
                    if attempt.topline
                    else None,
                    "fix_requests": attempt.topline.fix_requests if attempt.topline else None,
                    "workflow": attempt.topline.workflow if attempt.topline else None,
                }
                for attempt in attempts(name)
            ],
            "criteria": outcomes(name),
            "measures": _group_measures(group(name), attempts(name)),
        }
        for name in sorted({attempt.variant or "unknown" for attempt in report.attempts})
    }


def attempt_progress_lines(
    report: RunReport, stop_reasons: Mapping[str, str | None]
) -> tuple[str, ...]:
    def line(attempt: AttemptReport) -> str:
        topline = attempt.topline
        parts = [f"attempt {attempt.attempt_id} stopped ({stop_reasons.get(attempt.attempt_id)})"]
        if topline is not None and topline.workflow is not None:
            parts.append(f"workflow {topline.workflow}")
            parts.append(f"fix requests {topline.fix_requests}")
            parts.append(f"first check {topline.first_inspection}")
        return "; ".join(parts)

    return tuple(line(attempt) for attempt in report.attempts)
