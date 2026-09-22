import re
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from dryheave.errors import DryheaveError, InputError, LimitError
from dryheave.logs import claude, codex
from dryheave.logs.base import (
    ImportLimits,
    Session,
    discover,
    recorded_baseline,
    recorded_cwd,
)
from dryheave.models import AgentKind, StrictModel
from dryheave.repositories import Git, SnapshotLimits

DEFAULT_HISTORY_LIMIT = 200
MAX_HISTORY_LIMIT = 2000
DEFAULT_CANDIDATE_LIMIT = 10
MAX_CANDIDATE_LIMIT = 50
MIN_CONVENTION_SAMPLE = 20
PREFIX_SHARE_THRESHOLD = 0.6
VOCABULARY_COVERAGE = 0.9
MAX_VOCABULARY = 12
MAX_REPORTED_PATHS = 20
MAX_BODY_CHARACTERS = 2000
MAX_REPORTED_SKIPS = 20
MAX_SHAPE_FILES = 5
BYTES_PER_COMMIT = 16384
NUMSTAT_COLUMNS = 3
LOG_FIELDS = 5

RECORD = "\x1e"
FIELD = "\x1f"
LOG_FORMAT = f"{RECORD}%H{FIELD}%P{FIELD}%s{FIELD}%b{FIELD}"
COMMIT_REFERENCE = re.compile(r"^[0-9a-f]{7,64}$")
SUBJECT_PREFIX = re.compile(
    r"^([A-Za-z][A-Za-z0-9._-]{0,23})(?:\([^()\s][^()]{0,39}\))?!?\s*:\s+\S"
)
BUG_WORDS = re.compile(
    r"\b(?:fix(?:e[sd]|ing)?|bug(?:s|gy)?|broken|regress(?:ion|ions|ed)?|crash(?:e[sd]|ing)?"
    r"|fail(?:s|ed|ing|ure|ures)?|incorrect(?:ly)?|wrong(?:ly)?|defect(?:s)?|hotfix"
    r"|repair(?:s|ed|ing)?|misbehav\w*)\b",
    re.IGNORECASE,
)
BUG_VOCABULARY = ("fix", "bug", "hotfix", "repair", "defect", "regress", "crash", "broken")
DOCUMENTATION_SUFFIXES = (".md", ".markdown", ".rst", ".txt", ".adoc")
DOCUMENTATION_NAMES = (
    "license",
    "licence",
    "notice",
    "changelog",
    "authors",
    "contributing",
    "readme",
    "codeowners",
)
DOCUMENTATION_DIRECTORIES = ("docs", "doc", "documentation")
CONFIGURATION_SUFFIXES = (
    ".cfg",
    ".conf",
    ".ini",
    ".json",
    ".lock",
    ".properties",
    ".toml",
    ".yaml",
    ".yml",
)
CONFIGURATION_NAMES = (".dockerignore", ".editorconfig", ".gitattributes", ".gitignore")

ChangeClass = Literal["code", "documentation", "configuration", "support", "empty"]
JoinState = Literal["unique", "ambiguous", "none", "no_parent", "not_scanned"]
RepositoryMatch = Literal["same", "inside", "different", "unknown"]


class ScannedSession(StrictModel):
    path: str
    agent: AgentKind
    source_id: str | None
    parent_session_id: str | None
    cwd: str | None
    baseline_commit: str | None


class SkippedSession(StrictModel):
    path: str
    reason: str


class SessionScan(StrictModel):
    root: str
    agent: AgentKind
    sessions: tuple[ScannedSession, ...]
    sessions_with_baseline: int = Field(ge=0)
    skipped: tuple[SkippedSession, ...]
    skipped_count: int = Field(ge=0)


class SessionMatch(StrictModel):
    path: str
    agent: AgentKind
    source_id: str | None
    parent_session_id: str | None
    cwd: str | None
    baseline_commit: str
    repository_match: RepositoryMatch


