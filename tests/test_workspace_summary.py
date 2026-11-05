from pathlib import Path

from dryheave.skills import SkillConflict, SkillRefresh
from dryheave.tui_test_release import TUI_TEST_ACTION
from dryheave.workspace_summary import (
    InitOutcome,
    UpdateOutcome,
    commit_view,
    git_line,
    next_steps,
    render_init,
    render_update,
    repository_state,
    skills_lines,
    tui_test_line,
)

ROOT = Path("/work/bench two")
AGENTS = ROOT / ".agents/skills"
CLAUDE = ROOT / ".claude/skills"
FIRST = {
    "commit_paths": ["dryheave.toml"],
    "git_commit": "ask",
    "message": "workspace: initialize",
    "commit": {"state": "committed", "commit": "0123456789abcdef"},
}


def refused(target, name="dryheave-case", reason="Edited"):
    return SkillRefresh(target=target, conflicts=(SkillConflict(name=name, reason=reason),))


def outcome(**changes):
    values = {
        "root": ROOT,
        "repository": "created",
        "commit": FIRST,
        "skills": (SkillRefresh(target=AGENTS), SkillRefresh(target=CLAUDE)),
        "tui_test": Path("/bin/tui-test"),
        "installed_version": None,
        "ready": True,
    }
    return InitOutcome(**(values | changes))


def test_the_git_line_names_the_repository_and_the_commit():
    assert git_line(outcome(), ROOT) == (
        "new repository, committed 0123456 workspace: initialize (checkpoint commits: ask)"
    )
    off = {"commit_paths": [], "git_commit": "off", "message": "workspace: update"}
    assert git_line(outcome(repository="existing", commit=off), ROOT) == (
        "existing repository (checkpoint commits: off)"
    )
    assert git_line(outcome(repository="skipped"), ROOT) == "no repository (git not found)"


def test_a_failed_or_partly_ignored_first_commit_is_reported():
    failed = FIRST | {"commit": {"state": "failed", "error": "no identity"}}
    assert git_line(outcome(commit=failed), ROOT) == (
        "new repository, the commit failed: no identity (checkpoint commits: ask)"
    )
    ignored = FIRST | {"ignored_paths": [".claude/skills/dryheave-case"]}
    assert "git ignores .claude/skills/dryheave-case, so it was left out" in git_line(
        outcome(commit=ignored), ROOT
    )


def test_a_rerun_in_ask_mode_prints_the_command_that_commits_the_refreshed_skills():
    rerun = {
        "commit_paths": [".agents/skills/dryheave-case"],
        "git_commit": "ask",
        "message": "workspace: update",
    }
    assert git_line(outcome(repository="existing", commit=rerun), ROOT) == (
        "existing repository, not committed; to commit: git --literal-pathspecs add -- "
        ".agents/skills/dryheave-case && git --literal-pathspecs commit -m "
        "'workspace: update' -- .agents/skills/dryheave-case (checkpoint commits: ask)"
    )


def test_repository_states_are_the_three_setup_reports():
    assert [repository_state(value) for value in ("created", "existing", "skipped")] == [
        "created",
        "existing",
        "skipped",
    ]


def test_skill_folders_are_shown_relative_to_the_workspace_with_refusals_apart():
    mixed = outcome(
        skills=(
            SkillRefresh(target=AGENTS),
            SkillRefresh(target=CLAUDE, error="Expected a directory"),
            refused(Path("/elsewhere/skills")),
        )
    )
    assert skills_lines(mixed) == [
        "installed in .agents/skills and /elsewhere/skills",
        "not installed in .claude/skills: Expected a directory",
        "dryheave-case not installed in /elsewhere/skills: Edited",
        "move a refused skill folder aside, then run dryheave update",
    ]


def test_the_tui_test_line_names_an_install_or_the_action_when_none_is_supported():
    assert tui_test_line(outcome(installed_version="0.1.1")) == "0.1.1 installed at /bin/tui-test"
    assert tui_test_line(outcome(ready=False)) == f"no supported version found. {TUI_TEST_ACTION}"
    assert outcome(ready=False).next_text.endswith(f"Before native runs: {TUI_TEST_ACTION}")


def test_the_next_steps_enter_the_workspace_only_from_elsewhere():
    assert next_steps(outcome(), Path("/work"))[0] == "cd 'bench two'"
    assert next_steps(outcome(), Path("/home"))[0] == "cd '/work/bench two'"
    assert next_steps(outcome(root=Path("/work/-bench")), Path("/work"))[0] == "cd ./-bench"
    assert next_steps(outcome(), ROOT)[0] == (
        "Start Claude Code, Codex or your coding agent in the workspace."
    )


def test_the_json_next_text_says_the_same_steps_in_one_paragraph():
    assert outcome().next_text == (
        "Start Claude Code, Codex or your coding agent in the workspace. Run the "
        "dryheave-voice-profile skill to build your voice from your session logs: "
        "/dryheave-voice-profile in Claude Code, $dryheave-voice-profile in Codex. "
        "Other agents find the skills in .agents/skills and .claude/skills."
    )


