"""Extract the source files a failure actually touched.

Two consumers need this. The detector uses it for signal 3 -- "did the change
under test touch any code this test exercises?" -- and the classifier uses it to
decide which diff hunks are worth spending context on.

The hard part is not finding paths in a stack trace, it is discarding the ones
that belong to the framework. A pytest traceback is mostly ``_pytest`` internals
and a Java one mostly ``java.base``; treating those as the test's footprint would
make every test look like it exercises the entire runtime, and signal 3 would
never fire.
"""

from __future__ import annotations

import re
from typing import Final

# Filenames with a recognized source extension, optionally with directories.
_PATH_TOKEN: Final = re.compile(
    r"(?:[\w.\-]+[/\\])*[\w.\-]+"
    r"\.(?:py|pyi|pyx|js|jsx|mjs|cjs|ts|tsx|java|kt|kts|go|rb|cs|php|scala|rs|swift)"
)

# Substrings that mark a frame as belonging to a framework, a dependency or the
# language runtime rather than to this repository. Not tuning knobs -- facts
# about how these ecosystems lay out their code.
_NOISE_MARKERS: Final = (
    "site-packages/",
    "dist-packages/",
    "node_modules/",
    "/usr/lib/",
    "/usr/local/lib/",
    "_pytest/",
    "pluggy/",
    "unittest/case.py",
    "runpy.py",
    "asyncio/base_events.py",
    "jest-circus/",
    "jest-runtime/",
    "jest-each/",
    "internal/process/",
    "internal/modules/",
    "playwright-core/",
    "/gems/",
    "vendor/bundle/",
)

# JVM stack frames put the noise marker in the *package* qualifier, not in the
# filename: `at java.base/java.util.concurrent.ThreadPoolExecutor$Worker.run(
# ThreadPoolExecutor.java:642)` yields the token `ThreadPoolExecutor.java`, which
# looks like project code on its own. These markers are therefore matched against
# the whole line, and every path on a matching line is discarded.
_NOISE_LINE_MARKERS: Final = (
    "java.base/",
    "java.util.",
    "java.lang.reflect",
    "java.lang.thread",
    "jdk.internal",
    "jdk.proxy",
    "sun.nio",
    "sun.reflect",
    "org.junit.",
    "org.gradle.",
    "org.apache.maven.surefire",
    "org.testng.",
    "kotlin.coroutines",
)


def normalize(path: str) -> str:
    """Canonicalize separators and strip leading ``./`` noise."""
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def is_project_frame(path: str) -> bool:
    """Whether a path plausibly belongs to the repository under test."""
    candidate = normalize(path).lower()
    if not candidate:
        return False
    if candidate.startswith("/"):
        # An absolute path on the runner is almost always a dependency or the
        # runtime; repository-relative paths are what reporters emit for own code.
        return not any(marker in candidate for marker in _NOISE_MARKERS)
    return not any(marker in candidate for marker in _NOISE_MARKERS)


def is_noise_line(line: str) -> bool:
    """Whether a whole trace line belongs to a framework or the runtime."""
    lowered = line.lower()
    return any(marker in lowered for marker in _NOISE_LINE_MARKERS)


def extract_paths(text: str | None, *, project_only: bool = True) -> tuple[str, ...]:
    """Source paths mentioned in a stack trace, in order of first appearance.

    Scanning is line by line so that a frame's package qualifier can veto the
    filename it contains -- see :data:`_NOISE_LINE_MARKERS`.

    Order is preserved because the first project frame in a traceback is usually
    the most relevant one, and the classifier's context budget is spent head-first.
    """
    if not text:
        return ()

    seen: dict[str, None] = {}
    for line in text.splitlines():
        if project_only and is_noise_line(line):
            continue
        for match in _PATH_TOKEN.finditer(line):
            candidate = normalize(match.group(0))
            if not candidate:
                continue
            if project_only and not is_project_frame(candidate):
                continue
            seen.setdefault(candidate, None)
    return tuple(seen)


def footprint(
    *, test_file: str | None, stack_trace: str | None, failure_message: str | None = None
) -> frozenset[str]:
    """Files this test is known to exercise.

    The test's own file is always included: a change to the test itself is the
    most direct possible explanation for the test's failure, and omitting it
    would let signal 3 declare a failure "unrelated to your change" when the
    change edited the test.
    """
    paths: set[str] = set()
    if test_file:
        paths.add(normalize(test_file))
    paths.update(extract_paths(stack_trace))
    paths.update(extract_paths(failure_message))
    return frozenset(paths)


def diff_touches_footprint(diff_paths: frozenset[str], test_footprint: frozenset[str]) -> bool:
    """Whether any changed path matches any footprint path.

    Matching is by normalized suffix in both directions, because a stack frame's
    path and a diff's path are produced by different tools with different notions
    of the repository root.
    """
    for changed in diff_paths:
        changed_norm = normalize(changed)
        for known in test_footprint:
            known_norm = normalize(known)
            if changed_norm == known_norm:
                return True
            if changed_norm.endswith("/" + known_norm) or known_norm.endswith("/" + changed_norm):
                return True
    return False
