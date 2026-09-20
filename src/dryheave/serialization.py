import hashlib
import json
import math
from collections.abc import Iterable
from typing import cast

from pydantic import JsonValue, ValidationError

from dryheave.errors import InputError
from dryheave.models import StrictModel


def canonical_json(value: StrictModel | dict[str, JsonValue]) -> bytes:
    payload = (
        value.model_dump(mode="json", warnings=False) if isinstance(value, StrictModel) else value
    )
    try:
        return json.dumps(
            payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as error:
        raise InputError("Value cannot be represented as canonical UTF-8 JSON.") from error


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def digest_chunks(chunks: Iterable[bytes]) -> str:
    hasher = hashlib.sha256()
    for chunk in chunks:
        hasher.update(chunk)
    return hasher.hexdigest()


def _unique_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise InputError("JSON contains duplicate object keys.")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise InputError("JSON contains a non-finite numeric value.")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise InputError("JSON contains a non-finite numeric value.")
    return parsed


def parse_json(content: bytes) -> dict[str, JsonValue]:
    try:
        value = json.loads(
            content,
            object_pairs_hook=_unique_keys,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        raise InputError("Expected valid UTF-8 JSON.") from error
    if not isinstance(value, dict):
        raise InputError("Expected a JSON object.")
    return cast("dict[str, JsonValue]", value)


def validation_message(error: ValidationError) -> str:
    fields = [
        f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['type']}"
        for item in error.errors(include_url=False, include_context=False, include_input=False)
    ]
    return "Invalid schema: " + "; ".join(fields)


def parse_model[T: StrictModel](content: bytes, model: type[T]) -> T:
    parse_json(content)
    try:
        return model.model_validate_json(content)
    except ValidationError as error:
        raise InputError(validation_message(error)) from error
