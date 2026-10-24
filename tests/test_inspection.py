import json
import os
import sys
import threading
import time

import pytest

from dryheave.cases import TaskFact, load_frozen_case
from dryheave.controller_models import ControllerRecipe
from dryheave.controllers import CallContext
from dryheave.errors import InputError
from dryheave.inspection import InspectionRequest, inspect_workspace
from dryheave.judges import TranscriptEntry
from dryheave.models import CommandSpec
from dryheave.repositories import materialize_repository

FIXED = 'def greet(name):\n    return "Hello, " + name\n'
JUDGE = """
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
request = json.load(sys.stdin)
count = len(list(root.glob("request-*.json")))
(root / f"request-{count}.json").write_text(json.dumps(request))
responses = json.loads((root / "responses.json").read_text())
response = responses[min(count, len(responses) - 1)]
print(response if isinstance(response, str) else json.dumps(response))
"""
OBSERVABLE = TaskFact(
    fact_id="observable",
    phase="fix",
    text="Show a comma between Hello and the name.",
    check="Running the greeting shows a comma between Hello and the name.",
    curator_authored=True,
)
TONE = TaskFact(
    fact_id="tone",
    phase="fix",
    text="Keep the greeting friendly.",
    check="The greeting keeps its friendly tone without extra punctuation.",
    curator_authored=True,
)
VERDICTS = {"pass": "met", "fail": "unmet"}


def judgment(fact_id, outcome="pass", observation="Looks right when used.", evidence="greet.py:2"):
    return {
        "fact_id": fact_id,
        "verdict": VERDICTS.get(outcome, outcome),
        "observation": observation,
        "evidence": evidence,
    }


def with_verifier(case, **changes):
    punctuation = case.allowed_facts[0]
    command = punctuation.verifier.command.model_copy(update=changes)
    fact = punctuation.model_copy(
        update={"verifier": punctuation.verifier.model_copy(update={"command": command})}
    )
    return case.model_copy(update={"allowed_facts": (fact, *case.allowed_facts[1:])})


class Fixture:
    def __init__(self, store, graded_benchmark, tmp_path):
        self.store = store
        self.case_id = store.resolve(graded_benchmark.cases[0])
        frozen = load_frozen_case(store, self.case_id)
        self.case = frozen.model_copy(
            update={"allowed_facts": (*frozen.allowed_facts, OBSERVABLE, TONE)}
        )
        self.workspace = tmp_path / "runs" / "attempt" / "workspace"
        materialize_repository(store, self.case.repository_id, self.workspace)
        self.scratch = tmp_path / "private" / "scratch"
        self.retained = tmp_path / "private" / "retained"
        self.judge_root = tmp_path / "judge"
        self.judge_root.mkdir()
        script = tmp_path / "judge.py"
        script.write_text(JUDGE)
        self.recipe = ControllerRecipe(
            kind="json-command",
            isolation="trusted-native",
            command=CommandSpec(argv=(sys.executable, str(script), str(self.judge_root))),
        )
        self.deadlines = []
        self.identities = []

    def respond(self, *responses):
        (self.judge_root / "responses.json").write_text(json.dumps(list(responses)))

    def requests(self):
        return [
            json.loads(path.read_text()) for path in sorted(self.judge_root.glob("request-*.json"))
        ]

    def write(self, text):
        (self.workspace / "greet.py").write_text(text)

    def context(self, deadline, runtime_root):
        self.deadlines.append(deadline)
        index = len(self.deadlines) - 1
        return CallContext(
            call_id=f"inspect-{index}",
            index=index,
            deadline=deadline,
            cancelled=threading.Event(),
            inherited={"PATH": os.environ.get("PATH", os.defpath)},
            on_identities=self.identities.extend,
            runtime_root=runtime_root,
        )

    def request(self, *names, **changes):
        case = changes.get("case", self.case)
        facts = {fact.fact_id: fact for fact in case.allowed_facts}
        values = {
            "store": self.store,
            "case_id": self.case_id,
            "case": self.case,
            "facts": tuple(facts[name] for name in names),
            "inspection_id": "i-check",
            "turn": 2,
            "workspace": self.workspace,
            "transcript": (TranscriptEntry(role="user", text="Fix the greeting."),),
            "recipe": self.recipe,
            "scratch": self.scratch,
            "retained": self.retained,
            "call_context": self.context,
            "deadline": time.monotonic() + 60,
            "cancelled": threading.Event(),
            "on_identities": self.identities.extend,
        }
        return InspectionRequest(**(values | changes))


