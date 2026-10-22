import os

import pytest

from dryheave.drivers.auth import RuntimeBindings, check_codex_auth, runtime_file_source
from dryheave.errors import InputError
from dryheave.live_config import live_source
from dryheave.models import AgentKind
from dryheave.profile_models import RuntimeFileReference, codex_auth_reference


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


def codex_auth_plan(plan, reference=None):
    return plan.model_copy(update={"runtime_files": (reference or codex_auth_reference(),)})


def existing(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    return path


def codex_source(environment):
    return runtime_file_source(codex_auth_reference(), environment)


def test_codex_home_default_resolves_under_codex_home_then_home(tmp_path):
    user = tmp_path / "user"
    elsewhere = existing(tmp_path / "codex/auth.json")
    default = existing(user / ".codex/auth.json")
    tilde = existing(user / "elsewhere/auth.json")
    explicit = existing(tmp_path / "explicit.json")
    assert codex_source({"CODEX_HOME": str(tmp_path / "codex"), "HOME": str(user)}) == str(
        elsewhere
    )
    assert codex_source({"HOME": str(user)}) == str(default)
    assert codex_source({"CODEX_HOME": "~/elsewhere", "HOME": str(user)}) == str(tilde)
    assert codex_source(
        {"CODEX_AUTH_FILE": str(explicit), "CODEX_HOME": str(tmp_path / "other")}
    ) == str(explicit)
    assert codex_source({"CODEX_AUTH_FILE": "~/.codex/auth.json", "HOME": str(user)}) == str(
        default
    )


@pytest.mark.parametrize("codex_home", [None, "~/elsewhere", "relative-home"])
def test_codex_home_default_matches_the_live_config_root(tmp_path, monkeypatch, codex_home):
    monkeypatch.chdir(tmp_path)
    user = tmp_path / "user"
    environment = {"HOME": str(user)} | ({"CODEX_HOME": codex_home} if codex_home else {})
    expected = existing(live_source(AgentKind.CODEX, environment, user).root / "auth.json")
    assert codex_source(environment) == str(expected)


def test_codex_home_default_refuses_a_relative_path(tmp_path, monkeypatch):
    existing(tmp_path / "sub/auth.json")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(
        InputError, match="CODEX_AUTH_FILE must be an absolute path or start with ~/, not sub/auth"
    ):
        codex_source({"CODEX_AUTH_FILE": "sub/auth.json"})


def test_codex_home_default_refuses_another_users_home(tmp_path):
    with pytest.raises(InputError, match="Only ~/ paths expand against the home directory, not ~"):
        codex_source({"CODEX_AUTH_FILE": "~root/.codex/auth.json", "HOME": str(tmp_path)})


def test_codex_home_default_needs_a_home_to_resolve_the_default():
    with pytest.raises(InputError, match=r"HOME is not set, so ~/\.codex cannot be resolved"):
        codex_source({})


def test_codex_home_default_needs_no_home_for_an_absolute_codex_home(tmp_path):
    auth = existing(tmp_path / "codex/auth.json")
    assert codex_source({"CODEX_HOME": str(tmp_path / "codex")}) == str(auth)


@pytest.mark.parametrize("variable", ["CODEX_HOME", "CODEX_AUTH_FILE"])
def test_check_codex_auth_refuses_a_missing_file(tmp_path, variable):
    configured = tmp_path / "missing" if variable == "CODEX_HOME" else tmp_path / "missing.json"
    missing = configured / "auth.json" if variable == "CODEX_HOME" else configured
    with pytest.raises(InputError, match=f"Codex auth file {missing} does not exist"):
        check_codex_auth({variable: str(configured), "HOME": str(tmp_path)})


def test_codex_binding_links_the_default_auth_file_without_an_explicit_reference(
    tmp_path, driver_plan
):
    auth = existing(tmp_path / "user/.codex/auth.json")
    inherited = {"HOME": str(tmp_path / "user")}
    with RuntimeBindings(codex_auth_plan(driver_plan), inherited):
        assert os.readlink(tmp_path / "config/auth.json") == str(auth)
    assert inherited == {"HOME": str(tmp_path / "user")}


def test_codex_home_default_applies_to_a_custom_variable_name(tmp_path, driver_plan):
    auth = existing(tmp_path / "user/keys/codex.json")
    inherited = {"FIXTURE_CODEX_AUTH": "~/keys/codex.json", "HOME": str(tmp_path / "user")}
    reference = codex_auth_reference("FIXTURE_CODEX_AUTH")
    with RuntimeBindings(codex_auth_plan(driver_plan, reference), inherited):
        assert os.readlink(tmp_path / "config/auth.json") == str(auth)


def test_a_codex_auth_variable_without_a_default_source_needs_its_value(tmp_path, driver_plan):
    existing(tmp_path / "user/.codex/auth.json")
    reference = codex_auth_reference("CODEX_AUTH_FILE", default_source=None)
    with (
        pytest.raises(InputError, match="set CODEX_AUTH_FILE to its absolute path"),
        RuntimeBindings(codex_auth_plan(driver_plan, reference), {"HOME": str(tmp_path / "user")}),
    ):
        pytest.fail("Binding must refuse a reference whose variable is unset.")


def test_codex_binding_refuses_a_missing_default_auth_file(tmp_path, driver_plan):
    missing = tmp_path / "user/.codex/auth.json"
    with (
        pytest.raises(InputError, match=f"Codex auth file {missing} does not exist"),
        RuntimeBindings(codex_auth_plan(driver_plan), {"HOME": str(tmp_path / "user")}),
    ):
        pytest.fail("Binding must refuse a missing default auth file.")