class VocabularyEntry(StrictModel):
    token: str
    commits: int = Field(ge=0)
    share: float = Field(ge=0.0, le=1.0)
    bug_indicating: bool


class ConventionReport(StrictModel):
    detected: bool
    sampled: int = Field(ge=0)
    prefixed: int = Field(ge=0)
    prefixed_share: float = Field(ge=0.0, le=1.0)
    distinct_prefixes: int = Field(ge=0)
    vocabulary: tuple[VocabularyEntry, ...]
    vocabulary_coverage: float = Field(ge=0.0, le=1.0)
    minimum_sample: int
    share_threshold: float
    coverage_threshold: float
    maximum_vocabulary: int
    reason: str


class CommitCandidate(StrictModel):
    sha: str
    subject: str
    parent: str | None
    tier: Literal[1, 2, 3]
    signal: str
    change_class: ChangeClass
    files_changed: int = Field(ge=0)
    insertions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    paths: tuple[str, ...]
    paths_truncated: bool
    join: JoinState
    join_note: str
    sessions: tuple[SessionMatch, ...]


class ExcludedCommit(StrictModel):
    sha: str
    subject: str
    reason: Literal["merge", "documentation", "configuration", "support", "empty"]


class CommitSurvey(StrictModel):
    repository: str
    head: str
    history_limit: int
    commits_read: int = Field(ge=0)
    merges_excluded: int = Field(ge=0)
    change_class_excluded: int = Field(ge=0)
    excluded: tuple[ExcludedCommit, ...]
    convention: ConventionReport
    tier: Literal[0, 1, 2, 3]
    tier_note: str
    candidate_limit: int
    candidates: tuple[CommitCandidate, ...]
    log_root: str | None
    sessions_scanned: int | None
    sessions_with_baseline: int | None
    sessions_skipped: int | None
    skipped: tuple[SkippedSession, ...]
    join_note: str


class CommitResolution(StrictModel):
    repository: str
    commit: str
    subject: str
    parent: str | None
    change_class: ChangeClass
    files_changed: int = Field(ge=0)
    join: JoinState
    join_note: str
    sessions: tuple[SessionMatch, ...]
    log_root: str | None
    sessions_scanned: int | None


class _Commit:
    def __init__(self, sha: str, parents: tuple[str, ...], subject: str, body: str) -> None:
        self.sha = sha
        self.parents = parents
        self.subject = subject
        self.body = body
        self.paths: list[str] = []
        self.insertions = 0
        self.deletions = 0

    @property
    def merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def parent(self) -> str | None:
        return self.parents[0] if self.parents else None

    @property
    def change_class(self) -> ChangeClass:
        if not self.paths:
            return "empty"
        kinds = {_path_class(path) for path in self.paths}
        if "other" in kinds:
            return "code"
        if kinds == {"documentation"}:
            return "documentation"
        if kinds == {"configuration"}:
            return "configuration"
        return "support"


def _path_class(path: str) -> Literal["documentation", "configuration", "other"]:
    cleaned = path.rsplit(" => ", maxsplit=1)[-1].strip("}").strip()
    name = cleaned.rsplit("/", 1)[-1].lower()
    parts = [part.lower() for part in cleaned.split("/")[:-1]]
    stem = name.split(".")[0]
    suffix = f".{name.rsplit('.', 1)[-1]}" if "." in name[1:] else ""
    if (
        suffix in DOCUMENTATION_SUFFIXES
        or stem in DOCUMENTATION_NAMES
        or any(part in DOCUMENTATION_DIRECTORIES for part in parts)
    ):
        return "documentation"
    if suffix in CONFIGURATION_SUFFIXES or name in CONFIGURATION_NAMES:
        return "configuration"
    return "other"


def _open(repo: Path) -> Git:
    if repo.is_symlink() or not repo.is_dir():
        raise InputError("Surveyed repository must be a real directory.")
    return Git(repo.absolute(), SnapshotLimits())


