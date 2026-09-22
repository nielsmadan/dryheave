import json
import os
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest
from pydantic import ValidationError

from dryheave.claude_controller import claude_observation, claude_payload
from dryheave.controller_models import (
    ClaudeControllerRuntime,
    ControllerRecipe,
    ControllerRuntime,
    RoleBudget,
)
from dryheave.controllers import CallContext, invoke_controller
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.errors import InputError
from dryheave.models import CommandSpec, EnvironmentReference
from test_controllers import request_for

FIXTURE = Path(__file__).parent / "fixtures/claude-print-2.1.278-synthetic.json"


def recipe_for(executable, **changes):
    return ControllerRecipe.model_validate(
        {
            "kind": "claude",
            "version": "2.1.278",
            "model": "claude-haiku-4-5-20251001",
            "isolation": "trusted-native",
            "conversation_policy": "design-approval",
            "runtime": ClaudeControllerRuntime(),
            "command": CommandSpec(
                argv=(str(executable),),
                environment=(EnvironmentReference(name="CLAUDE_CODE_OAUTH_TOKEN"),),
            ),
            **changes,
        }
    )


def fake_executable(
    tmp_path, payload=None, *, version="2.1.278 (Claude Code)", extra="", exit_code=0
):
    executable = tmp_path / "synthetic-claude"
    payload = json.loads(FIXTURE.read_bytes()) if payload is None else payload
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,os,pathlib,sys,time,subprocess\n"
        f"if sys.argv[-1] == '--version':\n    print({version!r})\n    raise SystemExit(0)\n"
        "argv=sys.argv[1:]\n"
        "assert '--print' in argv and '--no-session-persistence' in argv\n"
        "assert argv[argv.index('--tools')+1] == ''\n"
        "assert argv[argv.index('--setting-sources')+1] == ''\n"
        "assert '--strict-mcp-config' in argv\n"
        "assert json.loads(argv[argv.index('--mcp-config')+1]) == {'mcpServers': {}}\n"
        "assert json.loads(argv[argv.index('--settings')+1])['disableAllHooks'] is True\n"
        "assert '--bare' not in argv and '--dangerously-skip-permissions' not in argv\n"
        "assert 'ANTHROPIC_API_KEY' not in os.environ\n"
        "assert pathlib.Path(os.environ['CLAUDE_CONFIG_DIR']).name == 'config'\n"
        "assert pathlib.Path.cwd().name == 'work'\n"
        "request=json.load(sys.stdin)\n"
        "schema=json.loads(argv[argv.index('--json-schema')+1])\n"
        "assert schema['additionalProperties'] is False\n"
        "assert set(schema['required']) == set(schema['properties'])\n"
        f"print({json.dumps(payload)!r}, flush=True)\n"
        + extra
        + f"\nraise SystemExit({exit_code})\n"
    )
    executable.chmod(0o700)
    return executable


def invoke(tmp_path, recipe, request, *, cancelled=None, duration=3, **options):
    identities = []
    context = CallContext(
        call_id="synthetic-call",
        index=options.get("index", 0),
        deadline=time.monotonic() + duration,
        cancelled=cancelled or threading.Event(),
        inherited=options.get("inherited")
        if options.get("inherited") is not None
        else {
            "PATH": os.defpath,
            "HOME": str(tmp_path),
            "CLAUDE_CODE_OAUTH_TOKEN": "synthetic-offline-value",
            "ANTHROPIC_API_KEY": "must-not-inherit",
        },
        on_identity=identities.append,
        runtime_root=tmp_path / "runtime",
    )
    writer = ArtifactWriter(tmp_path / "evidence", 1024 * 1024)
    return invoke_controller(recipe, request, writer, context), identities


