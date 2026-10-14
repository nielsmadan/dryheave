from collections import defaultdict
from collections.abc import Iterable

from pydantic import JsonValue

from dryheave.controller_models import ControllerRecipe, RoleCall
from dryheave.drivers.models import NativeEvent, NativeUsage
from dryheave.errors import DryheaveError
from dryheave.experiment_models import PriceRate, PriceTable, TokenRates
from dryheave.models import TokenUsage
from dryheave.result_models import RoleMetrics, UsageCharge
from dryheave.runner_models import CapturedAttempt
from dryheave.serialization import canonical_json, digest, parse_json

TOKEN_FIELDS = ("uncached_input", "cache_read", "cache_write", "output", "reasoning")
BILLABLE_FIELDS = TOKEN_FIELDS[:-1]


def token_sum(usages: Iterable[TokenUsage | None]) -> TokenUsage:
    values: dict[str, list[int]] = defaultdict(list)
    cache_write_1h: list[int] = []
    cache_write_1h_known = True
    for usage in usages:
        if usage is not None:
            for field in TOKEN_FIELDS:
                value = getattr(usage, field)
                if value is not None:
                    values[field].append(value)
            if usage.cache_write is not None:
                if usage.cache_write_1h is None:
                    cache_write_1h_known = False
                else:
                    cache_write_1h.append(usage.cache_write_1h)
    totals = {field: sum(values[field]) if values[field] else None for field in TOKEN_FIELDS}
    output, reasoning = totals["output"], totals["reasoning"]
    if output is not None and reasoning is not None and reasoning > output:
        totals["reasoning"] = None
    totals["cache_write_1h"] = (
        sum(cache_write_1h) if cache_write_1h_known and cache_write_1h else None
    )
    return TokenUsage(
        **totals,
        provenance="Sum of observed non-overlapping categories; null categories remain unobserved.",
    )


def _write_cost(usage: TokenUsage, rate: TokenRates) -> tuple[float, str | None]:
    cache_write = usage.cache_write or 0
    if usage.cache_write_1h is not None and rate.cache_write_1h is not None:
        return (
            (cache_write - usage.cache_write_1h) * rate.cache_write
            + usage.cache_write_1h * rate.cache_write_1h
        ), None
    if cache_write and usage.cache_write_1h is None:
        return cache_write * rate.cache_write, (
            "The 1-hour cache-write split is unknown for this record; "
            "priced entirely at the 5-minute rate."
        )
    if cache_write and rate.cache_write_1h is None:
        return cache_write * rate.cache_write, (
            "The price table has no 1-hour cache-write rate; priced entirely at the 5-minute rate."
        )
    return cache_write * rate.cache_write, None


def cache_write_1h_from_raw(raw: dict[str, JsonValue]) -> int | None:
    creation = raw.get("cache_creation")
    if not isinstance(creation, dict):
        return None
    value = creation.get("ephemeral_1h_input_tokens")
    return value if type(value) is int and value >= 0 else None


def call_usage_with_raw_split(
    usage: TokenUsage | None, raw_stdout: bytes | None
) -> TokenUsage | None:
    if usage is None or usage.cache_write_1h is not None or usage.cache_write is None:
        return usage
    if raw_stdout is None:
        return usage
    try:
        content = parse_json(raw_stdout)
    except DryheaveError:
        return usage
    values = content.get("usage")
    if not isinstance(values, dict):
        return usage
    derived = cache_write_1h_from_raw(values)
    if derived is None or derived > usage.cache_write:
        return usage
    return usage.model_copy(update={"cache_write_1h": derived})


def total_input(usage: TokenUsage) -> int | None:
    if usage.uncached_input is None or usage.cache_read is None or usage.cache_write is None:
        return None
    return usage.uncached_input + usage.cache_read + usage.cache_write


def _tier(
    rate: PriceRate, usage: TokenUsage, *, per_request: bool
) -> tuple[TokenRates | None, str]:
    long_prompt = rate.long_prompt
    if long_prompt is None:
        return rate, ""
    total = total_input(usage)
    if not per_request or total is None:
        return None, (
            " The price depends on each request's total input, and this record is not known "
            "to cover a single request."
        )
    if total > long_prompt.above_total_input_tokens:
        return long_prompt, (
            f" Priced at the long-prompt rates: total input of {total} tokens exceeds "
            f"{long_prompt.above_total_input_tokens}."
        )
    return rate, ""


def _charge(record: UsageCharge, table: PriceTable | None, *, per_request: bool) -> UsageCharge:
    model = record.observed_model or (
        record.requested_model if record.model_basis == "requested-root" else None
    )
    if record.cost is not None or table is None or model is None or record.usage is None:
        return record
    rate = next((rate for rate in table.rates if rate.model == model), None)
    if rate is None or any(getattr(record.usage, name) is None for name in BILLABLE_FIELDS):
        return record
    tier, tier_reason = _tier(rate, record.usage, per_request=per_request)
    if tier is None:
        return record.model_copy(update={"reason": record.reason + tier_reason})
    write_cost, write_reason = _write_cost(record.usage, tier)
    cost = (
        sum(
            getattr(record.usage, name) * getattr(tier, name)
            for name in BILLABLE_FIELDS
            if name != "cache_write"
        )
        + write_cost
    ) / 1e6
    return record.model_copy(
        update={
            "cost": cost,
            "cost_kind": "estimated",
            "currency": table.currency,
            "price_version": table.version,
            "price_date": table.effective_date,
            "reason": record.reason + tier_reason + (" " + write_reason if write_reason else ""),
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
        for field in (*TOKEN_FIELDS, "cache_write_1h"):
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
        cache_write, cache_write_1h = values["cache_write"], values["cache_write_1h"]
        if cache_write is not None and cache_write_1h is not None and cache_write_1h > cache_write:
            values["cache_write_1h"] = None
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
        if usage is not None and usage.cache_write_1h is None and usage.cache_write is not None:
            derived = cache_write_1h_from_raw(record.raw)
            if derived is not None and derived <= usage.cache_write:
                usage = usage.model_copy(update={"cache_write_1h": derived})
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
                per_request=record.accounting == "response",
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
    raw_stdout: dict[str, bytes] | None = None,
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
        usage = call_usage_with_raw_split(call.usage, (raw_stdout or {}).get(call.call_id))
        charges.append(
            _charge(
                UsageCharge(
                    identity=call.call_id,
                    usage=usage,
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
                per_request=False,
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
