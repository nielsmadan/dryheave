from pathlib import Path

from dryheave.cases import FrozenCase, RubricCriterion, load_frozen_case
from dryheave.controller_models import ControllerRecipe
from dryheave.errors import DryheaveError, InputError
from dryheave.experiment_models import (
    MAX_TRIALS,
    ExperimentDraft,
    FrozenExperiment,
    FrozenVariant,
    ScoringConfig,
    TrialSpec,
    VariantSpec,
)
from dryheave.filesystem import read_bytes
from dryheave.models import Manifest, ObjectKind, StrictModel
from dryheave.profile_models import DeriveSpec, NativeRecipe
from dryheave.profile_security import reject_secret_fields, validate_arguments
from dryheave.profiles import derive_profile, load_profile
from dryheave.serialization import canonical_json, digest, parse_model
from dryheave.storage import ObjectStore


def read_experiment(path: Path) -> ExperimentDraft:
    return parse_model(read_bytes(path, limit=4 * 1024 * 1024), ExperimentDraft)


def variant_changes(variant: VariantSpec) -> dict[str, object]:
    return {
        key: value
        for key, value in (
            ("model", variant.model),
            ("effort", variant.effort),
            ("workflow", variant.workflow),
        )
        if value is not None
    }


def validate_experiment(store: ObjectStore, draft: ExperimentDraft) -> ExperimentDraft:
    draft = parse_model(canonical_json(draft), ExperimentDraft)
    validate_role_recipe(draft.simulator)
    if draft.scoring.judge is not None:
        validate_role_recipe(draft.scoring.judge)
    validate_judge(draft.scoring)
    case_ids = tuple(store.resolve(reference) for reference in draft.cases)
    if len(set(case_ids)) != len(case_ids):
        raise InputError("Different aliases resolve to the same case.")
    for case_id in case_ids:
        case = load_frozen_case(store, case_id)
        validate_judge(draft.scoring, case)
        allowed = {fact.fact_id for fact in case.allowed_facts}
        if any(not set(step.decision.fact_ids) <= allowed for step in draft.simulator.script):
            raise InputError("Scripted simulator cites facts outside an experiment case.")
    variants = []
    for variant in draft.variants:
        profile_id = store.resolve(variant.profile)
        profile = load_profile(store, profile_id)
        changes = variant_changes(variant)
        if changes:
            NativeRecipe.model_validate(profile.recipe.model_dump() | changes)
            if all(getattr(profile.recipe, key) == value for key, value in changes.items()):
                raise InputError("Variant override must change the frozen profile.")
        variants.append(variant.model_copy(update={"profile": profile_id}))
    return draft.model_copy(update={"cases": case_ids, "variants": tuple(variants)})


def expand_trials(
    cases: tuple[str, ...], variants: tuple[FrozenVariant, ...], repetitions: int, seed: int
) -> tuple[TrialSpec, ...]:
    trials = []
    for case_id in cases:
        for repetition in range(1, repetitions + 1):
            trial_seed = int(digest(f"{seed}:{case_id}:{repetition}".encode())[:15], 16)
            for variant in variants:
                identity = digest(
                    f"{case_id}:{variant.name}:{variant.profile_id}:{repetition}".encode()
                )
                trials.append(
                    TrialSpec(
                        trial_id="t-" + identity[:24],
                        case_id=case_id,
                        profile_id=variant.profile_id,
                        variant=variant.name,
                        repetition=repetition,
                        seed=trial_seed,
                    )
                )
    return tuple(sorted(trials, key=lambda item: digest(f"{seed}:{item.trial_id}".encode())))


def create_experiment(store: ObjectStore, draft: ExperimentDraft) -> str:
    pinned = validate_experiment(store, draft)
    variants = tuple(
        FrozenVariant(
            name=variant.name,
            profile_id=derive_profile(store, variant.profile, DeriveSpec(recipe_changes=changes))
            if (changes := variant_changes(variant))
            else variant.profile,
        )
        for variant in pinned.variants
    )
    simulator_id = store.put(ObjectKind.SIMULATOR, pinned.simulator)
    scoring_id = store.put(ObjectKind.SCORING, pinned.scoring)
    frozen = FrozenExperiment(
        name=pinned.name,
        case_ids=pinned.cases,
        variants=variants,
        repetitions=pinned.repetitions,
        seed=pinned.seed,
        simulator_id=simulator_id,
        scoring_id=scoring_id,
        trials=expand_trials(pinned.cases, variants, pinned.repetitions, pinned.seed),
        fixture=pinned.fixture,
        initial_input_policy=pinned.initial_input_policy,
    )
    return store.put(ObjectKind.EXPERIMENT, frozen, references=references(frozen))


