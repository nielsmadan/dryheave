import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import re
import shlex
from importlib.resources import files
from itertools import chain
from pathlib import Path

import pytest

from dryheave.cli import _global_arguments, build_parser
from dryheave.errors import ConflictError, InputError, LockBusyError, PathError
from dryheave.filesystem import directory_fd
from dryheave.skills import (
    OWNER_FILE,
    SKILL_NAMES,
    bundled_skill,
    list_skills,
    manage_skills,
    refresh_skills,
)


def test_packaged_skill_resources_have_matching_frontmatter_and_workflows():
    for name in SKILL_NAMES:
        content = bundled_skill(name)
        assert (
            content
            == files("dryheave").joinpath("resources", "skills", name, "SKILL.md").read_bytes()
        )
        text = content.decode()
        assert text.startswith(f"---\nname: {name}\ndescription: ")
        assert all(
            section in text for section in ("## Instructions", "## Examples", "## Troubleshooting")
        )
    with pytest.raises(InputError, match="Unknown"):
        bundled_skill("../other")


def test_process_skills_describe_pilot_budget_judge_and_checkpoint_keys():
    case = bundled_skill("dryheave-case").decode()
    for phrase in (
        "--variant pilot=",
        "--budget-from PILOT_RUN",
        "case calibrate NAME --judge MODEL[@EFFORT]",
        "`commit_paths`",
        "`git_commit`",
        "case: calibrate NAME",
        "`commit.state`",
        "dryheave transcript PILOT_RUN",
        "--all-stand-in --voice NEW_NAME",
    ):
        assert phrase in case
    results = bundled_skill("dryheave-results").decode()
    for phrase in ("independence", "`elicited`", "`corrected`", "`indeterminate`", "pilot (n=1)"):
        assert phrase in results
    voice = bundled_skill("dryheave-voice-profile").decode()
    assert "Pacing is the harness's job" in voice
    assert "voice: create NAME" in voice
    assert '"Yes, save and commit"' in voice
    assert "`/dryheave-collect` in Claude Code" in voice
    assert "## Finding a first case" in bundled_skill("dryheave-collect").decode()


def test_bundle_listing_and_missing_target_are_read_only(tmp_path):
    target = tmp_path / "absent"
    assert [item["name"] for item in list_skills()["skills"]] == list(SKILL_NAMES)
    assert [item["status"] for item in list_skills(target)["skills"]] == ["missing"] * len(
        SKILL_NAMES
    )
    assert list(tmp_path.iterdir()) == []


def test_install_updates_from_recorded_old_bytes_and_uninstalls_owned_files(tmp_path, monkeypatch):
    target = tmp_path / "skills"
    name = SKILL_NAMES[0]
    current = bundled_skill(name)
    old = current + b"\nOld packaged instructions.\n"
    with monkeypatch.context() as context:
        context.setattr("dryheave.skills.bundled_skill", lambda _name: old)
        manage_skills("install", target, (name,))
    path = target / name
    ownership = json.loads((path / OWNER_FILE).read_text())
    assert ownership["files"] == {"SKILL.md": hashlib.sha256(old).hexdigest()}
    assert list_skills(target)["skills"][0]["status"] == "outdated"
    assert manage_skills("update", target, (name,))["action"] == "update"
    assert (path / "SKILL.md").read_bytes() == current
    assert list_skills(target)["skills"][0]["status"] == "current"
    (target / "foreign.txt").write_bytes(b"preserve sibling")
    manage_skills("uninstall", target, (name,))
    assert {entry.name: entry.read_bytes() for entry in target.iterdir()} == {
        "foreign.txt": b"preserve sibling"
    }


@pytest.mark.parametrize("action", ["install", "update", "uninstall"])
@pytest.mark.parametrize(
    "change", ["edit", "extra", "missing", "owner", "unowned", "schema", "traversal"]
)
def test_batch_refuses_conflicts_before_changing_other_owned_skills(tmp_path, action, change):
    target = tmp_path / "skills"
    manage_skills("install", target)
    path = target / SKILL_NAMES[-1]
    if change == "edit":
        (path / "SKILL.md").write_bytes(b"User-edited bytes")
    elif change == "extra":
        (path / "notes.txt").write_bytes(b"Foreign notes")
    elif change == "missing":
        (path / "SKILL.md").unlink()
    else:
        owner = json.loads((path / OWNER_FILE).read_text())
        if change == "owner":
            owner["name"] = SKILL_NAMES[0]
        elif change == "unowned":
            owner.pop("owner")
        elif change == "schema":
            owner["schema_version"] = 2
        else:
            owner["files"] = {"../foreign": "a" * 64}
        (path / OWNER_FILE).write_text(json.dumps(owner))
    before = {
        str(entry.relative_to(target)): entry.read_bytes()
        for entry in target.rglob("*")
        if entry.is_file()
    }
    with pytest.raises((ConflictError, InputError)):
        manage_skills(action, target)
    after = {
        str(entry.relative_to(target)): entry.read_bytes()
        for entry in target.rglob("*")
        if entry.is_file()
    }
    assert after == before
    assert list_skills(target)["skills"][-1]["status"] == "conflict"