def _head(git: Git) -> str:
    return git.run("rev-parse", "--verify", "HEAD^{commit}", limit=4096).stdout.decode().strip()


def _numstat(tail: str, commit: _Commit) -> None:
    for line in tail.splitlines():
        columns = line.split("\t")
        if len(columns) != NUMSTAT_COLUMNS:
            continue
        added, removed, path = columns
        commit.insertions += int(added) if added.isdigit() else 0
        commit.deletions += int(removed) if removed.isdigit() else 0
        commit.paths.append(path)


def _history(git: Git, limit: int, reference: str = "HEAD") -> list[_Commit]:
    raw = git.run(
        "log",
        f"--max-count={limit}",
        "--numstat",
        f"--format={LOG_FORMAT}",
        reference,
        limit=limit * BYTES_PER_COMMIT,
    ).stdout.decode("utf-8", errors="replace")
    commits: list[_Commit] = []
    for chunk in raw.split(RECORD)[1:]:
        fields = chunk.split(FIELD)
        if len(fields) < LOG_FIELDS:
            continue
        commit = _Commit(
            fields[0].strip(),
            tuple(fields[1].split()),
            fields[2],
            FIELD.join(fields[3:-1])[:MAX_BODY_CHARACTERS],
        )
        _numstat(fields[-1], commit)
        commits.append(commit)
    return commits


def _prefix_token(subject: str) -> str | None:
    match = SUBJECT_PREFIX.match(subject)
    return re.sub(r"\d+", "#", match.group(1).lower()) if match else None


def _convention(subjects: list[str]) -> ConventionReport:
    tokens = [token for token in (_prefix_token(subject) for subject in subjects) if token]
    counts = Counter(tokens)
    sampled, prefixed = len(subjects), len(tokens)
    share = prefixed / sampled if sampled else 0.0
    target = VOCABULARY_COVERAGE * prefixed
    selected: list[tuple[str, int]] = []
    covered = 0
    for token, count in counts.most_common():
        if covered >= target:
            break
        selected.append((token, count))
        covered += count
    coverage = covered / prefixed if prefixed else 0.0
    detected = (
        sampled >= MIN_CONVENTION_SAMPLE
        and share >= PREFIX_SHARE_THRESHOLD
        and bool(selected)
        and len(selected) <= MAX_VOCABULARY
        and coverage >= VOCABULARY_COVERAGE
    )
    return ConventionReport(
        detected=detected,
        sampled=sampled,
        prefixed=prefixed,
        prefixed_share=round(share, 4),
        distinct_prefixes=len(counts),
        vocabulary=tuple(
            VocabularyEntry(
                token=token,
                commits=count,
                share=round(count / prefixed, 4) if prefixed else 0.0,
                bug_indicating=_bug_token(token),
            )
            for token, count in selected[:MAX_VOCABULARY]
        ),
        vocabulary_coverage=round(coverage, 4),
        minimum_sample=MIN_CONVENTION_SAMPLE,
        share_threshold=PREFIX_SHARE_THRESHOLD,
        coverage_threshold=VOCABULARY_COVERAGE,
        maximum_vocabulary=MAX_VOCABULARY,
        reason=_convention_reason(sampled, prefixed, share, selected, coverage, detected),
    )


