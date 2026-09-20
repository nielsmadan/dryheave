from pathlib import Path

import pytest

from dryheave.models import StrictModel
from dryheave.storage import ObjectStore


class ExamplePayload(StrictModel):
    title: str
    count: int = 1


@pytest.fixture
def payload() -> ExamplePayload:
    return ExamplePayload(title="Historical task")


@pytest.fixture
def store(tmp_path: Path) -> ObjectStore:
    return ObjectStore(tmp_path / "store")


@pytest.fixture
def historical_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    from dryheave.repositories import Git, SnapshotLimits

    repo = tmp_path / "source"
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


import sys

from dryheave.models import AgentKind
from dryheave.profile_models import AssetSelection, CaptureLimits, CaptureSpec, NativeRecipe
from dryheave.profiles import capture_profile


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


@pytest.fixture
def benchmark(store, historical_repo, tmp_path):
    from dryheave.cases import DeterministicCriterion, TaskFact, draft_case, freeze_case
    from dryheave.controller_models import ControllerRecipe, ScriptStep, SimulatorDecision
    from dryheave.experiment_models import ExperimentDraft, FixtureTurn, VariantSpec
    from dryheave.logs.service import import_session
    from dryheave.models import CommandSpec
    from dryheave.personas import Persona
    from dryheave.repositories import capture_repository

    source, baseline, _, _ = historical_repo
    repository = capture_repository(store, source, baseline)
    session = import_session(store, Path("tests/fixtures/codex-recorded.jsonl"), AgentKind.CODEX)
    draft = draft_case(store, session, repository_id=repository)
    verifier = tmp_path / "hidden.py"
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
        tmp_path,
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


@pytest.fixture
def graded_benchmark(store, benchmark):
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


@pytest.fixture
def hostile_run(store, graded_benchmark):
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
