import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

PROMPT = "Fix the greeting; ask me which punctuation."
ANSWER = "Use a comma after Hello."
BASELINE = 'def greet(name):\n    return "Hello " + name\n'
SOLUTION = 'def greet(name):\n    return "Hello, " + name\n'
REPETITIONS = 2


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


class Demo:
    def __init__(self, executable: Path, output: Path) -> None:
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("--dryheave must name an absolute installed executable path.")
        destination = output.absolute()
        if ".." in destination.parts or any(
            path.is_symlink() for path in (destination, *destination.parents)
        ):
            raise ValueError("--output cannot contain symlinks or parent traversal.")
        destination.mkdir(parents=True, exist_ok=False)
        self.output = destination
        self.executable = executable
        self.store = destination / "store"
        self.records: list[dict[str, Any]] = []
        (destination / ".gitignore").write_text("*\n")
        (destination / "commands").mkdir()

    def run(self, *arguments: str, store: Path | None = None) -> dict[str, Any]:
        argv = [str(self.executable), "--store", str(store or self.store), "--json", *arguments]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=180, check=True)
        response = json.loads(result.stdout)
        data = response.get("data")
        if response.get("ok") is not True or not isinstance(data, dict):
            raise ValueError(f"Unexpected CLI JSON response for {arguments}")
        record = {
            "argv": argv,
            "exit_code": result.returncode,
            "stderr": result.stderr,
            "response": response,
        }
        self.records.append(record)
        write_json(
            self.output / "commands" / f"{len(self.records):03d}-{arguments[0]}.json", record
        )
        return data

    def git(self, repo: Path, *arguments: str) -> str:
        executable = shutil.which("git")
        if executable is None:
            raise ValueError("Install Git before running this example.")
        argv = [
            executable,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=Synthetic Example",
            "-c",
            "user.email=example@example.invalid",
            "-C",
            str(repo),
            *arguments,
        ]
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
            env={"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
        )
        return result.stdout.strip()

    def prepare_source(self) -> tuple[Path, str, str]:
        repo = self.output / "synthetic-source"
        repo.mkdir()
        self.git(repo, "init", "--template=", "-b", "main")
        (repo / "README.md").write_text(
            "Disposable synthetic greeting task; no real session evidence.\n"
        )
        self.git(repo, "add", "README.md")
        self.git(repo, "commit", "-m", "feat: add synthetic project")
        (repo / "greet.py").write_text(BASELINE)
        self.git(repo, "add", "greet.py")
        self.git(repo, "commit", "-m", "feat: add synthetic greeting")
        baseline = self.git(repo, "rev-parse", "HEAD")
        (repo / "greet.py").write_text(SOLUTION)
        self.git(repo, "add", "greet.py")
        self.git(repo, "commit", "-m", "fix: apply synthetic future solution")
        future = self.git(repo, "rev-parse", "HEAD")
        (self.output / "reference.patch").write_text(
            self.git(repo, "diff", baseline, future) + "\n"
        )
        logs = self.output / "logs"
        logs.mkdir()
        records = [
            {
                "type": "session_meta",
                "payload": {
                    "id": "synthetic-demo",
                    "cli_version": "0.153.4-synthetic",
                    "cwd": str(repo),
                    "git": {"commit_hash": baseline},
                },
            },
            *[
                {"type": "event_msg", "payload": {"type": kind, "message": message}}
                for kind, message in (
                    ("user_message", PROMPT),
                    ("agent_message", "Which punctuation?"),
                    ("user_message", ANSWER),
                    ("agent_message", "Done."),
                )
            ],
        ]
        (logs / "synthetic.jsonl").write_text("".join(json.dumps(item) + "\n" for item in records))
        return repo, baseline, future

    def curate(self, repo: Path, baseline: str) -> dict[str, str]:
        self.run("collect", "scan", "--agent", "codex", "--root", str(self.output / "logs"))
        session = self.run(
            "collect", "import", str(self.output / "logs/synthetic.jsonl"), "--agent", "codex"
        )["id"]
        self.run("collect", "show", session)
        persona_path = self.output / "persona.json"
        self.run("persona", "draft", session, "--out", str(persona_path))
        persona = json.loads(persona_path.read_text())
        persona.update(
            name="Synthetic concise user",
            instructions="Give concise factual replies.",
            disclosure_policy="Give approved facts only when asked.",
            unknown_answer_policy="Say when a requested fact is unknown.",
            examples=[],
            reviewed_subject_safe=True,
        )
        write_json(persona_path, persona)
        persona_id = self.run("persona", "create", str(persona_path))["id"]
        self.run("persona", "inspect", persona_id)
        case_path = self.output / "case.json"
        drafted = self.run(
            "case",
            "draft",
            session,
            "--repo",
            str(repo),
            "--commit",
            baseline,
            "--out",
            str(case_path),
        )
        case = json.loads(case_path.read_text())
        case.update(
            title="Synthetic punctuation clarification",
            intent_confirmed=True,
            facts_reviewed=True,
            unresolved_issues=[],
            persona=None,
            persona_id=persona_id,
            allowed_facts=[{"fact_id": "punctuation", "text": ANSWER, "curator_authored": True}],
            hidden_files={"check.py": "check.py"},
            reference_patch="reference.patch",
            criteria=[
                {
                    "criterion_id": "greeting",
                    "kind": "deterministic",
                    "description": "Check punctuation and correct name handling.",
                    "required": True,
                    "entrypoint": "check.py",
                    "command": {
                        "argv": [sys.executable, "{verifier}/check.py"],
                        "timeout_seconds": 30,
                        "max_output_bytes": 65536,
                    },
                    "expected_stdout": "GREETING_CHECK_PASSED",
                    "expected_failure_stdout": "GREETING_ASSERTION_FAILED",
                }
            ],
        )
        write_json(case_path, case)
        (self.output / "check.py").write_text(
            'import runpy\ngreet = runpy.run_path("greet.py")["greet"]\n'
            'try:\n    assert greet("Ada") == "Hello, Ada"\n    assert greet("") == "Hello, "\n'
            'except AssertionError:\n    print("GREETING_ASSERTION_FAILED", flush=True)\n    raise SystemExit(1)\n'
            'print("GREETING_CHECK_PASSED", flush=True)\n'
        )
        self.run("case", "validate", str(case_path))
        case_id = self.run("case", "freeze", str(case_path))["id"]
        self.run("case", "inspect", case_id)
        self.run("store", "verify", drafted["repository_id"])
        return {
            "session_id": session,
            "persona_id": persona_id,
            "case_id": case_id,
            "repository_id": drafted["repository_id"],
        }

    def profiles(self) -> tuple[str, str]:
        inputs = self.output / "selected-inputs"
        inputs.mkdir()
        (inputs / "AGENTS.md").write_text(
            "Synthetic instruction: ask for unspecified punctuation before editing.\n"
        )
        (inputs / "config.toml").write_text('model = "synthetic-model"\n')
        skill = inputs / "skills/greeting-review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: greeting-review\ndescription: Review greeting behavior when asked to fix a greeting.\n---\nCheck punctuation and empty names.\n"
        )
        spec = {
            "recipe": {
                "agent": "codex",
                "executable": sys.executable,
                "version": platform.python_version(),
                "model": "synthetic-model",
                "workflow": "synthetic-base",
            },
            "include_roots": {"selected": "selected-inputs"},
            "assets": [
                {
                    "root": "selected",
                    "path": path,
                    "kind": kind,
                    "layer": "global",
                    "target_root": "config",
                    "target": path,
                }
                for path, kind in (
                    ("config.toml", "config"),
                    ("AGENTS.md", "instruction"),
                    ("skills/greeting-review", "skill"),
                )
            ],
        }
        capture_path = self.output / "capture.json"
        write_json(capture_path, spec)
        baseline = self.run("profile", "capture", "base", "--spec", str(capture_path))["id"]
        self.run("profile", "inspect", baseline)
        derive_path = self.output / "derive.json"
        write_json(derive_path, {"recipe_changes": {"model": "synthetic-model-variant"}})
        changed = self.run(
            "profile", "derive", baseline, "--spec", str(derive_path), "--name", "changed"
        )["id"]
        self.run("profile", "diff", baseline, changed)
        (self.output / "preflight-workspace").mkdir()
        self.run(
            "profile",
            "preflight",
            changed,
            "--destination",
            str(self.output / "preflight-profile"),
            "--workspace",
            str(self.output / "preflight-workspace"),
        )
        return baseline, changed

    def experiment(self, case: str, profiles: tuple[str, str]) -> str:
        draft = {
            "name": "Synthetic offline model-label comparison",
            "cases": [case],
            "variants": [
                {"name": name, "profile": profile}
                for name, profile in zip(("base", "changed"), profiles, strict=True)
            ],
            "repetitions": REPETITIONS,
            "seed": 17,
            "simulator": {
                "kind": "scripted",
                "script": [
                    {
                        "assistant": "Which punctuation?",
                        "decision": {
                            "action": "reply",
                            "text": ANSWER,
                            "fact_ids": ["punctuation"],
                            "reason": "The approved fact answers the question.",
                        },
                    },
                    {
                        "assistant": "Done.",
                        "decision": {
                            "action": "stop",
                            "reason": "The synthetic subject reported its change.",
                        },
                    },
                ],
            },
            "fixture": [
                {"prompt": PROMPT, "assistant": "Which punctuation?"},
                {"prompt": ANSWER, "assistant": "Done.", "files": {"greet.py": SOLUTION}},
            ],
            "scoring": {
                "prices": {
                    "version": "synthetic-example-not-market-prices",
                    "effective_date": "2026-09-09",
                    "currency": "USD",
                    "rates": [
                        {
                            "model": model,
                            "uncached_input": 1.0,
                            "cache_read": 0.1,
                            "cache_write": 1.0,
                            "output": 2.0,
                        }
                        for model in ("synthetic-model", "synthetic-model-variant")
                    ],
                }
            },
        }
        path = self.output / "experiment.json"
        write_json(path, draft)
        self.run("experiment", "validate", str(path))
        identifier = self.run("experiment", "create", str(path), "--name", "synthetic-example")[
            "id"
        ]
        if not isinstance(identifier, str):
            raise ValueError("Experiment creation did not return an object ID.")
        self.run("experiment", "inspect", identifier)
        return identifier

    def execute(self, experiment: str) -> dict[str, Any]:
        run = self.run("run", experiment, "--mode", "offline-fixture")
        run_id = run["run_id"]
        self.run("run", "--status", run_id)
        for capture in run["pending_assessment"]:
            self.run("capture", capture)
        self.run("run", "--resume", run_id)
        self.run("assess", run_id)
        report = self.run("report", run_id)
        comparison = self.run(
            "compare", run_id, run_id, "--before-variant", "base", "--after-variant", "changed"
        )
        write_json(self.output / "report.json", report)
        write_json(self.output / "comparison.json", comparison)
        bundle = self.output / "results.tar"
        self.run("export", run_id, "--output", str(bundle))
        imported = self.output / "imported-store"
        portable = self.run("import", str(bundle), store=imported)["roots"][0]
        portable_report = self.run("report", portable, store=imported)
        write_json(self.output / "portable-report.json", portable_report)
        self.run(
            "compare",
            portable,
            portable,
            "--before-variant",
            "base",
            "--after-variant",
            "changed",
            store=imported,
        )
        if comparison["paired_count"] != REPETITIONS or any(
            group["eligible_passes"] != REPETITIONS for group in report["groups"]
        ):
            raise ValueError(
                "Synthetic fixture did not produce two eligible passing pairs; inspect saved evidence."
            )
        return {
            "run_id": run_id,
            "portable_report_id": portable,
            "paired_count": comparison["paired_count"],
        }

    def complete(self) -> dict[str, Any]:
        version = subprocess.run(
            [str(self.executable), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        repo, baseline, future = self.prepare_source()
        ids = self.curate(repo, baseline)
        profiles = self.profiles()
        experiment = self.experiment(ids["case_id"], profiles)
        results = self.execute(experiment)
        if self.git(repo, "rev-parse", "HEAD") != future or self.git(repo, "status", "--porcelain"):
            raise ValueError("Synthetic source repository changed after preparation.")
        summary = {
            "evidence": "synthetic logs, scripted simulator and offline-fixture subject; no model calls",
            "pricing": "synthetic saved rates, not current model prices; fixture roles incur no model spend",
            "version": version,
            "python": sys.version,
            "dryheave": str(self.executable),
            "output": str(self.output),
            "store": str(self.store),
            "source_repo": str(repo),
            "baseline_commit": baseline,
            "future_commit": future,
            "source_preserved": True,
            **ids,
            "profile_ids": list(profiles),
            "experiment_id": experiment,
            **results,
            "inputs": {
                name: str(self.output / name)
                for name in (
                    "case.json",
                    "persona.json",
                    "capture.json",
                    "derive.json",
                    "experiment.json",
                    "check.py",
                    "reference.patch",
                )
            },
            "command_records": str(self.output / "commands"),
        }
        write_json(self.output / "summary.json", summary)
        return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synthetic public-CLI-only offline demonstration; no model calls."
    )
    parser.add_argument(
        "--dryheave", type=Path, required=True, help="Absolute installed dryheave executable."
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Fresh directory for disposable synthetic inputs and results.",
    )
    arguments = parser.parse_args()
    try:
        summary = Demo(arguments.dryheave, arguments.output).complete()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr, file=sys.stderr)
        parser.exit(1, f"offline_demo: {error}\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