def test_the_next_steps_point_other_agents_at_the_usable_folders_only():
    failed = outcome(skills=(SkillRefresh(target=AGENTS), SkillRefresh(target=CLAUDE, error="x")))
    assert next_steps(failed, ROOT)[-1].splitlines()[-1] == (
        "       Other agents  the skills are in .agents/skills"
    )
    assert "Other agents find the skills in .agents/skills." in failed.next_text


def test_the_init_summary_lists_the_setup_then_numbered_steps():
    assert render_init(outcome(), Path("/work")).splitlines() == [
        "✔ Workspace ready: /work/bench two",
        (
            "  git       new repository, committed 0123456 workspace: initialize"
            " (checkpoint commits: ask)"
        ),
        "  skills    installed in .agents/skills and .claude/skills",
        "  tui-test  /bin/tui-test",
        "",
        "Next steps:",
        "  1. cd 'bench two'",
        "  2. Start Claude Code, Codex or your coding agent in the workspace.",
        "  3. Run the dryheave-voice-profile skill to build your voice from your session logs:",
        "       Claude Code   /dryheave-voice-profile",
        "       Codex         $dryheave-voice-profile",
        "       Other agents  the skills are in .agents/skills and .claude/skills",
    ]


def updated(commit, **changes):
    values = {
        "path": ROOT,
        "gitignore_added": (),
        "skill_installs": (
            SkillRefresh(target=AGENTS, changed=("dryheave-case", "dryheave-collect")),
            SkillRefresh(target=CLAUDE),
        ),
        "commit": commit,
    }
    return UpdateOutcome(**(values | changes))


ASK = {
    "git_commit": "ask",
    "message": "workspace: update",
    "commit_paths": [".agents/skills/dryheave-case", "a b"],
}


def test_the_update_summary_names_each_change_and_the_command_to_commit_it():
    ask = updated(ASK, gitignore_added=("replays/",))
    hint = (
        "git --literal-pathspecs add -- .agents/skills/dryheave-case 'a b' && "
        "git --literal-pathspecs commit -m 'workspace: update' -- "
        ".agents/skills/dryheave-case 'a b'"
    )
    assert render_update(ask, ROOT).splitlines() == [
        "✔ Workspace updated: /work/bench two",
        "  skills    .agents/skills: updated dryheave-case and dryheave-collect",
        "            .claude/skills: current",
        "  gitignore added replays/",
        f"  git       not committed; to commit: {hint}",
    ]


def test_the_commit_command_runs_in_the_workspace_from_a_nested_directory():
    line = render_update(updated(ASK), ROOT / "cases/x").splitlines()[-1]
    assert line.startswith("  git       not committed; to commit: git -C '/work/bench two' ")
    assert " && git -C '/work/bench two' --literal-pathspecs commit " in line


def test_the_update_git_line_reports_a_failed_or_disabled_commit():
    failed = ASK | {"git_commit": "auto", "commit": {"state": "failed", "error": "hook"}}
    assert render_update(updated(failed), ROOT).splitlines()[-1] == (
        "  git       the commit failed: hook"
    )
    off = ASK | {"git_commit": "off"}
    assert render_update(updated(off), ROOT).splitlines()[-1] == (
        "  git       not committed (checkpoint commits: off)"
    )


def test_a_refused_skill_makes_the_update_partial_and_names_the_way_out():
    partial = updated(
        ASK,
        skill_installs=(SkillRefresh(target=AGENTS, changed=("dryheave-case",)), refused(CLAUDE)),
    )
    lines = render_update(partial, ROOT).splitlines()
    assert lines[:4] == [
        "Workspace partly updated: /work/bench two",
        "  skills    .agents/skills: updated dryheave-case",
        "            .claude/skills: current",
        "            dryheave-case not updated in .claude/skills: Edited",
    ]
    assert lines[4] == "            move a refused skill folder aside, then run dryheave update"


def test_an_unchanged_workspace_is_up_to_date_unless_a_folder_needs_attention():
    current = updated(ASK | {"commit_paths": []}, skill_installs=(SkillRefresh(target=AGENTS),))
    assert render_update(current, ROOT) == "✔ Workspace already up to date: /work/bench two"
    failed = updated(ASK, skill_installs=(SkillRefresh(target=AGENTS, error="Locked"),))
    assert render_update(failed, ROOT).splitlines() == [
        "Nothing updated in /work/bench two; some skills need attention:",
        "  skills    not updated in .agents/skills: Locked",
    ]


def test_the_update_json_flattens_the_checkpoint_beside_the_outcome():
    data = updated(ASK, gitignore_added=("replays/",)).as_json()
    assert data["updated"] is True
    assert data["gitignore_added"] == ["replays/"]
    assert data["skill_installs"][0] == {
        "target": str(AGENTS),
        "changed": ["dryheave-case", "dryheave-collect"],
        "conflicts": [],
        "error": None,
    }
    assert data["commit_paths"] == ASK["commit_paths"]
    assert "commit" not in data


def test_the_commit_view_reads_the_checkpoint_fields():
    view = commit_view(FIRST | {"ignored_paths": ["x"]})
    assert (view.mode, view.message, view.state, view.sha, view.paths, view.ignored) == (
        "ask",
        "workspace: initialize",
        "committed",
        "0123456789abcdef",
        ("dryheave.toml",),
        ("x",),
    )
