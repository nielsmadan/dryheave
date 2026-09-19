import json
from pathlib import Path

import pytest

from dryheave.drivers.claude import ClaudeAdapter


def human(uuid="human", **changes):
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": "root",
        "cwd": "/fixture",
        "message": {"role": "user", "content": "Hello"},
        **changes,
    }


def assistant(stop_reason="end_turn", **changes):
    return {
        "type": "assistant",
        "uuid": "assistant",
        "parentUuid": "human",
        "sessionId": "root",
        "message": {
            "id": "response",
            "role": "assistant",
            "content": [{"type": "text", "text": "Done"}],
            "stop_reason": stop_reason,
        },
        **changes,
    }


def test_actual_shape_usage_and_tool_results_are_separate_from_human_acceptance():
    adapter = ClaudeAdapter()
    records = [
        json.loads(line)
        for line in Path("tests/fixtures/claude-recorded.jsonl").read_text().splitlines()
    ]
    events = [event for record in records for event in adapter.feed(record)]
    accepted = [event for event in events if event.kind == "accepted"]
    assert [event.text for event in accepted] == [
        "Add a greeting command.",
        "Use a comma after Hello.",
        "Change the command name to greet.",
    ]
    assert accepted[0].native_id == accepted[0].turn_id
    assert any(event.kind == "tool" and event.data.get("type") == "tool_result" for event in events)
    usage = [event for event in events if event.kind == "usage"]
    assert len({event.data["response_id"] for event in usage}) < len(usage)
    response = next(event for event in events if event.kind == "assistant")
    assert response.data["effort"] == "xhigh"


def test_completion_needs_linked_end_turn_and_lifecycle():
    adapter = ClaudeAdapter()
    adapter.feed(human())
    events = adapter.feed(assistant())
    assert [event.kind for event in events] == ["assistant"]
    completed = adapter.feed({"type": "system", "subtype": "turn_duration", "sessionId": "root"})
    assert completed[0].kind == "completed"
    assert completed[0].turn_id == "human"


def test_recorded_tool_only_response_emits_observed_metadata():
    adapter = ClaudeAdapter()
    records = [
        json.loads(line)
        for line in Path("tests/fixtures/claude-recorded.jsonl").read_text().splitlines()
    ]
    adapter.feed(records[0])
    adapter.feed(records[1])
    events = adapter.feed(records[6])
    assert [event.kind for event in events] == ["metadata", "tool", "usage"]
    assert events[0].data == {"model": "fixture-model", "effort": "xhigh", "isSidechain": False}
    assert events[0].session_id == "fixture-id-3"
    assert events[0].native_id == "fixture-id-24"
    assert events[1].data["type"] == "tool_use"
    assert events[2].data["response_id"] == "fixture-id-21"


def test_sidechain_tool_stop_and_hook_continuation_cannot_complete_human_turn():
    adapter = ClaudeAdapter()
    assert all(event.kind != "accepted" for event in adapter.feed(human(isSidechain=True)))
    adapter.feed(human())
    adapter.feed(assistant("tool_use"))
    assert adapter.feed({"type": "system", "subtype": "turn_duration", "sessionId": "root"}) == ()
    adapter.feed(assistant())
    hook = adapter.feed(
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "sessionId": "root",
            "preventedContinuation": True,
        }
    )[0]
    assert hook.data["preventedContinuation"] is True
    assert adapter.feed({"type": "system", "subtype": "turn_duration", "sessionId": "root"}) == ()
    adapter.feed(assistant(parentUuid="unrelated"))
    assert adapter.feed({"type": "system", "subtype": "turn_duration", "sessionId": "root"}) == ()


@pytest.mark.parametrize("partial,hook", [(False, False), (True, False), (False, True)])
def test_synthetic_278_composer_paste_and_linked_lifecycle(tmp_path, driver_plan, partial, hook):
    from dryheave.drivers.artifacts import ArtifactWriter
    from dryheave.drivers.fake import FakeEventSource, FakeTerminal
    from dryheave.drivers.models import DriverLimits, Screen
    from dryheave.drivers.session import DriverSession
    from dryheave.models import AgentKind

    class SyntheticClaudeTerminal(FakeTerminal):
        def paste(self, text):
            self.pastes.append(text)
            self.current = Screen(
                text="\u276f " + (text[:1] if partial else text.replace("\n", "\n  ")), exited=None
            )

        def enter(self):
            self.enters += 1
            self.current = Screen(text="Working\n\u276f", exited=None)

    prompt = "Fix greeting.\nAsk which punctuation first."
    adapter = ClaudeAdapter()
    accepted = human(
        version="2.1.278", cwd=driver_plan.cwd, message={"role": "user", "content": prompt}
    )
    response = assistant(
        message={
            "id": "synthetic-response",
            "role": "assistant",
            "model": "claude-haiku-4-5-20251001",
            "content": [{"type": "text", "text": "Which punctuation?"}],
            "stop_reason": "end_turn",
        }
    )
    records = [accepted, response]
    if hook:
        records.append({"type": "system", "subtype": "hook_started", "sessionId": "root"})
    records.append({"type": "system", "subtype": "turn_duration", "sessionId": "root"})
    events = [event for record in records for event in adapter.feed(record)]
    source = FakeEventSource([])
    terminal = SyntheticClaudeTerminal([Screen(text="\u276f", exited=None)])
    plan = driver_plan.model_copy(update={"agent": AgentKind.CLAUDE})
    session = DriverSession(
        terminal,
        source,
        ArtifactWriter(tmp_path / "evidence", 1000000),
        agent=AgentKind.CLAUDE,
        limits=DriverLimits(duration_seconds=2, max_polls=10, poll_seconds=0.01),
    )
    with session:
        assert session.start(plan, {}).state == "ready"
        intent = session.prepare(prompt)
        source.batches.extend([[], events])
        result = session.deliver(intent)
        assert result.state == ("partial_input" if partial else "timeout" if hook else "question")
        assert terminal.pastes == [prompt]
        assert terminal.enters == (0 if partial else 1)
        if not partial:
            assert session.launch.observed_model == "claude-haiku-4-5-20251001"
            assert session.launch.observed_effort is None
    assert terminal.closed
