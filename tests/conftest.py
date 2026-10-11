import shutil
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from dryheave.experiment_models import ExperimentDraft
from dryheave.models import AgentKind, StrictModel
from dryheave.profile_models import AssetSelection, CaptureLimits, CaptureSpec, NativeRecipe
from dryheave.profiles import capture_profile
from dryheave.runner_models import RunSummary
from dryheave.storage import ObjectStore


class ExamplePayload(StrictModel):
    title: str
    count: int = 1


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def payload() -> ExamplePayload:
    return ExamplePayload(title="Historical task")


def profile_recipe(**changes) -> NativeRecipe:
    return NativeRecipe(
        agent=AgentKind.CODEX, executable=sys.executable, version="0.153.4", **changes
    )


def profile_selection(
    path: str,
    *,
    target: str | None = None,
    kind="instruction",
    layer="global",
    overlay="preserve",
    target_root=None,
) -> AssetSelection:
    return AssetSelection(
        root="selected",
        path=path,
        kind=kind,
        layer=layer,
        target_root=target_root or ("project" if layer == "project" else "config"),
        target=target or path,
        overlay=overlay,
    )


def capture_profile_fixture(
    store, source: Path, *assets: AssetSelection, native=None, limits=None
) -> str:
    return capture_profile(
        store,
        CaptureSpec(
            recipe=native or profile_recipe(),
            include_roots={"selected": str(source)},
            assets=assets,
            limits=limits or CaptureLimits(),
        ),
    )


@pytest.fixture
def driver_plan(tmp_path: Path):
    from dryheave.profile_models import ExecutableResolution, LaunchPlan

    config = tmp_path / "config"
    config.mkdir()
    return LaunchPlan(
        profile_id="a" * 64,
        agent=AgentKind.CODEX,
        argv=(sys.executable, "-u", "fixture.py"),
        cwd=str(tmp_path),
        executable=ExecutableResolution(
            requested=sys.executable, resolved=sys.executable, expected_version=None
        ),
        config_roots={"config": str(config)},
        generated_environment={"CODEX_HOME": str(config)},
        environment_references=(),
        runtime_files=(),
        generated_files={},
        overlays=(),
        discovery_roots=(),
        issues=(),
        fidelity="captured",
        launchable=True,
    )


