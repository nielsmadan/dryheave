import os
import stat
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self

from dryheave.codex_policy import CODEX_HOME_DIRECTORY, CODEX_HOME_VARIABLE
from dryheave.errors import InputError
from dryheave.filesystem import configured_directory, directory_fd, expand_home
from dryheave.profile_models import RuntimeFileReference, codex_auth_reference


class BindingPlan(Protocol):
    @property
    def config_roots(self) -> dict[str, str]: ...

    @property
    def runtime_files(self) -> tuple[RuntimeFileReference, ...]: ...


def _codex_home_source(variable: str, environment: Mapping[str, str]) -> Path:
    configured = environment.get(variable)
    home = Path(value) if (value := environment.get("HOME")) else None
    if configured:
        if not configured.startswith("~") and not Path(configured).is_absolute():
            raise InputError(
                f"{variable} must be an absolute path or start with ~/, not {configured}."
            )
        return expand_home(configured, home)
    return (
        configured_directory(environment, CODEX_HOME_VARIABLE, home, CODEX_HOME_DIRECTORY)
        / "auth.json"
    )


def runtime_file_source(reference: RuntimeFileReference, environment: Mapping[str, str]) -> str:
    variable = reference.source_path_environment.name
    if reference.default_source == "codex-home":
        path = _codex_home_source(variable, environment)
        if not path.exists():
            raise InputError(
                f"Codex auth file {path} does not exist; log in with Codex or set "
                f"{variable} to the auth.json path Codex variants should use."
            )
        return str(path)
    source = environment.get(variable)
    if source is None:
        raise InputError(
            f"Runtime file reference is unavailable: {reference.name}; "
            f"set {variable} to its absolute path."
        )
    return source


def check_codex_auth(environment: Mapping[str, str]) -> None:
    runtime_file_source(codex_auth_reference(), environment)


class RuntimeBindings:
    def __init__(self, plan: BindingPlan, inherited: Mapping[str, str]) -> None:
        self.plan, self.inherited = plan, inherited
        self.owned: dict[str, tuple[int, int, int]] = {}
        self.errors: list[str] = []

    def __enter__(self) -> Self:
        try:
            for reference in self.plan.runtime_files:
                source = runtime_file_source(reference, self.inherited)
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
