import math

import pytest

from conftest import ExamplePayload
from dryheave.errors import InputError
from dryheave.serialization import canonical_json, digest, parse_json, parse_model


def test_canonical_json_orders_nested_keys_and_keeps_unicode() -> None:
    left = {"z": {"β": 2, "a": 1}, "a": "ä"}
    right = {"a": "ä", "z": {"a": 1, "β": 2}}
    assert canonical_json(left) == canonical_json(right)
    assert canonical_json(left) == '{"a":"ä","z":{"a":1,"β":2}}'.encode()
    assert digest(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


@pytest.mark.parametrize(
    "data",
    [
        b'{"x":1,"x":2}',
        b'{"x":{"a":1,"a":2}}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b"[]",
        b"null",
        b"{",
        b"\xff",
    ],
)
def test_json_rejects_ambiguous_or_invalid_inputs(data: bytes) -> None:
    with pytest.raises(InputError):
        parse_json(data)


@pytest.mark.parametrize("number", ["1e400", "-1e400"])
def test_json_rejects_nested_overflow_without_echoing_values(number: str) -> None:
    data = f'{{"sensitive-key":[{{"value":{number}}}]}}'.encode()
    with pytest.raises(InputError) as caught:
        parse_json(data)
    assert str(caught.value) == "JSON contains a non-finite numeric value."


def test_json_preserves_finite_floats() -> None:
    assert parse_json(b'{"values":[1.5,1e308,-1e308]}') == {"values": [1.5, 1e308, -1e308]}


@pytest.mark.parametrize("value", [math.nan, math.inf, "\ud800"])
def test_canonical_json_rejects_non_json_values(value: object) -> None:
    with pytest.raises(InputError):
        canonical_json({"value": value})


def test_schema_error_does_not_echo_supplied_values() -> None:
    with pytest.raises(InputError) as caught:
        parse_model(b'{"title":123,"count":"sensitive-value"}', ExamplePayload)
    assert str(caught.value) == "Invalid schema: title: string_type; count: int_type"


def test_model_round_trip(payload: ExamplePayload) -> None:
    assert parse_model(canonical_json(payload), ExamplePayload) == payload