def test_foreign_directory_and_identical_unowned_bytes_are_never_adopted(tmp_path):
    target = tmp_path / "skills"
    path = target / SKILL_NAMES[-1]
    path.mkdir(parents=True)
    content = bundled_skill(SKILL_NAMES[-1])
    (path / "SKILL.md").write_bytes(content)
    for action in ("install", "update", "uninstall"):
        with pytest.raises((ConflictError, FileNotFoundError)):
            manage_skills(action, target)
        assert (path / "SKILL.md").read_bytes() == content
        assert set(target.iterdir()) == {path}


@pytest.mark.parametrize("part", ["target", "parent", "skill", "document", "manifest"])
def test_symlink_components_and_owned_entries_are_rejected_without_touching_destination(
    tmp_path, part
):
    target = tmp_path / "skills"
    manage_skills("install", target)
    name = SKILL_NAMES[0]
    if part in {"target", "parent"}:
        link = tmp_path / "link"
        link.symlink_to(target if part == "target" else tmp_path, target_is_directory=True)
        selected = link if part == "target" else link / "skills"
    else:
        selected = target
        original = target / name
        if part in {"document", "manifest"}:
            original /= "SKILL.md" if part == "document" else OWNER_FILE
        saved = tmp_path / "saved"
        original.rename(saved)
        original.symlink_to(saved, target_is_directory=part == "skill")
    before = {str(entry): entry.read_bytes() for entry in tmp_path.rglob("*") if entry.is_file()}
    with pytest.raises(PathError):
        manage_skills("update", selected, (name,))
    after = {str(entry): entry.read_bytes() for entry in tmp_path.rglob("*") if entry.is_file()}
    assert after == before


def test_unsafe_targets_and_names_are_rejected(tmp_path):
    for target in (Path("/"), tmp_path / ".." / "other"):
        with pytest.raises(PathError):
            manage_skills("install", target)
    for names in (("unknown",), (SKILL_NAMES[0], SKILL_NAMES[0])):
        with pytest.raises(InputError):
            manage_skills("install", tmp_path / "skills", names)
    assert list(tmp_path.iterdir()) == []


def test_concurrent_writer_lock_preserves_target(tmp_path):
    target = tmp_path / "skills"
    target.mkdir()
    with directory_fd(target) as descriptor:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LockBusyError):
            manage_skills("install", target)
    assert list(target.iterdir()) == []


def test_repeated_install_is_read_only_for_current_owned_skills(tmp_path):
    target = tmp_path / "skills"
    manage_skills("install", target)
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in target.rglob("*")
        if path.is_file()
    }
    result = manage_skills("install", target)
    assert result["skills"] == list(SKILL_NAMES)
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in target.rglob("*")
        if path.is_file()
    } == before


def test_install_refuses_outdated_owned_skills_without_overwriting(tmp_path, monkeypatch):
    target = tmp_path / "skills"
    name = SKILL_NAMES[0]
    old = bundled_skill(name) + b"\nOld bundle\n"
    with monkeypatch.context() as context:
        context.setattr("dryheave.skills.bundled_skill", lambda _name: old)
        manage_skills("install", target, (name,))
    with pytest.raises(ConflictError, match="skills update"):
        manage_skills("install", target)
    assert {path.name for path in target.iterdir()} == {name}
    assert (target / name / "SKILL.md").read_bytes() == old


