"""
India Post test report for production activation.

Two clearly separated parts:

* **Automated module tests** — the project's own test suites for every India Post module, run against
  a simulated India Post server. They prove the integration logic is correct and stays correct.
* **Staging (UAT) scenarios** — the same booking/label/tracking/pickup flows executed against the
  real India Post staging server, with the actual requests and responses saved as evidence.
  This is the part India Post asks for before issuing production credentials. It is reported as
  NOT EXECUTED (with the reason) whenever the staging server cannot be reached; it is never faked.
"""

from __future__ import annotations

import io
import json
import logging
import platform
import subprocess
import time
import unittest
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import django
from django.conf import settings
from django.test.runner import DiscoverRunner
from django.utils import timezone

#test labels covering every India Post module (client, services, models, tasks, views, admin, UAT runner,
#checkout validation, returns pickup) plus the proof that it is independent of Delhivery
DEFAULT_LABELS = ["indiapost", "returns", "orders.test_carrier_independence"]

#test class -> the module / feature area it covers, for the per-module summary
AREA_BY_CLASS = {
    "ClientTests": "API client: authentication, token refresh, error handling",
    "Fix08BarcodePoolAndBaseUrlTests": "Configuration and barcode pool",
    "Fix09ConfigValidationTests": "Configuration and barcode pool",
    "BookingTests": "Booking, parcel rules and labels",
    "ParcelClassificationTests": "Booking, parcel rules and labels",
    "LabelPayloadTests": "Booking, parcel rules and labels",
    "Fix06MissingNameTests": "Booking, parcel rules and labels",
    "Fix07BarcodeReservationTests": "Booking, parcel rules and labels",
    "Fix14ConcurrencyTests": "Booking, parcel rules and labels",
    "Fix03RetryTests": "Retries and background tasks",
    "Fix01To02WebhookAuthTests": "Tracking: webhook and periodic sync",
    "Fix10TrackingSyncTests": "Tracking: webhook and periodic sync",
    "TrackingResponseTests": "Tracking: webhook and periodic sync",
    "Fix04AdminActionTests": "Admin and dashboard actions",
    "Fix05ManualBarcodeTests": "Admin and dashboard actions",
    "DashboardSectionTests": "Admin and dashboard actions",
    "Fix02MobileAndPincodeValidationTests": "Checkout address validation",
    "Fix13SavedAddressPincodeTests": "Checkout address validation",
    "PostOfficeCheckoutTests": "Checkout address validation",
    "ReversePickupTests": "Reverse pickup (India Post PICKUP booking)",
    "UATRunnerTests": "Staging runner and connectivity diagnosis",
    "ConnectionCheckTests": "Staging runner and connectivity diagnosis",
    "EligibilityTests": "Customer returns: eligibility and requests",
    "CreateRequestTests": "Customer returns: eligibility and requests",
    "CustomerPageTests": "Customer returns: eligibility and requests",
    "ReturnWindowSettingTests": "Customer returns: eligibility and requests",
    "ApprovalTests": "Customer returns: approval, pickup and close-out",
    "PickupTests": "Customer returns: approval, pickup and close-out",
    "CloseOutTests": "Customer returns: approval, pickup and close-out",
    "DashboardTests": "Customer returns: approval, pickup and close-out",
    "DelhiveryAloneTests": "Independence from Delhivery",
    "IndiaPostAloneTests": "Independence from Delhivery",
    "SwitchingTests": "Independence from Delhivery",
    "TrackingIsolationTests": "Independence from Delhivery",
}


@dataclass
class TestRecord:
    __test__ = False  # not a test class, despite the name

    test_id: str
    area: str
    name: str
    status: str  # PASS / FAIL / ERROR / SKIPPED
    seconds: float
    detail: str = ""


class _Collector(unittest.TextTestResult):
    """Records one TestRecord per test, with timing and the failure text."""

    records: list[TestRecord]

    def startTest(self, test):
        self._started = time.monotonic()
        super().startTest(test)

    def _record(self, test, status, detail=""):
        cls = type(test).__name__
        description = (test.shortDescription() or "").strip()
        self.records.append(
            TestRecord(
                test_id=test.id(),
                area=AREA_BY_CLASS.get(cls, cls),
                name=description or test._testMethodName.removeprefix("test_").replace("_", " "),
                status=status,
                seconds=time.monotonic() - self._started,
                detail=detail,
            )
        )

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, "PASS")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "FAIL", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, "ERROR", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, "SKIPPED", reason)


def run_automated_tests(labels: Optional[list[str]] = None) -> list[TestRecord]:
    """Run the module test suites on a throw-away test database and return one record per test."""
    runner = DiscoverRunner(verbosity=0, interactive=False)
    suite = runner.build_suite(labels or DEFAULT_LABELS)
    records: list[TestRecord] = []

    class Result(_Collector):
        pass

    Result.records = records
    logging.disable(logging.CRITICAL)  # the code under test logs errors on purpose; keep the report readable
    #test mode is essential, not cosmetic: it swaps the real email backend for an in-memory one, so the
    #notification emails the tests trigger are captured instead of being sent from the live mail account
    runner.setup_test_environment()
    old_config = runner.setup_databases()
    try:
        unittest.TextTestRunner(stream=io.StringIO(), resultclass=Result, verbosity=0).run(suite)
    finally:
        runner.teardown_databases(old_config)
        runner.teardown_test_environment()
        logging.disable(logging.NOTSET)
    return records