@pytest.fixture
def fixture(store, graded_benchmark, tmp_path):
    return Fixture(store, graded_benchmark, tmp_path)


def verdicts(result):
    return [(item.fact_id, item.verdict) for item in result.inspection.judgments]


def state(result):
    return result.inspection.status, result.inspection.error


@pytest.mark.integration
def test_judge_pass_sends_only_the_requested_facts_and_cleans_its_private_directory(fixture):
    fixture.write(FIXED)
    fixture.respond({"judgments": [judgment("observable")]})
    result = inspect_workspace(fixture.request("observable"))
    assert state(result) == ("completed", None)
    assert verdicts(result) == [("observable", "met")]
    assert (result.inspection.inspection_id, result.inspection.turn) == ("i-check", 2)
    assert [(call.role, call.status) for call in result.calls] == [("judge", "completed")]
    (sent,) = fixture.requests()
    assert sent["facts"] == [
        {"fact_id": "observable", "text": OBSERVABLE.text, "check": OBSERVABLE.check}
    ]
    assert sent["files"]["greet.py"] == FIXED
    assert sent["transcript"] == [{"role": "user", "text": "Fix the greeting."}]
    assert list(fixture.scratch.iterdir()) == []
    (kept,) = fixture.retained.iterdir()
    assert kept.name.startswith("inspection-")
    assert [path.name for path in (kept / "judge").iterdir()] == ["inspect-0"]
    assert result.inspection.snapshot_digest and result.inspection.input_digest
    assert result.seconds > 0


@pytest.mark.integration
def test_unmet_judgment_keeps_its_observation(fixture):
    fixture.respond(
        {
            "judgments": [
                judgment("observable", "fail", "The greeting still prints Hello Niels."),
                judgment("tone"),
            ]
        }
    )
    result = inspect_workspace(fixture.request("observable", "tone"))
    assert verdicts(result) == [("observable", "unmet"), ("tone", "met")]
    assert result.inspection.judgments[0].observation == "The greeting still prints Hello Niels."


@pytest.mark.integration
def test_malformed_judge_response_is_retried_once(fixture):
    fixture.respond("not json", {"judgments": [judgment("observable")]})
    result = inspect_workspace(fixture.request("observable"))
    assert verdicts(result) == [("observable", "met")]
    assert [(call.call_id, call.status) for call in result.calls] == [
        ("inspect-0", "failed"),
        ("inspect-1", "completed"),
    ]


@pytest.mark.integration
@pytest.mark.parametrize(
    "judgments",
    [
        [judgment("observable", "unclear")],
        [judgment("observable", "fail", evidence="")],
    ],
)
def test_unclear_or_unevidenced_judgment_is_judged_once_more_alone(fixture, judgments):
    fixture.respond({"judgments": [*judgments, judgment("tone")]}, {"judgments": judgments})
    result = inspect_workspace(fixture.request("observable", "tone"))
    assert state(result) == ("completed", None)
    assert verdicts(result) == [("observable", "unclear"), ("tone", "met")]
    assert [[item["fact_id"] for item in sent["facts"]] for sent in fixture.requests()] == [
        ["observable", "tone"],
        ["observable"],
    ]
    assert len(result.calls) == 2


@pytest.mark.integration
def test_a_second_look_settles_an_unclear_fact(fixture):
    fixture.respond(
        {"judgments": [judgment("observable", "unclear")]},
        {"judgments": [judgment("observable", "fail", "Hello and the name run together.")]},
    )
    result = inspect_workspace(fixture.request("observable"))
    assert verdicts(result) == [("observable", "unmet")]
    assert result.inspection.judgments[0].observation == "Hello and the name run together."


@pytest.mark.integration
def test_a_failed_second_look_leaves_the_fact_unclear(fixture):
    fixture.respond({"judgments": [judgment("observable", "unclear")]}, "not json")
    result = inspect_workspace(fixture.request("observable"))
    assert state(result) == ("completed", None)
    assert verdicts(result) == [("observable", "unclear")]
    assert [call.status for call in result.calls] == ["completed", "failed"]


@pytest.mark.integration
def test_missing_judgment_is_unavailable_after_one_retry(fixture):
    fixture.respond({"judgments": [judgment("observable")]})
    result = inspect_workspace(fixture.request("observable", "tone"))
    assert state(result) == ("unavailable", "judge_unavailable")
    assert result.inspection.judgments == ()
    assert [call.status for call in result.calls] == ["completed", "completed"]
    assert len(fixture.requests()) == 2


