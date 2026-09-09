import json
import sys
from pathlib import Path

from dryheave.cli import main


def test_profile_cli_capture_derive_diff_inspect_and_materialize(tmp_path: Path, capsys) -> None:
    source = tmp_path / "selected"
    source.mkdir()
    (source / "AGENTS.md").write_text("Use concise answers.")
    spec = tmp_path / "capture.json"
    spec.write_text(
        json.dumps(
            {
                "recipe": {"agent": "codex", "executable": sys.executable, "version": "0.153.4"},
                "include_roots": {"selected": "selected"},
                "assets": [
                    {
                        "root": "selected",
                        "path": "AGENTS.md",
                        "kind": "instruction",
                        "layer": "global",
                        "target_root": "config",
                        "target": "AGENTS.md",
                    }
                ],
            }
        )
    )
    common = ["--store", str(tmp_path / "store"), "--json"]
    assert main(["profile", "capture", "everyday", "--spec", str(spec), *common]) == 0
    first = json.loads(capsys.readouterr().out)["data"]["id"]
    assert main([*common, "profile", "inspect", "everyday"]) == 0
    inspected = json.loads(capsys.readouterr().out)["data"]
    assert inspected["profile"]["assets"][0]["target"] == "AGENTS.md"
    override = tmp_path / "derive.json"
    override.write_text(
        json.dumps({"recipe_changes": {"model": "variant-model", "effort": "high"}})
    )
    assert (
        main(
            [*common, "profile", "derive", "everyday", "--spec", str(override), "--name", "changed"]
        )
        == 0
    )
    second = json.loads(capsys.readouterr().out)["data"]["id"]
    assert main([*common, "profile", "diff", first, second]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["recipe_changes"]["model"] == {
        "before": None,
        "after": "variant-model",
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    options = ["--destination", str(tmp_path / "runtime"), "--workspace", str(workspace)]
    assert main([*common, "profile", "preflight", "changed", *options]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["launch_plan"]["argv"][1:3] == [
        "--model",
        "variant-model",
    ]
    assert (tmp_path / "runtime").exists() is False
    assert main([*common, "profile", "preflight", "changed", *options, "--strict"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_input"
    assert main([*common, "profile", "materialize", "changed", *options]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["materialized"] is True
    assert (tmp_path / "runtime/config/AGENTS.md").read_text() == "Use concise answers."
    assert main([*common, "profile", "capture", "--agent", "claude", "--spec", str(spec)]) == 2
    assert "translation" in json.loads(capsys.readouterr().err)["error"]["message"]


def test_profile_cli_diagnostics_do_not_echo_credential_values(tmp_path: Path, capsys) -> None:
    spec = tmp_path / "capture.json"
    spec.write_text(
        json.dumps(
            {
                "recipe": {
                    "agent": "codex",
                    "executable": "codex",
                    "arguments": ["--api-key", "synthetic-private-value"],
                }
            }
        )
    )
    assert (
        main(
            [
                "--store",
                str(tmp_path / "store"),
                "--json",
                "profile",
                "capture",
                "--spec",
                str(spec),
            ]
        )
        == 2
    )
    response = capsys.readouterr()
    assert response.out == ""
    assert (
        json.loads(response.err)["error"]["message"]
        == "Credential-bearing argv entry 0; use a runtime reference."
    )
    assert list((tmp_path / "store").rglob("manifest.json")) == []