def references(experiment: FrozenExperiment) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                *experiment.case_ids,
                *(variant.profile_id for variant in experiment.variants),
                experiment.simulator_id,
                experiment.scoring_id,
            }
        )
    )


def validate_role_recipe(recipe: ControllerRecipe) -> None:
    if recipe.command is not None:
        validate_arguments(recipe.command.argv)
        reject_secret_fields(recipe.command.model_dump(mode="json"), location="controller command")


def validate_judge(scoring: ScoringConfig, case: FrozenCase | None = None) -> None:
    judge = scoring.judge
    if judge is not None and judge.kind == "scripted":
        raise InputError("Scripted controllers are unsupported as judges.")
    if (
        case is not None
        and any(
            isinstance(criterion, RubricCriterion) and criterion.required
            for criterion in case.criteria
        )
        and (judge is None or judge.budget.max_calls == 0)
    ):
        raise InputError(
            "Required judge rubrics need a non-scripted judge with a positive call budget."
        )


def load_role[T: StrictModel](
    store: ObjectStore, identifier: str, model: type[T], kind: ObjectKind
) -> T:
    role = store.load(identifier, model, kind=kind)
    manifest = store.get(identifier, kind=kind)
    if manifest.files or manifest.references:
        raise InputError("Role configuration must be self-contained.")
    if isinstance(role, ControllerRecipe):
        validate_role_recipe(role)
    elif isinstance(role, ScoringConfig) and role.judge is not None:
        validate_role_recipe(role.judge)
    return role


def load_experiment(store: ObjectStore, reference: str) -> FrozenExperiment:
    identifier = store.resolve(reference)
    frozen = store.load(identifier, FrozenExperiment, kind=ObjectKind.EXPERIMENT)
    manifest = store.get(identifier, kind=ObjectKind.EXPERIMENT)
    _validate_matrix(frozen, manifest)
    simulator = load_role(store, frozen.simulator_id, ControllerRecipe, ObjectKind.SIMULATOR)
    scoring = load_role(store, frozen.scoring_id, ScoringConfig, ObjectKind.SCORING)
    validate_judge(scoring)
    for case_id in frozen.case_ids:
        case = load_frozen_case(store, case_id)
        validate_judge(scoring, case)
        if any(
            not set(step.decision.fact_ids) <= {fact.fact_id for fact in case.allowed_facts}
            for step in simulator.script
        ):
            raise InputError("Frozen simulator cites unavailable facts.")
    for variant in frozen.variants:
        load_profile(store, variant.profile_id)
    return frozen


def _validate_matrix(frozen: FrozenExperiment, manifest: Manifest) -> None:
    if manifest.references != references(frozen) or manifest.files:
        raise InputError("Experiment input closure differs from its declared inputs.")
    if len(set(frozen.case_ids)) != len(frozen.case_ids) or len(
        {variant.name for variant in frozen.variants}
    ) != len(frozen.variants):
        raise InputError("Frozen experiment matrix contains duplicates.")
    if len(frozen.case_ids) * len(frozen.variants) * frozen.repetitions > MAX_TRIALS:
        raise InputError("Frozen experiment matrix exceeds its bound.")
    if frozen.trials != expand_trials(
        frozen.case_ids, frozen.variants, frozen.repetitions, frozen.seed
    ):
        raise InputError("Frozen experiment expansion does not match its matrix.")


def inspect_experiment(
    store: ObjectStore, reference: str
) -> tuple[FrozenExperiment | None, str | None]:
    try:
        return load_experiment(store, reference), None
    except DryheaveError as error:
        failure = str(error)
    try:
        evidence = store.read_evidence(reference)
        if evidence.manifest.kind != ObjectKind.EXPERIMENT:
            raise InputError("Expected frozen experiment evidence.")
        frozen = parse_model(canonical_json(evidence.manifest.payload), FrozenExperiment)
        _validate_matrix(frozen, evidence.manifest)
        return frozen, failure
    except DryheaveError:
        return None, failure
