import argparse
from collections.abc import Callable
from typing import Never

from pydantic import JsonValue

from dryheave.errors import InputError
from dryheave.storage import ObjectStore

type CommandHandler = Callable[[argparse.Namespace, ObjectStore], dict[str, JsonValue]]
type CommandRegistrar = Callable[["CommandRegistry"], None]


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise InputError(message)


class CommandRegistry:
    def __init__(self, parser: argparse.ArgumentParser) -> None:
        self.commands = parser.add_subparsers(dest="command", required=True)

    def add(self, name: str, *, help_text: str) -> argparse.ArgumentParser:
        return self.commands.add_parser(name, help=help_text, description=help_text)

    @staticmethod
    def handler(parser: argparse.ArgumentParser, handler: CommandHandler) -> None:
        parser.set_defaults(handler=handler)
