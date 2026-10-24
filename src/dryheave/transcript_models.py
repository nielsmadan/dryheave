from typing import Literal

from pydantic import Field

from dryheave.cases import FactKind
from dryheave.models import Name, ObjectId, RunId, StrictModel
from dryheave.result_models import AttemptOutcome, OutcomeReason

type Author = Literal["initial", "stand_in", "fallback", "workflow", "unknown"]
type SceneKind = Literal["stand_in", "fallback", "inspection", "correction", "prompt_step", "stop"]
type StandInPurpose = Literal["reply", "fix_request", "dialog", "screen"]


class ConversationLine(StrictModel):
    turn: int = Field(ge=0)
    role: Literal["user", "assistant"]
    text: str
    author: Author | None = None
    origin: str | None = None


class InspectedFact(StrictModel):
    fact_id: Name
    verdict: Literal["met", "unmet", "unclear"]
    observation: str
    evidence: str


class SceneEntry(StrictModel):
    kind: SceneKind
    turn: int | None = Field(default=None, ge=0)
    call_id: Name | None = None
    purpose: StandInPurpose | None = None
    status: str | None = None
    action: Literal["reply", "stop"] | None = None
    text: str | None = None
    reason: str | None = None
    cited: tuple[Name, ...] = ()
    phase: Name | None = None
    handed: tuple[Name, ...] = ()
    rejection: str | None = None
    inspection_id: Name | None = None
    call_ids: tuple[Name, ...] = ()
    facts: tuple[InspectedFact, ...] = ()
    detail: str | None = None


class GapFact(StrictModel):
    fact_id: Name
    kind: FactKind
    phase: Name
    verdict: Literal["unmet", "unclear", "disputed"]
    text: str | None
    observation: str
    evidence: str
    source: Literal["verifier", "judge"]


class Gaps(StrictModel):
    assessed: bool
    facts: tuple[GapFact, ...] = ()
    uncovered: tuple[str, ...] = ()
    unreached_phases: tuple[Name, ...] = ()


class ReviewLanes(StrictModel):
    schema_version: Literal[1] = 1
    run_id: RunId
    attempt_id: Name
    case_id: ObjectId | None
    outcome: AttemptOutcome | None
    outcome_reason: OutcomeReason | None
    stop_reason: str | None
    conversation: tuple[ConversationLine, ...]
    behind_the_scenes: tuple[SceneEntry, ...]
    gaps: Gaps
    notes: tuple[str, ...] = ()


class RunTranscript(StrictModel):
    schema_version: Literal[1] = 1
    run_id: RunId
    attempts: tuple[ReviewLanes, ...]
