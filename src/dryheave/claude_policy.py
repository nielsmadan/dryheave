def validate_effort(model: str | None, effort: str | None) -> None:
    if effort is None:
        return
    if effort not in {"low", "medium", "high", "max"}:
        raise ValueError("Claude effort must be low, medium, high or max")
    if model is None or not any(name in model.lower() for name in ("sonnet", "opus")):
        raise ValueError(
            "Explicit Claude effort requires a supported Sonnet or Opus model; Haiku has no effort setting"
        )
    if effort == "max" and "opus" not in model.lower():
        raise ValueError("Maximum Claude effort requires an Opus model")
