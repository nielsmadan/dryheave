from collections.abc import Callable
from pathlib import Path

from dryheave.errors import InputError
from dryheave.logs import base, claude, codex, droid, opencode, pi
from dryheave.logs.base import EvidenceExcerpt, ImportLimits, Session
from dryheave.models import AgentKind, ObjectKind
from dryheave.storage import ObjectStore

LogParser = Callable[[Path, ImportLimits | None], Session]
PARSERS: dict[AgentKind, LogParser] = {
    AgentKind.CLAUDE: claude.parse,
    AgentKind.CODEX: codex.parse,
    AgentKind.DROID: droid.parse,
    AgentKind.PI: pi.parse,
    AgentKind.OPENCODE: opencode.parse,
}
SourceFinder = Callable[[Path, ImportLimits], tuple[Path, ...]]
FINDERS: dict[AgentKind, SourceFinder] = {AgentKind.OPENCODE: opencode.discover}


AGENT_CHOICES = tuple(sorted(agent.value for agent in PARSERS))
AGENT_OPTIONS = " or ".join(
    (", ".join(f"--agent {agent}" for agent in AGENT_CHOICES[:-1]), f"--agent {AGENT_CHOICES[-1]}")
)


def parser_for(agent: AgentKind) -> LogParser:
    return PARSERS[agent]


def discover_sources(agent: AgentKind, root: Path, limits: ImportLimits) -> tuple[Path, ...]:
    return FINDERS.get(agent, base.discover)(root, limits)


def import_session(
    store: ObjectStore, path: Path, agent: AgentKind, limits: ImportLimits | None = None
) -> str:
    session = parser_for(agent)(path, limits)
    if not session.events:
        raise InputError("No supported dialogue or tool events found in the selected log.")
    return store.put(ObjectKind.SESSION, session)


def verify_evidence(store: ObjectStore, evidence: tuple[EvidenceExcerpt, ...]) -> None:
    sessions: dict[str, Session] = {}
    for item in evidence:
        if item.session_id not in sessions:
            sessions[item.session_id] = store.load(
                item.session_id, Session, kind=ObjectKind.SESSION
            )
        event = next(
            (
                event
                for event in sessions[item.session_id].events
                if event.event_id == item.event_id
            ),
            None,
        )
        if event is None or item.excerpt not in event.text:
            raise InputError(f"Evidence excerpt does not match source event {item.event_id}.")
