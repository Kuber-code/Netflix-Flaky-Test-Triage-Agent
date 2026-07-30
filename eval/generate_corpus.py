"""Synthetic evaluation corpus generator.

**Disclosure, stated plainly because it bounds every figure downstream:** this
corpus is synthetic. It was written by hand from experience of what these failures
look like in real CI, then hand-labelled. The accuracy numbers it produces are
indicative of behaviour on realistic-looking inputs. They are not
production-validated, and no claim here should be read as one.

Two deliberate choices about corpus design, both of which make the numbers lower
and more honest:

**Adversarial cases are included on purpose**, not as an afterthought. A corpus
where every example's label is written on its face measures nothing but whether
the model can read. So it contains a real regression that presents as a flake, an
infrastructure failure whose trace is full of thread names, a shared-state leak
that looks like a race, and several cases where ``UNKNOWN`` is the only correct
answer because the evidence genuinely does not distinguish.

**Surface cues are varied.** If every ``EXTERNAL_DEPENDENCY`` example said
"Connection refused", the task would be keyword matching and the baseline would
score as well as the model -- which would make the comparison meaningless rather
than informative. Messages, languages and trace shapes differ within each class.

Run ``python eval/generate_corpus.py`` to regenerate ``eval/dataset/corpus.json``.
The output is committed so that the results table can be reproduced without
rerunning the generator.
"""

from __future__ import annotations

import json
from typing import Any

from paths import DATASET_DIR, ensure_dirs

CORPUS_VERSION = "1.0.0"


def example(
    identifier: str,
    label: str,
    rationale: str,
    *,
    test_name: str,
    suite_path: str,
    outcome: str = "fail",
    failure_type: str | None = None,
    failure_message: str | None = None,
    stack_trace: str | None = None,
    observations: int = 12,
    windows: int = 8,
    flake_rate: float = 0.30,
    divergence_rate: float = 0.30,
    intermittency_rate: float = 0.20,
    retry_data: bool = True,
    verdict: str = "flaky",
    detector_confidence: str = "medium",
    signals: tuple[str, ...] = ("same_sha_divergence", "cross_attempt_divergence"),
    failing_shards: tuple[str, ...] = (),
    infra_excluded: int = 0,
    merged_uncertain: bool = False,
    diff_paths: tuple[str, ...] = (),
    adversarial: bool = False,
) -> dict[str, Any]:
    """One labelled example. ``rationale`` is the one-line human justification."""
    return {
        "id": identifier,
        "label": label,
        "rationale": rationale,
        "adversarial": adversarial,
        "test_name": test_name,
        "suite_path": suite_path,
        "outcome": outcome,
        "failure_type": failure_type,
        "failure_message": failure_message,
        "stack_trace": stack_trace,
        "history": {
            "observations": observations,
            "windows": windows,
            "flake_rate": flake_rate,
            "divergence_rate": divergence_rate,
            "intermittency_rate": intermittency_rate,
            "retry_data_available": retry_data,
            "verdict": verdict,
            "detector_confidence": detector_confidence,
            "signals": list(signals),
            "failing_shards": list(failing_shards),
            "infra_excluded": infra_excluded,
            "merged_uncertain": merged_uncertain,
        },
        "diff_paths": list(diff_paths),
    }