@pytest.mark.integration
def test_judged_fact_without_an_inspector_is_unavailable(fixture):
    result = inspect_workspace(fixture.request("observable", recipe=None))
    assert (*state(result), result.calls) == ("unavailable", "no_inspector", ())


@pytest.mark.integration
def test_deterministic_pass_runs_the_hidden_verifier_without_judge_calls(fixture):
    fixture.write(FIXED)
    result = inspect_workspace(fixture.request("punctuation"))
    assert (*state(result), result.calls) == ("completed", None, ())
    assert verdicts(result) == [("punctuation", "met")]
    assert result.inspection.judgments[0].evidence == "verifier hidden.py: exited, exit code 0"
    assert fixture.requests() == []
    (kept,) = fixture.retained.iterdir()
    execution = json.loads((kept / "facts/punctuation/result.json").read_text())
    assert execution["outcome"] == "pass"
    assert list(fixture.scratch.iterdir()) == []


@pytest.mark.integration
@pytest.mark.parametrize(
    "source, category, evidence",
    [
        ('def greet(name):\n    return "Hello " + name\n', "failed", "exited, exit code 1"),
        (
            'raise RuntimeError("greeting exploded")\n',
            "crashed",
            "exited, exit code 1, expected_execution_evidence_missing",
        ),
        (
            "def greet(name)\n    return name\n",
            "crashed",
            "exited, exit code 1, expected_execution_evidence_missing",
        ),
    ],
)
def test_subject_failures_and_crashes_are_unmet(fixture, source, category, evidence):
    fixture.write(source)
    result = inspect_workspace(fixture.request("punctuation"))
    assert state(result) == ("completed", None)
    (judged,) = result.inspection.judgments
    assert (judged.fact_id, judged.verdict) == ("punctuation", "unmet")
    assert judged.observation == f"Use a comma after Hello. ({category})"
    assert judged.evidence == f"verifier hidden.py: {evidence}"


@pytest.mark.integration
def test_subject_owned_verifier_executable_is_unclear_and_never_runs(fixture, tmp_path):
    canary = tmp_path / "executed"
    wrapper = fixture.workspace / "wrapper"
    wrapper.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(canary)!r}).write_text('ran')\n"
    )
    wrapper.chmod(0o700)
    case = with_verifier(fixture.case, argv=(str(wrapper), "{verifier}/hidden.py"))
    result = inspect_workspace(fixture.request("punctuation", case=case))
    assert state(result) == ("completed", None)
    (judged,) = result.inspection.judgments
    assert (judged.verdict, judged.observation, judged.evidence) == (
        "unclear",
        "Use a comma after Hello. (verifier gave no result)",
        "verifier hidden.py: not launched, InputError",
    )
    assert not canary.exists()
    assert list(fixture.scratch.iterdir()) == []


@pytest.mark.integration
def test_mixed_facts_report_verifier_and_judge_verdicts_in_fact_order(fixture):
    fixture.respond(
        {"judgments": [judgment("observable", "fail", "Hello and the name run together.")]}
    )
    result = inspect_workspace(fixture.request("observable", "punctuation"))
    assert verdicts(result) == [("observable", "unmet"), ("punctuation", "unmet")]
    assert len(result.calls) == 1


@pytest.mark.integration
def test_an_inspection_is_reused_only_on_an_identical_input_digest(fixture):
    from dryheave.judges import TranscriptEntry

    fixture.respond(
        {"judgments": [judgment("observable", "fail", "Hello and the name run together.")]}
    )
    first = inspect_workspace(fixture.request("observable"))
    reused = inspect_workspace(
        fixture.request("observable", previous=(first.inspection,), inspection_id="i-again")
    )
    assert reused.calls == ()
    assert reused.inspection == first.inspection.model_copy(update={"inspection_id": "i-again"})
    assert len(fixture.requests()) == 1

    moved_on = (*fixture.request().transcript, TranscriptEntry(role="user", text="Try again."))
    talked = inspect_workspace(
        fixture.request("observable", previous=(first.inspection,), transcript=moved_on)
    )
    assert len(talked.calls) == 1
    assert talked.inspection.snapshot_digest == first.inspection.snapshot_digest
    assert talked.inspection.input_digest != first.inspection.input_digest

    fixture.write(FIXED)
    fixture.respond({"judgments": [judgment("observable")]})
    changed = inspect_workspace(fixture.request("observable", previous=(first.inspection,)))
    assert verdicts(changed) == [("observable", "met")]
    assert changed.inspection.snapshot_digest != first.inspection.snapshot_digest
    assert len(changed.calls) == 1


