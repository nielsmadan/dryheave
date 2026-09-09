from pydantic import JsonValue

from dryheave.models import StrictModel


def _required(value: JsonValue) -> None:
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict):
            value["required"] = list(properties)
        value.pop("default", None)
        for child in value.values():
            _required(child)
    elif isinstance(value, list):
        for child in value:
            _required(child)


def controller_schema(model: type[StrictModel]) -> dict[str, JsonValue]:
    schema = model.model_json_schema()
    _required(schema)
    return schema