def race_conditions() -> list[dict[str, Any]]:
    return [
        example(
            "race-001",
            "RACE_CONDITION",
            "Non-atomic increment of shared state from executor threads.",
            test_name="test_concurrent_reserve",
            suite_path="tests/integration/test_inventory.py",
            failure_type="AssertionError",
            failure_message="assert 2 == 1",
            stack_trace=(
                "tests/integration/test_inventory.py:88: in test_concurrent_reserve\n"
                "    assert inventory.reserved == 1\n"
                "E   AssertionError: assert 2 == 1\n"
                "src/inventory/store.py:44: in reserve\n"
                "    self.reserved += 1\n"
                '  File "/usr/lib/python3.12/concurrent/futures/thread.py", line 58, in run'
            ),
        ),
        example(
            "race-002",
            "RACE_CONDITION",
            "Java assertion on a counter mutated from a thread pool worker.",
            test_name="chargesCardExactlyOnce",
            suite_path="com/example/orders::OrderServiceTest",
            failure_type="java.lang.AssertionError",
            failure_message="expected:<1> but was:<2>",
            stack_trace=(
                "java.lang.AssertionError: expected:<1> but was:<2>\n"
                "\tat com.example.orders.OrderServiceTest"
                ".chargesCardExactlyOnce(OrderServiceTest.java:88)\n"
                "\tat com.example.orders.PaymentLedger.record(PaymentLedger.java:112)"
            ),
        ),
        example(
            "race-003",
            "RACE_CONDITION",
            "Go data race detector output; interleaving, not timing.",
            test_name="TestCacheWarmParallel",
            suite_path="internal/cache/cache_test.go",
            outcome="error",
            failure_type="DATA RACE",
            failure_message="WARNING: DATA RACE",
            stack_trace=(
                "WARNING: DATA RACE\n"
                "Write at 0x00c000123456 by goroutine 12:\n"
                "  internal/cache.(*Cache).Set()\n"
                "      internal/cache/cache.go:71 +0x64\n"
                "Previous read at 0x00c000123456 by goroutine 9:\n"
                "  internal/cache.(*Cache).Get()\n"
                "      internal/cache/cache.go:52 +0x3c"
            ),
        ),
        example(
            "race-004",
            "RACE_CONDITION",
            "Two callbacks append to one list without synchronisation.",
            test_name="emits events in order",
            suite_path="src/events/bus.test.ts",
            failure_type=None,
            failure_message=None,
            stack_trace=(
                "Error: expect(received).toEqual(expected)\n"
                "- Expected  - 2\n"
                "+ Received  + 2\n"
                '  Array [\n-   "created",\n-   "updated",\n'
                '+   "updated",\n+   "created",\n  ]\n'
                "    at src/events/bus.test.ts:64:24"
            ),
            flake_rate=0.42,
            divergence_rate=0.42,
        ),
        example(
            "race-005",
            "RACE_CONDITION",
            "Adversarial: reads like a timing bug but the mechanism is interleaving.",
            test_name="test_worker_pool_drains",
            suite_path="tests/unit/test_pool.py",
            failure_type="AssertionError",
            failure_message="assert 0 == 3  # queue not drained",
            stack_trace=(
                "tests/unit/test_pool.py:120: in test_worker_pool_drains\n"
                "    assert pool.pending == 3\n"
                "E   AssertionError: assert 0 == 3  # queue not drained\n"
                "src/pool/worker.py:88: in _drain\n"
                "    while self._queue:  # mutated by sibling threads\n"
                "        self._queue.pop()"
            ),
            adversarial=True,
        ),
        example(
            "race-006",
            "RACE_CONDITION",
            "Double-checked lock without a memory barrier; classic singleton race.",
            test_name="returnsOneInstanceUnderLoad",
            suite_path="com/example/config::ConfigLoaderTest",
            failure_type="java.lang.AssertionError",
            failure_message="expected same instance but got two",
            stack_trace=(
                "java.lang.AssertionError: expected same instance but got two\n"
                "\tat com.example.config.ConfigLoaderTest"
                ".returnsOneInstanceUnderLoad(ConfigLoaderTest.java:41)\n"
                "\tat com.example.config.ConfigLoader.getInstance(ConfigLoader.java:29)"
            ),
        ),
    ]


