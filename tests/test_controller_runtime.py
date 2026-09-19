import json
import sys
import threading
import time

import psutil
import pytest

from dryheave.controller_models import ControllerRecipe, ControllerRuntime
from dryheave.controller_runtime import prepare_runtime, reconcile_runtimes
from dryheave.controller_watch import MAX_CONTROLLER_DEPTH
from dryheave.controllers import CallContext, invoke_controller
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.errors import InputError
from dryheave.models import CommandSpec
from dryheave.setup_helpers import runtime_references
from test_controllers import request_for


def codex_recipe(executable):
    return ControllerRecipe(
        kind="codex",
        version="0.154.0",
        model="fixture-model",
        effort="low",
        isolation="trusted-native",
        command=CommandSpec(argv=tuple(executable)),
        runtime=ControllerRuntime(
            runtime_files=runtime_references("SYNTHETIC_AUTH_PATH", acknowledge=True)
        ),
    )


def test_modern_codex_runtime_keeps_auth_links_out_of_evidence(tmp_path):
    recipe = codex_recipe(("codex",))
    root = tmp_path / "role-runtime/call"
    artifacts = ArtifactWriter(tmp_path / "evidence", 100000)
    reference = tmp_path / "unread-nonexistent-credential"
    bindings = prepare_runtime(recipe, root, {"SYNTHETIC_AUTH_PATH": str(reference)}, artifacts)
    assert (root / "config/auth.json").is_symlink()
    marker = json.loads((root / "runtime-bindings.json").read_bytes())
    assert set(marker["identities"]) == {"auth.json"}
    reconcile_runtimes(root.parent)
    assert not (root / "config/auth.json").is_symlink()
    assert bindings.close() == ()
    reconcile_runtimes(root.parent)
    assert not reference.exists()


def test_recovery_preserves_replaced_binding(tmp_path):
    root = tmp_path / "role-runtime/call"
    writer = ArtifactWriter(tmp_path / "evidence", 100000)
    bindings = prepare_runtime(
        codex_recipe(("codex",)), root, {"SYNTHETIC_AUTH_PATH": str(tmp_path / "opaque")}, writer
    )
    path = root / "config/auth.json"
    path.unlink()
    path.write_text("foreign replacement")
    with pytest.raises(InputError, match="replaced"):
        reconcile_runtimes(root.parent)
    assert path.read_text() == "foreign replacement"
    assert bindings.close() == ("runtime_binding_replaced",)


def test_runtime_rejects_evidence_overlap_and_reuse(tmp_path):
    writer = ArtifactWriter(tmp_path / "evidence", 100000)
    recipe = codex_recipe(("codex",))
    with pytest.raises(InputError, match="separate"):
        prepare_runtime(recipe, writer.root / "runtime", {}, writer)
    runtime = tmp_path / "runtime"
    bindings = prepare_runtime(
        recipe, runtime, {"SYNTHETIC_AUTH_PATH": str(tmp_path / "opaque")}, writer
    )
    with pytest.raises(FileExistsError):
        prepare_runtime(recipe, runtime, {}, writer)
    assert bindings.close() == ()


def test_failed_binding_leaves_reconcilable_marker(tmp_path):
    writer = ArtifactWriter(tmp_path / "evidence", 100000)
    with pytest.raises(InputError, match="unavailable"):
        prepare_runtime(codex_recipe(("codex",)), tmp_path / "roles/call", {}, writer)
    reconcile_runtimes(tmp_path / "roles")
    assert (
        json.loads((tmp_path / "roles/call/runtime-bindings.json").read_bytes())["identities"] == {}
    )


