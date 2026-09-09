import json
import os
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import psutil

from dryheave.cases import FrozenCase, subject_context
from dryheave.controller_models import (
    ControllerRecipe,
    ControllerResponse,
    DialogueMessage,
    RoleCall,
    RoleObservation,
    SimulatorDecision,
    SimulatorInput,
)
from dryheave.controller_schema import controller_schema
from dryheave.controller_watch import ControllerWatch
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.models import CleanupReport, ProcessIdentity
from dryheave.errors import DryheaveError, InputError
from dryheave.filesystem import atomic_write, read_bytes
from dryheave.logs.base import mapping
from dryheave.models import CommandSpec, TokenUsage
from dryheave.personas import Persona, subject_persona
from dryheave.process_ownership import ProcessOwner
from dryheave.processes import CommandControl, CommandResult, run_command
from dryheave.serialization import canonical_json, parse_json, parse_model
from dryheave.token_usage import codex_usage


def simulator_projection(
    case: FrozenCase, persona: Persona, dialogue: tuple[DialogueMessage, ...], seed: int
) -> SimulatorInput:
    return parse_model(
        json.dumps(
            {
                **subject_context(case),
                "persona": subject_persona(persona),
                "dialogue": [item.model_dump(mode="json") for item in dialogue],
                "seed": seed,
            }
        ).encode(),
        SimulatorInput,
    )


def controller_command(recipe: ControllerRecipe, root: Path) -> CommandSpec:
    if recipe.command is None:
        raise InputError("Controller recipe has no native command.")
    argv = recipe.command.argv
    if recipe.kind == "codex":
        argv += (
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--json",
            "--color",
            "never",
            "--output-schema",
            str(root / "schema.json"),
            "--output-last-message",
            str(root / "response.json"),
            "--model",
            recipe.model or "",
            "--config",
            f'model_reasoning_effort="{recipe.effort}"',
            "-",
        )
    return recipe.command.model_copy(
        update={
            "argv": argv,
            "timeout_seconds": min(recipe.command.timeout_seconds, recipe.budget.call_seconds),
            "max_output_bytes": min(
                recipe.command.max_output_bytes, recipe.budget.max_output_bytes
            ),
        }
    )


def controller_environment(
    recipe: ControllerRecipe, inherited: Mapping[str, str]
) -> dict[str, str]:
    selected = {
        name: inherited[name] for name in ("PATH", "HOME", "LANG", "LC_ALL") if name in inherited
    }
    if recipe.command is not None:
        for reference in recipe.command.environment:
            if reference.name not in inherited:
                raise InputError(f"Missing controller environment reference: {reference.name}.")
            selected[reference.name] = inherited[reference.name]
    selected.setdefault("PATH", os.defpath)
    return selected


def _scripted(recipe: ControllerRecipe, request: SimulatorInput, index: int) -> ControllerResponse:
    latest = request.dialogue[-1].text if request.dialogue else ""
    if index >= len(recipe.script):
        decision = SimulatorDecision(
            action="stop",
            reason="Script exhausted; no further condition-matched reply is authorized.",
        )
    else:
        step = recipe.script[index]
        matched = latest == step.assistant if step.match == "exact" else step.assistant in latest
        decision = (
            step.decision
            if matched
            else SimulatorDecision(
                action="stop",
                reason="Script divergence: latest assistant text does not match the declared condition.",
            )
        )
    return ControllerResponse(decision=decision)


def _codex_usage(content: bytes) -> TokenUsage | None:
    latest = None
    for line in content.splitlines():
        event = parse_json(line)
        if event.get("type") == "turn.completed":
            latest = mapping(event.get("usage"))
    if not latest:
        return None
    return codex_usage(
        latest,
        protocol="exec-0.153.4",
        provenance="codex-exec-0.153.4-thread-cumulative; all-zero fallback is unknown",
    )


@dataclass
class CallContext:
    call_id: str
    index: int
    deadline: float
    cancelled: Event
    inherited: Mapping[str, str]
    on_identity: Callable[[ProcessIdentity], None]
    cleanup: CleanupReport | None = None


def invoke_controller(
    recipe: ControllerRecipe,
    request: SimulatorInput,
    artifacts: ArtifactWriter,
    context: CallContext,
) -> RoleCall:
    started = time.monotonic()
    try:
        content = canonical_json(request)
        if len(content) > recipe.budget.max_input_bytes:
            raise InputError("Simulator projection exceeds its frozen input bound.")
        artifacts.record("request", request)
        if context.cancelled.is_set() or started >= context.deadline:
            raise InputError("Controller cancelled or overall subject deadline exhausted.")
        if recipe.kind == "scripted":
            response = _scripted(recipe, request, context.index)
        else:
            response = _native(recipe, content, artifacts, context)
        if time.monotonic() > context.deadline or context.cancelled.is_set():
            raise InputError("Controller exhausted the overall subject deadline or was cancelled.")
        if not set(response.decision.fact_ids) <= {fact.fact_id for fact in request.allowed_facts}:
            raise InputError("Simulator response cites an unavailable allowed fact.")
        artifacts.record("response", response)
        return RoleCall(
            call_id=context.call_id,
            status="completed",
            elapsed_seconds=time.monotonic() - started,
            decision=response.decision,
            usage=response.usage,
            usage_reason="Observed controller usage."
            if response.usage is not None
            else "Scripted fixture has no model usage."
            if recipe.kind == "scripted"
            else "Controller usage was not observed.",
            observed_model=response.observed_model,
            observed_effort=response.observed_effort,
            cleanup=context.cleanup,
        )
    except (DryheaveError, OSError) as error:
        artifacts.record("error", {"code": type(error).__name__, "message": str(error)})
        observation = failure_observation(recipe, artifacts)
        return RoleCall(
            call_id=context.call_id,
            status="failed",
            elapsed_seconds=time.monotonic() - started,
            error=type(error).__name__,
            usage=observation.usage,
            observed_model=observation.observed_model,
            observed_effort=observation.observed_effort,
            usage_reason="Failed call; any observed usage is retained independently of response validity.",
            cleanup=context.cleanup,
        )