def timing_dependencies() -> list[dict[str, Any]]:
    return [
        example(
            "time-001",
            "TIMING_DEPENDENCY",
            "Fixed sleep shorter than the debounce interval on a loaded runner.",
            test_name="settles after debounce",
            suite_path="src/checkout/cart.test.ts",
            failure_message=None,
            failure_type=None,
            stack_trace=(
                "Error: Timeout - Async callback was not invoked within the 5000 ms "
                "timeout specified by jest.setTimeout.\n"
                "    at src/checkout/cart.test.ts:74:5"
            ),
        ),
        example(
            "time-002",
            "TIMING_DEPENDENCY",
            "Asserts on wall-clock elapsed time; fails when the runner is slow.",
            test_name="test_cache_expires_after_ttl",
            suite_path="tests/unit/test_cache.py",
            failure_type="AssertionError",
            failure_message="assert 1.03 < 1.0",
            stack_trace=(
                "tests/unit/test_cache.py:56: in test_cache_expires_after_ttl\n"
                "    time.sleep(1.0)\n"
                "    assert time.monotonic() - start < 1.0\n"
                "E   AssertionError: assert 1.03 < 1.0"
            ),
            observations=30,
            flake_rate=0.12,
            divergence_rate=0.12,
        ),
        example(
            "time-003",
            "TIMING_DEPENDENCY",
            "Playwright locator timeout waiting for an element that arrives late.",
            test_name="checkout > applies a coupon",
            suite_path="e2e/checkout.spec.ts",
            failure_type="FAILURE",
            failure_message="e2e/checkout.spec.ts:31:5 applies a coupon",
            stack_trace=(
                "Error: locator.click: Timeout 20000ms exceeded.\n"
                "Call log:\n"
                "  - waiting for getByRole('button', { name: 'Apply' })\n"
                "    at e2e/checkout.spec.ts:38:41"
            ),
        ),
        example(
            "time-004",
            "TIMING_DEPENDENCY",
            "Test depends on a date boundary; fails when the run crosses midnight.",
            test_name="test_report_covers_today",
            suite_path="tests/unit/test_reports.py",
            failure_type="AssertionError",
            failure_message="assert date(2026, 7, 21) == date(2026, 7, 20)",
            stack_trace=(
                "tests/unit/test_reports.py:33: in test_report_covers_today\n"
                "    assert report.day == date.today()\n"
                "E   AssertionError: assert date(2026, 7, 21) == date(2026, 7, 20)"
            ),
            observations=40,
            flake_rate=0.06,
            divergence_rate=0.06,
        ),
        example(
            "time-005",
            "TIMING_DEPENDENCY",
            "Deadline exceeded inside a polling helper with a fixed retry budget.",
            test_name="TestWaitForReady",
            suite_path="internal/health/health_test.go",
            outcome="error",
            failure_type="context.DeadlineExceeded",
            failure_message="context deadline exceeded after 2s",
            stack_trace=(
                "    health_test.go:47: context deadline exceeded after 2s\n"
                "        internal/health/wait.go:22: polling every 100ms, 20 attempts"
            ),
        ),
    ]


def order_dependencies() -> list[dict[str, Any]]:
    return [
        example(
            "order-001",
            "TEST_ORDER_DEPENDENCY",
            "Fails only in shard 3; depends on a fixture another test installs.",
            test_name="test_admin_can_delete_user",
            suite_path="tests/integration/test_admin.py",
            failure_type="KeyError",
            failure_message="'admin_token'",
            stack_trace=(
                "tests/integration/test_admin.py:71: in test_admin_can_delete_user\n"
                "    token = session_registry['admin_token']\n"
                "E   KeyError: 'admin_token'"
            ),
            failing_shards=("3",),
            signals=("same_sha_divergence", "historical_instability"),
        ),
        example(
            "order-002",
            "TEST_ORDER_DEPENDENCY",
            "Passes alone, fails after another test mutates a module-level default.",
            test_name="test_default_currency_is_usd",
            suite_path="tests/unit/test_pricing.py",
            failure_type="AssertionError",
            failure_message="assert 'EUR' == 'USD'",
            stack_trace=(
                "tests/unit/test_pricing.py:18: in test_default_currency_is_usd\n"
                "    assert pricing.DEFAULT_CURRENCY == 'USD'\n"
                "E   AssertionError: assert 'EUR' == 'USD'\n"
                "  note: passes when run with -k test_default_currency_is_usd"
            ),
            failing_shards=("2",),
        ),
        example(
            "order-003",
            "TEST_ORDER_DEPENDENCY",
            "JUnit test relies on static state initialised by an earlier class.",
            test_name="readsCachedSchema",
            suite_path="com/example/schema::SchemaCacheTest",
            failure_type="java.lang.NullPointerException",
            failure_message='Cannot invoke "Schema.version()" because "schema" is null',
            stack_trace=(
                "java.lang.NullPointerException: Cannot invoke "
                '"Schema.version()" because "schema" is null\n'
                "\tat com.example.schema.SchemaCacheTest.readsCachedSchema(SchemaCacheTest.java:52)"
            ),
            failing_shards=("1",),
        ),
        example(
            "order-004",
            "TEST_ORDER_DEPENDENCY",
            "Adversarial: shard-confined, but the message suggests shared state.",
            test_name="test_user_count_is_zero_at_start",
            suite_path="tests/integration/test_users.py",
            failure_type="AssertionError",
            failure_message="assert 4 == 0",
            stack_trace=(
                "tests/integration/test_users.py:24: in test_user_count_is_zero_at_start\n"
                "    assert User.objects.count() == 0\n"
                "E   AssertionError: assert 4 == 0\n"
                "  note: only fails when scheduled after tests/integration/test_signup.py"
            ),
            failing_shards=("4",),
            adversarial=True,
        ),
        example(
            "order-005",
            "TEST_ORDER_DEPENDENCY",
            "Mocha suite where a sibling leaves a stubbed clock installed.",
            test_name="formats relative dates",
            suite_path="test/format.spec.js",
            failure_type="AssertionError",
            failure_message="expected 'in 3 days' to equal 'in 2 days'",
            stack_trace=(
                "AssertionError: expected 'in 3 days' to equal 'in 2 days'\n"
                "    at Context.<anonymous> (test/format.spec.js:61:34)\n"
                "    note: sinon clock still installed by test/timers.spec.js"
            ),
            failing_shards=("1",),
        ),
    ]


