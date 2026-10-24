import hashlib
import json
import shutil
import signal
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import HOST, broken_then_fixed, workflow_benchmark
from dryheave import transcript_cli
from dryheave.cases import load_frozen_case
from dryheave.cli import main
from dryheave.controller_models import RaisedFact, RoleCall
from dryheave.controllers import stand_in_projection
from dryheave.errors import CancelledError, InputError, IntegrityError, LockBusyError, NotFoundError
from dryheave.experiments import create_experiment
from dryheave.journals import RunStore
from dryheave.judges import FactJudgeInput, FactJudgeResponse, FactJudgment, JudgeFact
from dryheave.models import ObjectKind
from dryheave.personas import Persona, load_frozen_persona
from dryheave.replay import (
    ReplayOverrides,
    ReplayRoles,
    StoredCall,
    _old_output,
    _prepare,
    _roles,
    _selected,
    inspector_request,
    render_replay,
    replay_attempt,
    stand_in_request,
    stored_call,
)
from dryheave.runner import run_experiment
from dryheave.runner_models import RunOptions
from dryheave.runner_state import ExecutionJournal

VOICE = Persona(
    name="Terse",
    instructions="Answer in as few words as possible.",
    disclosure_policy="Share approved facts only when asked.",
    unknown_answer_policy="Say you do not know.",
    reviewed_subject_safe=True,
)