def test_modern_codex_protocol_and_cleanup_are_versioned(store, benchmark, tmp_path):
    script = tmp_path / "synthetic-codex.py"
    script.write_text(
        "import json,os,pathlib,sys\n"
        "if sys.argv[-1]=='--version':\n    print('codex-cli 0.154.0')\n    raise SystemExit(0)\n"
        "argv=sys.argv\n"
        "assert argv[argv.index('--disable')+1] == 'plugins'\n"
        "assert 'skills.bundled.enabled=false' in argv\n"
        "assert '--ignore-user-config' in argv and '--ignore-rules' in argv\n"
        "assert pathlib.Path(os.environ['CODEX_HOME'],'auth.json').is_symlink()\n"
        "request=json.load(sys.stdin)\n"
        "pathlib.Path(argv[argv.index('--output-last-message')+1]).write_text(json.dumps({'action':'stop','text':'','fact_ids':[],'reason':'Synthetic fixture'}))\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':20,'cached_input_tokens':2,'output_tokens':7}}))\n"
    )
    recipe = codex_recipe((sys.executable, str(script)))
    writer = ArtifactWriter(tmp_path / "evidence", 1000000)
    context = CallContext(
        call_id="codex-call",
        index=0,
        deadline=time.monotonic() + 3,
        cancelled=threading.Event(),
        inherited={"SYNTHETIC_AUTH_PATH": str(tmp_path / "opaque")},
        on_identity=lambda _identity: None,
        runtime_root=tmp_path / "roles/call",
    )
    result = invoke_controller(recipe, request_for(store), writer, context)
    assert result.status == "completed"
    assert result.cleanup.known_writers_stopped
    assert result.usage.uncached_input == 18
    assert "0.154.0" in result.usage.provenance
    assert result.observed_model is None
    assert not (context.runtime_root / "config/auth.json").is_symlink()


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("pathlib.Path('large.bin').write_bytes(b'x' * 700000)", "controller_file_limit"),
        (
            "[pathlib.Path(os.environ['TMPDIR'], str(i)).mkdir() for i in range(257)]",
            "controller_file_limit",
        ),
        (
            f"pathlib.Path(os.environ['CODEX_HOME'], *(['deep'] * {MAX_CONTROLLER_DEPTH})).mkdir(parents=True)",
            "controller_file_depth",
        ),
    ],
)
def test_runtime_limits_cancel_controller_and_child_and_remove_auth_binding(
    store, benchmark, tmp_path, mutation, expected
):
    child = (
        "import os,pathlib,time\n"
        "pathlib.Path('child.pid').write_text(str(os.getpid()))\n"
        "time.sleep(0.3)\n"
        f"{mutation}\n"
        "time.sleep(30)\n"
    )
    script = tmp_path / "synthetic-codex.py"
    script.write_text(
        "import subprocess,sys\n"
        "if sys.argv[-1]=='--version':\n    print('codex-cli 0.154.0')\n    raise SystemExit(0)\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}]).wait()\n"
    )
    recipe = codex_recipe((sys.executable, str(script)))
    writer = ArtifactWriter(tmp_path / "evidence", 1000000)
    identities = []
    runtime = tmp_path / "roles/call"
    context = CallContext(
        call_id="codex-call",
        index=0,
        deadline=time.monotonic() + 10,
        cancelled=threading.Event(),
        inherited={"SYNTHETIC_AUTH_PATH": str(tmp_path / "nonexistent-opaque-source")},
        on_identity=identities.append,
        runtime_root=runtime,
    )
    started = time.monotonic()
    result = invoke_controller(recipe, request_for(store), writer, context)
    assert time.monotonic() - started < 5
    assert result.status == "failed"
    assert result.cleanup.known_writers_stopped
    assert result.cleanup.errors == (expected,)
    assert result.cleanup.survivors == ()
    child_pid = int((runtime / "work/child.pid").read_text())
    assert any(identity.pid == child_pid for identity in identities)
    assert (
        not psutil.pid_exists(child_pid)
        or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
    )
    command = json.loads(next(writer.root.glob("*-command-result.json")).read_bytes())
    assert command["outcome"] == "timeout"
    assert not (runtime / "config/auth.json").is_symlink()
    cleanup = json.loads(next(writer.root.glob("*-cleanup.json")).read_bytes())
    assert cleanup == result.cleanup.model_dump(mode="json")
