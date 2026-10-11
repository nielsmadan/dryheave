import json
import os
import sys
import threading
import time

import pytest

from dryheave.cases import InspectStep, RubricCriterion, load_frozen_case
from dryheave.controller_models import ControllerRecipe, InspectionFinding
from dryheave.controllers import CallContext
from dryheave.errors import InputError
from dryheave.inspection import (
    FINDINGS_CHARACTERS,
    InspectionRequest,
    fit_findings,
    inspect_workspace,
)
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
OBSERVABLE = RubricCriterion(
    criterion_id="observable",
    required=False,
    rubric="Running the greeting shows a comma between Hello and the name.",
)
TONE = RubricCriterion(
    criterion_id="tone",
    required=False,
    rubric="The greeting keeps its friendly tone without extra punctuation.",
)


def judgment(criterion_id, outcome="pass", rationale="Looks right when used."):
    return {"criterion_id": criterion_id, "outcome": outcome, "rationale": rationale}


class Fixture:
    def __init__(self, store, graded_benchmark, tmp_path):
        self.store = store
        self.case_id = store.resolve(graded_benchmark.cases[0])
        frozen = load_frozen_case(store, self.case_id)
        self.case = frozen.model_copy(update={"criteria": (*frozen.criteria, OBSERVABLE, TONE)})
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

    def request(self, *criteria, **changes):
        values = {
            "store": self.store,
            "case_id": self.case_id,
            "case": self.case,
            "step": InspectStep(step_id="check", criteria=criteria),
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


@pytest.mark.integration
def test_judge_pass_sends_only_the_step_rubric_and_cleans_its_private_directory(fixture):
    fixture.write(FIXED)
    fixture.respond({"judgments": [judgment("observable")]})
    result = inspect_workspace(fixture.request("observable"))
    assert (result.status, result.findings, result.error) == ("passed", (), None)
    assert [(call.role, call.status) for call in result.calls] == [("judge", "completed")]
    (sent,) = fixture.requests()
    assert [item["criterion_id"] for item in sent["criteria"]] == ["observable"]
    assert sent["checks"] == []
    assert sent["files"]["greet.py"] == FIXED
    assert sent["transcript"] == [{"role": "user", "text": "Fix the greeting."}]
    assert list(fixture.scratch.iterdir()) == []
    (kept,) = fixture.retained.iterdir()
    assert kept.name.startswith("inspection-")
    assert [path.name for path in (kept / "judge").iterdir()] == ["inspect-0"]
    assert result.snapshot_digest and result.seconds > 0


@pytest.mark.integration
def test_judge_failure_returns_its_rationale_as_a_finding(fixture):
    fixture.respond(
        {
            "judgments": [
                judgment("observable", "fail", "The greeting still prints Hello Niels."),
                judgment("tone"),
            ]
        }
    )
    result = inspect_workspace(fixture.request("observable", "tone"))
    assert result.status == "failed"
    assert result.findings == (
        InspectionFinding(
            criterion_id="observable", rationale="The greeting still prints Hello Niels."
        ),
    )


@pytest.mark.integration
def test_malformed_judge_response_is_retried_once(fixture):
    fixture.respond("not json", {"judgments": [judgment("observable")]})
    result = inspect_workspace(fixture.request("observable"))
    assert result.status == "passed"
    assert [(call.call_id, call.status) for call in result.calls] == [
        ("inspect-0", "failed"),
        ("inspect-1", "completed"),
    ]


@pytest.mark.integration
@pytest.mark.parametrize(
    "response",
    [
        {"judgments": [judgment("observable")]},
        {"judgments": [judgment("observable"), judgment("tone", "error")]},
    ],
)
def test_missing_or_errored_judgment_is_unavailable_after_one_retry(fixture, response):
    fixture.respond(response)
    result = inspect_workspace(fixture.request("observable", "tone"))
    assert (result.status, result.error, result.findings) == (
        "unavailable",
        "judge_unavailable",
        (),
    )
    assert [call.status for call in result.calls] == ["completed", "completed"]
    assert len(fixture.requests()) == 2


@pytest.mark.integration
def test_rubric_step_without_an_inspector_is_unavailable(fixture):
    result = inspect_workspace(fixture.request("observable", recipe=None))
    assert (result.status, result.error, result.calls) == ("unavailable", "no_inspector", ())


@pytest.mark.integration
def test_deterministic_pass_runs_the_hidden_verifier_without_judge_calls(fixture):
    fixture.write(FIXED)
    result = inspect_workspace(fixture.request("greeting"))
    assert (result.status, result.calls, result.error) == ("passed", (), None)
    assert fixture.requests() == []
    (kept,) = fixture.retained.iterdir()
    execution = json.loads((kept / "criteria/greeting/result.json").read_text())
    assert execution["outcome"] == "pass"
    assert list(fixture.scratch.iterdir()) == []


@pytest.mark.integration
@pytest.mark.parametrize(
    "source, category",
    [
        ('def greet(name):\n    return "Hello " + name\n', "failed"),
        ('raise RuntimeError("greeting exploded")\n', "crashed"),
    ],
)
def test_subject_failures_and_crashes_are_findings(fixture, source, category):
    fixture.write(source)
    result = inspect_workspace(fixture.request("greeting"))
    assert (result.status, result.error) == ("failed", None)
    (finding,) = result.findings
    assert finding.criterion_id == "greeting"
    assert finding.rationale == f"Validate punctuation and correct name handling. ({category})"


@pytest.mark.integration
def test_subject_owned_verifier_executable_is_unavailable_and_never_runs(fixture, tmp_path):
    canary = tmp_path / "executed"
    wrapper = fixture.workspace / "wrapper"
    wrapper.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(canary)!r}).write_text('ran')\n"
    )
    wrapper.chmod(0o700)
    greeting = fixture.case.criteria[0]
    hijacked = greeting.model_copy(
        update={
            "command": greeting.command.model_copy(
                update={"argv": (str(wrapper), "{verifier}/hidden.py")}
            )
        }
    )
    case = fixture.case.model_copy(update={"criteria": (hijacked, *fixture.case.criteria[1:])})
    result = inspect_workspace(fixture.request("greeting", case=case))
    assert (result.status, result.error, result.findings) == ("unavailable", "InputError", ())
    assert not canary.exists()
    assert list(fixture.scratch.iterdir()) == []


