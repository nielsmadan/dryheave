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
