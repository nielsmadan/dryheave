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
