from dryheave.codex_policy import config_bypasses


def test_config_bypasses_read_top_level_settings():
    assert config_bypasses({"sandbox_mode": "danger-full-access", "approval_policy": "never"}) == {
        "sandbox",
        "approvals",
    }


def test_config_bypasses_prefer_the_selected_profile():
    config = {
        "sandbox_mode": "danger-full-access",
        "approval_policy": "on-request",
        "profile": "work",
        "profiles": {"work": {"sandbox_mode": "read-only", "approval_policy": "never"}},
    }
    assert config_bypasses(config) == {"approvals"}
