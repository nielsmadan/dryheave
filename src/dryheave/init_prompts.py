from collections.abc import Mapping
from typing import Literal, TextIO

from dryheave.errors import CancelledError
from dryheave.menu import Choice, Question, choose
from dryheave.transport_setup import InstallTarget
from dryheave.workspaces import GitCommitMode

type InstallChoice = Literal["install", "manual"]

PROMPT_DEFAULT: GitCommitMode = "ask"
MODE_CHOICES: tuple[Choice[GitCommitMode], ...] = (
    Choice("ask", "ask: offer a commit after each checkpoint and let you decide"),
    Choice("auto", "auto: commit the files each checkpoint command wrote"),
    Choice("off", "off: never offer commits"),
)
MODE_QUESTION = Question(
    "Commit authored files (voices, cases, reports) to this workspace's git repository?",
    MODE_CHOICES,
    title="git commits",
    default=PROMPT_DEFAULT,
)


def cancelled(flags: str) -> CancelledError:
    return CancelledError(f"init cancelled; nothing was written. Rerun it, or pass {flags}.")


def install_question(target: InstallTarget) -> Question[InstallChoice]:
    return Question(
        "dryheave drives agent CLIs through tui-test, which is not installed.\n"
        f"{target.description} It installs with:\n"
        f"  {target.command}\n"
        "Installing it for you leaves your shell profile unchanged.",
        (
            Choice("install", "Install it for me", f"install {target.version}"),
            Choice("manual", "I will install it", "install it myself"),
        ),
        title="tui-test",
    )


def choose_install(
    target: InstallTarget, *, stdin: TextIO, output: TextIO, environment: Mapping[str, str]
) -> InstallChoice:
    choice = choose(install_question(target), stdin=stdin, output=output, environment=environment)
    if choice is None:
        raise cancelled("--install-tui-test or --tui-test PATH")
    return choice


def choose_mode(
    flag: GitCommitMode | None,
    existing: GitCommitMode | None,
    *,
    interactive: bool,
    stdin: TextIO,
    output: TextIO,
    environment: Mapping[str, str],
) -> GitCommitMode:
    if flag is not None:
        return flag
    if existing is not None:
        return existing
    if not interactive:
        return PROMPT_DEFAULT
    mode = choose(MODE_QUESTION, stdin=stdin, output=output, environment=environment)
    if mode is None:
        raise cancelled("--git-commit ask|auto|off")
    return mode
