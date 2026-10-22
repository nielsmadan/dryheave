from functools import lru_cache
from importlib.resources import files

from dryheave.experiment_models import PriceTable
from dryheave.serialization import parse_model

BUILTIN_PRICE_TABLE_VERSION = "list-2026-10-16"


@lru_cache(maxsize=1)
def builtin_price_table() -> PriceTable:
    content = (
        files("dryheave")
        .joinpath("resources", "prices", f"{BUILTIN_PRICE_TABLE_VERSION}.json")
        .read_bytes()
    )
    return parse_model(content, PriceTable)