@pytest.mark.integration
def test_mixed_step_reports_verifier_and_judge_findings_in_step_order(fixture):
    fixture.respond(
        {"judgments": [judgment("observable", "fail", "Hello and the name run together.")]}
    )
    result = inspect_workspace(fixture.request("greeting", "observable"))
    assert result.status == "failed"
    assert [item.criterion_id for item in result.findings] == ["greeting", "observable"]
    assert len(result.calls) == 1


@pytest.mark.integration
def test_unchanged_snapshot_reuses_the_previous_verdict_without_a_call(fixture):
    fixture.respond(
        {"judgments": [judgment("observable", "fail", "Hello and the name run together.")]}
    )
    first = inspect_workspace(fixture.request("observable"))
    reused = inspect_workspace(fixture.request("observable", previous=first))
    assert (reused.status, reused.findings, reused.calls) == ("failed", first.findings, ())
    assert reused.snapshot_digest == first.snapshot_digest
    assert len(fixture.requests()) == 1

    fixture.write(FIXED)
    fixture.respond({"judgments": [judgment("observable")]})
    changed = inspect_workspace(fixture.request("observable", previous=first))
    assert changed.status == "passed"
    assert changed.snapshot_digest != first.snapshot_digest
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
    assert (result.status, result.error, result.calls) == (
        "unavailable",
        "snapshot_unavailable",
        (),
    )
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
    assert inspect_workspace(fixture.request("observable", deadline=deadline)).status == "passed"
    assert fixture.deadlines == [deadline]

    later = time.monotonic() + 3600
    inspect_workspace(fixture.request("observable", deadline=later))
    assert fixture.deadlines[1] < later - 3000


@pytest.mark.integration
def test_exhausted_deadline_is_unavailable_without_snapshot_or_calls(fixture):
    result = inspect_workspace(fixture.request("observable", deadline=time.monotonic()))
    assert (result.status, result.error, result.snapshot_digest, result.calls) == (
        "unavailable",
        "deadline_exhausted",
        None,
        (),
    )


