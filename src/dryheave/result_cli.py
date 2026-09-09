import argparse
import shlex
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from dryheave.assessments import assess_run, review_assessment
from dryheave.bundles import SENSITIVE_CLASSES, export_bundle, import_bundle
from dryheave.commands import CommandRegistry
from dryheave.errors import CancelledError
from dryheave.reports import compare_runs, report_run
from dryheave.result_models import AuditReview
from dryheave.storage import ObjectStore


def _assess(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    try:
        identifiers = assess_run(store, args.run_id)
    except KeyboardInterrupt as error:
        resume = shlex.join(("dryheave", "--store", str(store.root), "assess", args.run_id))
        raise CancelledError(
            f"Assessment interrupted; captured output and grading progress are retained. Resume with: {resume}"
        ) from error
    return {
        "assessment_ids": list(identifiers),
        "report": report_run(store, args.run_id).model_dump(mode="json"),
    }


def _report(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    return report_run(store, args.reference).model_dump(mode="json")


def _compare(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    return compare_runs(
        store,
        args.before,
        args.after,
        before_variant=args.before_variant,
        after_variant=args.after_variant,
    ).model_dump(mode="json")


def _review(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    review = AuditReview(
        finding_id=args.finding,
        decision=args.decision,
        reviewer=args.reviewer,
        reason=args.reason,
        reviewed_at=datetime.now(UTC),
    )
    identifier = review_assessment(store, args.run_id, args.attempt, review)
    return {
        "assessment_id": identifier,
        "report": report_run(store, args.run_id).model_dump(mode="json"),
    }


def _export(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    result = export_bundle(
        store,
        args.reference,
        args.output,
        include_sensitive=tuple(args.include_sensitive),
        aliases=tuple(args.alias),
    )
    return {"path": str(args.output.absolute()), "bundle": result.model_dump(mode="json")}


def _import(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    result = import_bundle(store, args.path, alias_policy=args.alias_policy)
    return {"bundle": result.model_dump(mode="json"), "roots": list(result.roots)}


def register_results(registry: CommandRegistry) -> None:
    assess = registry.add(
        "assess",
        help_text="Audit stopped attempts, run hidden checks and optional judge, and retain finished results.",
    )
    assess.add_argument("run_id")
    registry.handler(assess, _assess)
    report = registry.add(
        "report",
        help_text="Read durable run progress or an imported portable report without taking the writer lock.",
    )
    report.add_argument("reference")
    registry.handler(report, _report)
    compare = registry.add(
        "compare", help_text="Pair compatible retained results and show attrition and spending."
    )
    compare.add_argument("before")
    compare.add_argument("after")
    compare.add_argument("--before-variant")
    compare.add_argument("--after-variant")
    registry.handler(compare, _compare)
    review = registry.add(
        "review", help_text="Append an explicit review of a suspected access finding."
    )
    review.add_argument("run_id")
    review.add_argument("--attempt", required=True)
    review.add_argument("--finding", required=True)
    review.add_argument("--decision", choices=["dismiss", "uphold"], required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--reason", required=True)
    registry.handler(review, _review)
    export = registry.add(
        "export",
        help_text="Write a verified portable bundle with explicit sensitive artifact selection.",
    )
    export.add_argument("reference")
    export.add_argument("--output", type=Path, required=True)
    export.add_argument(
        "--include-sensitive", choices=SENSITIVE_CLASSES, action="append", default=[]
    )
    export.add_argument("--alias", action="append", default=[])
    registry.handler(export, _export)
    import_ = registry.add(
        "import",
        help_text="Verify and import a portable bundle; aliases use an explicit collision policy.",
    )
    import_.add_argument("path", type=Path)
    import_.add_argument("--alias-policy", choices=["error", "skip", "replace"], default="error")
    registry.handler(import_, _import)