def tree(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def attempt(store, run_id):
    execution = ExecutionJournal(RunStore(store.root).inspect(run_id))
    (state,) = execution.attempts.values()
    return execution, state


def test_replay_resends_every_stand_in_call_with_the_overridden_voice(store, assessed, tmp_path):
    voice_id = store.put(ObjectKind.PERSONA, VOICE)
    _, state = attempt(store, assessed.run_id)
    before = tree(store.root)
    report = replay_attempt(
        store,
        assessed.run_id,
        state.attempt_id,
        call_id=None,
        overrides=ReplayOverrides(voice_id=voice_id, persona=load_frozen_persona(store, voice_id)),
        out=tmp_path / "replay",
        inherited={},
    )
    assert tree(store.root) == before
    assert [item.call_id for item in report.calls] == [call.call_id for call in state.calls]
    first, second = report.calls
    assert (first.source, first.old.decision.text, first.new.decision.text) == (
        "store",
        "Use a comma after Hello.",
        "Use a comma after Hello.",
    )
    assert (second.old.decision.action, second.new.decision.action) == ("stop", "stop")
    sent = json.loads(
        (tmp_path / "replay" / first.call_id / "controller" / "000001-request.json").read_text()
    )
    assert sent["persona"]["instructions"] == VOICE.instructions
    assert sent["dialogue"][-1] == {"role": "assistant", "text": "Which punctuation?"}
    saved = json.loads((tmp_path / "replay" / "replay.json").read_text())
    assert (saved["voice_id"], len(saved["calls"])) == (voice_id, 2)


def test_replay_reads_the_capture_when_the_run_directory_lost_its_requests(
    store, assessed, tmp_path
):
    _, state = attempt(store, assessed.run_id)
    shutil.rmtree(
        store.root / "runs" / assessed.run_id / "attempts" / state.attempt_id / "controller"
    )
    report = replay_attempt(
        store,
        assessed.run_id,
        state.attempt_id,
        call_id=state.calls[0].call_id,
        overrides=ReplayOverrides(),
        out=tmp_path / "replay",
        inherited={},
    )
    (call,) = report.calls
    assert (call.source, call.new.status) == ("store", "completed")


def test_a_call_with_no_stored_request_anywhere_is_refused(store, assessed):
    execution, state = attempt(store, assessed.run_id)
    shutil.rmtree(
        store.root / "runs" / assessed.run_id / "attempts" / state.attempt_id / "controller"
    )
    detached = state.model_copy(update={"capture_id": None})
    with pytest.raises(InputError, match="No stored request for call c-"):
        stored_call(store, execution, detached, state.calls[0])


def test_the_run_directory_supplies_requests_for_an_uncaptured_attempt(store, assessed):
    execution, state = attempt(store, assessed.run_id)
    detached = state.model_copy(update={"capture_id": None})
    stored = stored_call(store, execution, detached, state.calls[1])
    assert (stored.source, stored.index, stored.role) == ("run", 1, "stand_in")


def test_replay_refuses_unknown_calls_and_outputs_inside_the_store_or_taken(
    store, assessed, tmp_path
):
    _, state = attempt(store, assessed.run_id)

    def replay(**changes):
        arguments = {
            "call_id": state.calls[0].call_id,
            "overrides": ReplayOverrides(),
            "out": tmp_path / "fresh",
            "inherited": {},
        } | changes
        return replay_attempt(store, assessed.run_id, state.attempt_id, **arguments)

    with pytest.raises(NotFoundError, match="no stand-in or inspector call c-missing"):
        replay(call_id="c-missing")
    with pytest.raises(InputError, match="outside the store"):
        replay(out=store.root / "replay")
    (tmp_path / "taken").mkdir()
    with pytest.raises(InputError, match="already exists"):
        replay(out=tmp_path / "taken")
    assert not (tmp_path / "fresh").exists()


def test_a_replacement_case_rewrites_the_stand_in_facts_and_raised_texts(store, graded_benchmark):
    case = load_frozen_case(store, graded_benchmark.cases[0])
    request = stand_in_projection(
        case,
        load_frozen_persona(store, case.persona_id),
        (),
        3,
        inspection=(RaisedFact(fact_id="punctuation", text="old text", observation="No comma."),),
    )
    fact = case.allowed_facts[0].model_copy(update={"text": "Put a comma right after Hello."})
    changed = case.model_copy(update={"allowed_facts": (fact,), "initial_prompt": "Greet well."})
    rebuilt = stand_in_request(request, ReplayOverrides(case=changed), 128 * 1024)
    assert rebuilt.initial_prompt == "Greet well."
    assert [(item.fact_id, item.text) for item in rebuilt.allowed_facts] == [
        ("punctuation", "Put a comma right after Hello.")
    ]
    assert [(item.text, item.observation) for item in rebuilt.inspection] == [
        ("Put a comma right after Hello.", "No comma.")
    ]
    with pytest.raises(InputError, match="has no fact punctuation"):
        stand_in_request(
            request, ReplayOverrides(case=case.model_copy(update={"allowed_facts": ()})), 131072
        )


def test_a_replacement_case_gives_the_inspector_the_new_check_text(store, graded_benchmark):
    case = load_frozen_case(store, graded_benchmark.cases[0])
    request = FactJudgeInput(
        initial_prompt=case.initial_prompt,
        facts=(JudgeFact(fact_id="punctuation", text="old", check="old check"),),
        files={"greet.py": "def greet(name):\n"},
        omitted_binary_files=(),
    )
    fact = case.allowed_facts[0].model_copy(
        update={"check": "greet('A') returns 'Hello, A'.", "verifier": None}
    )
    rebuilt = inspector_request(
        request, ReplayOverrides(case=case.model_copy(update={"allowed_facts": (fact,)})), 65536
    )
    assert [(item.fact_id, item.check) for item in rebuilt.facts] == [
        ("punctuation", "greet('A') returns 'Hello, A'.")
    ]
    assert rebuilt.files == request.files


def test_replay_cli_shows_old_output_next_to_new(store, assessed, tmp_path, capsys):
    _, state = attempt(store, assessed.run_id)
    prefix = ["--store", str(store.root), "replay", assessed.run_id, "--attempt", state.attempt_id]
    assert main(prefix) != 0
    assert "Choose --call ID or --all-stand-in." in capsys.readouterr().err
    voice_id = store.put(ObjectKind.PERSONA, VOICE)
    store.set_alias("terse", voice_id)
    chosen = ["--all-stand-in", "--voice", "terse", "--out", str(tmp_path / "out")]
    assert main([*prefix, *chosen]) == 0
    printed = capsys.readouterr().out
    assert f"Voice: {voice_id}\n" in printed
    first = state.calls[0].call_id
    assert f"stand-in call {first} (request from store)\n    old:\n      reply\n" in printed
    assert printed.count("        Use a comma after Hello.") == 2
    assert f"Output: {(tmp_path / 'out').resolve()}" in printed


@pytest.mark.integration
def test_replaying_an_inspection_sends_the_new_check_and_reports_a_failed_resend(
    store, graded_benchmark, tmp_path
):
    draft, inspector = workflow_benchmark(store, graded_benchmark, tmp_path)
    result = run_experiment(
        store,
        create_experiment(store, broken_then_fixed(draft)),
        options=RunOptions(mode="offline-fixture", workflow=True),
        host=HOST,
    )
    _, state = attempt(store, result.run_id)
    judge = next(call for call in state.calls if call.role == "judge")
    case = load_frozen_case(store, draft.cases[0])
    facts = tuple(
        fact.model_copy(update={"check": "greet keeps one parameter called name."})
        if fact.fact_id == "interface"
        else fact
        for fact in case.allowed_facts
    )
    changed = store.put(
        ObjectKind.CASE,
        case.model_copy(update={"allowed_facts": facts}),
        files=store.read_blobs(draft.cases[0]),
        references=(case.persona_id, case.repository_id),
    )
    before = tree(store.root / "runs" / result.run_id)
    sent = len(list(inspector.glob("request-*.json")))
    report = replay_attempt(
        store,
        result.run_id,
        state.attempt_id,
        call_id=judge.call_id,
        overrides=ReplayOverrides(case_id=changed, case=load_frozen_case(store, changed)),
        out=tmp_path / "inspect",
        inherited={},
    )
    assert tree(store.root / "runs" / result.run_id) == before
    (call,) = report.calls
    assert (call.role, call.new.status) == ("inspector", "completed")
    assert [(item.fact_id, item.verdict) for item in call.old.judgments] == [("interface", "met")]
    printed = render_replay(report)
    assert f"inspector call {judge.call_id} (request from store)" in printed
    assert "        interface: met — Reads well." in printed
    resent = json.loads((inspector / f"request-{sent}.json").read_text())
    assert resent["facts"] == [
        {
            "fact_id": "interface",
            "text": "Keep greet's public interface.",
            "check": "greet keeps one parameter called name.",
        }
    ]

    (inspector / "outcomes.json").write_text(json.dumps(["broken"]))
    failed = replay_attempt(
        store,
        result.run_id,
        state.attempt_id,
        call_id=judge.call_id,
        overrides=ReplayOverrides(),
        out=tmp_path / "broken",
        inherited={},
    )
    (call,) = failed.calls
    assert (call.new.status, call.new.error, call.new.judgments) == ("failed", "InputError", ())


def test_a_cancelled_replay_stops_with_the_cancelled_error_and_writes_no_report(
    store, assessed, tmp_path, monkeypatch
):
    _, state = attempt(store, assessed.run_id)
    sent = []
    before = signal.getsignal(signal.SIGTERM)

    def fake(recipe, request, artifacts, context):
        sent.append(context.call_id)
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        assert context.cancelled.is_set()
        return state.calls[0].model_copy(update={"call_id": context.call_id})

    monkeypatch.setattr("dryheave.replay.invoke_controller", fake)
    out = tmp_path / "replay"
    with pytest.raises(CancelledError):
        replay_attempt(
            store,
            assessed.run_id,
            state.attempt_id,
            call_id=None,
            overrides=ReplayOverrides(),
            out=out,
            inherited={},
        )
    assert len(sent) == 1
    assert not (out / "replay.json").exists()
    assert signal.getsignal(signal.SIGTERM) is before


def test_the_old_inspector_output_applies_the_same_settling_as_the_run():
    response = FactJudgeResponse(
        judgments=(
            FactJudgment(fact_id="punctuation", verdict="met", observation="ok", evidence=""),
        )
    )
    stored = StoredCall(
        call=RoleCall(call_id="j-old", role="judge", status="completed"),
        role="inspector",
        index=0,
        source="store",
        files={"000001-response.json": response.model_dump_json().encode()},
    )
    assert [item.verdict for item in _old_output(stored).judgments] == ["unclear"]


def test_a_busy_native_lock_leaves_no_output_directory_behind(store, assessed, tmp_path):
    _, state = attempt(store, assessed.run_id)
    with store.native_lock(), pytest.raises(LockBusyError):
        replay_attempt(
            store,
            assessed.run_id,
            state.attempt_id,
            call_id=state.calls[0].call_id,
            overrides=ReplayOverrides(),
            out=tmp_path / "replay",
            inherited={},
        )
    assert not (tmp_path / "replay").exists()


def test_a_corrupt_capture_blob_is_not_replaced_by_the_run_directory_copy(store, assessed):
    execution, state = attempt(store, assessed.run_id)
    blobs = store.object_path(state.capture_id) / "blobs"
    for blob in blobs.iterdir():
        blob.write_bytes(b"{}")
    with pytest.raises(IntegrityError, match="hash mismatch"):
        stored_call(store, execution, state, state.calls[0])


def test_inspector_calls_refuse_a_voice_and_a_missing_inspector(store, assessed):
    execution, state = attempt(store, assessed.run_id)
    judge = RoleCall(call_id="j-x", role="judge", status="completed")
    held = state.model_copy(update={"calls": (*state.calls, judge)})
    persona = load_frozen_persona(store, store.put(ObjectKind.PERSONA, VOICE))
    with pytest.raises(InputError, match="--voice applies to stand-in calls"):
        _selected(held, "j-x", ReplayOverrides(persona=persona))
    request = FactJudgeInput(
        initial_prompt="Fix it.",
        facts=(JudgeFact(fact_id="punctuation", text="t", check="c"),),
        files={},
        omitted_binary_files=(),
    )
    stored = StoredCall(
        call=judge,
        role="inspector",
        index=0,
        source="store",
        files={"000001-request.json": request.model_dump_json().encode()},
    )
    roles = replace(_roles(store, execution), inspector=None)
    assert isinstance(roles, ReplayRoles)
    with pytest.raises(InputError, match="no inspector to replay"):
        _prepare(stored, roles, ReplayOverrides())


def test_an_attempt_without_stand_in_calls_has_nothing_to_replay_in_bulk(store, assessed):
    _, state = attempt(store, assessed.run_id)
    with pytest.raises(InputError, match="made no stand-in calls"):
        _selected(state.model_copy(update={"calls": ()}), None, ReplayOverrides())


def test_replay_cli_resolves_a_replacement_case_and_a_workspace_voice(
    store, assessed, graded_benchmark, tmp_path, capsys, monkeypatch
):
    _, state = attempt(store, assessed.run_id)
    case_id = graded_benchmark.cases[0]
    prefix = ["--store", str(store.root), "replay", assessed.run_id, "--attempt", state.attempt_id]
    assert (
        main(
            [
                *prefix,
                "--call",
                state.calls[0].call_id,
                "--case",
                case_id,
                "--out",
                str(tmp_path / "o"),
            ]
        )
        == 0
    )
    assert f"Case: {case_id}\n" in capsys.readouterr().out
    voice_id = store.put(ObjectKind.PERSONA, VOICE)
    catalog = SimpleNamespace(voices={"v": SimpleNamespace(name="mine", persona_id=voice_id)})
    monkeypatch.setattr("dryheave.transcript_cli.read_catalog", lambda _: catalog)
    workspace = SimpleNamespace(path=lambda _: tmp_path)
    assert transcript_cli._voice(store, workspace, "mine") == voice_id


@pytest.mark.integration
def test_replay_cli_defaults_its_output_to_the_workspace_replays_folder(
    store, assessed, tmp_path, short_runtime, monkeypatch, capsys
):
    _, state = attempt(store, assessed.run_id)
    workspace = tmp_path / "ws"
    assert main(["init", str(workspace), "--runtime-root", str(short_runtime), "--json"]) == 0
    capsys.readouterr()
    monkeypatch.chdir(workspace)
    prefix = ["--store", str(store.root), "replay", assessed.run_id, "--attempt", state.attempt_id]
    assert main([*prefix, "--all-stand-in", "--json"]) == 0
    output = Path(json.loads(capsys.readouterr().out)["data"]["out"])
    assert output.parent == (workspace / "replays").resolve()
    assert len(output.name) == 12
    assert "replays/" in (workspace / ".gitignore").read_text().splitlines()


def test_replay_cli_without_a_workspace_defaults_to_the_current_directory(
    store, assessed, tmp_path, monkeypatch, capsys
):
    _, state = attempt(store, assessed.run_id)
    monkeypatch.chdir(tmp_path)
    prefix = ["--store", str(store.root), "replay", assessed.run_id, "--attempt", state.attempt_id]
    assert main([*prefix, "--all-stand-in", "--json"]) == 0
    output = Path(json.loads(capsys.readouterr().out)["data"]["out"])
    assert output.parent == tmp_path.resolve()
    assert output.name.startswith("replay-")
