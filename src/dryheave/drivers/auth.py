import os
import stat
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Self

from dryheave.errors import InputError
from dryheave.filesystem import directory_fd
from dryheave.profile_models import LaunchPlan


class RuntimeBindings:
    def __init__(self, plan: LaunchPlan, inherited: Mapping[str, str]) -> None:
        self.plan, self.inherited = plan, inherited
        self.owned: dict[str, tuple[int, int, int]] = {}
        self.errors: list[str] = []

    def __enter__(self) -> Self:
        try:
            for reference in self.plan.runtime_files:
                source = self.inherited.get(reference.source_path_environment.name)
                if source is None:
                    raise InputError(f"Runtime file reference is unavailable: {reference.name}")
                if not Path(source).is_absolute() or "\x00" in source:
                    raise InputError(
                        f"Runtime file reference needs an absolute path: {reference.name}"
                    )
                config = self.plan.config_roots.get("config")
                if config is None:
                    raise InputError("Launch plan has no owned config root for runtime binding.")
                with directory_fd(Path(config)) as descriptor:
                    os.symlink(source, reference.target, dir_fd=descriptor)
                    identity = os.stat(reference.target, dir_fd=descriptor, follow_symlinks=False)
                    self.owned[reference.target] = (
                        identity.st_dev,
                        identity.st_ino,
                        identity.st_ctime_ns,
                    )
                    os.fsync(descriptor)
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> tuple[str, ...]:
        config = self.plan.config_roots.get("config")
        if config is None or not self.owned:
            return tuple(self.errors)
        with directory_fd(Path(config)) as descriptor:
            for target, expected in tuple(self.owned.items()):
                try:
                    identity = os.stat(target, dir_fd=descriptor, follow_symlinks=False)
                    if (
                        not stat.S_ISLNK(identity.st_mode)
                        or (identity.st_dev, identity.st_ino, identity.st_ctime_ns) != expected
                    ):
                        self.errors.append("runtime_binding_replaced")
                        continue
                    os.unlink(target, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileNotFoundError:
                    pass
                finally:
                    del self.owned[target]
        return tuple(self.errors)