def _convention_reason(
    sampled: int,
    prefixed: int,
    share: float,
    selected: list[tuple[str, int]],
    coverage: float,
    detected: bool,
) -> str:
    measured = (
        f"{prefixed} of {sampled} sampled subjects carry a prefix token ({share:.0%}); "
        f"{len(selected)} token(s) cover {coverage:.0%} of those."
    )
    if detected:
        return f"Convention detected: {measured} Thresholds: at least {MIN_CONVENTION_SAMPLE} sampled commits, {PREFIX_SHARE_THRESHOLD:.0%} prefixed, {VOCABULARY_COVERAGE:.0%} covered by at most {MAX_VOCABULARY} tokens."
    if sampled < MIN_CONVENTION_SAMPLE:
        return f"No convention: {measured} Fewer than {MIN_CONVENTION_SAMPLE} commits were sampled, so a dominating vocabulary cannot be established."
    if share < PREFIX_SHARE_THRESHOLD:
        return f"No convention: {measured} The prefixed share is below the {PREFIX_SHARE_THRESHOLD:.0%} threshold."
    if len(selected) > MAX_VOCABULARY:
        return f"No convention: {measured} More than {MAX_VOCABULARY} tokens are needed to cover {VOCABULARY_COVERAGE:.0%} of the prefixed subjects, which is free text rather than a classifying vocabulary."
    return f"No convention: {measured} No small token set covers {VOCABULARY_COVERAGE:.0%} of the prefixed subjects."


def _bug_token(token: str) -> bool:
    return any(word in token for word in BUG_VOCABULARY)


def _tier_one(commits: list[_Commit], convention: ConventionReport) -> list[tuple[_Commit, str]]:
    if not convention.detected:
        return []
    bugish = sorted(entry.token for entry in convention.vocabulary if entry.bug_indicating)
    if not bugish:
        return []
    selected: list[tuple[_Commit, str]] = []
    for commit in commits:
        token = _prefix_token(commit.subject)
        if token in bugish:
            selected.append(
                (
                    commit,
                    f"Subject prefix {token!r} is a bug-indicating member of this repository's detected prefix vocabulary ({', '.join(bugish)}).",
                )
            )
    return selected


def _tier_two(commits: list[_Commit]) -> list[tuple[_Commit, str]]:
    selected: list[tuple[_Commit, str]] = []
    for commit in commits:
        found = list(
            dict.fromkeys(
                match.group(0).lower()
                for match in BUG_WORDS.finditer(f"{commit.subject}\n{commit.body}")
            )
        )
        if found:
            selected.append(
                (
                    commit,
                    f"Bug-indicating words in the commit message: {', '.join(found[:5])}.",
                )
            )
    return selected


def _tier_three(commits: list[_Commit]) -> list[tuple[_Commit, str]]:
    shaped = [commit for commit in commits if 0 < len(commit.paths) <= MAX_SHAPE_FILES]
    shaped.sort(key=lambda commit: (len(commit.paths), commit.insertions + commit.deletions))
    return [
        (
            commit,
            f"Change shape only: {len(commit.paths)} file(s), {commit.insertions + commit.deletions} line(s) changed. Nothing here shows this commit is a bug fix.",
        )
        for commit in shaped
    ]


def _repository_match(cwd: str | None, repo: Path) -> RepositoryMatch:
    if cwd is None:
        return "unknown"
    recorded = Path(cwd)
    if not recorded.is_absolute():
        return "unknown"
    if recorded == repo:
        return "same"
    return "inside" if repo in recorded.parents else "different"


def _matches(
    parent: str | None, index: dict[str, list[ScannedSession]], repo: Path
) -> tuple[tuple[SessionMatch, ...], JoinState, str]:
    if parent is None:
        return (
            (),
            "no_parent",
            "Root commit: it has no parent, so no session baseline can match it.",
        )
    found = index.get(parent.lower(), [])
    matched = tuple(
        SessionMatch(
            path=session.path,
            agent=session.agent,
            source_id=session.source_id,
            parent_session_id=session.parent_session_id,
            cwd=session.cwd,
            baseline_commit=session.baseline_commit or "",
            repository_match=_repository_match(session.cwd, repo),
        )
        for session in found
    )
    if not matched:
        return (
            matched,
            "none",
            "No matching session: no scanned log records this parent as its starting baseline. The commit may be hand-written, or its session may have been deleted or lie outside the scanned root.",
        )
    partial = "A session records only the baseline it started from, so a match identifies that session's first task, not every commit it produced. Confirm the recorded cwd names the same repository."
    if len(matched) > 1:
        return (
            matched,
            "ambiguous",
            f"Ambiguous: {len(matched)} scanned sessions record this parent as their starting baseline, so the commit cannot be attributed to one of them. Each match repeats the source_id and parent_session_id its log records, which is how a subagent or resumed rollout shows up where the log states one. {partial}",
        )
    return matched, "unique", f"One scanned session records this parent as its baseline. {partial}"


