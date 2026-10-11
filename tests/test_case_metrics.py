import json
import sys
import threading
from types import SimpleNamespace

import pytest

from dryheave.case_metrics import (
    MetricsPaths,
    execute_metrics,
    metrics_environment,
    parse_metrics,
    process_failure,
)
from dryheave.cases import MetricsCommand
from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.errors import InputError
from dryheave.models import CommandSpec, EnvironmentReference
from dryheave.processes import CommandResult
from dryheave.result_models import MetricValue


def output(*metrics: dict) -> bytes:
    return json.dumps({"metrics": list(metrics)}).encode()


def test_parse_metrics_reads_typed_values():
    values = parse_metrics(
        output(
            {
                "metric_id": "tests_passed",
                "label": "Tests passed",
                "value": 12,
                "unit": "tests",
                "better": "higher",
            },
            {"metric_id": "coverage", "label": "Line coverage", "value": 81.5, "unit": "%"},
            {"metric_id": "built", "label": "Build succeeded", "value": True},
            {"metric_id": "runner", "label": "Test runner", "value": "pytest"},
        )
    )
    assert values == (
        MetricValue(
            metric_id="tests_passed", label="Tests passed", value=12, unit="tests", better="higher"
        ),
        MetricValue(metric_id="coverage", label="Line coverage", value=81.5, unit="%"),
        MetricValue(metric_id="built", label="Build succeeded", value=True),
        MetricValue(metric_id="runner", label="Test runner", value="pytest"),
    )
    assert [type(item.value) for item in values] == [int, float, bool, str]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"tests: 12", "metrics output is not valid JSON"),
        (b'{"metrics": [], "metrics": []}', "metrics output is not valid JSON"),
        (b'{"metrics": [{"metric_id": "c", "label": "C", "value": NaN}]}', "not valid JSON"),
        (b"[]", "metrics output is not valid JSON"),
        (
            output({"metric_id": "tests", "label": "Tests", "value": [1, 2]}),
            "violates the contract: metrics.0.value: Value error, metric value must be a number",
        ),
        (
            output({"metric_id": "tests", "label": 12, "value": 1}),
            "violates the contract: metrics.0.label: Input should be a valid string",
        ),
        (
            output({"metric_id": "tests", "label": "Tests", "value": 1, "better": "more"}),
            "violates the contract: metrics.0.better",
        ),
        (
            output({"metric_id": "tests", "label": "Tests", "value": 1, "extra": 2}),
            "metrics.0.extra: Extra inputs are not permitted",
        ),
        (
            output(
                {"metric_id": "a", "label": "A", "value": 1},
                {"metric_id": "a", "label": "A", "value": 2},
            ),
            "metric IDs must be unique",
        ),
        (
            output(
                *({"metric_id": f"m{index}", "label": "M", "value": index} for index in range(65))
            ),
            "metrics: Tuple should have at most 64 items",
        ),
        (
            output({"metric_id": "tests", "label": "x" * 201, "value": 1}),
            "metrics.0.label: String should have at most 200 characters",
        ),
        (
            output({"metric_id": "tests", "label": "Tests", "value": "x" * 501}),
            "metrics.0.value: Value error, metric string value exceeds 500 characters",
        ),
        (
            b'{"values": []}',
            "violates the contract: values: Extra inputs are not permitted; metrics: Field required",
        ),
    ],
)
def test_parse_metrics_rejects_contract_violations_with_specific_reasons(content, message):
    with pytest.raises(InputError, match=message):
        parse_metrics(content)


@pytest.mark.parametrize(
    ("returncode", "outcome", "message"),
    [
        (0, "exited", None),
        (3, "exited", "metrics command exited with status 3"),
        (-9, "exited", "metrics command was killed by signal 9"),
        (-9, "timeout", "metrics command timed out after 30 seconds"),
        (-9, "output_limit", "metrics command exceeded its stdout/stderr byte limit"),
    ],
)
def test_process_failure_names_the_reason(returncode, outcome, message):
    result = CommandResult(("sh",), returncode, b"", b"", outcome)
    assert process_failure(result, 30) == message