def external_dependencies() -> list[dict[str, Any]]:
    return [
        example(
            "ext-001",
            "EXTERNAL_DEPENDENCY",
            "Connection reset reaching a third-party payments endpoint.",
            test_name="test_payment_capture",
            suite_path="tests/integration/test_checkout.py",
            outcome="error",
            failure_type="ConnectionResetError",
            failure_message="[Errno 104] Connection reset by peer",
            stack_trace=(
                "tests/integration/test_checkout.py:120: in test_payment_capture\n"
                "    resp = session.post(PAYMENTS_URL, json=payload)\n"
                "E   ConnectionResetError: [Errno 104] Connection reset by peer"
            ),
        ),
        example(
            "ext-002",
            "EXTERNAL_DEPENDENCY",
            "DNS resolution failure for a service hostname.",
            test_name="resolvesCatalogueService",
            suite_path="com/example/catalogue::CatalogueClientTest",
            outcome="error",
            failure_type="java.net.UnknownHostException",
            failure_message="catalogue.internal: Name or service not known",
            stack_trace=(
                "java.net.UnknownHostException: catalogue.internal: "
                "Name or service not known\n"
                "\tat com.example.catalogue.CatalogueClient.fetch(CatalogueClient.java:66)"
            ),
        ),
        example(
            "ext-003",
            "EXTERNAL_DEPENDENCY",
            "Upstream returned 503; the test asserts on a live third-party API.",
            test_name="fetches exchange rates",
            suite_path="src/fx/rates.test.ts",
            failure_message=None,
            failure_type=None,
            stack_trace=(
                "Error: Request failed with status code 503\n"
                "    at createError (src/fx/http.ts:16:15)\n"
                "    at src/fx/rates.test.ts:29:20\n"
                "  body: <html>503 Service Temporarily Unavailable</html>"
            ),
        ),
        example(
            "ext-004",
            "EXTERNAL_DEPENDENCY",
            "TLS handshake failure against a sandbox endpoint.",
            test_name="test_webhook_signature_roundtrip",
            suite_path="tests/integration/test_webhooks.py",
            outcome="error",
            failure_type="ssl.SSLError",
            failure_message="[SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error",
            stack_trace=(
                "tests/integration/test_webhooks.py:88: in test_webhook_signature_roundtrip\n"
                "    client.post(SANDBOX_URL, data=body)\n"
                "E   ssl.SSLError: [SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error"
            ),
        ),
        example(
            "ext-005",
            "EXTERNAL_DEPENDENCY",
            "Testcontainers dependency not ready inside the wait window.",
            test_name="test_search_indexes_document",
            suite_path="tests/integration/test_search.py",
            outcome="error",
            failure_type="ContainerNotReady",
            failure_message="elasticsearch:8.13 did not become healthy within 60s",
            stack_trace=(
                "tests/integration/test_search.py:31: in test_search_indexes_document\n"
                "    with ElasticsearchContainer() as es:\n"
                "E   ContainerNotReady: elasticsearch:8.13 did not become healthy within 60s"
            ),
        ),
        example(
            "ext-006",
            "EXTERNAL_DEPENDENCY",
            "Adversarial: a timeout message, but the cause is an unreachable dependency.",
            test_name="TestFetchProfile",
            suite_path="internal/profile/client_test.go",
            outcome="error",
            failure_type="net.Error",
            failure_message="dial tcp 10.0.3.14:8080: i/o timeout",
            stack_trace=(
                "    client_test.go:58: Get http://profiles.svc/v1/me: "
                "dial tcp 10.0.3.14:8080: i/o timeout\n"
                "        internal/profile/client.go:34"
            ),
            adversarial=True,
        ),
    ]


