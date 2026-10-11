import re
import shutil
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from uuid import uuid4

from pydantic import JsonValue

from dryheave.cases import load_frozen_case
from dryheave.controller_models import ClaudeControllerRuntime, ControllerRecipe, RoleBudget
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.auth import RuntimeBindings
from dryheave.drivers.dialogs import composer, composer_matches
from dryheave.drivers.log_source import NativeLogSource, native_log_root
from dryheave.drivers.models import DriverLimits, Observation, Screen
from dryheave.drivers.session import DriverSession
from dryheave.drivers.tui_test import TuiTestTerminal
from dryheave.errors import InputError
from dryheave.experiment_models import ExperimentDraft, ScoringConfig, VariantSpec
from dryheave.journals import RunStore
from dryheave.live_config import live_source, require_servers, snapshot_live_config
from dryheave.models import AgentKind, CommandSpec, EnvironmentReference, TrialStage
from dryheave.pricing import builtin_price_table
from dryheave.profile_launch import LaunchOptions, launch_environment, materialize_profile
from dryheave.profile_models import CaptureSpec, LiveConfigManifest, NativeRecipe
from dryheave.profiles import capture_profile
from dryheave.report_models import AttemptReport, RunReport
from dryheave.runner import run_status
from dryheave.skills import SKILL_NAMES
from dryheave.storage import ObjectStore

AUTH_REFERENCE = "CLAUDE_CODE_OAUTH_TOKEN"
PROBE = "dryheave launch check"
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
        raise InputError(f"Expected NAME=MODEL[@EFFORT][,-mcp:SERVER...], got {text!r}.")
    removed = []
    for change in filter(None, match[3].split(",")):
        kind, _, value = change.partition(":")
        if kind != "-mcp" or not value:
            raise InputError(f"Unsupported variant change {change!r}; supported: -mcp:SERVER.")
        removed.append(value)
    return VariantChoice(
        name=match[1], role=parse_role(match[2]), removed_mcp_servers=tuple(removed)
    )


def variant_recipe(choice: VariantChoice, executable: str) -> NativeRecipe:
    return NativeRecipe(
        agent=AgentKind.CLAUDE,
        executable=executable,
        model=choice.role.model,
        effort=choice.role.effort,
        config_source="live",
        workspace_trust="trusted",
        removed_mcp_servers=choice.removed_mcp_servers,
        disabled_skill_names=SKILL_NAMES,
        environment=(EnvironmentReference(name=AUTH_REFERENCE),),
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
                environment=(EnvironmentReference(name=AUTH_REFERENCE),),
            ),
            "runtime": ClaudeControllerRuntime(),
            "budget": budget,
        }
    )


def bench_draft(
    case_id: str,
    profiles: Mapping[str, str],
    *,
    simulator: ControllerRecipe,
    judge: ControllerRecipe | None,
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
        scoring=ScoringConfig(judge=judge, prices=builtin_price_table()),
    )


def capture_variants(
    store: ObjectStore, variants: tuple[VariantChoice, ...], executable: str
) -> dict[str, str]:
    names = [variant.name for variant in variants]
    if len(set(names)) != len(names):
        raise InputError("Variant names must be unique.")
    return {
        variant.name: capture_profile(
            store, CaptureSpec(recipe=variant_recipe(variant, executable))
        )
        for variant in variants
    }


def check_case(store: ObjectStore, reference: str, *, judged: bool) -> str:
    case_id = store.resolve(reference)
    case = load_frozen_case(store, case_id)
    if not judged and any(criterion.kind == "judge" for criterion in case.criteria):
        raise InputError("This case has judge criteria; pass --judge MODEL[@EFFORT].")
    return case_id


def check_removals(manifest: LiveConfigManifest, variants: tuple[VariantChoice, ...]) -> None:
    require_servers(
        manifest.mcp_servers,
        (name for variant in variants for name in variant.removed_mcp_servers),
    )


def snapshot_for_check(root: Path, environment: Mapping[str, str]) -> LiveConfigManifest:
    source, state = live_source(environment, Path.home())
    return snapshot_live_config(source, state, root / "live-config")


def launch_check(
    store: ObjectStore,
    profile_id: str,
    root: Path,
    *,
    runtime_root: Path,
    transport: Path,
    environment: Mapping[str, str],
) -> Observation:
    workspace = root / profile_id[:12] / "workspace"
    workspace.mkdir(parents=True)
    plan = materialize_profile(
        store,
        profile_id,
        workspace.parent / "profile",
        workspace,
        LaunchOptions(probe_version=True, live_config=root / "live-config"),
    )
    if not plan.launchable:
        raise InputError(
            "Variant profile is not launchable: " + ", ".join(item.code for item in plan.issues)
        )
    limits = DriverLimits(duration_seconds=60.0)
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
        terminal.paste(PROBE)
        if not _settles(terminal, lambda screen: composer_matches(plan.agent, screen, PROBE)):
            return _unsupported(terminal, "Typed text did not appear in the input box as typed.")
        for _ in range(4):
            terminal.clear_line()
            if _settles(terminal, lambda screen: composer(plan.agent, screen) == ""):
                return observation
        return _unsupported(terminal, "The input box could not be cleared after typing.")


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


def resumable_run(store: ObjectStore, experiment_id: str) -> PreviousRun | None:
    runs = RunStore(store.root)
    matching = sorted(
        (
            metadata
            for metadata in map(runs.read_metadata, runs.list_runs())
            if metadata.experiment_id == experiment_id
        ),
        key=lambda metadata: metadata.created_at,
    )
    if not matching:
        return None
    status = run_status(store, matching[-1].run_id)
    return PreviousRun(
        run_id=status.run_id,
        finished=not status.unstarted_trials
        and all(state.stage == TrialStage.FINISHED for state in status.attempts),
    )


def summarize(report: RunReport, stop_reasons: Mapping[str, str | None]) -> dict[str, JsonValue]:
    def attempts(name: str) -> list[AttemptReport]:
        return [attempt for attempt in report.attempts if (attempt.variant or "unknown") == name]

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
                }
                for attempt in attempts(name)
            ],
            "criteria": outcomes(name),
        }
        for name in sorted({attempt.variant or "unknown" for attempt in report.attempts})
    }
