from argparse import Namespace

import pytest

from dryheave import bench_cli
from dryheave.drivers.base import DeliveryUncertain
from dryheave.drivers.models import Observation
from dryheave.errors import InputError


def _launch_checks(store, monkeypatch, check):
    roots = []

    def snapshot(root, environment):
        roots.append(root)

    monkeypatch.setattr(bench_cli, "snapshot_for_check", snapshot)
    monkeypatch.setattr(bench_cli, "check_removals", lambda manifest, variants: None)
    monkeypatch.setattr(bench_cli, "launch_check", check)
    with pytest.raises(InputError) as raised:
        bench_cli._launch_checks(
            Namespace(variant=["sonnet=claude-sonnet-4-6@low"]),
            store,
            {"sonnet": "a" * 64},
            (store.root / "rt", store.root / "tui-test"),
        )
    return str(raised.value), roots[0]


def test_launch_check_failure_names_its_reason_and_retained_evidence(store, monkeypatch):
    def unsupported(*args, **kwargs):
        return Observation(
            state="unsupported",
            reason="The typing probe lost its terminal acknowledgement: lost.",
            text="> ",
        )

    message, root = _launch_checks(store, monkeypatch, unsupported)
    assert message.startswith(
        "Variant sonnet did not reach an empty prompt (unsupported: The typing probe lost its "
        f"terminal acknowledgement: lost.). Nothing was sent to a model. Evidence: {root}\n"
    )


def test_launch_check_transport_failure_names_its_retained_evidence(store, monkeypatch):
    def uncertain(*args, **kwargs):
        raise DeliveryUncertain("Terminal command ended without a delivery acknowledgement.")

    message, root = _launch_checks(store, monkeypatch, uncertain)
    assert message == (
        "Variant sonnet launch check lost its terminal transport (Terminal command ended without "
        f"a delivery acknowledgement.). Nothing was sent to a model. Evidence: {root}"
    )


def test_launch_checks_probe_the_case_slash_commands_for_each_variant(store, monkeypatch):
    seen = []

    def ready(*args, workflow, commands, **kwargs):
        seen.append((workflow, commands))
        return Observation(state="ready", reason="Composer is empty.", text="> ")

    monkeypatch.setattr(bench_cli, "snapshot_for_check", lambda root, environment: None)
    monkeypatch.setattr(bench_cli, "check_removals", lambda manifest, variants: None)
    monkeypatch.setattr(bench_cli, "launch_check", ready)
    bench_cli._launch_checks(
        Namespace(variant=["a=claude-sonnet-4-6@low", "b=claude-haiku-4-5"]),
        store,
        {"a": "a" * 64, "b": "b" * 64},
        (store.root / "rt", store.root / "tui-test"),
        ("/code-review --quick",),
    )
    assert seen == [
        (True, ("/code-review --quick",)),
        (True, ("/code-review --quick",)),
    ]
