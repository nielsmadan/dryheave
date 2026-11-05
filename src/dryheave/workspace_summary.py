import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from dryheave.menu import CHECK
from dryheave.models import StrictModel
from dryheave.skills import SkillRefresh
from dryheave.tui_test_release import TUI_TEST_ACTION

VOICE_SKILL = "dryheave-voice-profile"
START = "Start Claude Code, Codex or your coding agent in the workspace."
VOICE_STEP = f"Run the {VOICE_SKILL} skill to build your voice from your session logs"
INVOCATIONS = (("Claude Code", f"/{VOICE_SKILL}"), ("Codex", f"${VOICE_SKILL}"))
OTHER_AGENTS = "Other agents"
REFUSED_HINT = "move a refused skill folder aside, then run dryheave update"

type Repository = Literal["created", "existing", "skipped"]
REPOSITORIES: dict[str, Repository] = {
    "created": "created",
    "existing": "existing",
    "skipped": "skipped",
}


def repository_state(value: JsonValue) -> Repository:
    return REPOSITORIES[str(value)]


@dataclass(frozen=True)
class CommitView:
    mode: str
    message: str
    state: str | None
    sha: str | None
    error: str | None
    paths: tuple[str, ...]
    ignored: tuple[str, ...]


def _text(values: Mapping[str, JsonValue], key: str) -> str | None:
    value = values.get(key)
    return value if isinstance(value, str) else None


def _texts(values: Mapping[str, JsonValue], key: str) -> tuple[str, ...]:
    value = values.get(key)
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()


def commit_view(result: Mapping[str, JsonValue]) -> CommitView:
    commit = result.get("commit")
    nested: Mapping[str, JsonValue] = commit if isinstance(commit, dict) else {}
    return CommitView(
        mode=_text(result, "git_commit") or "off",
        message=_text(result, "message") or "",
        state=_text(nested, "state"),
        sha=_text(nested, "commit"),
        error=_text(nested, "error"),
        paths=_texts(result, "commit_paths"),
        ignored=_texts(result, "ignored_paths"),
    )


def shown(path: Path, base: Path) -> str:
    return str(path.relative_to(base)) if path.is_relative_to(base) else str(path)


def shell_path(path: str) -> str:
    return shlex.quote(f"./{path}" if path.startswith("-") else path)


def _names(names: Sequence[str]) -> str:
    return ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 else "".join(names)


def commit_hint(view: CommitView, root: Path, cwd: Path) -> str:
    git = "git" if cwd == root else f"git -C {shlex.quote(str(root))}"
    paths = " ".join(shlex.quote(path) for path in view.paths)
    return (
        f"{git} --literal-pathspecs add -- {paths} && "
        f"{git} --literal-pathspecs commit -m {shlex.quote(view.message)} -- {paths}"
    )


def commit_text(view: CommitView, root: Path, cwd: Path) -> str | None:
    if view.state == "committed" and view.sha is not None:
        done: str | None = f"committed {view.sha[:7]} {view.message}"
    elif view.state == "failed":
        done = f"the commit failed: {view.error}"
    elif view.state is None and view.mode == "ask" and view.paths:
        done = f"not committed; to commit: {commit_hint(view, root, cwd)}"
    else:
        done = None
    ignored = f"git ignores {_names(view.ignored)}, so it was left out" if view.ignored else None
    return "; ".join(part for part in (done, ignored) if part) or None


def refused_lines(skills: Sequence[SkillRefresh], root: Path, verb: str) -> list[str]:
    lines = [
        line
        for item in skills
        for line in (
            [f"not {verb} in {shown(item.target, root)}: {item.error}"]
            if item.error is not None
            else [
                f"{conflict.name} not {verb} in {shown(item.target, root)}: {conflict.reason}"
                for conflict in item.conflicts
            ]
        )
    ]
    return [*lines, REFUSED_HINT] if any(item.conflicts for item in skills) else lines


def _rows(label: str, values: Sequence[str]) -> list[str]:
    return [f"  {label if index == 0 else '':<9} {value}" for index, value in enumerate(values)]