# --------------------------------------------------------------------------- live staging section

@dataclass
class LiveSection:
    executed: bool
    reason: str = ""            # why it was not executed
    hint: str = ""
    results: list = field(default_factory=list)  # list[indiapost.uat.ScenarioResult]
    evidence_dir: str = ""


def run_staging_scenarios(output_dir: Path, *, destination_pincode: str, receiver_mobile: str, allow_production: bool) -> LiveSection:
    """Run the live scenarios against the configured India Post server, or explain why they cannot run."""
    from indiapost.services import config_problems
    from indiapost.uat import UAT_HOST, UATRunner, check_connection

    host = urlparse(settings.INDIA_POST_BASE_URL).netloc
    if host != UAT_HOST and not allow_production:
        return LiveSection(False, f"INDIA_POST_BASE_URL points at '{host or 'nothing'}', not the staging server {UAT_HOST}.")
    problems = config_problems()
    if problems:
        return LiveSection(False, "India Post is not configured: " + "; ".join(problems) + ".")
    connection = check_connection(settings.INDIA_POST_BASE_URL)
    if not connection.ok:
        return LiveSection(False, f"Cannot reach the staging server ({connection.stage.upper()}): {connection.detail}", connection.hint)

    evidence = output_dir / "staging_evidence"
    runner = UATRunner(output_dir=evidence, destination_pincode=destination_pincode, receiver_mobile=receiver_mobile)
    return LiveSection(True, results=runner.run(), evidence_dir=str(evidence))


# --------------------------------------------------------------------------- rendering

def _commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5, cwd=settings.BASE_DIR
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def summarise(records: list[TestRecord]) -> dict:
    counts = {s: sum(1 for r in records if r.status == s) for s in ("PASS", "FAIL", "ERROR", "SKIPPED")}
    counts["total"] = len(records)
    counts["seconds"] = round(sum(r.seconds for r in records), 1)
    return counts


def by_area(records: list[TestRecord]) -> "OrderedDict[str, list[TestRecord]]":
    grouped: OrderedDict[str, list[TestRecord]] = OrderedDict()
    for record in sorted(records, key=lambda r: (r.area, r.test_id)):
        grouped.setdefault(record.area, []).append(record)
    return grouped