def scan_sessions(root: Path, agent: AgentKind, limits: ImportLimits | None = None) -> SessionScan:
    limits = limits or ImportLimits()
    parser = codex.parse if agent == AgentKind.CODEX else claude.parse
    sessions: list[ScannedSession] = []
    skipped: list[SkippedSession] = []
    for path in discover(root, limits):
        try:
            sessions.append(session_baseline(path, parser(path, limits)))
        except (DryheaveError, OSError, ValidationError) as error:
            skipped.append(SkippedSession(path=str(path), reason=str(error)))
    return SessionScan(
        root=str(root.absolute()),
        agent=agent,
        sessions=tuple(sessions),
        sessions_with_baseline=sum(1 for item in sessions if item.baseline_commit),
        skipped=tuple(skipped[:MAX_REPORTED_SKIPS]),
        skipped_count=len(skipped),
    )


def session_baseline(path: Path, session: Session) -> ScannedSession:
    return ScannedSession(
        path=str(path),
        agent=session.agent,
        source_id=session.source_id,
        parent_session_id=session.parent_session_id,
        cwd=recorded_cwd(session),
        baseline_commit=recorded_baseline(session),
    )


def _index(scan: SessionScan | None) -> dict[str, list[ScannedSession]]:
    index: dict[str, list[ScannedSession]] = {}
    for session in scan.sessions if scan is not None else ():
        if session.baseline_commit:
            index.setdefault(session.baseline_commit.lower(), []).append(session)
    return index


def _bounds(history_limit: int, candidate_limit: int) -> None:
    if not 1 <= history_limit <= MAX_HISTORY_LIMIT:
        raise LimitError(f"Survey history must cover 1-{MAX_HISTORY_LIMIT} commits.")
    if not 1 <= candidate_limit <= MAX_CANDIDATE_LIMIT:
        raise LimitError(f"Survey candidates must be limited to 1-{MAX_CANDIDATE_LIMIT}.")


def _eligible(commits: list[_Commit]) -> tuple[list[_Commit], list[ExcludedCommit], int]:
    eligible: list[_Commit] = []
    excluded: list[ExcludedCommit] = []
    merges = 0
    for commit in commits:
        if commit.merge:
            merges += 1
            excluded.append(ExcludedCommit(sha=commit.sha, subject=commit.subject, reason="merge"))
            continue
        kind = commit.change_class
        if kind == "code":
            eligible.append(commit)
        else:
            excluded.append(ExcludedCommit(sha=commit.sha, subject=commit.subject, reason=kind))
    return eligible, excluded, merges


def _select(
    eligible: list[_Commit], convention: ConventionReport
) -> tuple[Literal[1, 2, 3] | None, list[tuple[_Commit, str]], str]:
    chosen = _tier_one(eligible, convention)
    if chosen:
        return (
            1,
            chosen,
            "Tier 1: candidates carry a bug-indicating prefix from the vocabulary this repository's own history shows.",
        )
    chosen = _tier_two(eligible)
    if chosen:
        note = "Tier 2: no usable convention produced candidates, so bug-indicating words anywhere in the subject or body selected these commits."
        return 2, chosen, note
    chosen = _tier_three(eligible)
    if chosen:
        return (
            3,
            chosen,
            f"Tier 3: neither the repository's prefix vocabulary nor bug-indicating words produced candidates, so these commits are ranked by change shape alone - fewest files first, then fewest changed lines, at most {MAX_SHAPE_FILES} files. This tier carries no evidence that any candidate is a bug fix.",
        )
    return (
        None,
        [],
        "No candidate commits: every surveyed commit was a merge, touched only documentation or configuration, or changed no tracked file.",
    )


