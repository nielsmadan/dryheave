import os

import pytest

from dryheave.drivers.auth import RuntimeBindings
from dryheave.errors import InputError
from dryheave.profile_models import RuntimeFileReference


def auth_plan(plan):
    reference = RuntimeFileReference.model_validate(
        {
            "name": "opaque",
            "source_path_environment": {"name": "FIXTURE_AUTH_PATH", "source": "inherited"},
            "acknowledge_source_writes": True,
        }
    )
    return plan.model_copy(update={"runtime_files": (reference,)})


def test_binding_uses_only_explicit_path_without_reading_credential_file(tmp_path, driver_plan):
    plan = auth_plan(driver_plan)
    source = tmp_path / "not-created-auth"
    target = tmp_path / "config/auth.json"
    with RuntimeBindings(plan, {"FIXTURE_AUTH_PATH": str(source)}) as bindings:
        assert target.is_symlink()
        assert os.readlink(target) == str(source)
        assert bindings.errors == []
    assert not target.is_symlink()


def test_binding_replacement_is_preserved_and_disclosed(tmp_path, driver_plan):
    target = tmp_path / "config/auth.json"
    with RuntimeBindings(
        auth_plan(driver_plan), {"FIXTURE_AUTH_PATH": str(tmp_path / "opaque")}
    ) as bindings:
        target.unlink()
        target.write_text("replacement fixture")
    assert target.read_text() == "replacement fixture"
    assert bindings.errors == ["runtime_binding_replaced"]


def test_missing_reference_and_existing_destination_are_safe(tmp_path, driver_plan):
    with pytest.raises(InputError, match="opaque"), RuntimeBindings(auth_plan(driver_plan), {}):
        pytest.fail("Binding must reject a missing reference.")
    target = tmp_path / "config/auth.json"
    target.write_text("existing fixture")
    with (
        pytest.raises(FileExistsError),
        RuntimeBindings(auth_plan(driver_plan), {"FIXTURE_AUTH_PATH": str(tmp_path / "opaque")}),
    ):
        pytest.fail("Binding must reject an existing destination.")
    assert target.read_text() == "existing fixture"