@pytest.mark.integration
def test_incomplete_snapshot_is_retried_once_then_unavailable(fixture, monkeypatch):
    from dryheave import inspection

    captures = []
    real = inspection.capture_workspace

    def counted(*args):
        captures.append(args)
        return real(*args)

    monkeypatch.setattr(inspection, "capture_workspace", counted)
    tiny = fixture.case.model_copy(
        update={"capture_policy": fixture.case.capture_policy.model_copy(update={"max_files": 1})}
    )
    result = inspect_workspace(fixture.request("observable", case=tiny))
    assert (*state(result), result.calls) == ("unavailable", "snapshot_unavailable", ())
    assert len(captures) == 2
    assert fixture.requests() == []
    assert list(fixture.scratch.iterdir()) == []


@pytest.mark.integration
def test_scratch_inside_the_attempt_tree_is_refused(fixture):
    with pytest.raises(InputError, match="outside the attempt tree"):
        inspect_workspace(
            fixture.request("observable", scratch=fixture.workspace.parent / "inspection")
        )
    assert not (fixture.workspace.parent / "inspection").exists()


@pytest.mark.integration
def test_judge_deadline_is_capped_by_the_subject_deadline(fixture):
    fixture.respond({"judgments": [judgment("observable")]})
    deadline = time.monotonic() + 5
    assert state(inspect_workspace(fixture.request("observable", deadline=deadline))) == (
        "completed",
        None,
    )
    assert fixture.deadlines == [deadline]

    later = time.monotonic() + 3600
    inspect_workspace(fixture.request("observable", deadline=later))
    assert fixture.deadlines[1] < later - 3000


@pytest.mark.integration
def test_exhausted_deadline_is_unavailable_without_snapshot_or_calls(fixture):
    result = inspect_workspace(fixture.request("observable", deadline=time.monotonic()))
    assert (*state(result), result.inspection.snapshot_digest, result.calls) == (
        "unavailable",
        "deadline_exhausted",
        None,
        (),
    )


@pytest.mark.integration
def test_verifier_running_past_the_subject_deadline_is_unavailable(fixture):
    fixture.write("import time\ntime.sleep(30)\n" + FIXED)
    started = time.monotonic()
    result = inspect_workspace(fixture.request("punctuation", deadline=started + 3))
    assert state(result) == ("unavailable", "deadline_exhausted")
    assert time.monotonic() - started < 15
    assert list(fixture.scratch.iterdir()) == []


@pytest.mark.integration
def test_verifier_timeout_before_the_inspection_deadline_is_unmet(fixture):
    fixture.write("import time\ntime.sleep(5)\n" + FIXED)
    case = with_verifier(fixture.case, timeout_seconds=1)
    result = inspect_workspace(
        fixture.request("punctuation", case=case, deadline=time.monotonic() + 60)
    )
    assert state(result) == ("completed", None)
    (judged,) = result.inspection.judgments
    assert (judged.verdict, judged.observation) == ("unmet", "Use a comma after Hello. (timed out)")


@pytest.mark.integration
def test_a_verifier_timeout_drained_past_the_deadline_is_still_its_own_timeout(
    fixture, monkeypatch
):
    from dryheave import grading

    real = grading.run_command

    def slow_drain(*args, **options):
        result = real(*args, **options)
        time.sleep(max(0, deadline + 0.2 - time.monotonic()))
        return result

    monkeypatch.setattr(grading, "run_command", slow_drain)
    fixture.write("import time\ntime.sleep(5)\n" + FIXED)
    case = with_verifier(fixture.case, timeout_seconds=1)
    deadline = time.monotonic() + 2
    result = inspect_workspace(fixture.request("punctuation", case=case, deadline=deadline))
    (judged,) = result.inspection.judgments
    assert (judged.verdict, judged.observation) == ("unmet", "Use a comma after Hello. (timed out)")


@pytest.mark.integration
def test_verifier_killed_at_its_output_bound_is_unmet(fixture):
    fixture.write("print('x' * 100000)\n" + FIXED)
    case = with_verifier(fixture.case, max_output_bytes=1024)
    result = inspect_workspace(fixture.request("punctuation", case=case))
    assert state(result) == ("completed", None)
    (judged,) = result.inspection.judgments
    assert (judged.verdict, judged.observation) == (
        "unmet",
        "Use a comma after Hello. (output limit)",
    )


