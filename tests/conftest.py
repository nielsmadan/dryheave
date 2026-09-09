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