def failure_observation(recipe: ControllerRecipe, artifacts: ArtifactWriter) -> RoleObservation:
    usage = None
    source = "stdout.bin"
    if recipe.kind == "codex":
        source = "response.json"
        with suppress(OSError, DryheaveError):
            usage = _codex_usage(
                read_bytes(artifacts.root / "stdout.bin", limit=recipe.budget.max_output_bytes)
            )
    try:
        raw = parse_json(read_bytes(artifacts.root / source, limit=recipe.budget.max_output_bytes))
    except (OSError, DryheaveError):
        return RoleObservation(usage=usage)
    if recipe.kind == "codex":
        raw["usage"] = usage.model_dump(mode="json") if usage is not None else None
    valid = {}
    for name in RoleObservation.model_fields:
        if name in raw:
            try:
                parse_model(canonical_json({name: raw[name]}), RoleObservation)
            except DryheaveError:
                continue
            valid[name] = raw[name]
    return parse_model(canonical_json(valid), RoleObservation)


def _native(
    recipe: ControllerRecipe,
    content: bytes,
    artifacts: ArtifactWriter,
    context: CallContext,
) -> ControllerResponse:
    root = artifacts.root
    if recipe.kind == "codex":
        schema = controller_schema(SimulatorDecision)
        atomic_write(
            root / "schema.json",
            canonical_json(schema),
            replace=False,
        )
    command = controller_command(recipe, root)
    artifacts.record(
        "command-intent",
        {
            "argv": list(command.argv),
            "environment_names": [reference.name for reference in command.environment],
            "isolation": recipe.isolation,
        },
    )
    result = run_role_command(recipe, content, artifacts, context, command)
    if result.returncode or result.outcome != "exited":
        raise InputError("Controller command did not complete successfully.")
    if recipe.kind == "codex":
        response = parse_model(
            read_bytes(root / "response.json", limit=recipe.budget.max_output_bytes),
            SimulatorDecision,
        )
        return ControllerResponse(decision=response, usage=_codex_usage(result.stdout))
    return parse_model(result.stdout, ControllerResponse)


def run_role_command(
    recipe: ControllerRecipe,
    content: bytes,
    artifacts: ArtifactWriter,
    context: CallContext,
    command: CommandSpec,
) -> CommandResult:
    environment = controller_environment(recipe, context.inherited)
    owner = ProcessOwner()
    watch = ControllerWatch(
        artifacts.root,
        context.cancelled,
        max_bytes=recipe.budget.max_input_bytes + 8 * recipe.budget.max_output_bytes,
        output_bytes=recipe.budget.max_output_bytes,
    )

    def claim(pid: int) -> None:
        owner.add(psutil.Process(pid))
        publish()

    def publish() -> None:
        owner.publish(context.on_identity)

    control = CommandControl(deadline=context.deadline, cancelled=watch.cancelled, on_poll=publish)
    try:
        if recipe.kind == "codex" and recipe.command is not None:
            version = run_command(
                CommandSpec(
                    argv=(*recipe.command.argv, "--version"),
                    timeout_seconds=5,
                    max_output_bytes=4096,
                ),
                artifacts.root,
                environment=environment,
                on_start=claim,
                control=control,
            )
            artifacts.record(
                "version",
                {
                    "returncode": version.returncode,
                    "outcome": version.outcome,
                    "stdout": version.stdout.decode(errors="replace"),
                    "stderr": version.stderr.decode(errors="replace"),
                },
            )
            if (
                version.returncode
                or version.outcome != "exited"
                or version.stdout.strip() != b"codex-cli 0.153.4"
            ):
                raise InputError("Codex controller version does not match the verified adapter.")
        result = run_command(
            command,
            artifacts.root,
            input_bytes=content,
            environment=environment,
            on_start=claim,
            control=control,
        )
        artifacts.append("stdout.bin", result.stdout)
        artifacts.append("stderr.bin", result.stderr)
        artifacts.record(
            "command-result", {"returncode": result.returncode, "outcome": result.outcome}
        )
    finally:
        cleanup = owner.stop(timeout=3, terminal_closed=True)
        context.cleanup = cleanup
        watch.close()
        cleanup = cleanup.model_copy(
            update={
                "logs_drained": True,
                "errors": (*cleanup.errors, *((watch.error,) if watch.error is not None else ())),
            }
        )
        context.cleanup = cleanup
        publish()
        artifacts.record("cleanup", cleanup)
        if not cleanup.known_writers_stopped or watch.error is not None:
            raise InputError(watch.error or "Controller cleanup could not stop every known writer.")
    return result