def shared_state_leaks() -> list[dict[str, Any]]:
    return [
        example(
            "state-001",
            "SHARED_STATE_LEAK",
            "Unique constraint violated by a row a previous test left behind.",
            test_name="test_create_organisation",
            suite_path="tests/integration/test_orgs.py",
            outcome="error",
            failure_type="psycopg2.errors.UniqueViolation",
            failure_message='duplicate key value violates unique constraint "orgs_slug_key"',
            stack_trace=(
                "tests/integration/test_orgs.py:44: in test_create_organisation\n"
                "    Organisation.objects.create(slug='acme')\n"
                "E   psycopg2.errors.UniqueViolation: duplicate key value violates "
                'unique constraint "orgs_slug_key"\n'
                "  DETAIL:  Key (slug)=(acme) already exists."
            ),
        ),
        example(
            "state-002",
            "SHARED_STATE_LEAK",
            "A temp file from an earlier run is still present.",
            test_name="test_export_writes_fresh_file",
            suite_path="tests/unit/test_export.py",
            outcome="error",
            failure_type="FileExistsError",
            failure_message="[Errno 17] File exists: '/tmp/export.csv'",
            stack_trace=(
                "tests/unit/test_export.py:29: in test_export_writes_fresh_file\n"
                "    os.mkdir(EXPORT_DIR)\n"
                "E   FileExistsError: [Errno 17] File exists: '/tmp/export.csv'"
            ),
        ),
        example(
            "state-003",
            "SHARED_STATE_LEAK",
            "A module-level cache is not cleared, so a stale value is read.",
            test_name="test_feature_flag_reflects_config",
            suite_path="tests/unit/test_flags.py",
            failure_type="AssertionError",
            failure_message="assert False is True",
            stack_trace=(
                "tests/unit/test_flags.py:52: in test_feature_flag_reflects_config\n"
                "    assert flags.enabled('beta') is True\n"
                "E   AssertionError: assert False is True\n"
                "src/flags/registry.py:18: in enabled\n"
                "    return _CACHE[name]  # populated at import, never reset"
            ),
        ),
        example(
            "state-004",
            "SHARED_STATE_LEAK",
            "Redis keys survive between tests because the db is not flushed.",
            test_name="countsActiveSessions",
            suite_path="com/example/session::SessionStoreTest",
            failure_type="java.lang.AssertionError",
            failure_message="expected:<0> but was:<7>",
            stack_trace=(
                "java.lang.AssertionError: expected:<0> but was:<7>\n"
                "\tat com.example.session.SessionStoreTest"
                ".countsActiveSessions(SessionStoreTest.java:63)\n"
                "\tat com.example.session.RedisSessionStore.count(RedisSessionStore.java:88)"
            ),
        ),
        example(
            "state-005",
            "SHARED_STATE_LEAK",
            "Adversarial: presents as a race, but the leak is a global fixture.",
            test_name="test_audit_log_starts_empty",
            suite_path="tests/integration/test_audit.py",
            failure_type="AssertionError",
            failure_message="assert 12 == 0",
            stack_trace=(
                "tests/integration/test_audit.py:19: in test_audit_log_starts_empty\n"
                "    assert len(audit.entries) == 0\n"
                "E   AssertionError: assert 12 == 0\n"
                "src/audit/log.py:7: in <module>\n"
                "    entries = []  # module-level, shared across the session"
            ),
            adversarial=True,
        ),
    ]


