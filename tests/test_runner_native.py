from pathlib import Path
from uuid import uuid4

import pytest

from dryheave.drivers.artifacts import ArtifactWriter
from dryheave.drivers.base import TransportError
from dryheave.drivers.fake import FakeEventSource
from dryheave.drivers.models import ProcessIdentity
from dryheave.errors import InputError
from dryheave.experiments import create_experiment
from dryheave.fixture_subject import FixtureTerminal
from dryheave.models import AgentKind
from dryheave.runner import load_capture, run_experiment
from dryheave.runner_models import RunOptions


def test_native_wiring_builds_log_source_before_launch_and_preserves_plan(
    store, benchmark, monkeypatch
):
    source = FakeEventSource([])
    order = []
    launched = []

    class Owner:
        def snapshot(self):
            return ()

    class Terminal(FixtureTerminal):
        def __init__(self, executable, runtime, *, limits):
            super().__init__(benchmark.fixture, source, AgentKind.CODEX)
            self.artifacts = ArtifactWriter(runtime, limits.max_artifact_bytes)
            self.owner = Owner()
            self.session = "synthetic-native"
            self.runtime = runtime
            assert executable.name == "tui-test"

        def start(self, plan, environment):
            order.append("launch")
            launched.append((plan, environment))
            observed = (
                super()
                .start(plan, environment)
                .model_copy(
                    update={
                        "transport": "synthetic-native",
                        "daemon": ProcessIdentity(pid=9999999, created=1.0),
                    }
                )
            )
            self.artifacts.record("launch-observed", observed)
            return observed

    def logs(root, agent, artifacts, *, limits):
        order.append("log-source")
        assert root.parent.is_dir()
        assert root.name == "sessions"
        assert agent == AgentKind.CODEX
        return source

    monkeypatch.setattr("dryheave.runner_attempt.TuiTestTerminal", Terminal)
    monkeypatch.setattr("dryheave.runner_attempt.NativeLogSource", logs)
    runtime = Path(".cache") / ("n" + uuid4().hex[:3])
    identifier = create_experiment(store, benchmark)
    result = run_experiment(
        store,
        identifier,
        options=RunOptions(
            mode="native", runtime_root=str(runtime), transport=".tools/tui-test/tui-test"
        ),
    )
    captured = load_capture(store, result.pending_assessment[0])
    assert order == ["log-source", "launch"]
    assert (captured.mode, captured.launch.transport, captured.accepted_turns) == (
        "native",
        "synthetic-native",
        2,
    )
    plan, environment = launched[0]
    assert plan == captured.launch_plan
    assert environment["CODEX_HOME"] == plan.config_roots["config"]
    assert result.attempts[0].terminal_session == "synthetic-native"
    assert Path(result.attempts[0].runtime).parent == runtime.absolute()


@pytest.mark.parametrize("terminal_closed", [False, True])
def test_lost_launch_acknowledgement_requires_terminal_reconciliation(
    store, benchmark, monkeypatch, terminal_closed
):
    source = FakeEventSource([])
    launches = []

    class Owner:
        def snapshot(self):
            return ()

    class Terminal(FixtureTerminal):
        def __init__(self, executable, runtime, *, limits):
            super().__init__(benchmark.fixture, source, AgentKind.CODEX)
            self.artifacts = ArtifactWriter(runtime, limits.max_artifact_bytes)
            self.owner = Owner()
            self.session = "unacknowledged-fixture"

        def start(self, plan, environment):
            launches.append(plan)
            raise TransportError("Launch acknowledgement lost before daemon identity.")

        def close(self):
            return (
                super()
                .close()
                .model_copy(
                    update={
                        "terminal_closed": terminal_closed,
                        "errors": () if terminal_closed else ("terminal_close_failed",),
                    }
                )
            )

    monkeypatch.setattr("dryheave.runner_attempt.TuiTestTerminal", Terminal)
    monkeypatch.setattr("dryheave.runner_attempt.NativeLogSource", lambda *args, **kwargs: source)
    result = run_experiment(
        store,
        create_experiment(store, benchmark.model_copy(update={"repetitions": 2})),
        options=RunOptions(
            mode="native",
            runtime_root=str(Path(".cache") / ("l" + uuid4().hex[:3])),
            transport=".tools/tui-test/tui-test",
        ),
    )
    expected = 2 if terminal_closed else 1
    assert len(launches) == len(result.attempts) == expected
    assert len(result.unstarted_trials) == 2 - expected
    captured = load_capture(store, result.pending_assessment[0])
    assert captured.launch is None
    assert captured.accepted_turns == 0
    assert captured.cleanup.known_writers_stopped
    assert captured.cleanup.terminal_closed is terminal_closed
    if terminal_closed:
        assert captured.workspace.complete
        assert run_experiment(store, resume=result.run_id) == result
    else:
        assert captured.cleanup.errors == ("terminal_close_failed",)
        assert captured.workspace.complete is False
        assert captured.evidence_omissions == ("cleanup_unresolved",)
        with pytest.raises(InputError, match="reconciliation before another subject launch"):
            run_experiment(store, resume=result.run_id)
