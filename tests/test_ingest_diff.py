from __future__ import annotations

from pathlib import Path

from flaketriage.ingest.diff import parse_diff_file, parse_unified_diff
from flaketriage.models import ChangeType, LineRange

UNIFIED_ZERO = """diff --git a/src/orders/service.py b/src/orders/service.py
index 1a2b3c4..5d6e7f8 100644
--- a/src/orders/service.py
+++ b/src/orders/service.py
@@ -41,0 +42,3 @@ class OrderService:
+    def _retry(self, attempts: int) -> None:
+        for _ in range(attempts):
+            self._flush()
@@ -88 +91 @@ class OrderService:
-        self._lock = None
+        self._lock = threading.Lock()
diff --git a/tests/unit/test_orders.py b/tests/unit/test_orders.py
index aaa..bbb 100644
--- a/tests/unit/test_orders.py
+++ b/tests/unit/test_orders.py
@@ -12 +12,2 @@ def test_reserve():
-    assert svc.reserve()
+    assert svc.reserve()
+    assert svc.reserved_count == 1
"""

WITH_CONTEXT = """diff --git a/src/cache.py b/src/cache.py
--- a/src/cache.py
+++ b/src/cache.py
@@ -8,7 +8,8 @@ class Cache:
     def __init__(self) -> None:
         self._store = {}
-        self._ttl = 60
+        self._ttl = 30
+        self._clock = time.monotonic

     def get(self, key):
         return self._store.get(key)
"""


def test_unified_zero_hunk_headers_become_ranges() -> None:
    """With --unified=0 the header alone is the changed range."""
    result = parse_unified_diff(UNIFIED_ZERO)

    assert result.warnings == ()
    assert [change.path for change in result.files] == [
        "src/orders/service.py",
        "tests/unit/test_orders.py",
    ]

    service = result.files[0]
    assert service.change_type is ChangeType.MODIFIED
    assert service.new_ranges == (LineRange(start=42, end=44), LineRange(start=91, end=91))
    # An added-only hunk has no old-side range; a replaced line has one.
    assert service.old_ranges == (LineRange(start=88, end=88),)


def test_context_lines_are_not_treated_as_changed() -> None:
    """Counting context as changed would make diff-overlap tests useless."""
    (change,) = parse_unified_diff(WITH_CONTEXT).files
    assert change.new_ranges == (LineRange(start=10, end=11),)
    assert change.old_ranges == (LineRange(start=10, end=10),)


def test_added_file() -> None:
    diff = """diff --git a/src/new_thing.py b/src/new_thing.py
new file mode 100644
--- /dev/null
+++ b/src/new_thing.py
@@ -0,0 +1,2 @@
+import os
+VALUE = 1
"""
    (change,) = parse_unified_diff(diff).files
    assert change.change_type is ChangeType.ADDED
    assert change.path == "src/new_thing.py"
    assert change.new_ranges == (LineRange(start=1, end=2),)
    assert change.old_ranges == ()


def test_deleted_file() -> None:
    diff = """diff --git a/src/old_thing.py b/src/old_thing.py
deleted file mode 100644
--- a/src/old_thing.py
+++ /dev/null
@@ -1,2 +0,0 @@
-import os
-VALUE = 1
"""
    (change,) = parse_unified_diff(diff).files
    assert change.change_type is ChangeType.DELETED
    assert change.path == "src/old_thing.py"
    assert change.old_ranges == (LineRange(start=1, end=2),)
    assert change.new_ranges == ()


def test_rename_records_both_paths() -> None:
    """A rename is exactly the case where old_path decides whether history holds."""
    diff = """diff --git a/tests/test_login.py b/tests/auth/test_login.py
similarity index 96%
rename from tests/test_login.py
rename to tests/auth/test_login.py
--- a/tests/test_login.py
+++ b/tests/auth/test_login.py
@@ -3 +3 @@
-import old
+import new
"""
    (change,) = parse_unified_diff(diff).files
    assert change.change_type is ChangeType.RENAMED
    assert change.path == "tests/auth/test_login.py"
    assert change.old_path == "tests/test_login.py"


def test_binary_file_is_flagged_and_has_no_ranges() -> None:
    diff = """diff --git a/assets/logo.png b/assets/logo.png
index 111..222 100644
Binary files a/assets/logo.png and b/assets/logo.png differ
"""
    (change,) = parse_unified_diff(diff).files
    assert change.binary is True
    assert change.new_ranges == ()


def test_adjacent_hunks_are_merged() -> None:
    diff = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -5,0 +5,1 @@
+one
@@ -6,0 +6,1 @@
+two
"""
    (change,) = parse_unified_diff(diff).files
    assert change.new_ranges == (LineRange(start=5, end=6),)


def test_empty_diff_yields_nothing_and_no_warning() -> None:
    result = parse_unified_diff("")
    assert result.files == ()
    assert result.warnings == ()


def test_garbage_input_warns_instead_of_raising() -> None:
    """A diff is supporting evidence; a bad one weakens a call, not the run."""
    result = parse_unified_diff("this is not a diff at all\njust prose\n")
    assert result.files == ()
    assert [w.reason for w in result.warnings] == ["no_file_changes_found"]


def test_malformed_diff_header_is_reported_and_skipped() -> None:
    result = parse_unified_diff("diff --git nonsense\n")
    assert [w.reason for w in result.warnings] == ["bad_diff_header"]


def test_hunk_without_a_preceding_file_header_is_ignored() -> None:
    result = parse_unified_diff("@@ -1 +1 @@\n-a\n+b\n")
    assert result.files == ()


def test_change_for_tolerates_path_shape_differences() -> None:
    """Stack traces and diffs rarely agree on path shape."""
    summary = parse_unified_diff(UNIFIED_ZERO)
    assert summary.touches("src/orders/service.py")
    assert summary.touches("./src/orders/service.py")
    assert summary.touches("orders/service.py")
    # A bare filename is what a Java stack frame gives you, and it resolves
    # here because only one changed file carries that name.
    assert summary.touches("service.py")
    assert summary.touches("src/orders/other.py") is False


def test_ambiguous_suffix_match_is_refused() -> None:
    """Guessing between same-named files would attribute a change to the wrong test."""
    diff = """diff --git a/orders/service.py b/orders/service.py
--- a/orders/service.py
+++ b/orders/service.py
@@ -1 +1 @@
-a
+b
diff --git a/billing/service.py b/billing/service.py
--- a/billing/service.py
+++ b/billing/service.py
@@ -1 +1 @@
-a
+b
"""
    summary = parse_unified_diff(diff)
    assert summary.change_for("service.py") is None
    assert summary.touches("orders/service.py") is True


def test_paths_includes_pre_rename_path() -> None:
    diff = """diff --git a/old.py b/new.py
rename from old.py
rename to new.py
"""
    summary = parse_unified_diff(diff)
    assert summary.paths() == frozenset({"new.py", "old.py"})


def test_parse_diff_file_reads_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "change.patch"
    path.write_text(UNIFIED_ZERO, encoding="utf-8")
    assert len(parse_diff_file(path).files) == 2


def test_parse_diff_file_missing_warns(tmp_path: Path) -> None:
    result = parse_diff_file(tmp_path / "absent.patch")
    assert [w.reason for w in result.warnings] == ["unreadable_file"]
    assert result.files == ()


def test_crlf_line_endings() -> None:
    """Windows runners write CRLF patch files."""
    result = parse_unified_diff(UNIFIED_ZERO.replace("\n", "\r\n"))
    assert len(result.files) == 2
    assert result.files[0].new_ranges == (LineRange(start=42, end=44), LineRange(start=91, end=91))