def test_update_upgrades_three_owned_skills_and_installs_new_bundle_names(tmp_path, monkeypatch):
    target = tmp_path / "skills"
    old_names = ("dryheave-collect", "dryheave-case", "dryheave-results")
    contents = {name: bundled_skill(name) + b"\nPrevious bundle bytes.\n" for name in old_names}
    with monkeypatch.context() as context:
        context.setattr("dryheave.skills.bundled_skill", contents.__getitem__)
        manage_skills("install", target, old_names)
    (target / "personal-notes").write_text("Preserve unrelated user file")
    result = manage_skills("update", target)
    assert result["skills"] == list(SKILL_NAMES)
    assert [item["status"] for item in list_skills(target)["skills"]] == ["current"] * len(
        SKILL_NAMES
    )
    assert (target / "personal-notes").read_text() == "Preserve unrelated user file"


ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
FENCE = re.compile(r"^ *```[a-z]*\n(.*?)^ *```", re.DOTALL | re.MULTILINE)
INLINE = re.compile(r"`([^`]+)`")
PLACEHOLDER = re.compile(r"<[A-Za-z][A-Za-z-]*>|\b[A-Z][A-Z0-9_]*\b")
PLACEHOLDER_VALUES = {"AGENT": "codex"}
SHELL_OPERATORS = {">", "|", "&&", ";", "2>"}


def substitute(token: str) -> str:
    return PLACEHOLDER.sub(lambda match: PLACEHOLDER_VALUES.get(match.group(), "1"), token)


def command_argv(text: str) -> list[str] | None:
    words = shlex.split(
        text.replace("\\\n", " ").replace("[@EFFORT]", "").replace("[", "").replace("]", ""),
        comments=True,
    )
    if words[:1] != ["dryheave"]:
        return None
    kept: list[str] = []
    for word in words[1:]:
        if word in SHELL_OPERATORS:
            break
        if word not in {"...", "\u2026"}:
            kept.append(substitute(word))
    return kept


def documented_commands(text: str) -> list[str]:
    fenced = [block for match in FENCE.finditer(text) for block in match.group(1).splitlines()]
    joined = FENCE.sub("", text)
    inline = [" ".join(span.split()) for span in INLINE.findall(joined)]
    return [
        candidate.strip()
        for candidate in (*_continued(fenced), *inline)
        if candidate.strip().startswith("dryheave ")
    ]


def _continued(lines: list[str]) -> list[str]:
    merged: list[str] = []
    pending = ""
    for line in lines:
        stripped = line.strip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
        else:
            merged.append(pending + stripped)
            pending = ""
    return merged