@pytest.mark.integration
def test_synthetic_print_protocol_and_owned_runtime(store, benchmark, tmp_path):
    recipe = recipe_for(fake_executable(tmp_path))
    call, identities = invoke(tmp_path, recipe, request_for(store))
    assert call.status == "completed"
    assert call.decision.text == "Use a comma after Hello."
    assert (
        call.usage.uncached_input,
        call.usage.cache_write,
        call.usage.cache_read,
        call.usage.output,
    ) == (20, 5, 10, 12)
    assert call.usage.reasoning is None
    assert call.observed_model == "claude-haiku-4-5-20251001"
    assert call.observed_effort is None
    assert call.cleanup.known_writers_stopped and call.cleanup.terminal_closed
    assert len(identities) >= 2
    command = json.loads(next((tmp_path / "evidence").glob("*-command-intent.json")).read_bytes())
    assert command["environment_names"] == ["CLAUDE_CODE_OAUTH_TOKEN"]
    assert "--effort" not in command["argv"]
    assert (
        json.loads((tmp_path / "runtime/runtime-bindings.json").read_bytes())["runtime_files"] == []
    )
    assert (tmp_path / "runtime/config").stat().st_mode & 0o777 == 0o700
    assert all(
        b"synthetic-offline-value" not in path.read_bytes()
        for path in (tmp_path / "evidence").iterdir()
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "change",
    [
        {"structured_output": {"action": "approve", "reason": "invalid"}},
        {"structured_output": None, "result": '{"action":"stop","reason":"not structured"}'},
        {"subtype": "error_max_turns", "is_error": True, "errors": ["synthetic failure"]},
        {"stop_reason": "max_tokens"},
        {"unrecognized_protocol_field": True},
        {"permission_denials": [{"tool_name": "Bash"}]},
    ],
)
def test_invalid_response_preserves_provider_usage(store, benchmark, tmp_path, change):
    payload = json.loads(FIXTURE.read_bytes()) | change
    call, _ = invoke(tmp_path, recipe_for(fake_executable(tmp_path, payload)), request_for(store))
    assert call.status == "failed"
    assert call.usage.output == 12
    assert call.observed_model == "claude-haiku-4-5-20251001"
    assert call.observed_effort is None
    assert call.cleanup.known_writers_stopped


@pytest.mark.integration
@pytest.mark.parametrize("exit_code,extra", [(7, ""), (0, "time.sleep(5)")])
def test_failed_or_timed_out_process_retains_usage_and_stops(
    store, benchmark, tmp_path, exit_code, extra
):
    recipe = recipe_for(
        fake_executable(tmp_path, extra=extra, exit_code=exit_code),
        budget=RoleBudget(call_seconds=1),
    )
    start = time.monotonic()
    call, _ = invoke(tmp_path, recipe, request_for(store))
    assert call.status == "failed"
    assert call.usage.output == 12
    assert call.cleanup.known_writers_stopped
    assert time.monotonic() - start < 3


@pytest.mark.integration
def test_cancellation_stops_process_and_retains_completed_output(store, benchmark, tmp_path):
    cancelled = threading.Event()
    ready = tmp_path / "runtime/work/ready"
    recipe = recipe_for(
        fake_executable(
            tmp_path,
            extra="pathlib.Path('ready').write_text('ready')\ntime.sleep(5)",
        )
    )

    def cancel_after_output() -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if ready.exists():
                cancelled.set()
                return
            time.sleep(0.01)

    canceller = threading.Thread(target=cancel_after_output)
    canceller.start()
    try:
        call, _ = invoke(tmp_path, recipe, request_for(store), cancelled=cancelled)
    finally:
        canceller.join()
    assert ready.read_text() == "ready"
    assert call.status == "failed"
    assert call.usage.output == 12
    assert call.cleanup.known_writers_stopped


@pytest.mark.parametrize("reason", ["cancelled", "deadline", "missing-auth", "budget"])
def test_prelaunch_failure_does_not_spawn_or_create_runtime(store, benchmark, tmp_path, reason):
    cancelled = threading.Event()
    if reason == "cancelled":
        cancelled.set()
    call, identities = invoke(
        tmp_path,
        recipe_for("/no-agent-launch"),
        request_for(store),
        cancelled=cancelled,
        duration=-1 if reason == "deadline" else 3,
        inherited={} if reason == "missing-auth" else None,
        index=3 if reason == "budget" else 0,
    )
    assert call.status == "failed"
    assert identities == []
    assert not (tmp_path / "runtime").exists()