def survey_repository(
    repo: Path,
    *,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    scan: SessionScan | None = None,
) -> CommitSurvey:
    _bounds(history_limit, candidate_limit)
    git = _open(repo)
    head = _head(git)
    commits = _history(git, history_limit)
    eligible, excluded, merges = _eligible(commits)
    convention = _convention([commit.subject for commit in commits if not commit.merge])
    tier, chosen, note = _select(eligible, convention)
    index = _index(scan)
    absolute = repo.absolute()
    candidates: list[CommitCandidate] = []
    for commit, signal in chosen[:candidate_limit] if tier is not None else []:
        matched, join, join_note = (
            _matches(commit.parent, index, absolute)
            if scan is not None
            else ((), "not_scanned", "No log root was scanned, so no session join was attempted.")
        )
        candidates.append(
            CommitCandidate(
                sha=commit.sha,
                subject=commit.subject,
                parent=commit.parent,
                tier=tier if tier is not None else 3,
                signal=signal,
                change_class=commit.change_class,
                files_changed=len(commit.paths),
                insertions=commit.insertions,
                deletions=commit.deletions,
                paths=tuple(commit.paths[:MAX_REPORTED_PATHS]),
                paths_truncated=len(commit.paths) > MAX_REPORTED_PATHS,
                join=join,
                join_note=join_note,
                sessions=matched,
            )
        )
    return CommitSurvey(
        repository=str(absolute),
        head=head,
        history_limit=history_limit,
        commits_read=len(commits),
        merges_excluded=merges,
        change_class_excluded=len(excluded) - merges,
        excluded=tuple(excluded[:MAX_CANDIDATE_LIMIT]),
        convention=convention,
        tier=tier if tier is not None else 0,
        tier_note=note,
        candidate_limit=candidate_limit,
        candidates=tuple(candidates),
        log_root=scan.root if scan is not None else None,
        sessions_scanned=len(scan.sessions) if scan is not None else None,
        sessions_with_baseline=scan.sessions_with_baseline if scan is not None else None,
        sessions_skipped=scan.skipped_count if scan is not None else None,
        skipped=scan.skipped if scan is not None else (),
        join_note="A commit's parent is the repository state its session started from, so the join matches a session's recorded baseline. Every match is reported; none is asserted as the only possible one.",
    )


def resolve_commit(
    repo: Path,
    reference: str,
    *,
    scan: SessionScan | None = None,
) -> CommitResolution:
    if not COMMIT_REFERENCE.fullmatch(reference):
        raise InputError(
            "Supply an explicit lowercase hexadecimal commit SHA of at least 7 characters; branches and HEAD are not accepted."
        )
    git = _open(repo)
    sha = (
        git.run("rev-parse", "--verify", f"{reference}^{{commit}}", limit=4096)
        .stdout.decode()
        .strip()
    )
    commits = _history(git, 1, sha)
    if not commits:
        raise InputError("The selected commit could not be read from the source repository.")
    commit = commits[0]
    if commit.merge:
        raise InputError(
            "Merge commits record no single starting baseline; select one of the merged commits instead."
        )
    absolute = repo.absolute()
    matched, join, note = (
        _matches(commit.parent, _index(scan), absolute)
        if scan is not None
        else ((), "not_scanned", "No log root was scanned, so no session join was attempted.")
    )
    return CommitResolution(
        repository=str(absolute),
        commit=commit.sha,
        subject=commit.subject,
        parent=commit.parent,
        change_class=commit.change_class,
        files_changed=len(commit.paths),
        join=join,
        join_note=note,
        sessions=matched,
        log_root=scan.root if scan is not None else None,
        sessions_scanned=len(scan.sessions) if scan is not None else None,
    )
