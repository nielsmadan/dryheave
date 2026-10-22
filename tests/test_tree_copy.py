import pytest

from dryheave.errors import InputError
from dryheave.tree_copy import TreeLimits, TreePolicy, copy_tree

LIMITS = TreeLimits(files=100, directories=100, bytes=1024 * 1024, screened_bytes=1024 * 1024)


def test_copy_tree_copies_admitted_entries_and_hashes_them(tmp_path):
    (tmp_path / "source/skills/x").mkdir(parents=True)
    (tmp_path / "source/skills/x/SKILL.md").write_text("Skill.")
    (tmp_path / "source/skills/x/skip.md").write_text("Skipped.")
    (tmp_path / "copy").mkdir()
    tree = copy_tree(
        tmp_path / "source",
        ("skills",),
        tmp_path / "copy",
        policy=TreePolicy(
            dereference=False, admit=lambda relative, directory: not relative.endswith("skip.md")
        ),
        limits=LIMITS,
    )
    assert sorted(tree.records) == ["skills/x/SKILL.md"]
    assert (tmp_path / "copy/skills/x/SKILL.md").read_text() == "Skill."


def test_copy_tree_reports_a_missing_entry_as_moved(tmp_path):
    (tmp_path / "source").mkdir()
    with pytest.raises(InputError, match="Live config entry skills changed while it was copied"):
        copy_tree(
            tmp_path / "source",
            ("skills",),
            None,
            policy=TreePolicy(dereference=False),
            limits=LIMITS,
        )


def test_copy_tree_reports_a_file_removed_mid_walk_as_moved(tmp_path):
    source = tmp_path / "source"
    (source / "skills").mkdir(parents=True)
    (source / "skills/a.md").write_text("A.")
    (source / "skills/b.md").write_text("B.")

    def remove_next(relative: str, directory: bool) -> bool:
        if relative == "skills/a.md":
            (source / "skills/b.md").unlink()
        return True

    with pytest.raises(InputError, match=r"entry skills/b\.md changed while it was copied"):
        copy_tree(
            source,
            ("skills",),
            None,
            policy=TreePolicy(dereference=False, admit=remove_next),
            limits=LIMITS,
        )