def survey_repo(root: Path, name: str = "source"):
    from dryheave.repositories import Git, SnapshotLimits

    repo = root / name
    repo.mkdir()
    git = Git(repo, SnapshotLimits())
    git.environment.update(
        {
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
    )
    git.run("init", "--quiet", "--template=", "--initial-branch=main", ".")
    return git


def survey_commit(git, subject: str, files: dict[str, str], body: str = "") -> str:
    for name, content in files.items():
        target = git.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git.run("add", "-A")
    git.run("commit", "--quiet", "-m", f"{subject}\n\n{body}" if body else subject)
    return git.run("rev-parse", "HEAD").stdout.decode().strip()


def write_baseline_log(
    path: Path, baseline: str | None, cwd: str, session_id: str, parent: str | None = None
) -> None:
    import json

    payload: dict[str, object] = {"id": session_id, "cwd": cwd, "cli_version": "0.153.4"}
    if baseline is not None:
        payload["git"] = {"commit_hash": baseline, "branch": "main"}
    if parent is not None:
        payload["parent_thread_id"] = parent
    path.write_text(
        json.dumps({"type": "session_meta", "payload": payload})
        + "\n"
        + json.dumps(
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Fix the parser."}}
        )
        + "\n"
    )


def build_historical_repo(root: Path) -> tuple[Path, str, str, str]:
    from dryheave.repositories import Git, SnapshotLimits

    repo = root / "source"
    repo.mkdir()
    git = Git(repo, SnapshotLimits())
    git.environment.update(
        {
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
    )
    git.run("init", "--quiet", "--template=", "--initial-branch=main", ".")
    (repo / "greet.py").write_text('def greet(name):\n    return "Hello " + name\n')
    git.run("add", "greet.py")
    git.run("commit", "--quiet", "-m", "initial")
    (repo / "history.txt").write_text("Historical context\n")
    (repo / ".gitattributes").write_text(
        "history.txt export-ignore\nsubst.txt export-subst\ngreet.py filter=hostile text\n"
    )
    (repo / "subst.txt").write_bytes(b"$Format:%H$\r\n")
    git.run("add", ".")
    git.run("commit", "--quiet", "-m", "baseline")
    baseline = git.run("rev-parse", "HEAD").stdout.decode().strip()
    (repo / "greet.py").write_text(
        'def greet(name):\n    return "Secret future solution, " + name\n'
    )
    git.run("add", "greet.py")
    git.run("commit", "--quiet", "-m", "secret future solution")
    future = git.run("rev-parse", "HEAD").stdout.decode().strip()
    tree = git.run("rev-parse", "HEAD^{tree}").stdout.decode().strip()
    unreachable = (
        git.run("commit-tree", tree, "-p", baseline, input_bytes=b"Unreachable solution\n")
        .stdout.decode()
        .strip()
    )
    return repo, baseline, future, unreachable


def build_benchmark(store, historical_repo, root: Path) -> ExperimentDraft:
    from dryheave.cases import DeterministicCriterion, TaskFact, draft_case, freeze_case
    from dryheave.controller_models import ControllerRecipe, ScriptStep, SimulatorDecision
    from dryheave.experiment_models import FixtureTurn, VariantSpec
    from dryheave.logs.service import import_session
    from dryheave.models import CommandSpec
    from dryheave.personas import Persona
    from dryheave.repositories import capture_repository

    source, baseline, _, _ = historical_repo
    repository = capture_repository(store, source, baseline)
    session = import_session(store, FIXTURES / "codex-recorded.jsonl", AgentKind.CODEX)
    draft = draft_case(store, session, repository_id=repository)
    verifier = root / "hidden.py"
    verifier.write_text('print("PRIVATE_VERIFIER_SENTINEL")\n')
    case = freeze_case(
        store,
        draft.model_copy(
            update={
                "initial_prompt": "Fix the greeting; ask me which punctuation.",
                "intent_confirmed": True,
                "facts_reviewed": True,
                "unresolved_issues": (),
                "persona": Persona(
                    name="User",
                    instructions="Give concise factual replies.",
                    disclosure_policy="Disclose approved facts on request.",
                    unknown_answer_policy="Say when a fact is unknown.",
                    reviewed_subject_safe=True,
                ),
                "allowed_facts": (
                    TaskFact(
                        fact_id="punctuation",
                        text="Use a comma after Hello.",
                        curator_authored=True,
                    ),
                ),
                "hidden_files": {"hidden.py": str(verifier)},
                "criteria": (
                    DeterministicCriterion(
                        criterion_id="greeting",
                        description="Validate punctuation and correct name handling.",
                        command=CommandSpec(argv=(sys.executable, "{verifier}/hidden.py")),
                        entrypoint="hidden.py",
                        expected_stdout="PRIVATE_VERIFIER_SENTINEL",
                    ),
                ),
            }
        ),
        root,
    )
    profile = capture_profile(store, CaptureSpec(recipe=profile_recipe()))
    store.set_alias("task", case)
    store.set_alias("subject", profile)
    return ExperimentDraft(
        name="Greeting",
        cases=("task",),
        variants=(VariantSpec(name="base", profile="subject"),),
        simulator=ControllerRecipe(
            script=(
                ScriptStep(
                    assistant="Which punctuation?",
                    decision=SimulatorDecision(
                        action="reply",
                        text="Use a comma after Hello.",
                        fact_ids=("punctuation",),
                        reason="The approved punctuation fact answers the question.",
                    ),
                ),
                ScriptStep(
                    assistant="Done.",
                    decision=SimulatorDecision(
                        action="stop", reason="The subject reported its implementation."
                    ),
                ),
            )
        ),
        fixture=(
            FixtureTurn(
                prompt="Fix the greeting; ask me which punctuation.", assistant="Which punctuation?"
            ),
            FixtureTurn(
                prompt="Use a comma after Hello.",
                assistant="Done.",
                files={
                    "greet.py": 'def greet(name):\n    return "Hello, " + name\n',
                    "added.txt": "new task file\n",
                },
            ),
        ),
    )


def build_graded_benchmark(store, benchmark) -> ExperimentDraft:
    from dryheave.cases import load_frozen_case
    from dryheave.models import ObjectKind

    case_id = store.resolve(benchmark.cases[0])
    case = load_frozen_case(store, case_id)
    files = store.read_blobs(case_id)
    files["verifiers/hidden.py"] = (
        b"import runpy\n"
        b'greet = runpy.run_path("greet.py")["greet"]\n'
        b"try:\n"
        b'    assert greet("Niels") == "Hello, Niels"\n'
        b'    assert greet("") == "Hello, "\n'
        b"except AssertionError:\n"
        b'    print("GREETING_ASSERTION_FAILED", flush=True)\n'
        b"    raise SystemExit(1)\n"
        b'print("PRIVATE_VERIFIER_SENTINEL", flush=True)\n'
    )
    files["reference.patch"] = (
        b"diff --git a/greet.py b/greet.py\n"
        b"--- a/greet.py\n"
        b"+++ b/greet.py\n"
        b"@@ -1,2 +1,2 @@\n"
        b" def greet(name):\n"
        b'-    return "Hello " + name\n'
        b'+    return "Hello, " + name\n'
    )
    changed = store.put(
        ObjectKind.CASE,
        case.model_copy(
            update={
                "reference_patch_file": "reference.patch",
                "criteria": tuple(
                    item.model_copy(update={"expected_failure_stdout": "GREETING_ASSERTION_FAILED"})
                    for item in case.criteria
                ),
            }
        ),
        files=files,
        references=(case.persona_id, case.repository_id),
    )
    return benchmark.model_copy(update={"cases": (changed,)})


HOSTILE = (
    "<script>alert(1)</script><img src=x onerror=alert(1)> javascript:alert(1) </script><!-- \"&'`"
)


def build_assessed(store, graded_benchmark) -> RunSummary:
    from dryheave.assessments import assess_run
    from dryheave.experiments import create_experiment
    from dryheave.runner import run_experiment
    from dryheave.runner_models import RunOptions

    summary = run_experiment(
        store,
        create_experiment(store, graded_benchmark),
        options=RunOptions(mode="offline-fixture"),
    )
    assess_run(store, summary.run_id)
    return summary


def build_hostile_run(store, graded_benchmark) -> tuple[str, str]:
    from dryheave.assessments import assess_run
    from dryheave.cases import load_frozen_case
    from dryheave.experiments import create_experiment
    from dryheave.models import ObjectKind
    from dryheave.runner import run_experiment
    from dryheave.runner_models import RunOptions

    case_id = store.resolve(graded_benchmark.cases[0])
    case = load_frozen_case(store, case_id)
    files = store.read_blobs(case_id)
    files["verifiers/hidden.py"] = (
        b"import runpy\nimport sys\n"
        b'greet = runpy.run_path("greet.py")["greet"]\n'
        b'assert greet("Niels") == "Hello, Niels"\n'
        + f"sys.stdout.buffer.write({HOSTILE!r}.encode())\n".encode()
        + b'sys.stdout.buffer.write(b"\\xed\\xa0\\x80")\n'
        b'sys.stdout.buffer.write(b"PRIVATE_VERIFIER_SENTINEL\\n")\n'
    )
    changed = store.put(
        ObjectKind.CASE,
        case.model_copy(update={"initial_prompt": HOSTILE}),
        files=files,
        references=(case.persona_id, case.repository_id),
    )
    script = graded_benchmark.simulator.script
    draft = graded_benchmark.model_copy(
        update={
            "cases": (changed,),
            "simulator": graded_benchmark.simulator.model_copy(
                update={"script": (script[0].model_copy(update={"assistant": HOSTILE}), script[1])}
            ),
            "fixture": (
                graded_benchmark.fixture[0].model_copy(
                    update={"prompt": HOSTILE, "assistant": HOSTILE}
                ),
                graded_benchmark.fixture[1].model_copy(
                    update={
                        "files": {**graded_benchmark.fixture[1].files, "added.txt": HOSTILE + "\n"}
                    }
                ),
            ),
        }
    )
    summary = run_experiment(
        store, create_experiment(store, draft), options=RunOptions(mode="offline-fixture")
    )
    assess_run(store, summary.run_id)
    return summary.run_id, summary.attempts[0].attempt_id


def build_calibrated(store, graded_benchmark) -> str:
    from dryheave.calibrations import calibrate_case

    return calibrate_case(store, graded_benchmark.cases[0])


def judged_case_benchmark(store, graded_benchmark, tmp_path):
    import json as jsonlib

    from dryheave.cases import (
        CaseCheck,
        CheckCondition,
        MetricsCommand,
        RubricCriterion,
        load_frozen_case,
    )
    from dryheave.controller_models import ControllerRecipe
    from dryheave.experiment_models import ScoringConfig
    from dryheave.models import CommandSpec, ObjectKind

    case_id = graded_benchmark.cases[0]
    case = load_frozen_case(store, case_id)
    measure = (
        b"import json, os\n"
        b"from pathlib import Path\n"
        b'Path(os.environ["DRYHEAVE_METRICS_OUTPUT"]).write_text(json.dumps(\n'
        b'    {"metrics": [{"metric_id": "changed_lines", "label": "Changed lines", "value": 1}]}\n'
        b"))\n"
    )
    files = store.read_blobs(case_id) | {"verifiers/measure.py": measure}
    case = case.model_copy(
        update={
            "criteria": (
                *case.criteria,
                RubricCriterion(
                    criterion_id="quality", rubric="The greeting keeps its public interface."
                ),
            ),
            "checks": (
                CaseCheck(
                    check_id="asked",
                    question="Did the agent ask which punctuation to use?",
                    expect="yes",
                ),
                CaseCheck(
                    check_id="confirmed",
                    question="Did the agent confirm the punctuation before finishing?",
                    expect="yes",
                    only_if=CheckCondition(check_id="asked", answer="yes"),
                ),
            ),
            "metrics": (
                MetricsCommand(
                    metrics_id="greeting-metrics",
                    description="Measure how the final greeting differs from the baseline.",
                    command=CommandSpec(argv=(sys.executable, "{verifier}/measure.py")),
                    entrypoint="measure.py",
                ),
            ),
            "hidden_files": {**case.hidden_files, "measure.py": "verifiers/measure.py"},
            "disabled_rubrics": ("test-quality",),
        }
    )
    changed_id = store.put(
        ObjectKind.CASE, case, files=files, references=(case.persona_id, case.repository_id)
    )
    script = tmp_path / "judge.py"
    script.write_text(
        "import pathlib,sys\n"
        f"root = pathlib.Path({str(tmp_path)!r})\n"
        "sys.stdin.buffer.read()\n"
        'print((root / "response.json").read_text())\n'
    )
    (tmp_path / "response.json").write_text(
        jsonlib.dumps(
            {
                "judgments": [
                    {
                        "criterion_id": "quality",
                        "outcome": "pass",
                        "rationale": "Judged quality.",
                    },
                    {
                        "criterion_id": "code-quality",
                        "outcome": "pass",
                        "rationale": "Judged code quality.",
                    },
                    {
                        "criterion_id": "codebase-consistency",
                        "outcome": "pass",
                        "rationale": "Judged consistency.",
                    },
                ],
                "checks": [
                    {
                        "check_id": "asked",
                        "answer": "yes",
                        "rationale": "Asked about punctuation.",
                    },
                    {
                        "check_id": "confirmed",
                        "answer": "yes",
                        "rationale": "Confirmed before finishing.",
                    },
                ],
            }
        )
    )
    scoring = ScoringConfig(
        judge=ControllerRecipe(
            kind="json-command",
            isolation="trusted-native",
            command=CommandSpec(argv=(sys.executable, str(script))),
        )
    )
    benchmark = graded_benchmark.model_copy(update={"cases": (changed_id,), "scoring": scoring})
    return benchmark, changed_id


INSPECTOR = """
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
request = json.load(sys.stdin)
count = len(list(root.glob("request-*.json")))
(root / f"request-{count}.json").write_text(json.dumps(request))
outcomes = json.loads((root / "outcomes.json").read_text())
outcome = outcomes[min(count, len(outcomes) - 1)]
if outcome == "broken":
    print("not json")
    raise SystemExit(0)
rationale = "The greeting reads oddly when used." if outcome == "fail" else "Reads well."
print(json.dumps({
    "judgments": [
        {"criterion_id": item["criterion_id"], "outcome": outcome, "rationale": rationale}
        for item in request["criteria"]
    ],
    "usage": {"output": 7, "provenance": "fixture"},
}))
"""


def workflow_benchmark(store, graded_benchmark, tmp_path, *, outcomes=("pass",), workflow=None):
    import json as jsonlib

    from dryheave.cases import load_frozen_case
    from dryheave.controller_models import ControllerRecipe
    from dryheave.models import CommandSpec, ObjectKind

    benchmark, case_id = judged_case_benchmark(store, graded_benchmark, tmp_path)
    if workflow is not None:
        case = load_frozen_case(store, case_id)
        case_id = store.put(
            ObjectKind.CASE,
            case.model_copy(update={"workflow": workflow}),
            files=store.read_blobs(case_id),
            references=(case.persona_id, case.repository_id),
        )
    root = tmp_path / "inspector"
    root.mkdir()
    (root / "outcomes.json").write_text(jsonlib.dumps(list(outcomes)))
    script = tmp_path / "inspector.py"
    script.write_text(INSPECTOR)
    inspector = ControllerRecipe(
        kind="json-command",
        isolation="trusted-native",
        command=CommandSpec(argv=(sys.executable, str(script), str(root))),
    )
    return benchmark.model_copy(
        update={
            "cases": (case_id,),
            "scoring": benchmark.scoring.model_copy(update={"inspector": inspector}),
        }
    ), root


DIALOG_RULE = "\u2500" * 60


@dataclass(frozen=True)
class AskedQuestion:
    header: str
    question: str
    options: tuple[tuple[str, str], ...]
    multi: bool = False


class ClaudeQuestionUI:
    def __init__(
        self,
        questions: tuple[AskedQuestion, ...],
        *,
        numbered: bool = True,
        wrap_width: int | None = None,
    ) -> None:
        self.questions, self.numbered, self.wrap_width = questions, numbered, wrap_width
        self.index = self.focus = 0
        self.checked: list[set[str]] = [set() for _ in questions]
        self.typed = ["" for _ in questions]
        self.answers: dict[int, str] = {}
        self.result: str | None = None

    @property
    def open(self) -> bool:
        return self.result is None

    @property
    def reviewing(self) -> bool:
        return self.index == len(self.questions)

    def press(self, key: str) -> None:
        if not self.open:
            return
        if self.reviewing:
            if key in {"Up", "Down"}:
                self.focus = 1 if key == "Down" else 0
            elif key == "Enter":
                self.result = "submitted" if self.focus == 0 else "cancelled"
            return
        question = self.questions[self.index]
        count = len(question.options)
        last = count + (2 if question.multi else 1)
        if key in {"Up", "Down"}:
            self.focus = max(0, min(last, self.focus + (1 if key == "Down" else -1)))
        elif key in {"Space", "Enter"} and self.focus < count and question.multi:
            label = question.options[self.focus][0]
            self.checked[self.index] ^= {label}
        elif key == "Enter" and question.multi and self.focus == count + 1:
            chosen = [label for label, _ in question.options if label in self.checked[self.index]]
            self._answer(", ".join([*chosen, *filter(None, [self.typed[self.index]])]))
        elif key == "Enter" and not question.multi and self.focus < count:
            self._answer(question.options[self.focus][0])
        elif (
            key == "Enter" and not question.multi and self.focus == count and self.typed[self.index]
        ):
            self._answer(self.typed[self.index])

    def type(self, text: str) -> None:
        if (
            self.open
            and not self.reviewing
            and self.focus == len(self.questions[self.index].options)
        ):
            self.typed[self.index] += text

    def _answer(self, value: str) -> None:
        self.answers[self.index] = value
        if len(self.questions) == 1 and not self.questions[0].multi:
            self.result = "submitted"
            return
        self.index += 1
        self.focus = 0

    def tabs(self) -> str:
        if len(self.questions) == 1 and not self.questions[0].multi:
            return f"\u2610 {self.questions[0].header}"
        marks = "  ".join(
            f"{'\u2612' if index in self.answers else '\u2610'} {question.header}"
            for index, question in enumerate(self.questions)
        )
        return f"\u2190 {marks}  \u2714 Submit \u2192"

    def marker(self, index: int) -> str:
        return "\u276f" if self.focus == index else " "

    def render(self) -> str:
        lines = [DIALOG_RULE, self.tabs(), ""]
        if self.reviewing:
            lines += ["Review your answers", ""]
            for index, question in enumerate(self.questions):
                lines += [f" \u25cf {question.question}", f"   \u2192 {self.answers[index]}"]
            lines += ["", "Ready to submit your answers?", ""]
            lines += [f"{self.marker(0)} 1. Submit answers", f"{self.marker(1)} 2. Cancel"]
            return "\n".join(" " + line if line else "" for line in lines)
        question = self.questions[self.index]
        lines += [question.question, ""]
        for index, (label, description) in enumerate(question.options):
            number = f"{index + 1}. " if self.numbered else ""
            box = (
                f"[{'\u2714' if label in self.checked[self.index] else ' '}] "
                if question.multi
                else ""
            )
            indent = 2 + len(number) + len(box) if self.numbered else 4 + len(box)
            lines += [f"{self.marker(index)} {number}{box}{label}", " " * indent + description]
        count = len(question.options)
        typed = self.typed[self.index]
        placeholder = "Type something" if question.multi else "Type something."
        box = f"[{'\u2714' if typed else ' '}] " if question.multi else ""
        number = f"{count + 1}. " if self.numbered else ""
        content = typed or placeholder
        first, *rest = (
            textwrap.wrap(content, self.wrap_width) or [""] if self.wrap_width else [content]
        )
        lines.append(f"{self.marker(count)} {number}{box}{first}")
        indent = 2 + len(number) + len(box) if self.numbered else 4 + len(box)
        lines += [" " * indent + line for line in rest]
        if question.multi:
            label = "Submit" if self.index == len(self.questions) - 1 else "Next"
            lines.append(f"{self.marker(count + 1)}    {label}")
        chat = count + (2 if question.multi else 1)
        lines += [
            DIALOG_RULE,
            f"{self.marker(chat)} {count + 2}. Chat about this",
            "",
            "Enter to select \u00b7 \u2191/\u2193 to navigate \u00b7 Esc to cancel",
        ]
        return "\n".join(" " + line if line else "" for line in lines)


@dataclass(frozen=True)
class Baseline:
    source: Path
    commits: tuple[str, str, str]
    stores: dict[str, Path]
    benchmark: ExperimentDraft
    graded_benchmark: ExperimentDraft
    assessed: RunSummary
    calibrated: str
    hostile_run: tuple[str, str]


STAGES = ("benchmark", "graded_benchmark", "assessed", "calibrated", "hostile_run")
DERIVED = ("assessed", "calibrated", "hostile_run")


def requested_stage(request) -> str | None:
    names = set(request.fixturenames)
    stages = [stage for stage in STAGES if stage in names]
    if len([stage for stage in stages if stage in DERIVED]) > 1:
        raise RuntimeError("A test cannot share one baseline store with two prebuilt artefacts.")
    return stages[-1] if stages else None


def rebuilds_repository(request) -> bool:
    return "historical_repo" in request.fixturenames


@pytest.fixture(scope="session")
def baseline(tmp_path_factory) -> Baseline:
    root = tmp_path_factory.mktemp("baseline")
    repo_root = root / "repo"
    repo_root.mkdir()
    source, first, future, unreachable = build_historical_repo(repo_root)
    stores: dict[str, Path] = {}

    benchmark_root = root / "benchmark"
    shutil.copytree(repo_root, benchmark_root)
    benchmark_store = ObjectStore(benchmark_root / "store")
    benchmark = build_benchmark(
        benchmark_store,
        (benchmark_root / "source", first, future, unreachable),
        benchmark_root,
    )
    stores["benchmark"] = benchmark_store.root

    graded_root = root / "graded"
    shutil.copytree(benchmark_root, graded_root)
    graded = build_graded_benchmark(ObjectStore(graded_root / "store"), benchmark)
    stores["graded_benchmark"] = graded_root / "store"

    assessed_root = root / "assessed"
    shutil.copytree(graded_root, assessed_root)
    assessed = build_assessed(ObjectStore(assessed_root / "store"), graded)
    stores["assessed"] = assessed_root / "store"

    calibrated_root = root / "calibrated"
    shutil.copytree(graded_root, calibrated_root)
    calibrated = build_calibrated(ObjectStore(calibrated_root / "store"), graded)
    stores["calibrated"] = calibrated_root / "store"

    hostile_root = root / "hostile"
    shutil.copytree(graded_root, hostile_root)
    hostile = build_hostile_run(ObjectStore(hostile_root / "store"), graded)
    stores["hostile_run"] = hostile_root / "store"

    return Baseline(
        source=source,
        commits=(first, future, unreachable),
        stores=stores,
        benchmark=benchmark,
        graded_benchmark=graded,
        assessed=assessed,
        calibrated=calibrated,
        hostile_run=hostile,
    )


@pytest.fixture
def store(tmp_path: Path, request) -> ObjectStore:
    root = tmp_path / "store"
    stage = requested_stage(request)
    if stage is not None and not rebuilds_repository(request):
        shutil.copytree(request.getfixturevalue("baseline").stores[stage], root)
    return ObjectStore(root)


@pytest.fixture
def historical_repo(tmp_path: Path, request) -> tuple[Path, str, str, str]:
    if requested_stage(request) is not None:
        return build_historical_repo(tmp_path)
    prebuilt = request.getfixturevalue("baseline")
    shutil.copytree(prebuilt.source, tmp_path / "source")
    return (tmp_path / "source", *prebuilt.commits)


@pytest.fixture
def benchmark(store, tmp_path: Path, request) -> ExperimentDraft:
    if rebuilds_repository(request):
        return build_benchmark(store, request.getfixturevalue("historical_repo"), tmp_path)
    return request.getfixturevalue("baseline").benchmark


@pytest.fixture
def graded_benchmark(store, benchmark, request) -> ExperimentDraft:
    if rebuilds_repository(request):
        return build_graded_benchmark(store, benchmark)
    return request.getfixturevalue("baseline").graded_benchmark


@pytest.fixture
def assessed(store, graded_benchmark, request) -> RunSummary:
    if rebuilds_repository(request):
        return build_assessed(store, graded_benchmark)
    return request.getfixturevalue("baseline").assessed


@pytest.fixture
def calibrated(store, graded_benchmark, request) -> str:
    if rebuilds_repository(request):
        return build_calibrated(store, graded_benchmark)
    return request.getfixturevalue("baseline").calibrated


@pytest.fixture
def hostile_run(store, graded_benchmark, request) -> tuple[str, str]:
    if rebuilds_repository(request):
        return build_hostile_run(store, graded_benchmark)
    return request.getfixturevalue("baseline").hostile_run