@pytest.mark.integration
def test_exact_version_mismatch_stops_before_model_call(store, benchmark, tmp_path):
    call, identities = invoke(
        tmp_path,
        recipe_for(fake_executable(tmp_path, version="2.1.263 (Claude Code)")),
        request_for(store),
    )
    assert call.status == "failed"
    assert call.usage is None
    assert len(identities) == 1
    assert call.cleanup.known_writers_stopped
    assert not (tmp_path / "evidence/stdout.bin").exists()


@pytest.mark.integration
def test_owned_child_is_stopped_before_return(store, benchmark, tmp_path):
    extra = (
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "pathlib.Path('child.pid').write_text(str(child.pid))\n"
        "time.sleep(0.2)\n"
    )
    call, identities = invoke(
        tmp_path, recipe_for(fake_executable(tmp_path, extra=extra)), request_for(store)
    )
    pid = int((tmp_path / "runtime/work/child.pid").read_text())
    assert any(identity.pid == pid for identity in identities)
    assert call.cleanup.known_writers_stopped
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def test_usage_model_is_observed_not_requested_and_ambiguous_models_stay_unknown():
    payload = json.loads(FIXTURE.read_bytes())
    payload["modelUsage"]["another-observed-model"] = {"outputTokens": 2}
    observed = claude_observation(json.dumps(payload).encode())
    assert observed.observed_model is None
    assert observed.usage.output == 12
    payload["usage"] = {"input_tokens": True, "output_tokens": 12}
    assert claude_observation(json.dumps(payload).encode()).usage is None
    payload["usage"] = {"input_tokens": 0, "output_tokens": 0}
    assert claude_observation(json.dumps(payload).encode()).usage is None
    payload["usage"] = {"output_tokens": 4}
    partial = claude_observation(json.dumps(payload).encode()).usage
    assert (partial.uncached_input, partial.cache_write, partial.output) == (None, None, 4)


def test_server_tools_violate_frozen_tool_disabled_policy():
    payload = json.loads(FIXTURE.read_bytes())
    payload["usage"]["server_tool_use"]["web_search_requests"] = 1
    with pytest.raises(InputError, match="server tool"):
        claude_payload(json.dumps(payload).encode())


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "2.1.263"},
        {"effort": "high"},
        {"runtime": ControllerRuntime()},
        {"runtime": {"discovery": "claude-tool-free-2.1.278", "sandbox": "read-only"}},
        {"model": "invalid\x00model"},
        {"command": CommandSpec(argv=("claude", "--bare"))},
        {"command": CommandSpec(argv=("claude",))},
        {
            "command": CommandSpec(
                argv=("claude",), environment=(EnvironmentReference(name="ANTHROPIC_API_KEY"),)
            )
        },
    ],
)
def test_recipe_rejects_incompatible_version_policy_and_auth(changes):
    with pytest.raises(ValidationError):
        recipe_for("claude", **changes)


def test_claude_runtime_serializes_only_agent_specific_policy():
    assert recipe_for("claude").model_dump(mode="json")["runtime"] == {
        "discovery": "claude-tool-free-2.1.278",
        "home_policy": "native",
    }


@pytest.mark.integration
def test_sonnet_effort_is_requested_but_not_reported_as_observed(store, benchmark, tmp_path):
    recipe = recipe_for(fake_executable(tmp_path), model="claude-sonnet-4-6", effort="low")
    call, _ = invoke(tmp_path, recipe, request_for(store))
    assert call.status == "completed"
    command = json.loads(next((tmp_path / "evidence").glob("*-command-intent.json")).read_bytes())
    assert command["argv"][command["argv"].index("--effort") + 1] == "low"
    assert command["argv"][command["argv"].index("--model") + 1] == "claude-sonnet-4-6"
    assert call.observed_model == "claude-haiku-4-5-20251001"
    assert call.observed_effort is None
