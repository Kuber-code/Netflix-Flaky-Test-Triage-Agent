"""Resolve who owns a test.

A quarantine recommendation with no owner is a recommendation to nobody, and
nobody acts on it. So every recommendation carries an owner and, just as
importantly, **where that owner came from** -- a name from `CODEOWNERS` is a
statement of responsibility, while the last committer to a file is a guess that
happens to be usually right. Presenting the two identically would let a guess
inherit the authority of a declaration.

Resolution order, most authoritative first:

1. ``CODEOWNERS``, using the last matching rule (GitHub's own precedence).
2. The last committer to the test's file, via ``git log``.
3. Unresolved -- reported as such, never invented.
"""

from __future__ import annotations

import re
import subprocess  # fixed argv, shell=False; see _last_committer
from enum import StrEnum
from pathlib import Path
from typing import Final

from flaketriage.obs import get_logger

log = get_logger(__name__)

#: Where GitHub looks for CODEOWNERS, in its documented order of precedence.
CODEOWNERS_LOCATIONS: Final = (
    Path(".github/CODEOWNERS"),
    Path("CODEOWNERS"),
    Path("docs/CODEOWNERS"),
)

_GIT_TIMEOUT_SECONDS: Final = 15
_COMMENT = re.compile(r"#.*$")


class OwnerSource(StrEnum):
    CODEOWNERS = "codeowners"
    LAST_COMMITTER = "last_committer"
    UNRESOLVED = "unresolved"


class Owner:
    """A resolved owner and the evidence behind it."""

    __slots__ = ("name", "source")

    def __init__(self, name: str, source: OwnerSource) -> None:
        self.name = name
        self.source = source

    @property
    def resolved(self) -> bool:
        return self.source is not OwnerSource.UNRESOLVED

    @property
    def is_authoritative(self) -> bool:
        """Whether the owner was declared rather than inferred."""
        return self.source is OwnerSource.CODEOWNERS

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Owner({self.name!r}, {self.source.value})"


UNRESOLVED: Final = Owner("", OwnerSource.UNRESOLVED)


class CodeOwners:
    """Parsed CODEOWNERS rules.

    Deliberately a subset of the real syntax: literal paths, directory prefixes,
    and ``*`` globs. Character classes and negation are not supported, and a rule
    using them simply will not match rather than matching wrongly -- so the
    fallback runs and the owner is labelled as a guess. Silently mis-resolving an
    owner is worse than declining to.
    """

    def __init__(self, rules: list[tuple[str, tuple[str, ...]]]) -> None:
        self._rules = rules

    @property
    def rules(self) -> list[tuple[str, tuple[str, ...]]]:
        return list(self._rules)

    @classmethod
    def parse(cls, text: str) -> CodeOwners:
        rules: list[tuple[str, tuple[str, ...]]] = []
        for raw in text.splitlines():
            line = _COMMENT.sub("", raw).strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            pattern, *owners = parts
            rules.append((pattern, tuple(owners)))
        return cls(rules)

    @classmethod
    def load(cls, root: Path) -> CodeOwners | None:
        for candidate in CODEOWNERS_LOCATIONS:
            path = root / candidate
            if path.is_file():
                try:
                    return cls.parse(path.read_text(encoding="utf-8", errors="replace"))
                except OSError as exc:  # pragma: no cover - unreadable file
                    log.warning("codeowners_unreadable", path=str(path), error=str(exc))
        return None

    def owners_for(self, path: str) -> tuple[str, ...]:
        """Owners of ``path``. The **last** matching rule wins, as GitHub does."""
        target = path.replace("\\", "/").lstrip("./")
        matched: tuple[str, ...] = ()
        for pattern, owners in self._rules:
            if _matches(pattern, target):
                matched = owners
        return matched


def _matches(pattern: str, path: str) -> bool:
    cleaned = pattern.strip()
    if not cleaned:
        return False
    if cleaned == "*":
        return True

    anchored = cleaned.startswith("/")
    cleaned = cleaned.lstrip("/")

    # A trailing slash, or a bare directory name, owns everything beneath it.
    if cleaned.endswith("/"):
        prefix = cleaned
        return path.startswith(prefix) if anchored else f"/{path}".find(f"/{prefix}") >= 0

    if "*" not in cleaned:
        if path == cleaned:
            return True
        # Directory prefix without a trailing slash.
        if path.startswith(cleaned + "/"):
            return True
        return not anchored and (path.endswith("/" + cleaned) or f"/{cleaned}/" in f"/{path}")

    return _glob_matches(cleaned, path, anchored=anchored)


def _glob_matches(pattern: str, path: str, *, anchored: bool) -> bool:
    """Translate the supported glob subset into a regex.

    ``**`` crosses directory separators; a single ``*`` does not. fnmatch is not
    used because its ``*`` matches ``/``, which would make ``tests/*.py`` own
    every file in every subdirectory.
    """
    parts = re.split(r"(\*\*/|\*\*|\*)", pattern)
    expression = ""
    for part in parts:
        if part == "**/":
            expression += r"(?:[^/]+/)*"
        elif part == "**":
            expression += r".*"
        elif part == "*":
            expression += r"[^/]*"
        else:
            expression += re.escape(part)

    if anchored:
        return re.fullmatch(expression, path) is not None
    return (
        re.fullmatch(expression, path) is not None
        or re.search(f"(^|/){expression}$", path) is not None
    )


def last_committer(path: str, *, cwd: Path | None = None) -> str | None:
    """Last person to touch a file, via ``git log``.

    A fallback, not an answer: the last committer may have fixed a typo in a test
    somebody else owns. That is why the source is recorded and surfaced.
    """
    argv = ["git", "log", "-1", "--format=%an", "--", path]
    try:
        completed = subprocess.run(  # noqa: S603 -- fixed argv, shell=False
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("git_log_failed", path=path, error=str(exc))
        return None
    if completed.returncode != 0:
        return None
    name = completed.stdout.strip()
    return name or None


def resolve_owner(
    file_path: str | None,
    *,
    codeowners: CodeOwners | None = None,
    repo_root: Path | None = None,
    allow_git: bool = True,
) -> Owner:
    """Resolve an owner, preferring a declaration over an inference."""
    if not file_path:
        return UNRESOLVED

    if codeowners is not None:
        owners = codeowners.owners_for(file_path)
        if owners:
            return Owner(", ".join(owners), OwnerSource.CODEOWNERS)

    if allow_git:
        committer = last_committer(file_path, cwd=repo_root)
        if committer:
            return Owner(committer, OwnerSource.LAST_COMMITTER)

    return UNRESOLVED
