import json
from datetime import UTC, datetime

from dryheave.assessment_inputs import AssessmentInputs, role_metrics
from dryheave.controller_models import RoleCall
from dryheave.models import TokenUsage
from dryheave.pricing import builtin_price_table
from dryheave.runner_models import AttemptState


def _state(call: RoleCall) -> AttemptState:
    return AttemptState(
        attempt_id="a1",
        trial_id="t1",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        workspace="/w",
        calls=(call,),
    )


def _usage(**overrides: int) -> TokenUsage:
    base = {"uncached_input": 1, "cache_read": 0, "cache_write": 100, "output": 1, "reasoning": 0}
    base.update(overrides)
    return TokenUsage(provenance="fixture", **base)


def test_role_metrics_falls_back_to_the_builtin_price_table_when_scoring_has_none():
    call = RoleCall(
        call_id="c-test",
        status="completed",
        usage=_usage(),
        observed_model="claude-sonnet-5",
    )
    inputs = AssessmentInputs()
    roles = role_metrics(inputs, _state(call), ())
    simulator = next(role for role in roles if role.role == "simulator")
    assert simulator.records[0].price_version == builtin_price_table().version


def test_role_metrics_derives_1h_split_for_a_saved_simulator_call_from_raw_evidence():
    call = RoleCall(
        call_id="c-old",
        status="completed",
        usage=_usage(cache_write=100),
        observed_model="claude-sonnet-5",
    )
    raw_stdout = json.dumps(
        {"usage": {"cache_creation": {"ephemeral_1h_input_tokens": 40}}}
    ).encode()
    inputs = AssessmentInputs(files={"evidence/controller/c-old/stdout.bin": raw_stdout})
    roles = role_metrics(inputs, _state(call), ())
    simulator = next(role for role in roles if role.role == "simulator")
    assert simulator.known_tokens.cache_write_1h == 40


def test_role_metrics_does_not_derive_a_1h_split_that_exceeds_cache_write():
    call = RoleCall(
        call_id="c-old",
        status="completed",
        usage=_usage(cache_write=10),
        observed_model="claude-sonnet-5",
    )
    raw_stdout = json.dumps(
        {"usage": {"cache_creation": {"ephemeral_1h_input_tokens": 40}}}
    ).encode()
    inputs = AssessmentInputs(files={"evidence/controller/c-old/stdout.bin": raw_stdout})
    roles = role_metrics(inputs, _state(call), ())
    simulator = next(role for role in roles if role.role == "simulator")
    assert simulator.known_tokens.cache_write_1h is None
