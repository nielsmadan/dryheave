from collections import defaultdict
from collections.abc import Iterable

from dryheave.controller_models import ControllerRecipe, RoleCall
from dryheave.drivers.models import NativeEvent, NativeUsage
from dryheave.experiment_models import PriceTable
from dryheave.models import TokenUsage
from dryheave.result_models import RoleMetrics, UsageCharge
from dryheave.runner_models import CapturedAttempt
from dryheave.serialization import canonical_json, digest

TOKEN_FIELDS = ("uncached_input", "cache_read", "cache_write", "output", "reasoning")
BILLABLE_FIELDS = TOKEN_FIELDS[:-1]


def token_sum(usages: Iterable[TokenUsage | None]) -> TokenUsage:
    values: dict[str, list[int]] = defaultdict(list)
    for usage in usages:
        if usage is not None:
            for field in TOKEN_FIELDS:
                value = getattr(usage, field)
                if value is not None:
                    values[field].append(value)
    totals = {field: sum(values[field]) if values[field] else None for field in TOKEN_FIELDS}
    output, reasoning = totals["output"], totals["reasoning"]
    if output is not None and reasoning is not None and reasoning > output:
        totals["reasoning"] = None
    return TokenUsage(
        **totals,
        provenance="Sum of observed non-overlapping categories; null categories remain unobserved.",
    )


def _charge(record: UsageCharge, table: PriceTable | None) -> UsageCharge:
    model = record.observed_model or (
        record.requested_model if record.model_basis == "requested-root" else None
    )
    if record.cost is not None or table is None or model is None or record.usage is None:
        return record
    rate = next((rate for rate in table.rates if rate.model == model), None)
    if rate is None or any(getattr(record.usage, name) is None for name in BILLABLE_FIELDS):
        return record
    cost = sum(getattr(record.usage, name) * getattr(rate, name) for name in BILLABLE_FIELDS) / 1e6
    return record.model_copy(
        update={
            "cost": cost,
            "cost_kind": "estimated",
            "currency": table.currency,
            "price_version": table.version,
            "price_date": table.effective_date,
        }
    )


def summarize_role(
    role: str, records: tuple[UsageCharge, ...], *, partial: bool, reason: str
) -> RoleMetrics:
    currencies = {item.currency for item in records if item.cost is not None}
    costs = [item.cost for item in records if item.cost is not None]
    fixture = bool(records) and all(item.model_basis == "scripted" for item in records)
    tokens = token_sum(record.usage for record in records)
    complete = bool(records) and all(
        item.usage is not None
        and all(getattr(item.usage, field) is not None for field in BILLABLE_FIELDS)
        and item.cost is not None
        for item in records
    )
    return RoleMetrics.model_validate(
        {
            "role": role,
            "records": records,
            "known_tokens": tokens,
            "known_cost": sum(costs) if costs and len(currencies) == 1 else None,
            "currency": next(iter(currencies)) if len(currencies) == 1 else None,
            "coverage": "fixture"
            if fixture
            else "complete"
            if complete and not partial
            else "partial"
            if records
            else "unknown",
            "reasons": (reason,),
        }
    )


def fixture_charge(identity: str, reason: str) -> UsageCharge:
    return UsageCharge(
        identity=identity,
        usage=TokenUsage(
            uncached_input=0,
            cache_read=0,
            cache_write=0,
            output=0,
            reasoning=0,
            provenance="Explicit non-model fixture.",
        ),
        model_basis="scripted",
        cost=0.0,
        cost_kind="fixture",
        raw_evidence_sha256=digest(reason.encode()),
        reason=reason,
    )


def _cumulative(records: list[NativeUsage]) -> tuple[list[NativeUsage], bool]:
    previous: TokenUsage | None = None
    converted = []
    reset = False
    for record in records:
        current = record.usage
        if current is None:
            converted.append(record)
            continue
        values = {}
        decreased = previous is not None and any(
            getattr(current, field) is not None
            and getattr(previous, field) is not None
            and getattr(current, field) < getattr(previous, field)
            for field in BILLABLE_FIELDS
        )
        for field in TOKEN_FIELDS:
            value = getattr(current, field)
            before = getattr(previous, field) if previous else None
            values[field] = (
                value
                if decreased or previous is None
                else (
                    value - before
                    if value is not None and before is not None and value >= before
                    else None
                )
            )
        output, reasoning = values["output"], values["reasoning"]
        if output is not None and reasoning is not None and reasoning > output:
            values["reasoning"] = None
        converted.append(
            record.model_copy(
                update={
                    "usage": TokenUsage(
                        **values,
                        provenance="Observed cumulative delta; resets begin a new partial segment.",
                    )
                }
            )
        )
        previous = current
        reset = reset or decreased
    return converted, reset


