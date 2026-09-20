import argparse
import sys
import webbrowser

from pydantic import JsonValue

from dryheave.commands import CommandRegistry
from dryheave.storage import ObjectStore
from dryheave.viewer import ViewerService, resolve_target
from dryheave.viewer_http import ViewerServer


def _view(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    target = resolve_target(store, args.reference) if args.reference else None
    server = ViewerServer(ViewerService(store, target=target))
    print(f"Serving {server.url} (read-only; Ctrl-C to stop).", file=sys.stderr, flush=True)
    if args.open:
        webbrowser.open(server.url)
    server.serve()
    return {"url": server.url, "target": target.model_dump(mode="json") if target else None}


def register_viewer(registry: CommandRegistry) -> None:
    parser = registry.add(
        "view",
        help_text="Serve a loopback-only read-only results website; never runs or grades.",
    )
    parser.add_argument(
        "reference", nargs="?", help="Optional run ID, portable report ID or alias to focus."
    )
    parser.add_argument("--open", action="store_true", help="Open the viewer URL in a browser.")
    registry.handler(parser, _view)