@dataclass(frozen=True)
class InitOutcome:
    root: Path
    repository: Repository
    commit: Mapping[str, JsonValue]
    skills: Sequence[SkillRefresh]
    tui_test: Path | None
    installed_version: str | None
    ready: bool

    @property
    def skill_folders(self) -> list[str]:
        return [shown(item.target, self.root) for item in self.skills if item.error is None]

    @property
    def next_text(self) -> str:
        invocations = ", ".join(f"{command} in {agent}" for agent, command in INVOCATIONS)
        folders = self.skill_folders
        located = f" {OTHER_AGENTS} find the skills in {_names(folders)}." if folders else ""
        steps = f"{START} {VOICE_STEP}: {invocations}.{located}"
        return steps if self.ready else f"{steps} Before native runs: {TUI_TEST_ACTION}"


def git_line(outcome: InitOutcome, cwd: Path) -> str:
    if outcome.repository == "skipped":
        return "no repository (git not found)"
    repository = "new repository" if outcome.repository == "created" else "existing repository"
    view = commit_view(outcome.commit)
    text = commit_text(view, outcome.root, cwd)
    return f"{repository}{f', {text}' if text else ''} (checkpoint commits: {view.mode})"


def skills_lines(outcome: InitOutcome) -> list[str]:
    folders = outcome.skill_folders
    return [
        *([f"installed in {_names(folders)}"] if folders else []),
        *refused_lines(outcome.skills, outcome.root, "installed"),
    ]


def tui_test_line(outcome: InitOutcome) -> str:
    if not outcome.ready or outcome.tui_test is None:
        return f"no supported version found. {TUI_TEST_ACTION}"
    if outcome.installed_version is not None:
        return f"{outcome.installed_version} installed at {outcome.tui_test}"
    return str(outcome.tui_test)


def next_steps(outcome: InitOutcome, cwd: Path) -> list[str]:
    enter = [f"cd {shell_path(shown(outcome.root, cwd))}"] if outcome.root != cwd else []
    folders = outcome.skill_folders
    examples = [
        *(f"{agent:<13} {command}" for agent, command in INVOCATIONS),
        *([f"{OTHER_AGENTS:<13} the skills are in {_names(folders)}"] if folders else []),
    ]
    return [*enter, START, "\n       ".join([f"{VOICE_STEP}:", *examples])]


def render_init(outcome: InitOutcome, cwd: Path) -> str:
    steps = next_steps(outcome, cwd)
    return "\n".join(
        [
            f"{CHECK} Workspace ready: {outcome.root}",
            *_rows("git", [git_line(outcome, cwd)]),
            *_rows("skills", skills_lines(outcome)),
            *_rows("tui-test", [tui_test_line(outcome)]),
            "",
            "Next steps:",
            *(f"  {number}. {step}" for number, step in enumerate(steps, start=1)),
        ]
    )


class UpdateOutcome(StrictModel):
    path: Path
    gitignore_added: tuple[str, ...]
    skill_installs: tuple[SkillRefresh, ...]
    commit: dict[str, JsonValue]

    @property
    def updated(self) -> bool:
        return bool(self.gitignore_added) or any(item.changed for item in self.skill_installs)

    def as_json(self) -> dict[str, JsonValue]:
        fields: dict[str, JsonValue] = self.model_dump(mode="json", exclude={"commit"})
        return fields | {"updated": self.updated} | self.commit


def _update_header(outcome: UpdateOutcome) -> str:
    problems = any(item.problem for item in outcome.skill_installs)
    if outcome.updated:
        return (
            f"Workspace partly updated: {outcome.path}"
            if problems
            else f"{CHECK} Workspace updated: {outcome.path}"
        )
    return (
        f"Nothing updated in {outcome.path}; some skills need attention:"
        if problems
        else f"{CHECK} Workspace already up to date: {outcome.path}"
    )


def render_update(outcome: UpdateOutcome, cwd: Path) -> str:
    root = outcome.path
    refused = refused_lines(outcome.skill_installs, root, "updated")
    if not outcome.updated:
        return "\n".join([_update_header(outcome), *_rows("skills", refused)])
    skills = [
        f"{shown(item.target, root)}: "
        + (f"updated {_names(item.changed)}" if item.changed else "current")
        for item in outcome.skill_installs
        if item.error is None
    ]
    added = outcome.gitignore_added
    view = commit_view(outcome.commit)
    return "\n".join(
        [
            _update_header(outcome),
            *_rows("skills", [*skills, *refused]),
            *_rows("gitignore", [f"added {', '.join(added)}" if added else "current"]),
            *_rows(
                "git",
                [
                    commit_text(view, root, cwd)
                    or f"not committed (checkpoint commits: {view.mode})"
                ],
            ),
        ]
    )