def _native_records(capture: CapturedAttempt) -> tuple[list[NativeUsage], bool]:
    groups: dict[tuple[str | None, str | None], list[NativeUsage]] = defaultdict(list)
    for item in {
        record.identity: record for record in (*capture.usage.records, *capture.usage.unattributed)
    }.values():
        groups[(item.session_id, item.thread_id)].append(item)
    output = []
    reset = False
    for records in groups.values():
        responses = [item for item in records if item.accounting == "response"]
        if responses:
            output.extend(responses)
        else:
            cumulative, changed = _cumulative(
                [item for item in records if item.accounting == "cumulative"]
            )
            output.extend(cumulative)
            reset = reset or changed
        output.extend(item for item in records if item.accounting == "unknown")
    return output, reset


def subject_metrics(
    capture: CapturedAttempt | None,
    table: PriceTable | None,
    events: tuple[NativeEvent, ...] = (),
    requested_model: str | None = None,
) -> RoleMetrics:
    if capture is None:
        return summarize_role(
            "subject",
            (),
            partial=True,
            reason="No independently verified subject usage evidence is available.",
        )
    if capture.mode == "offline-fixture":
        return summarize_role(
            "subject",
            (fixture_charge("fixture-subject", "Explicit offline subject executed no model."),),
            partial=False,
            reason="Explicit offline subject fixture.",
        )
    records, reset = _native_records(capture)
    charges = []
    root = capture.launch.observed_session_id if capture.launch else None
    requested = requested_model
    for record in records:
        observed = None
        is_root = root is not None and record.session_id == root
        for event in events:
            if (
                event.kind == "metadata"
                and record.session_id is not None
                and event.session_id == record.session_id
                and event.source == record.source
                and event.line <= record.line
            ):
                model = event.data.get("model")
                if isinstance(model, str):
                    observed = model
        if observed is None and is_root and capture.launch:
            observed = capture.launch.observed_model
        usage = (
            record.usage.model_copy(
                update={"provenance": "Native usage record " + digest(canonical_json(record))}
            )
            if record.usage
            else None
        )
        charges.append(
            _charge(
                UsageCharge(
                    identity=digest(record.identity.encode()),
                    usage=usage,
                    observed_model=observed,
                    requested_model=requested if is_root else None,
                    model_basis="observed"
                    if observed
                    else "requested-root"
                    if is_root and requested
                    else "unknown",
                    raw_evidence_sha256=digest(canonical_json(record)),
                    reason="Observed native usage; descendant model is unknown unless metadata establishes it.",
                ),
                table,
            )
        )
    return summarize_role(
        "subject",
        tuple(charges),
        partial=True,
        reason=capture.usage.reason
        + (
            " Cumulative reset observed; known segments retained with partial coverage."
            if reset
            else ""
        ),
    )


def call_metrics(
    role: str,
    calls: tuple[RoleCall, ...],
    recipe: ControllerRecipe | None,
    table: PriceTable | None,
) -> RoleMetrics:
    if recipe is None and not calls:
        return summarize_role(
            role,
            (
                fixture_charge(
                    "not-configured", "This role was not configured and made no model call."
                ),
            ),
            partial=False,
            reason="Role not configured.",
        )
    charges = []
    for call in calls:
        if recipe is not None and recipe.kind == "scripted" and call.status == "completed":
            charges.append(
                fixture_charge(call.call_id, "Explicit scripted simulator made no model call.")
            )
            continue
        charges.append(
            _charge(
                UsageCharge(
                    identity=call.call_id,
                    usage=call.usage,
                    observed_model=call.observed_model,
                    requested_model=recipe.model if recipe else None,
                    model_basis="observed"
                    if call.observed_model
                    else "requested-root"
                    if recipe and recipe.model
                    else "unknown",
                    cost=call.cost,
                    cost_kind="provider" if call.cost is not None else "unknown",
                    raw_evidence_sha256=digest(canonical_json(call)),
                    reason=call.usage_reason,
                ),
                table,
            )
        )
    if not calls and recipe is not None:
        charges.append(
            fixture_charge("no-calls", "Durable assessment observed no calls for this role.")
        )
    return summarize_role(
        role,
        tuple(charges),
        partial=any(call.status != "completed" for call in calls),
        reason="Every durable call intent is retained, including failed or interrupted calls; unknown usage and costs remain null.",
    )