def test_metrics_environment_passes_only_declared_references_and_contract_paths(tmp_path):
    spec = CommandSpec(
        argv=("/bin/sh", "{verifier}/metrics.sh"),
        environment=(EnvironmentReference(name="TOOLCHAIN"),),
    )
    paths = MetricsPaths(
        workspace=tmp_path / "workspace",
        baseline=tmp_path / "baseline",
        hidden=tmp_path / "hidden",
        output=tmp_path / "output" / "metrics.json",
    )
    inherited = {"HOME": "/home/u", "PATH": "/usr/bin", "TOOLCHAIN": "/opt/t", "SECRET": "s"}
    assert metrics_environment(spec, inherited, paths) == {
        "TOOLCHAIN": "/opt/t",
        "HOME": "/home/u",
        "PATH": "/usr/bin",
        "DRYHEAVE_BASELINE_DIR": str(tmp_path / "baseline"),
        "DRYHEAVE_METRICS_OUTPUT": str(tmp_path / "output" / "metrics.json"),
    }
    with pytest.raises(InputError, match="unavailable: TOOLCHAIN"):
        metrics_environment(spec, {"PATH": "/usr/bin"}, paths)


def run_script(tmp_path, program: str, *, timeout: int = 60):
    paths = MetricsPaths(
        workspace=tmp_path / "workspace",
        baseline=tmp_path / "baseline",
        hidden=tmp_path / "hidden",
        output=tmp_path / "output" / "metrics.json",
    )
    for directory in (paths.workspace, paths.baseline, paths.hidden, paths.output.parent):
        directory.mkdir()
    (paths.workspace / "greet.py").write_text("final\n")
    (paths.baseline / "greet.py").write_text("baseline\n")
    (paths.hidden / "metrics.py").write_text(program)
    command = MetricsCommand(
        metrics_id="tests",
        description="Measure the final workspace against its baseline.",
        command=CommandSpec(
            argv=(sys.executable, "{verifier}/metrics.py"), timeout_seconds=timeout
        ),
        entrypoint="metrics.py",
    )
    identities = []
    context = SimpleNamespace(
        cancelled=threading.Event(), original_workspace=None, on_identity=identities.append
    )
    evidence = ArtifactWriter(tmp_path / "evidence", 1000000)
    result = execute_metrics(command, paths, evidence, context, {"PATH": "/usr/bin:/bin"})
    assert identities
    return result, evidence


WRITE_METRICS = """
import json, os
from pathlib import Path
print("compiling 3 targets... noise on stdout")
baseline = Path(os.environ["DRYHEAVE_BASELINE_DIR"], "greet.py").read_text().strip()
final = Path("greet.py").read_text().strip()
Path(os.environ["DRYHEAVE_METRICS_OUTPUT"]).write_text(json.dumps({"metrics": [
    {"metric_id": "tests_passed", "label": "Tests passed", "value": 12, "better": "higher"},
    {"metric_id": "changed", "label": f"{baseline} -> {final}", "value": baseline != final},
]}))
"""


@pytest.mark.integration
def test_script_output_file_becomes_metrics_and_logs_stay_evidence(tmp_path):
    result, evidence = run_script(tmp_path, WRITE_METRICS)
    assert (result.outcome, result.error) == ("completed", None)
    assert result.values == (
        MetricValue(metric_id="tests_passed", label="Tests passed", value=12, better="higher"),
        MetricValue(metric_id="changed", label="baseline -> final", value=True),
    )
    assert (result.execution.outcome, result.execution.execution_observed) == ("pass", True)
    assert (evidence.root / "stdout.bin").read_bytes().startswith(b"compiling 3 targets")
    assert json.loads((evidence.root / "metrics.json").read_bytes())["metrics"][0]["value"] == 12


@pytest.mark.integration
@pytest.mark.parametrize(
    ("program", "message"),
    [
        (
            'import os\nopen(os.environ["DRYHEAVE_METRICS_OUTPUT"], "w").write("{broken")\n',
            "metrics output is not valid JSON",
        ),
        (
            "import json, os\n"
            'open(os.environ["DRYHEAVE_METRICS_OUTPUT"], "w").write(json.dumps('
            '{"metrics": [{"metric_id": "t", "label": "T", "value": {"n": 1}}]}))\n',
            "metrics output violates the contract: metrics.0.value: Value error, metric value must",
        ),
        (
            "import json, os\n"
            'open(os.environ["DRYHEAVE_METRICS_OUTPUT"], "w").write(json.dumps({"metrics": []}))\n'
            "raise SystemExit(2)\n",
            "metrics command exited with status 2",
        ),
        ('print("{}")\n', "metrics command did not write DRYHEAVE_METRICS_OUTPUT"),
        ("import time\ntime.sleep(60)\n", "metrics command timed out after 2 seconds"),
    ],
)
def test_failed_metrics_commands_become_errors_with_their_reason(tmp_path, program, message):
    result, _ = run_script(tmp_path, program, timeout=2 if "sleep" in program else 60)
    assert (result.outcome, result.values) == ("error", ())
    assert result.error.startswith(message)