def resource_exhaustion() -> list[dict[str, Any]]:
    return [
        example(
            "res-001",
            "RESOURCE_EXHAUSTION",
            "Port already bound by a server a sibling test left running.",
            test_name="test_server_starts_on_8080",
            suite_path="tests/integration/test_server.py",
            outcome="error",
            failure_type="OSError",
            failure_message="[Errno 98] Address already in use",
            stack_trace=(
                "tests/integration/test_server.py:22: in test_server_starts_on_8080\n"
                "    server.bind(('127.0.0.1', 8080))\n"
                "E   OSError: [Errno 98] Address already in use"
            ),
        ),
        example(
            "res-002",
            "RESOURCE_EXHAUSTION",
            "JVM heap exhausted by a large fixture.",
            test_name="loadsLargeCatalogue",
            suite_path="com/example/catalogue::CatalogueLoadTest",
            outcome="error",
            failure_type="java.lang.OutOfMemoryError",
            failure_message="Java heap space",
            stack_trace=(
                "java.lang.OutOfMemoryError: Java heap space\n"
                "\tat com.example.catalogue.CatalogueLoader.readAll(CatalogueLoader.java:140)"
            ),
        ),
        example(
            "res-003",
            "RESOURCE_EXHAUSTION",
            "File descriptors leaked by unclosed sockets in the test itself.",
            test_name="test_opens_many_connections",
            suite_path="tests/unit/test_pool.py",
            outcome="error",
            failure_type="OSError",
            failure_message="[Errno 24] Too many open files",
            stack_trace=(
                "tests/unit/test_pool.py:77: in test_opens_many_connections\n"
                "    conns = [connect() for _ in range(2000)]\n"
                "E   OSError: [Errno 24] Too many open files"
            ),
        ),
        example(
            "res-004",
            "RESOURCE_EXHAUSTION",
            "Node heap limit reached while building a fixture array.",
            test_name="builds a large report",
            suite_path="src/reports/build.test.ts",
            failure_message=None,
            failure_type=None,
            stack_trace=(
                "FATAL ERROR: Reached heap limit Allocation failed - "
                "JavaScript heap out of memory\n"
                "    at src/reports/build.test.ts:41:18"
            ),
        ),
    ]


def infra_flakes() -> list[dict[str, Any]]:
    return [
        example(
            "infra-001",
            "INFRA_FLAKE",
            "Runner disk filled; no test assertion was reached.",
            test_name="test_uploads_artifact",
            suite_path="tests/integration/test_artifacts.py",
            outcome="error",
            failure_type="OSError",
            failure_message="No space left on device",
            stack_trace="OSError: [Errno 28] No space left on device",
            infra_excluded=2,
            verdict="flaky",
        ),
        example(
            "infra-002",
            "INFRA_FLAKE",
            "Container image could not be pulled; the suite never started.",
            test_name="test_suite_bootstrap",
            suite_path="tests/integration/test_bootstrap.py",
            outcome="error",
            failure_type="ImagePullBackOff",
            failure_message="error pulling image: manifest unknown",
            stack_trace="ImagePullBackOff: error pulling image ghcr.io/acme/ci:latest",
            infra_excluded=1,
        ),
        example(
            "infra-003",
            "INFRA_FLAKE",
            "Spot instance reclaimed mid-run; the worker was killed.",
            test_name="test_long_migration",
            suite_path="tests/integration/test_migrations.py",
            outcome="error",
            failure_type="WorkerLost",
            failure_message="the runner has received a shutdown signal",
            stack_trace="WorkerLost: spot instance interruption notice received",
            infra_excluded=3,
        ),
        example(
            "infra-004",
            "INFRA_FLAKE",
            "Adversarial: thread names in the trace, but it is a harness kill.",
            test_name="test_parallel_import",
            suite_path="tests/integration/test_import.py",
            outcome="error",
            failure_type="SIGKILL",
            failure_message="Received SIGKILL; container exited with code 137",
            stack_trace=(
                "Received SIGKILL; container exited with code 137\n"
                "  last observed: ThreadPoolExecutor-3_2 running import batch 41\n"
                "  no assertion was evaluated"
            ),
            adversarial=True,
        ),
        example(
            "infra-005",
            "INFRA_FLAKE",
            "CI control plane lost the agent; results were never written.",
            test_name="test_smoke",
            suite_path="tests/smoke/test_smoke.py",
            outcome="error",
            failure_type="AgentLost",
            failure_message="lost communication with the server",
            stack_trace="AgentLost: lost communication with the server after 240s",
            infra_excluded=1,
        ),
    ]