def render_markdown(records: list[TestRecord], live: LiveSection, *, labels: list[str]) -> str:
    counts = summarise(records)
    host = urlparse(settings.INDIA_POST_BASE_URL).netloc or "(not configured)"
    automated_ok = counts["FAIL"] == 0 and counts["ERROR"] == 0 and counts["total"] > 0
    live_failed = [r for r in live.results if r.status in ("FAIL", "ERROR")]
    live_ok = live.executed and not live_failed and any(r.status == "PASS" for r in live.results)

    lines = [
        "# India Post Integration - Test Report",
        "",
        "| | |",
        "|---|---|",
        "| Application | Yarn Guy storefront (Django) |",
        f"| Application version | commit `{_commit()}` |",
        f"| Generated | {timezone.now().isoformat(timespec='seconds')} |",
        f"| India Post staging URL | `{settings.INDIA_POST_BASE_URL or '(not configured)'}` |",
        f"| Environment | Python {platform.python_version()}, Django {django.get_version()} |",
        f"| Automated test suites | {', '.join(labels)} |",
        f"| **Automated module tests** | **{'ALL PASSED' if automated_ok else 'FAILURES PRESENT'}** - {counts['PASS']} of {counts['total']} passed, "
        f"{counts['FAIL']} failed, {counts['ERROR']} errors, {counts['SKIPPED']} skipped ({counts['seconds']} s) |",
        "| **Staging (UAT) scenarios** | **"
        + ("ALL PASSED" if live_ok else "FAILURES PRESENT" if live.executed else "NOT EXECUTED")
        + "**"
        + (f" - {sum(1 for r in live.results if r.status == 'PASS')} of {len(live.results)} passed" if live.executed else f" - {live.reason}")
        + " |",
        "",
        "## 1. Automated module tests",
        "",
        "These run the integration code against a *simulated* India Post server, so every success, error, "
        "token-expiry, duplicate and outage path can be exercised. They are not a substitute for the staging "
        "scenarios in section 2.",
        "",
        "### Summary by module",
        "",
        "| Module / area | Tests | Passed | Failed | Errors | Skipped |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for area, items in by_area(records).items():
        c = {s: sum(1 for r in items if r.status == s) for s in ("PASS", "FAIL", "ERROR", "SKIPPED")}
        lines.append(f"| {area} | {len(items)} | {c['PASS']} | {c['FAIL']} | {c['ERROR']} | {c['SKIPPED']} |")
    lines.append(f"| **Total** | **{counts['total']}** | **{counts['PASS']}** | **{counts['FAIL']}** | **{counts['ERROR']}** | **{counts['SKIPPED']}** |")

    lines += ["", "### Every test case", ""]
    for area, items in by_area(records).items():
        lines += [f"#### {area}", "", "| # | Test case | Result | Time (s) |", "|---:|---|---|---:|"]
        for index, r in enumerate(items, 1):
            lines.append(f"| {index} | {r.name} | **{r.status}** | {r.seconds:.2f} |")
        lines.append("")

    problems = [r for r in records if r.status in ("FAIL", "ERROR")]
    if problems:
        lines += ["### Failures and errors", ""]
        for r in problems:
            lines += [f"**{r.status}: {r.test_id}**", "", "```", r.detail.strip()[-1500:], "```", ""]

    lines += ["## 2. Staging (UAT) scenarios against the real India Post server", ""]
    if not live.executed:
        lines += [
            f"**Not executed.** {live.reason}",
            "",
            live.hint,
            "",
            "These scenarios are required evidence for production activation. Fix the cause above and run the "
            "command again; nothing in this section is simulated or assumed.",
            "",
        ]
    else:
        lines += [
            f"Executed against `{host}`. The actual HTTP requests and responses are saved in `{live.evidence_dir}` "
            "(credentials and tokens are masked).",
            "",
            "| ID | Scenario | Expected | Actual | Result |",
            "|---|---|---|---|---|",
        ]
        for r in live.results:
            actual = r.actual.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {r.id} | {r.name} | {r.expected} | {actual} | **{r.status}** |")
        lines.append("")
        if live_failed:
            lines += ["### Staging failures", ""] + [f"- **{r.id} {r.name}** - {r.actual}" for r in live_failed] + [""]
    return "\n".join(lines)


def render_text(records: list[TestRecord], live: LiveSection) -> str:
    """The same report as plain text, for printing in the terminal."""
    counts = summarise(records)
    out = ["", "=" * 100, "INDIA POST INTEGRATION - TEST REPORT", "=" * 100]
    out.append(f"India Post staging URL : {settings.INDIA_POST_BASE_URL or '(not configured)'}")
    out.append(f"Application version    : commit {_commit()}")
    out.append(f"Generated              : {timezone.now().isoformat(timespec='seconds')}")
    out += ["", "1. AUTOMATED MODULE TESTS (simulated India Post server)", "-" * 100]
    width = max([len(a) for a in by_area(records)] + [10])
    out.append(f"{'Module / area':<{width}}  {'Tests':>5} {'Pass':>5} {'Fail':>5} {'Err':>4}")
    for area, items in by_area(records).items():
        p = sum(1 for r in items if r.status == "PASS")
        f = sum(1 for r in items if r.status == "FAIL")
        e = sum(1 for r in items if r.status == "ERROR")
        out.append(f"{area:<{width}}  {len(items):>5} {p:>5} {f:>5} {e:>4}")
    out.append(f"{'TOTAL':<{width}}  {counts['total']:>5} {counts['PASS']:>5} {counts['FAIL']:>5} {counts['ERROR']:>4}   ({counts['seconds']} s)")
    out += ["", "Every test case:", ""]
    for area, items in by_area(records).items():
        out.append(f"  {area}")
        for r in items:
            out.append(f"    [{r.status:<7}] {r.name}  ({r.seconds:.2f}s)")
        out.append("")
    for r in (x for x in records if x.status in ("FAIL", "ERROR")):
        out += [f"--- {r.status}: {r.test_id}", r.detail.strip()[-1200:], ""]

    out += ["2. STAGING (UAT) SCENARIOS (real India Post server)", "-" * 100]
    if not live.executed:
        out += [f"NOT EXECUTED: {live.reason}"] + ([live.hint] if live.hint else [])
        out.append("This is required evidence for production activation. Nothing here is simulated.")
    else:
        for r in live.results:
            out.append(f"[{r.status:<7}] {r.id}  {r.name}")
            if r.status in ("FAIL", "ERROR"):
                out.append(f"           {r.actual}")
        out.append(f"Evidence (requests/responses): {live.evidence_dir}")
    out.append("=" * 100)
    return "\n".join(out)


def write_outputs(output_dir: Path, records: list[TestRecord], live: LiveSection, *, labels: list[str]) -> Path:
    """Write the Markdown report and a machine-readable results.json next to it."""
    output_dir.mkdir(parents=True, exist_ok=True)
    report = output_dir / "INDIAPOST_TEST_REPORT.md"
    report.write_text(render_markdown(records, live, labels=labels), encoding="utf-8")
    (output_dir / "results.json").write_text(
        json.dumps(
            {
                "generated_at": timezone.now().isoformat(),
                "commit": _commit(),
                "automated": {"summary": summarise(records), "tests": [r.__dict__ for r in records]},
                "staging": {
                    "executed": live.executed,
                    "reason": live.reason,
                    "scenarios": [{k: v for k, v in r.__dict__.items() if k != "exchanges"} for r in live.results],
                },
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return report
