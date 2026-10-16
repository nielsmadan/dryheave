import argparse
import shlex
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from dryheave.assessments import assess_run, regrade_run, review_assessment
from dryheave.bundles import SENSITIVE_CLASSES, export_bundle, import_bundle
from dryheave.commands import CommandRegistry
from dryheave.errors import CancelledError, InputError
from dryheave.report_export import write_report_html
from dryheave.report_models import RunSetup
from dryheave.reports import compare_runs, report_run, run_setups
from dryheave.result_models import AuditReview
from dryheave.storage import ObjectStore

REPORT_SENSITIVE_CLASSES = ("assessment-evidence",)
MAX_REPORT_REFERENCES = 16


def _assess(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    if args.case is not None and not args.regrade:
        raise InputError("--case requires --regrade.")
    regrade: dict[str, JsonValue] = {}
    try:
        if args.regrade:
            regrade_id, identifiers = regrade_run(store, args.run_id, case=args.case)
            regrade = {"regrade_id": regrade_id}
        else:
            identifiers = assess_run(store, args.run_id)
    except KeyboardInterrupt as error:
        options = (
            ("--regrade", *(("--case", args.case) if args.case else ())) if args.regrade else ()
        )
        resume = shlex.join(
            ("dryheave", "--store", str(store.root), "assess", args.run_id, *options)
        )
        raise CancelledError(
            f"Assessment interrupted; captured output and grading progress are retained. Resume with: {resume}"
        ) from error
    return {
        "assessment_ids": list(identifiers),
        **regrade,
        "report": report_run(store, args.run_id).model_dump(mode="json"),
    }


def _report(args: argparse.Namespace, store: ObjectStore) -> dict[str, JsonValue]:
    if args.html is None and (args.force or args.include_sensitive):
        raise InputError("--force and --include-sensitive require --html.")
    if len(args.references) > MAX_REPORT_REFERENCES:
        raise InputError(f"Report at most {MAX_REPORT_REFERENCES} runs at once.")
    if len(set(args.references)) != len(args.references):
        raise InputError("Each report reference may appear only once.")
    reports = [report_run(store, reference) for reference in args.references]
    if len({report.run_id for report in reports}) != len(reports):
        raise InputError("Each run may appear only once; two references name the same run.")
    setups: tuple[RunSetup | None, ...] = (
        run_setups(store, reports) if len(reports) > 1 else (None,)
    )
    if args.html is not None:
        path = write_report_html(
            store,
            list(zip(reports, setups, strict=True)),
            args.html,
            include_reasoning="assessment-evidence" in args.include_sensitive,
            replace=args.force,
        )
        return {"path": str(path)}
    if len(reports) == 1:
        return reports[0].model_dump(mode="json")
    return {
        "reports": [report.model_dump(mode="json") for report in reports],
        "setups": [setup.model_dump(mode="json") for setup in setups if setup is not None],
    }


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
    assess.add_argument(
        "--regrade",
        action="store_true",
        help="Grade every captured attempt again, including finished ones, as a new chained assessment.",
    )
    assess.add_argument(
        "--case",
        help="With --regrade, grade against this replacement case (same repository and initial prompt).",
    )
    registry.handler(assess, _assess)
    report = registry.add(
        "report",
        help_text="Read durable run progress or an imported portable report without taking the writer lock.",
    )
    report.add_argument(
        "references",
        metavar="REF",
        nargs="+",
        help="Run or portable report IDs; several give one combined report.",
    )
    report.add_argument(
        "--html", type=Path, help="Write a self-contained shareable HTML report to this path."
    )
    report.add_argument(
        "--force", action="store_true", help="With --html, overwrite an existing destination."
    )
    report.add_argument(
        "--include-sensitive",
        choices=REPORT_SENSITIVE_CLASSES,
        action="append",
        default=[],
        help="With --html, include judge reasoning only when 'assessment-evidence' is given.",
    )
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
