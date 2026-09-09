import json
import sys
import threading
import time

import pytest
from pydantic import ValidationError

from dryheave.cases import load_frozen_case
from dryheave.controller_models import (
    ControllerRecipe,
    DialogueMessage,
    RoleBudget,
    SimulatorDecision,
)
from dryheave.controllers import (
    CallContext,
    _codex_usage,
    controller_command,
    invoke_controller,
    simulator_projection,
)
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.models import CommandSpec
from dryheave.personas import load_frozen_persona


def request_for(store):
    case = load_frozen_case(store, "task")
    return simulator_projection(
        case,
        load_frozen_persona(store, case.persona_id),
        (DialogueMessage(role="assistant", text="Which punctuation?"),),
        17,
    )


def invoke(recipe, request, tmp_path, *, duration=3, cancelled=None):
    identities = []
    context = CallContext(
        call_id="call-1",
        index=0,
        deadline=time.monotonic() + duration,
        cancelled=cancelled or threading.Event(),
        inherited={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        on_identity=identities.append,
    )
    writer = ArtifactWriter(tmp_path / "call", 1024 * 1024)
    return invoke_controller(recipe, request, writer, context), identities


def test_projection_has_only_approved_input(store, benchmark):
    request = request_for(store)
    payload = request.model_dump()
    assert set(payload) == {
        "schema_version",
        "initial_prompt",
        "allowed_facts",
        "persona",
        "dialogue",
        "seed",
        "instruction",
    }
    assert request.allowed_facts[0].fact_id == "punctuation"
    assert set(payload["persona"]) == {
        "instructions",
        "disclosure_policy",
        "unknown_answer_policy",
        "examples",
    }
    encoded = request.model_dump_json()
    for forbidden in (
        "PRIVATE_VERIFIER_SENTINEL",
        str(store.root),
        "hidden.py",
        "Secret future solution",
    ):
        assert forbidden not in encoded


def test_script_condition_and_divergence(store, benchmark, tmp_path):
    request = request_for(store)
    result, identities = invoke(benchmark.simulator, request, tmp_path)
    assert (result.status, result.decision.action, result.decision.fact_ids) == (
        "completed",
        "reply",
        ("punctuation",),
    )
    assert identities == []
    diverged = request.model_copy(
        update={"dialogue": (DialogueMessage(role="assistant", text="What database?"),)}
    )
    result, _ = invoke(benchmark.simulator, diverged, tmp_path / "different")
    assert result.decision.action == "stop"
    assert "divergence" in result.decision.reason


@pytest.mark.parametrize(
    "text", ["/permissions", "!ls", "$skill", "\x1b[A", "hi\rthere", "\t!command"]
)
def test_rejects_simulator_native_authority(text):
    with pytest.raises(ValidationError):
        SimulatorDecision(action="reply", text=text, reason="answer")


def test_json_command_records_observed_usage_and_dedicated_cwd(store, benchmark, tmp_path):
    script = "import json,sys,os; q=json.load(sys.stdin); print(json.dumps({'decision':{'action':'reply','text':q['allowed_facts'][0]['text'],'fact_ids':['punctuation'],'reason':'answer'},'usage':{'output':12,'provenance':'fixture'},'observed_model':'observed'}))"
    recipe = ControllerRecipe(
        kind="json-command",
        isolation="trusted-native",
        command=CommandSpec(argv=(sys.executable, "-c", script)),
    )
    result, identities = invoke(recipe, request_for(store), tmp_path)
    assert (result.status, result.usage.output, result.observed_model) == (
        "completed",
        12,
        "observed",
    )
    assert len(identities) >= 1
    assert (tmp_path / "call/stdout.bin").is_file()


@pytest.mark.parametrize(
    "script",
    [
        "print('not json')",
        "import sys; print('failure',file=sys.stderr); sys.exit(2)",
        "print('x'*70000)",
        "import time; time.sleep(4)",
    ],
)
def test_controller_failures_preserve_unknown_usage(store, benchmark, tmp_path, script):
    recipe = ControllerRecipe(
        kind="json-command",
        isolation="trusted-native",
        command=CommandSpec(argv=(sys.executable, "-c", script)),
        budget=RoleBudget(call_seconds=1),
    )
    started = time.monotonic()
    result, _ = invoke(recipe, request_for(store), tmp_path, duration=0.3)
    assert result.status == "failed"
    assert result.usage is None
    assert result.cost is None
    assert time.monotonic() - started < 2
    assert (tmp_path / "call/stdout.bin").exists()


def test_codex_builder_and_real_format_cumulative_usage(tmp_path):
    recipe = ControllerRecipe(
        kind="codex",
        isolation="trusted-native",
        command=CommandSpec(argv=(sys.executable, "fixture.py")),
        version="0.153.4",
        model="selected",
        effort="ultra",
    )
    argv = controller_command(recipe, tmp_path).argv
    assert argv[:3] == (sys.executable, "fixture.py", "exec")
    assert argv[-1] == "-"
    assert argv[argv.index("--config") + 1] == 'model_reasoning_effort="ultra"'
    usage = {
        "input_tokens": 100,
        "cached_input_tokens": 20,
        "cache_write_input_tokens": 10,
        "output_tokens": 30,
        "reasoning_output_tokens": 15,
    }
    content = b"\n".join(
        json.dumps({"type": "turn.completed", "usage": usage}).encode() for _ in range(2)
    )
    observed = _codex_usage(content)
    assert (
        observed.uncached_input,
        observed.cache_read,
        observed.cache_write,
        observed.output,
    ) == (70, 20, 10, 30)
    assert "cumulative" in observed.provenance
    assert (
        _codex_usage(
            json.dumps({"type": "turn.completed", "usage": dict.fromkeys(usage, 0)}).encode()
        )
        is None
    )


def test_codex_adapter_executes_only_python_protocol_fixture(store, benchmark, tmp_path):
    script = tmp_path / "codex-fixture.py"
    script.write_text("""import json,sys,pathlib
if sys.argv[-1] == "--version":
    print("codex-cli 0.153.4")
    raise SystemExit(0)
assert sys.argv[1] == "exec"
assert "--ignore-user-config" in sys.argv
q = json.load(sys.stdin)
schema = json.loads(pathlib.Path(sys.argv[sys.argv.index("--output-schema") + 1]).read_text())
assert schema["properties"]["action"]
assert set(schema["required"]) == {"action", "text", "fact_ids", "reason"}
assert schema["additionalProperties"] is False
pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1]).write_text(json.dumps({"action":"reply","text":q["allowed_facts"][0]["text"],"fact_ids":["punctuation"],"reason":"fixture answer"}))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":20,"cached_input_tokens":2,"cache_write_input_tokens":0,"output_tokens":10,"reasoning_output_tokens":4}}))
""")
    recipe = ControllerRecipe(
        kind="codex",
        isolation="trusted-native",
        command=CommandSpec(argv=(sys.executable, str(script))),
        version="0.153.4",
        model="fixture",
        effort="high",
    )
    result, _ = invoke(recipe, request_for(store), tmp_path)
    assert result.status == "completed"
    assert result.usage.uncached_input == 18
    assert result.decision.text == "Use a comma after Hello."


def test_controller_output_file_limit_stops_writer(store, benchmark, tmp_path):
    script = (
        "import pathlib,time; pathlib.Path('response.json').write_bytes(b'x'*100000); time.sleep(5)"
    )
    recipe = ControllerRecipe(
        kind="json-command",
        isolation="trusted-native",
        command=CommandSpec(argv=(sys.executable, "-c", script)),
    )
    started = time.monotonic()
    result, _ = invoke(recipe, request_for(store), tmp_path)
    assert result.status == "failed"
    assert time.monotonic() - started < 2
    error = next((tmp_path / "call").glob("*-error.json"))
    assert "controller_response_file_limit" in error.read_text()


def test_invalid_decision_preserves_independently_reported_usage(store, benchmark, tmp_path):
    script = "import json; print(json.dumps({'decision':{'action':'approve','reason':'bad authority'},'usage':{'output':9,'provenance':'fixture'}}))"
    recipe = ControllerRecipe(
        kind="json-command",
        isolation="trusted-native",
        command=CommandSpec(argv=(sys.executable, "-c", script)),
    )
    result, _ = invoke(recipe, request_for(store), tmp_path)
    assert result.status == "failed"
    assert result.usage.output == 9
    assert result.cost is None


def test_controller_cleanup_failure_is_retained_in_role_result(
    store, benchmark, tmp_path, monkeypatch
):
    from dryheave.process_ownership import ProcessOwner

    class DeniedOwner(ProcessOwner):
        def stop(self, *, timeout, terminal_closed):
            report = super().stop(timeout=timeout, terminal_closed=terminal_closed)
            return report.model_copy(
                update={
                    "known_writers_stopped": False,
                    "survivors": report.owned,
                    "errors": ("process_signal_denied",),
                }
            )

    monkeypatch.setattr("dryheave.controllers.ProcessOwner", DeniedOwner)
    recipe = ControllerRecipe(
        kind="json-command",
        isolation="trusted-native",
        command=CommandSpec(
            argv=(sys.executable, "-c", 'print(\'{"decision":{"action":"stop","reason":"done"}}\')')
        ),
    )
    result, identities = invoke(recipe, request_for(store), tmp_path)
    assert result.status == "failed"
    assert result.cleanup.known_writers_stopped is False
    assert result.cleanup.survivors == result.cleanup.owned
    assert result.cleanup.survivors[0] in identities
    assert result.cleanup.errors == ("process_signal_denied",)
    evidence = json.loads(next((tmp_path / "call").glob("*-cleanup.json")).read_bytes())
    assert evidence == result.cleanup.model_dump(mode="json")