def _option_strings(parser: argparse.ArgumentParser) -> set[str]:
    nested = (
        action.choices.values()
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return set(parser._option_string_actions) | {
        option for child in chain.from_iterable(nested) for option in _option_strings(child)
    }


def parse_documented(command: str):
    argv = command_argv(command)
    assert argv is not None
    parser = build_parser()
    unknown = [
        word
        for word in argv
        if word.startswith("--") and word.split("=")[0] not in _option_strings(parser)
    ]
    assert not unknown, f"{command!r} spells flags the CLI does not define: {unknown}"
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
        try:
            return parser.parse_args(_global_arguments(argv))
        except SystemExit as error:
            if error.code == 0:
                return None
            pytest.fail(f"{command!r} does not parse: {stderr.getvalue().strip()}")
        except InputError as error:
            pytest.fail(f"{command!r} does not parse: {error}")


DOCUMENTS = {
    "README.md": README.read_text(),
    "real-workflow.md": (ROOT / "src/dryheave/resources/examples/real-workflow.md").read_text(),
    **{path.name: path.read_text() for path in sorted((ROOT / "docs/user").glob("*.md"))},
    **{name: bundled_skill(name).decode() for name in SKILL_NAMES},
}
DOCUMENTED = [
    pytest.param(command, id=f"{source}:{command[:70]}")
    for source, text in DOCUMENTS.items()
    for command in dict.fromkeys(documented_commands(text))
]


def test_placeholder_scheme_fills_capitals_and_angle_names_with_one_or_a_choice():
    namespace = parse_documented(
        "dryheave bench my-case --variant pilot=MODEL[@EFFORT] --budget-from <RUN> "
        "--judge MODEL --repetitions N"
    )
    assert (namespace.command, namespace.variant, namespace.budget_from, namespace.repetitions) == (
        "bench",
        ["pilot=1"],
        "1",
        1,
    )
    assert parse_documented("dryheave collect select NAME FILE FILE --agent AGENT").agent == "codex"


def test_abbreviated_and_unknown_flags_are_rejected():
    with pytest.raises(AssertionError, match="--judg"):
        parse_documented("dryheave bench c --variant a=b --judg m --budget 1")


def test_continued_fenced_lines_and_shell_tails_are_one_command():
    block = "```sh\ndryheave report RUN \\\n  --html out.html > log.txt\n```"
    [command] = documented_commands(block)
    assert command_argv(command) == ["report", "1", "--html", "out.html"]


def test_documents_name_the_commands_the_process_relies_on():
    named = {
        " ".join(argv[:2]) if argv[0] in {"case", "voice", "store"} else argv[0]
        for command in dict.fromkeys(
            command for text in DOCUMENTS.values() for command in documented_commands(text)
        )
        if (argv := [word for word in (command_argv(command) or []) if not word.startswith("-")])
    }
    assert {
        "init",
        "voice create",
        "case draft",
        "case validate",
        "case freeze",
        "case calibrate",
        "store alias",
        "bench",
        "transcript",
        "replay",
        "assess",
        "report",
        "view",
    } <= named


@pytest.mark.parametrize("command", DOCUMENTED)
def test_documented_dryheave_invocations_parse(command):
    parse_documented(command)


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    return next(
        (
            dict(action.choices)
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ),
        {},
    )


def _command_parser(words: list[str]) -> argparse.ArgumentParser | None:
    parser = build_parser()
    current = None
    for word in words:
        child = _subcommands(current or parser).get(word)
        if child is None:
            break
        current = child
    return current


def unprefixed_mentions(text: str) -> list[str]:
    commands = set(_subcommands(build_parser()))
    spans = (" ".join(span.split()) for span in INLINE.findall(FENCE.sub("", text)))
    return [
        span
        for span in spans
        if not span.startswith("dryheave ")
        and len(span.split()) > 1
        and span.split()[0] in commands
    ]


UNPREFIXED = [
    pytest.param(span, id=f"{source}:{span[:70]}")
    for source, text in DOCUMENTS.items()
    for span in dict.fromkeys(unprefixed_mentions(text))
]


@pytest.mark.parametrize("span", UNPREFIXED)
def test_unprefixed_command_mentions_name_real_subcommands_and_flags(span):
    words = [
        word.strip("()[]")
        for word in shlex.split(span.replace("\\|", " ").replace("|", " "), comments=True)
    ]
    parser = _command_parser(words)
    assert parser is not None, f"{span!r} names no dryheave command"
    known = set(build_parser()._option_string_actions) | set(parser._option_string_actions)
    unknown = [
        word.split("=")[0]
        for word in words
        if word.startswith("--") and word.split("=")[0] not in known
    ]
    assert not unknown, f"{span!r} spells flags its command does not define: {unknown}"


def test_refresh_installs_missing_updates_outdated_and_reports_refused_skills(
    tmp_path, monkeypatch
):
    target = tmp_path / "skills"
    old = target / SKILL_NAMES[0]
    with monkeypatch.context() as context:
        context.setattr("dryheave.skills.bundled_skill", lambda _name: b"Old instructions.\n")
        manage_skills("install", target, (SKILL_NAMES[0], SKILL_NAMES[1]))
    (target / SKILL_NAMES[1] / "SKILL.md").write_bytes(b"User-edited bytes")
    refresh = refresh_skills(target)
    assert refresh.changed == (SKILL_NAMES[0], *SKILL_NAMES[2:])
    assert [conflict.name for conflict in refresh.conflicts] == [SKILL_NAMES[1]]
    assert "user-edited" in refresh.conflicts[0].reason
    assert refresh.error is None
    assert (old / "SKILL.md").read_bytes() == bundled_skill(SKILL_NAMES[0])
    assert (target / SKILL_NAMES[1] / "SKILL.md").read_bytes() == b"User-edited bytes"
    assert target / SKILL_NAMES[1] not in refresh.owned_paths
    assert refresh_skills(target).changed == ()


def test_refresh_reports_a_target_it_cannot_use_without_writing(tmp_path):
    target = tmp_path / "skills"
    target.write_text("a file, not a folder")
    refresh = refresh_skills(target)
    assert refresh.error is not None
    assert refresh.changed_paths == ()
    assert refresh.owned_paths == ()
    assert target.read_text() == "a file, not a folder"


def test_refresh_writes_missing_and_outdated_skills_under_one_lock(tmp_path, monkeypatch):
    target = tmp_path / "skills"
    manage_skills("install", target, (SKILL_NAMES[1],))
    with monkeypatch.context() as context:
        context.setattr("dryheave.skills.bundled_skill", lambda _name: b"Old instructions.\n")
        manage_skills("update", target, (SKILL_NAMES[1],))
    calls = []
    real = manage_skills

    def recording(action, path, names=()):
        calls.append((action, names))
        return real(action, path, names)

    monkeypatch.setattr("dryheave.skills.manage_skills", recording)
    refresh_skills(target)
    assert calls == [("update", SKILL_NAMES)]