def real_regressions() -> list[dict[str, Any]]:
    """The adversarial heart of the corpus.

    Every one of these was flagged as a flake candidate by something upstream, and
    every one is actually a defect. Misclassifying these is the error the
    dangerous-error rate measures.
    """
    return [
        example(
            "reg-001",
            "REAL_REGRESSION",
            "Deterministic since one commit, and the diff touches the failing path.",
            test_name="test_totals_include_tax",
            suite_path="tests/integration/test_checkout.py",
            failure_type="AssertionError",
            failure_message="assert Decimal('10.00') == Decimal('10.80')",
            stack_trace=(
                "tests/integration/test_checkout.py:61: in test_totals_include_tax\n"
                "    assert order.total == Decimal('10.80')\n"
                "E   AssertionError: assert Decimal('10.00') == Decimal('10.80')\n"
                "src/checkout/totals.py:19: in compute_total"
            ),
            verdict="regression",
            detector_confidence="high",
            signals=(),
            flake_rate=0.0,
            divergence_rate=0.0,
            intermittency_rate=0.08,
            diff_paths=("src/checkout/totals.py",),
            adversarial=True,
        ),
        example(
            "reg-002",
            "REAL_REGRESSION",
            "Adversarial: intermittent-looking history, but the diff broke a branch.",
            test_name="test_discount_applies_to_bulk_orders",
            suite_path="tests/unit/test_pricing.py",
            failure_type="AssertionError",
            failure_message="assert 100 == 90",
            stack_trace=(
                "tests/unit/test_pricing.py:88: in test_discount_applies_to_bulk_orders\n"
                "    assert price_for(quantity=50) == 90\n"
                "E   AssertionError: assert 100 == 90\n"
                "src/pricing/discount.py:41: in price_for\n"
                "    if quantity > 50:  # was >= 50 before this change"
            ),
            verdict="persistent_failure",
            detector_confidence="medium",
            signals=(),
            flake_rate=0.0,
            divergence_rate=0.0,
            diff_paths=("src/pricing/discount.py",),
            adversarial=True,
        ),
        example(
            "reg-003",
            "REAL_REGRESSION",
            "Null dereference introduced by a refactor on the exercised path.",
            test_name="rendersEmptyCart",
            suite_path="com/example/cart::CartViewTest",
            failure_type="java.lang.NullPointerException",
            failure_message='Cannot read field "items" because "cart" is null',
            stack_trace=(
                "java.lang.NullPointerException: Cannot read field "
                '"items" because "cart" is null\n'
                "\tat com.example.cart.CartView.render(CartView.java:52)\n"
                "\tat com.example.cart.CartViewTest.rendersEmptyCart(CartViewTest.java:20)"
            ),
            verdict="regression",
            detector_confidence="high",
            signals=(),
            flake_rate=0.0,
            divergence_rate=0.0,
            diff_paths=("src/main/java/com/example/cart/CartView.java",),
            adversarial=True,
        ),
        example(
            "reg-004",
            "REAL_REGRESSION",
            "Adversarial: a timeout, but caused by a new unbounded query.",
            test_name="test_dashboard_loads_under_budget",
            suite_path="tests/integration/test_dashboard.py",
            outcome="error",
            failure_type="QueryTimeout",
            failure_message="statement timeout after 30000ms",
            stack_trace=(
                "tests/integration/test_dashboard.py:44: in test_dashboard_loads_under_budget\n"
                "    dashboard.load(user)\n"
                "E   QueryTimeout: statement timeout after 30000ms\n"
                "src/dashboard/queries.py:71: in recent_activity\n"
                "    return Activity.objects.all()  # no limit added in this change"
            ),
            verdict="regression",
            detector_confidence="high",
            signals=(),
            flake_rate=0.0,
            divergence_rate=0.0,
            diff_paths=("src/dashboard/queries.py",),
            adversarial=True,
        ),
        example(
            "reg-005",
            "REAL_REGRESSION",
            "Off-by-one in pagination, deterministic, diff on the failing file.",
            test_name="returns the last page",
            suite_path="src/api/paginate.test.ts",
            failure_message=None,
            failure_type=None,
            stack_trace=(
                "Error: expect(received).toHaveLength(expected)\n"
                "Expected length: 10\nReceived length: 9\n"
                "    at src/api/paginate.test.ts:52:31\n"
                "    at src/api/paginate.ts:18:22"
            ),
            verdict="regression",
            detector_confidence="high",
            signals=(),
            flake_rate=0.0,
            divergence_rate=0.0,
            diff_paths=("src/api/paginate.ts",),
            adversarial=True,
        ),
    ]


