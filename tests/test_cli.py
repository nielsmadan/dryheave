import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ExamplePayload
from dryheave.cli import CommandRegistry, main
from dryheave.constants import VERSION
from dryheave.models import ObjectKind
from dryheave.storage import ObjectStore


def test_help_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "store" in capsys.readouterr().out
    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0
    assert capsys.readouterr().out == f"dryheave {VERSION}\n"


@pytest.mark.parametrize("placement", ["before", "after", "middle", "equals"])
def test_global_options_are_consistent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], placement: str
) -> None:
    root = tmp_path / "store"
    if placement == "before":
        args = ["--store", str(root), "--json", "store", "path"]
    elif placement == "after":
        args = ["store", "path", "--json", "--store", str(root)]
    elif placement == "middle":
        args = ["store", "--store", str(root), "path", "--json"]
    else:
        args = ["store", "path", f"--store={root}", "--json"]
    assert main(args) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": True, "data": {"path": str(root)}}
    assert captured.err == ""
    assert not root.exists()


def test_inspection_verification_and_alias_lifecycle(
    store: ObjectStore, payload: ExamplePayload, capsys: pytest.CaptureFixture[str]
) -> None:
    identifier = store.put(ObjectKind.CASE, payload)
    base = ["--store", str(store.root), "--json", "store"]
    assert main([*base, "alias", "set", "case-one", identifier]) == 0
    assert json.loads(capsys.readouterr().out)["data"] == {"name": "case-one", "id": identifier}
    assert main([*base, "inspect", "case-one"]) == 0
    inspected = json.loads(capsys.readouterr().out)["data"]
    assert inspected["id"] == identifier
    assert inspected["manifest"]["payload"]["title"] == payload.title
    assert main([*base, "verify", "case-one"]) == 0
    assert json.loads(capsys.readouterr().out)["data"] == {
        "id": identifier,
        "kind": "case",
        "verified": True,
    }
    assert main([*base, "alias", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["data"] == {"aliases": {"case-one": identifier}}
    assert main([*base, "alias", "delete", "case-one"]) == 0
    assert json.loads(capsys.readouterr().out)["data"] == {"deleted": "case-one"}


@pytest.mark.parametrize(
    "arguments,code",
    [
        (["unknown"], "invalid_input"),
        (["store", "verify", "missing"], "not_found"),
        (["--store"], "invalid_input"),
        (["--store", "--json", "store", "path"], "invalid_input"),
        (["store", "--bad"], "invalid_input"),
    ],
)
def test_errors_are_structured_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], arguments: list[str], code: str
) -> None:
    assert main(["--store", str(tmp_path), *arguments, "--json"]) != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["ok"] is False
    assert error["error"]["code"] == code
    assert error["error"]["message"]


def test_human_output_and_error_are_readable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["store", "path", "--store", str(tmp_path)]) == 0
    assert capsys.readouterr().out == f"{tmp_path}\n"
    assert main(["store", "alias", "list", "--store", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"aliases": {}}
    assert main(["nonsense"]) == 2
    assert capsys.readouterr().err.startswith("dryheave: ")


def test_domain_commands_register_without_importing_cli_in_services(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handle(args: argparse.Namespace, store: ObjectStore) -> dict[str, str]:
        return {"title": args.title, "store": str(store.root)}

    def register(registry: CommandRegistry) -> None:
        command = registry.add("example", help_text="Registered by a domain CLI module.")
        command.add_argument("title")
        registry.handler(command, handle)

    assert (
        main(["example", "Task", "--store", str(tmp_path), "--json"], registrars=(register,)) == 0
    )
    assert json.loads(capsys.readouterr().out)["data"] == {"title": "Task", "store": str(tmp_path)}


def test_installed_module_entrypoint(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "dryheave", "store", "path", "--store", str(tmp_path), "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(result.stdout) == {"ok": True, "data": {"path": str(tmp_path)}}