@pytest.mark.integration
def test_unconfirmed_verifier_cleanup_leaves_the_private_directory_in_place(fixture, monkeypatch):
    from dryheave import inspection

    real = inspection.check_snapshot

    def forced(*args):
        execution, evidence = real(*args)
        return (
            execution.model_copy(update={"outcome": "error", "error": "verifier_cleanup_failed"}),
            evidence,
        )

    monkeypatch.setattr(inspection, "check_snapshot", forced)
    fixture.write(FIXED)
    result = inspect_workspace(fixture.request("punctuation"))
    assert state(result) == ("unavailable", "verifier_cleanup_failed")
    (kept,) = fixture.scratch.iterdir()
    assert kept.name.startswith("inspection-")


@pytest.mark.integration
def test_unconfirmed_judge_cleanup_leaves_the_private_directory_in_place(fixture, monkeypatch):
    from dryheave import inspection

    real = inspection.invoke_fact_judge

    def forced(*args):
        call, judgments = real(*args)
        cleanup = call.cleanup.model_copy(update={"known_writers_stopped": False})
        return call.model_copy(update={"cleanup": cleanup}), judgments

    monkeypatch.setattr(inspection, "invoke_fact_judge", forced)
    fixture.respond({"judgments": [judgment("observable")]})
    result = inspect_workspace(fixture.request("observable"))
    assert state(result) == ("unavailable", "judge_cleanup_failed")
    (kept,) = fixture.scratch.iterdir()
    assert kept.name.startswith("inspection-")


@pytest.mark.integration
def test_cancellation_with_unconfirmed_writers_keeps_the_private_directory(fixture, monkeypatch):
    from dryheave import grading

    def forced_execute_check(criterion, workspace, hidden, artifacts, context, deadline=None):
        artifacts.record(
            "cleanup",
            {
                "terminal_closed": True,
                "known_writers_stopped": False,
                "coverage": "partial-native",
            },
        )
        raise KeyboardInterrupt

    monkeypatch.setattr(grading, "execute_check", forced_execute_check)
    fixture.write(FIXED)
    with pytest.raises(KeyboardInterrupt):
        inspect_workspace(fixture.request("punctuation"))
    (kept,) = fixture.scratch.iterdir()
    assert kept.name.startswith("inspection-")


@pytest.mark.integration
def test_cancellation_during_an_inspection_is_unavailable(fixture):
    cancelled = threading.Event()
    cancelled.set()
    result = inspect_workspace(fixture.request("observable", cancelled=cancelled))
    assert (*state(result), result.calls) == ("unavailable", "cancelled", ())
    assert list(fixture.scratch.iterdir()) == []


@pytest.mark.integration
def test_retained_inside_the_attempt_tree_is_refused(fixture):
    with pytest.raises(InputError, match="outside the attempt tree"):
        inspect_workspace(
            fixture.request("observable", retained=fixture.workspace.parent / "inspection")
        )
    assert not (fixture.workspace.parent / "inspection").exists()


@pytest.mark.integration
def test_unavailable_previous_result_is_not_reused(fixture):
    from dryheave.inspection import unavailable

    stale = unavailable(fixture.request("observable"), "no_inspector", None)
    fixture.respond({"judgments": [judgment("observable")]})
    result = inspect_workspace(fixture.request("observable", previous=(stale,)))
    assert verdicts(result) == [("observable", "met")]
    assert len(result.calls) == 1


def test_retained_evidence_copies_regular_files_and_skips_links_and_fifos(tmp_path):
    from dryheave.inspection import _retain

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("subject data")
    source = tmp_path / "evidence"
    (source / "nested").mkdir(parents=True)
    (source / "result.json").write_text("{}")
    (source / "nested" / "stdout.bin").write_bytes(b"out")
    (source / "file-link").symlink_to(outside / "secret.txt")
    (source / "dir-link").symlink_to(outside)
    os.mkfifo(source / "pipe")
    target = tmp_path / "retained" / "facts" / "punctuation"
    _retain(source, target)
    assert sorted(
        (str(path.relative_to(target)), path.is_symlink(), path.is_file())
        for path in target.rglob("*")
    ) == [
        ("nested", False, False),
        ("nested/stdout.bin", False, True),
        ("result.json", False, True),
    ]
