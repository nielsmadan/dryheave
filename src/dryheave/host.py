from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HostContext:
    environment: Mapping[str, str]
    home: Path
