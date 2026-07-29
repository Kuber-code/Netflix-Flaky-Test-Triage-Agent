"""Test identity: the stable primary key for a logical test.

Naive identity -- the raw ``classname#method`` string a reporter emits -- breaks
in three ordinary situations, each of which silently destroys history:

* **Parameterized tests.** ``test_login[user=alice]`` and
  ``test_login[user=bob]`` are separate cases that share a logical parent. Treat
  them as one test and you lose per-case flake rates; treat them as unrelated
  and a suite with a 200-case parameter matrix has 200 tests with no history.
* **Dialect disagreement.** pytest reports a dotted module path in
  ``classname``, Surefire a Java FQCN, Playwright a nested describe path, and
  jest-junit whatever the reporter was configured to emit. The same physical
  test yields different strings depending on who wrote the reporter.
* **Renames and moves.** Handled separately by alias resolution in phase P2,
  because it needs cross-run evidence and cannot be decided from one case.

This module covers the first two: normalize the dialects into a
``(suite_path, test_name, parameters)`` triple and hash it. What it does not
cover is stated in ADR-0002 rather than hidden.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

from flaketriage.models import TestCaseResult, TestIdentity

# Trailing bracketed segments carry parameterization in pytest, JUnit 5
# @ParameterizedTest display names, and Go subtests rendered into JUnit XML.
# Nested brackets occur (``test_x[a[0]=1]``), so the match is greedy from the
# first bracket that closes at end of string.
_TRAILING_BRACKET: Final = re.compile(r"^(?P<base>.*?)\[(?P<params>.*)\]$")

# A Java/C#-style class segment: leading capital, no separators. Used to decide
# whether the tail of a dotted classname is a class rather than a module.
_CLASS_SEGMENT: Final = re.compile(r"^[A-Z][A-Za-z0-9_]*$")

_SOURCE_SUFFIXES: Final = (".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rb")

_FINGERPRINT_LENGTH: Final = 16


def split_parameters(name: str) -> tuple[str, str]:
    """Split ``test_login[user=alice]`` into ``("test_login", "user=alice")``.

    Only the bracket convention is recognized. Reporters that flatten parameters
    into the name with a separator (``"adds 1 + 2"``) are indistinguishable from
    ordinary test names without a per-framework rule, so no guess is made --
    guessing here would merge genuinely distinct tests, which is the more
    damaging error.
    """
    match = _TRAILING_BRACKET.match(name.strip())
    if match is None:
        return name.strip(), ""
    base = match.group("base").strip()
    params = match.group("params").strip()
    # ``[tag]`` with no base is a name, not a parameter list.
    if not base:
        return name.strip(), ""
    return base, params


def normalize_suite_path(*, classname: str, suite_path: str, file_path: str | None) -> str:
    """Derive one comparable suite path from whichever fields the dialect filled.

    Preference order is most-specific-first: a real file path is the most stable
    thing a reporter can give us, a dotted classname is next, and the nested
    ``<testsuite>`` names are the fallback for reporters that provide neither.
    """
    if file_path:
        base = _normalize_file_path(file_path)
        qualifier = _class_qualifier(classname)
        return f"{base}::{qualifier}" if qualifier else base

    if classname:
        return _normalize_dotted(classname)

    return _normalize_file_path(suite_path)


def fingerprint(suite_path: str, test_name: str, parameters: str = "") -> str:
    """Content-addressed identity for a logical test instance.

    A hash rather than the concatenated string so that the value is fixed-width
    and safe to index, and so that a change in the normalization rules produces
    a visibly different key instead of a silently reinterpreted one.
    """
    payload = "\x1f".join((suite_path, test_name, parameters)).encode()
    return hashlib.sha256(payload).hexdigest()[:_FINGERPRINT_LENGTH]


def identity_for(case: TestCaseResult) -> TestIdentity:
    """Resolve a parsed case into its stable identity."""
    test_name, parameters = split_parameters(case.name)
    suite_path = normalize_suite_path(
        classname=case.classname,
        suite_path=case.suite_path,
        file_path=case.file_path,
    )
    return TestIdentity(
        fingerprint=fingerprint(suite_path, test_name, parameters),
        suite_path=suite_path,
        test_name=test_name,
        parameters=parameters,
        file_path=_normalize_file_path(case.file_path) if case.file_path else None,
    )


def _normalize_file_path(path: str) -> str:
    """Canonicalize separators and strip the noise CI prefixes add."""
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _normalize_dotted(classname: str) -> str:
    """Turn a dotted module or FQCN into a path, keeping the class distinct.

    ``tests.unit.test_login`` becomes ``tests/unit/test_login``, while
    ``com.example.OrderTest`` becomes ``com/example::OrderTest`` so that two
    classes in one package do not collapse into a single suite path.
    """
    cleaned = classname.strip().strip(".")
    if not cleaned:
        return ""
    if "/" in cleaned or cleaned.endswith(_SOURCE_SUFFIXES):
        # Already a path (jest-junit and some mocha configurations do this).
        return _normalize_file_path(cleaned)

    segments = cleaned.split(".")
    if len(segments) > 1 and _CLASS_SEGMENT.match(segments[-1]):
        return "/".join(segments[:-1]) + "::" + segments[-1]
    return "/".join(segments)


def _class_qualifier(classname: str) -> str:
    """Extract the class portion of a classname, if it names a class at all.

    When a file path is already available, the module part of the classname is
    redundant; only the class adds information.
    """
    cleaned = classname.strip().strip(".")
    if not cleaned:
        return ""
    segments = re.split(r"[./]", cleaned)
    tail = segments[-1]
    if _CLASS_SEGMENT.match(tail):
        return tail
    return ""