def unknowns() -> list[dict[str, Any]]:
    """Cases where UNKNOWN is the only correct answer.

    Constructed deliberately: a corpus without them cannot measure whether the
    classifier abstains when it should, which is the behaviour ADR-0003 is about.
    """
    return [
        example(
            "unk-001",
            "UNKNOWN",
            "No message and no trace; nothing to reason from.",
            test_name="test_end_to_end",
            suite_path="tests/e2e/test_flow.py",
            failure_type=None,
            failure_message=None,
            stack_trace=None,
            observations=11,
            flake_rate=0.18,
        ),
        example(
            "unk-002",
            "UNKNOWN",
            "Bare assertion with no context; consistent with several causes.",
            test_name="test_invariant_holds",
            suite_path="tests/unit/test_core.py",
            failure_type="AssertionError",
            failure_message="assert False",
            stack_trace="tests/unit/test_core.py:12: AssertionError",
        ),
        example(
            "unk-003",
            "UNKNOWN",
            "Trace truncated by a killed writer; the useful frames are gone.",
            test_name="test_batch_processes_all",
            suite_path="tests/integration/test_batch.py",
            failure_type="AssertionError",
            failure_message="assert 998 == 1000",
            stack_trace="tests/integration/test_batch.py:71: in test_batch_pr",
        ),
        example(
            "unk-004",
            "UNKNOWN",
            "Evidence fits timing and external dependency equally; no tiebreak.",
            test_name="test_sync_completes",
            suite_path="tests/integration/test_sync.py",
            outcome="error",
            failure_type="TimeoutError",
            failure_message="operation timed out",
            stack_trace=(
                "tests/integration/test_sync.py:52: in test_sync_completes\n"
                "    sync.run()\n"
                "E   TimeoutError: operation timed out"
            ),
            adversarial=True,
        ),
        example(
            "unk-005",
            "UNKNOWN",
            "Generic runtime error with no project frames at all.",
            test_name="handles input",
            suite_path="src/parse/parse.test.ts",
            failure_message=None,
            failure_type=None,
            stack_trace="Error: undefined is not a function",
        ),
        example(
            "unk-006",
            "UNKNOWN",
            "Only one observation and a bare message; insufficient evidence.",
            test_name="test_new_feature",
            suite_path="tests/unit/test_new.py",
            failure_type="Exception",
            failure_message="failed",
            stack_trace=None,
            observations=1,
            windows=1,
            flake_rate=0.0,
            divergence_rate=0.0,
            intermittency_rate=0.0,
            retry_data=False,
            verdict="new_failure",
            detector_confidence="low",
            signals=(),
        ),
        example(
            "unk-007",
            "UNKNOWN",
            "Merged across an uncertain rename; the history may not be this test's.",
            test_name="test_renamed_thing",
            suite_path="tests/unit/test_thing.py",
            failure_type="AssertionError",
            failure_message="assert 1 == 2",
            stack_trace="tests/unit/test_thing.py:9: AssertionError",
            merged_uncertain=True,
            adversarial=True,
        ),
        example(
            "unk-008",
            "UNKNOWN",
            "Contradictory evidence: an assertion failure with an infra message.",
            test_name="test_writes_report",
            suite_path="tests/integration/test_report.py",
            failure_type="AssertionError",
            failure_message="assert report_written  # runner was reclaimed mid-write?",
            stack_trace=(
                "tests/integration/test_report.py:66: in test_writes_report\n"
                "    assert report_written\n"
                "E   AssertionError"
            ),
            adversarial=True,
        ),
    ]


def build_corpus() -> dict[str, Any]:
    examples = [
        *race_conditions(),
        *timing_dependencies(),
        *order_dependencies(),
        *external_dependencies(),
        *shared_state_leaks(),
        *resource_exhaustion(),
        *infra_flakes(),
        *real_regressions(),
        *unknowns(),
    ]

    identifiers = [item["id"] for item in examples]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("duplicate example ids")

    return {
        "corpus_version": CORPUS_VERSION,
        "synthetic": True,
        "disclosure": (
            "This corpus is synthetic and hand-labelled. Accuracy figures computed "
            "against it are indicative of behaviour on realistic-looking inputs, "
            "not production-validated."
        ),
        "examples": examples,
    }


def main() -> None:
    ensure_dirs()
    corpus = build_corpus()
    target = DATASET_DIR / "corpus.json"
    target.write_text(
        json.dumps(corpus, indent=2, ensure_ascii=True) + "\n", encoding="utf-8", newline="\n"
    )

    counts: dict[str, int] = {}
    for item in corpus["examples"]:
        counts[item["label"]] = counts.get(item["label"], 0) + 1
    adversarial = sum(1 for item in corpus["examples"] if item["adversarial"])

    print(f"wrote {len(corpus['examples'])} examples to {target}")
    for label in sorted(counts):
        print(f"  {label:24s} {counts[label]}")
    print(f"  {'adversarial':24s} {adversarial}")


if __name__ == "__main__":
    main()
