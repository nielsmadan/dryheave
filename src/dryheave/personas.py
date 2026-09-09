from typing import Literal

from pydantic import Field

from dryheave.errors import InputError
from dryheave.logs.base import EvidenceExcerpt
from dryheave.logs.service import verify_evidence
from dryheave.models import ObjectKind, StrictModel
from dryheave.storage import ObjectStore


class Persona(StrictModel):
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=200)
    instructions: str = Field(max_length=16000)
    disclosure_policy: str = Field(max_length=8000)
    unknown_answer_policy: str = Field(max_length=8000)
    examples: tuple[EvidenceExcerpt, ...] = ()
    reviewed_subject_safe: bool = False


def validate_persona(persona: Persona) -> tuple[str, ...]:
    issues: list[str] = []
    for name in ("instructions", "disclosure_policy", "unknown_answer_policy"):
        if not getattr(persona, name).strip():
            issues.append(f"Persona {name} requires curator-authored content.")
    if not persona.reviewed_subject_safe:
        issues.append("Persona must be reviewed for solution-bearing text and marked subject-safe.")
    if any(item.visibility != "subject" for item in persona.examples):
        issues.append("Persona examples must contain only curated subject-safe evidence.")
    return tuple(issues)


def freeze_persona(store: ObjectStore, persona: Persona) -> str:
    issues = validate_persona(persona)
    if issues:
        raise InputError("; ".join(issues))
    verify_evidence(store, persona.examples)
    return store.put(ObjectKind.PERSONA, persona)


def subject_persona(persona: Persona) -> dict[str, object]:
    issues = validate_persona(persona)
    if issues:
        raise InputError("; ".join(issues))
    return {
        "instructions": persona.instructions,
        "disclosure_policy": persona.disclosure_policy,
        "unknown_answer_policy": persona.unknown_answer_policy,
        "examples": [item.excerpt for item in persona.examples],
    }


def load_frozen_persona(store: ObjectStore, reference: str) -> Persona:
    persona = store.load(reference, Persona, kind=ObjectKind.PERSONA)
    issues = validate_persona(persona)
    if issues:
        raise InputError("; ".join(issues))
    manifest = store.get(reference, kind=ObjectKind.PERSONA)
    if manifest.references or manifest.files:
        raise InputError("Persona runtime inputs must be self-contained curated text.")
    return persona
