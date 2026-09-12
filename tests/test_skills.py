import fcntl
import hashlib
import json
from importlib.resources import files
from pathlib import Path

import pytest

from dryheave.errors import ConflictError, InputError, LockBusyError, PathError
from dryheave.filesystem import directory_fd
from dryheave.skills import OWNER_FILE, SKILL_NAMES, bundled_skill, list_skills, manage_skills


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
