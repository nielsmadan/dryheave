from typing import Literal, Self

from pydantic import Field, model_validator

from dryheave.models import ObjectId, RelativePath, StrictModel


class CapturedFile(StrictModel):
    path: RelativePath
    blob: RelativePath
    mode: Literal["file", "executable", "symlink"]
    sha256: ObjectId
    size: int = Field(ge=0)


class CaptureOmission(StrictModel):
    path: str
    reason: str
    intentional: bool = False


class WorkspaceCapture(StrictModel):
    directories: tuple[RelativePath, ...] = ()
    git_directories: tuple[RelativePath, ...] = ()
    files: tuple[CapturedFile, ...] = ()
    git_files: tuple[CapturedFile, ...] = ()
    omissions: tuple[CaptureOmission, ...] = ()
    patch_file: RelativePath | None = None
    git_inventory_file: RelativePath | None = None
    complete: bool
    git_complete: bool

    @model_validator(mode="after")
    def exact_entries(self) -> Self:
        for directories, entries in (
            (self.directories, self.files),
            (self.git_directories, self.git_files),
        ):
            if len(set(directories)) != len(directories):
                raise ValueError("captured directories must be distinct")
            names = {entry.path for entry in entries}
            if any(
                directory in names or any(directory.startswith(name + "/") for name in names)
                for directory in directories
            ):
                raise ValueError("captured directories collide with files")
        if any(
            ".git" in path.split("/")
            for path in (*self.directories, *(entry.path for entry in self.files))
        ):
            raise ValueError("workspace content cannot contain Git metadata")
        for entries, prefix in ((self.files, "workspace"), (self.git_files, "git")):
            paths = set()
            for entry in entries:
                if entry.path in paths or entry.blob != f"{prefix}/{entry.path}":
                    raise ValueError("capture paths must be distinct and match their blob prefix")
                paths.add(entry.path)
        if self.complete and any(
            not omission.intentional and not omission.path.startswith(".git")
            for omission in self.omissions
        ):
            raise ValueError("complete capture cannot contain task-file failures")
        return self
