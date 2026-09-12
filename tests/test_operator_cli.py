import json
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

import pytest

from dryheave.cli import main
from dryheave.skills import SKILL_NAMES


def test_public_skill_lifecycle_json_and_conflict_error(tmp_path, capsys):
    target = tmp_path / "operator skills ü"
    prefix = ["--json", "--store", str(tmp_path / "store"), "skills"]
    for action in ("install", "doctor", "list", "update", "uninstall"):
        assert main([*prefix, action, "--target", str(target)]) == 0
        response = json.loads(capsys.readouterr().out)
        assert response["ok"] is True
        assert response["data"]["target"] == str(target)
    assert main([*prefix, "install", "--target", str(target), SKILL_NAMES[0]]) == 0
    capsys.readouterr()
    assert main([*prefix, "install", "--target", str(target), SKILL_NAMES[0]]) == 0
    capsys.readouterr()
    (target / SKILL_NAMES[0] / "SKILL.md").write_bytes(b"User edits")
    assert main([*prefix, "install", "--target", str(target), SKILL_NAMES[0]]) == 1
    response = capsys.readouterr()
    assert response.out == ""
    assert json.loads(response.err)["error"]["code"] == "conflict"
    assert set(tmp_path.iterdir()) == {target}


@pytest.mark.parametrize(
    "arguments",
    [
        ["skills", "install"],
        ["skills", "doctor"],
        ["example", "write"],
        ["skills", "install", "--target", "unused", "other-skill"],
    ],
)
def test_explicit_targets_and_bundled_names_are_required(arguments, capsys):
    assert main([*arguments, "--json"]) == 2
    result = capsys.readouterr()
    assert result.out == ""
    assert json.loads(result.err)["error"]["code"] == "invalid_input"


def test_runtime_doctor_cli_returns_diagnostics_without_initializing_store(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("dryheave.doctor.shutil.which", lambda _name: None)
    assert main(["--store", str(tmp_path / "store"), "doctor", "--agent", "codex", "--json"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["data"]["ready"] is False
    assert response["data"]["checks"][-1]["tool"] == "codex"
    assert list(tmp_path.iterdir()) == []


def test_example_copy_uses_packaged_bytes_and_refuses_overwrite_or_symlink(tmp_path, capsys):
    target = tmp_path / "examples"
    assert main(["example", "write", "--target", str(target), "--json"]) == 0
    response = json.loads(capsys.readouterr().out)
    for name in response["data"]["files"]:
        assert (target / name).read_bytes() == files("dryheave").joinpath(
            "resources", "examples", name
        ).read_bytes()
    (target / "real-workflow.md").write_bytes(b"User edits")
    assert main(["example", "write", "--target", str(target), "--json"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "conflict"
    assert (target / "real-workflow.md").read_bytes() == b"User edits"
    link = tmp_path / "linked"
    link.symlink_to(target, target_is_directory=True)
    assert main(["example", "write", "--target", str(link / "child"), "--json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "unsafe_path"
    assert (
        main(
            ["example", "write", "--target", str(tmp_path / "unused" / ".." / "escaped"), "--json"]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "unsafe_path"
    assert {entry.name for entry in tmp_path.iterdir()} == {"examples", "linked"}


def test_packaged_offline_example_runs_only_public_cli_and_preserves_source(tmp_path, capsys):
    copied = tmp_path / "examples"
    assert main(["example", "write", "--target", str(copied), "--json"]) == 0
    capsys.readouterr()
    executable = Path(sys.executable).parent / "dryheave"
    output = tmp_path / "demo ü"
    argv = [
        sys.executable,
        str(copied / "offline_demo.py"),
        "--dryheave",
        str(executable),
        "--output",
        str(output),
    ]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=180, check=False)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert (summary["paired_count"], summary["source_preserved"]) == (2, True)
    assert summary["dryheave"] == str(executable)
    report = json.loads((output / "report.json").read_text())
    assert [
        (group["variant"], group["eligible_passes"], group["attempts"])
        for group in report["groups"]
    ] == [("base", 2, 2), ("changed", 2, 2)]
    assert {attempt["mode"] for attempt in report["attempts"]} == {"offline-fixture"}
    assert len({attempt["capture_id"] for attempt in report["attempts"]}) == 4
    for attempt in report["attempts"]:
        criterion = attempt["result"]["criteria"][0]
        assert criterion["execution"]["execution_observed"] is True
        assert criterion["calibration"]["status"] == "demonstrated"
        assert criterion["calibration"]["baseline"]["outcome"] == "fail"
        assert criterion["calibration"]["reference"]["outcome"] == "pass"
    portable = json.loads((output / "portable-report.json").read_text())
    assert portable["portable"] is True
    assert {attempt["raw_evidence"] for attempt in portable["attempts"]} == {"unavailable"}
    records = [json.loads(path.read_text()) for path in (output / "commands").iterdir()]
    assert {record["argv"][0] for record in records} == {str(executable)}
    assert (output / ".gitignore").read_text() == "*\n"
    second = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    assert second.returncode == 1
    assert "File exists" in second.stderr
    assert json.loads((output / "summary.json").read_text()) == summary
