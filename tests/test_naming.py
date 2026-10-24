from pathlib import Path

RETIRED = "simu" + "lator"
ROOT = Path(__file__).resolve().parents[1]
HISTORICAL = (ROOT / "docs" / "plans", ROOT / "docs" / "decisions")


def _hits(paths):
    return [
        str(path.relative_to(ROOT))
        for path in sorted(paths)
        if path.is_file()
        and "__pycache__" not in path.parts
        and (
            RETIRED in path.name.lower()
            or RETIRED in path.read_text(encoding="utf-8", errors="ignore").lower()
        )
    ]


def test_source_never_uses_the_retired_name():
    assert _hits((ROOT / "src").rglob("*")) == []


def test_docs_and_readme_never_use_the_retired_name_outside_plans_and_decisions():
    documents = [
        path
        for path in (ROOT / "docs").rglob("*.md")
        if not any(path.is_relative_to(folder) for folder in HISTORICAL)
    ]
    assert _hits([ROOT / "README.md", *documents]) == []
