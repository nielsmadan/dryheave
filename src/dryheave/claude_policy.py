CLAUDE_EFFORTS = ("low", "medium", "high", "max")
CLAUDE_EFFORT_TEXT = ", ".join(CLAUDE_EFFORTS[:-1]) + " or " + CLAUDE_EFFORTS[-1]


def validate_effort(model: str | None, effort: str | None) -> None:
    if effort is None:
        return
    if effort not in CLAUDE_EFFORTS:
        raise ValueError("Claude effort must be " + CLAUDE_EFFORT_TEXT)
    if model is None or not any(name in model.lower() for name in ("sonnet", "opus")):
        raise ValueError(
            "Explicit Claude effort requires a supported Sonnet or Opus model; Haiku has no effort setting"
        )
    if effort == "max" and "opus" not in model.lower():
        raise ValueError("Maximum Claude effort requires an Opus model")