@pytest.mark.integration
def test_verifier_running_past_the_subject_deadline_is_unavailable(fixture):
    fixture.write("import time\ntime.sleep(30)\n" + FIXED)
    started = time.monotonic()
    result = inspect_workspace(fixture.request("greeting", deadline=started + 3))
    assert (result.status, result.error) == ("unavailable", "deadline_exhausted")
    assert time.monotonic() - started < 15
    assert list(fixture.scratch.iterdir()) == []


@pytest.mark.integration
def test_verifier_timeout_before_the_subject_deadline_is_a_subject_finding(fixture):
    fixture.write("import time\ntime.sleep(5)\n" + FIXED)
    greeting = fixture.case.criteria[0]
    quick = greeting.model_copy(
        update={"command": greeting.command.model_copy(update={"timeout_seconds": 1})}
    )
    case = fixture.case.model_copy(update={"criteria": (quick, *fixture.case.criteria[1:])})
    result = inspect_workspace(
        fixture.request("greeting", case=case, deadline=time.monotonic() + 60)
    )
    assert (result.status, result.error) == ("failed", None)
    (finding,) = result.findings
    assert finding.criterion_id == "greeting"
    assert finding.rationale == "Validate punctuation and correct name handling. (timed out)"


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
    result = inspect_workspace(fixture.request("greeting"))
    assert (result.status, result.error) == ("unavailable", "verifier_cleanup_failed")
    (kept,) = fixture.scratch.iterdir()
    assert kept.name.startswith("inspection-")


@pytest.mark.integration
def test_unconfirmed_judge_cleanup_leaves_the_private_directory_in_place(fixture, monkeypatch):
    from dryheave import inspection

    real = inspection.invoke_judge

    def forced(*args):
        call, criteria, answers, judgments = real(*args)
        cleanup = call.cleanup.model_copy(update={"known_writers_stopped": False})
        return call.model_copy(update={"cleanup": cleanup}), criteria, answers, judgments

    monkeypatch.setattr(inspection, "invoke_judge", forced)
    fixture.respond({"judgments": [judgment("observable")]})
    result = inspect_workspace(fixture.request("observable"))
    assert (result.status, result.error) == ("unavailable", "judge_cleanup_failed")
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
        inspect_workspace(fixture.request("greeting"))
    (kept,) = fixture.scratch.iterdir()
    assert kept.name.startswith("inspection-")


@pytest.mark.integration
def test_cancellation_during_an_inspection_is_unavailable(fixture):
    cancelled = threading.Event()
    cancelled.set()
    result = inspect_workspace(fixture.request("observable", cancelled=cancelled))
    assert (result.status, result.error, result.calls) == ("unavailable", "cancelled", ())
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
    fixture.respond(
        {"judgments": [judgment("observable", "fail", "Hello and the name run together.")]}
    )
    first = inspect_workspace(fixture.request("observable"))
    stale = first.model_copy(update={"status": "unavailable", "error": "no_inspector"})

    fixture.respond({"judgments": [judgment("observable")]})
    result = inspect_workspace(fixture.request("observable", previous=stale))
    assert result.status == "passed"
    assert len(result.calls) == 1
    assert result.snapshot_digest == first.snapshot_digest


def test_findings_are_fitted_to_a_shared_character_bound():
    long = InspectionFinding(criterion_id="observable", rationale="x" * 8000)
    short = InspectionFinding(criterion_id="tone", rationale="Too terse.")
    fitted = fit_findings((long, short))
    assert len(fitted[0].rationale) == FINDINGS_CHARACTERS // 2
    assert fitted[0].rationale.endswith("…")
    assert fitted[1] == short
    assert sum(len(item.rationale) for item in fitted) <= FINDINGS_CHARACTERS


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
    target = tmp_path / "retained" / "criteria" / "greeting"
    _retain(source, target)
    assert sorted(
        (str(path.relative_to(target)), path.is_symlink(), path.is_file())
        for path in target.rglob("*")
    ) == [
        ("nested", False, False),
        ("nested/stdout.bin", False, True),
        ("result.json", False, True),
    ]
