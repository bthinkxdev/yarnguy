"""
India Post UAT test runner.

Runs every portal scenario against the configured India Post environment through the
same code paths production uses (``IndiaPostClient`` + ``book_shipment``), records each
real HTTP request/response, and writes an evidence folder plus a test report.

Nothing here is mocked: a scenario passes only if the live system behaved as expected.
Credentials and tokens are masked in everything that is written to disk.
"""

from __future__ import annotations

import json
import socket
import ssl
import subprocess
import time
import traceback
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from indiapost.client import (
    IndiaPostAPIError,
    IndiaPostAuthError,
    IndiaPostClient,
)
from indiapost.models import IndiaPostBarcodePool, IndiaPostShipment, ShipmentStatus
from indiapost.services import (
    IndiaPostBookingError,
    PICKUP_SLOTS,
    book_reverse_pickup,
    book_shipment,
    build_article_payload,
    extract_tracking_items,
    generate_and_save_shipping_label,
    load_config,
    order_lines,
    plan_parcel,
    resolve_recipient,
    store_party,
)

UAT_HOST = "test.cept.gov.in"
UAT_ORDER_PREFIX = "UAT-"
PASS, FAIL, ERROR, SKIPPED = "PASS", "FAIL", "ERROR", "SKIPPED"


@dataclass
class ConnectionCheck:
    ok: bool
    stage: str  # "dns", "tcp", "tls" or "ready"
    detail: str
    hint: str = ""


def _public_ip() -> str:
    """This machine's public address as the internet sees it (what India Post must whitelist)."""
    try:
        return urllib.request.urlopen("https://api.ipify.org", timeout=8).read().decode().strip()
    except Exception:
        return ""


def check_connection(base_url: str, *, timeout: int = 10) -> ConnectionCheck:
    """
    Can this machine open a TLS connection to the India Post server? Sends no API request.

    A server that accepts the TCP connection and then drops the TLS handshake is almost always an
    IP allowlist: India Post only serves whitelisted addresses, so the hint names the public IP to
    send them.
    """
    parsed = urlparse(base_url)
    host, port = parsed.hostname, parsed.port or 443
    if not host:
        return ConnectionCheck(False, "dns", "INDIA_POST_BASE_URL is empty or not a URL.")
    try:
        socket.gethostbyname(host)
    except OSError as exc:
        return ConnectionCheck(False, "dns", f"Cannot resolve {host}: {exc}", "Check the base URL and this machine's DNS / internet access.")
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        return ConnectionCheck(False, "tcp", f"Cannot connect to {host}:{port}: {exc}", "A firewall or proxy is blocking outbound HTTPS to this host.")
    try:
        with raw, ssl.create_default_context().wrap_socket(raw, server_hostname=host):
            return ConnectionCheck(True, "ready", f"TLS connection to {host}:{port} succeeded.")
    except (OSError, ssl.SSLError) as exc:
        ip = _public_ip()
        return ConnectionCheck(
            False,
            "tls",
            f"{host} accepted the connection and then dropped the TLS handshake: {exc}",
            "India Post's servers only accept whitelisted IP addresses. Ask integrations.cept@indiapost.gov.in to whitelist "
            + (f"this machine's public IP ({ip})" if ip else "this machine's public IP address")
            + ", then run the command again.",
        )


@dataclass
class ScenarioResult:
    id: str
    name: str
    expected: str
    status: str = ERROR
    actual: str = ""
    evidence_file: str = ""
    exchanges: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0


class Outcome:
    """What a scenario method returns: its verdict and a one-line description of what happened."""

    def __init__(self, passed: bool, actual: str):
        self.passed = passed
        self.actual = actual


class Skip(Exception):
    """Raised by a scenario that cannot run (missing prerequisite)."""


class UATRunner:
    def __init__(
        self,
        *,
        output_dir: Path,
        destination_pincode: str = "682001",
        receiver_mobile: str = "9876543210",
    ):
        self.output_dir = Path(output_dir)
        self.destination_pincode = destination_pincode
        self.receiver_mobile = receiver_mobile
        self._exchanges: list[dict[str, Any]] = []
        self.client = IndiaPostClient(recorder=self._exchanges.append)
        self.config = load_config()
        self.state: dict[str, Any] = {}
        self._run_stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
        self._order_seq = 0

        self.scenarios: list[tuple[str, str, str, Callable[[], Outcome]]] = [
            ("UAT-01", "Login with valid credentials", "HTTP 200, success=true and an access token is returned", self.login),
            ("UAT-02", "Login with invalid credentials", "Login is rejected and no token is issued", self.login_invalid),
            ("UAT-03", "Token refresh after expiry", "A stale token is refused (401), the client logs in again and the retried call succeeds", self.token_refresh),
            ("UAT-04", "Pincode search - valid pincode", "Post office list returned for the destination pincode", self.pincode_valid),
            ("UAT-05", "Pincode search - invalid pincode", "No post offices returned (or the request is rejected) for pincode 000000", self.pincode_invalid),
            ("UAT-06", "Origin booking office lookup", "An eligible delivery office id is returned for the origin pincode", self.origin_office),
            ("UAT-07", "Speed Post tariff", "Tariff returned for a 500 g parcel from origin to destination", self.speed_post_tariff),
            ("UAT-08", "Business Parcel tariff", "Tariff returned for a 500 g parcel from origin to destination", self.business_parcel_tariff),
            ("UAT-09", "Booking - prepaid order", "Booking accepted; barcode, batch id and tariff stored; shipment status Booked", self.book_prepaid),
            ("UAT-10", "Booking - cash on delivery order", "Booking accepted with COD flag and COD amount equal to the order total", self.book_cod),
            ("UAT-11", "Shipping label PDF", "A valid PDF label is generated and saved for the booked article", self.label_pdf),
            ("UAT-12", "Tracking", "Bulk tracking responds successfully for the booked barcodes", self.tracking),
            ("UAT-13", "Failure - invalid destination pincode", "Booking refused locally with a PIN error; no booking request is sent", self.fail_bad_pincode),
            ("UAT-14", "Failure - invalid receiver mobile", "Booking refused locally with a mobile error; no booking request is sent", self.fail_bad_phone),
            ("UAT-15", "Failure - non-existent pincode", "India Post rejects the booking and the shipment is marked Failed", self.fail_unknown_pincode),
            ("UAT-16", "Failure - duplicate barcode", "India Post rejects a second booking that reuses an already booked barcode", self.fail_duplicate_barcode),
            ("UAT-17", "Idempotency - repeat booking request", "Re-booking an already booked order sends no second booking request", self.idempotent_rebook),
            ("UAT-18", "Reverse pickup - customer return", "India Post accepts a PICKUP booking collecting from the customer's address to the store", self.reverse_pickup),
        ]

    # ------------------------------------------------------------------ orchestration

    def run(self, only: Optional[list[str]] = None) -> list[ScenarioResult]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_uat_barcode_pool()
        results = []
        for scenario_id, name, expected, method in self.scenarios:
            if only and scenario_id not in only:
                continue
            results.append(self._run_one(scenario_id, name, expected, method))
        self._cancel_uat_orders()
        return results

    def _run_one(self, scenario_id: str, name: str, expected: str, method: Callable[[], Outcome]) -> ScenarioResult:
        result = ScenarioResult(id=scenario_id, name=name, expected=expected)
        self._exchanges.clear()
        started = time.monotonic()
        try:
            outcome = method()
            result.status = PASS if outcome.passed else FAIL
            result.actual = outcome.actual
        except Skip as exc:
            result.status, result.actual = SKIPPED, str(exc)
        except Exception as exc:  # a crash is reported, never hidden
            result.status = ERROR
            result.actual = f"{type(exc).__name__}: {exc}"
            self._exchanges.append({"error": traceback.format_exc()})
        result.duration_ms = round((time.monotonic() - started) * 1000)
        result.exchanges = list(self._exchanges)
        result.evidence_file = self._write_evidence(result)
        return result

    def _write_evidence(self, result: ScenarioResult) -> str:
        filename = f"{result.id}.json"
        payload = {
            "scenario": result.id,
            "name": result.name,
            "expected": result.expected,
            "actual": result.actual,
            "status": result.status,
            "exchanges": result.exchanges,
        }
        (self.output_dir / filename).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return filename

    # ------------------------------------------------------------------ helpers

    def _ensure_uat_barcode_pool(self) -> None:
        """Bookings need barcodes; on the UAT server use the series India Post issues for testing."""
        if urlparse(settings.INDIA_POST_BASE_URL).netloc == UAT_HOST and not IndiaPostBarcodePool.objects.filter(is_active=True).exists():
            IndiaPostBarcodePool.objects.create(prefix="ET", current_number=21433001, end_number=21434000, suffix="IN")

    def _calls_to(self, fragment: str) -> list[dict[str, Any]]:
        return [e for e in self._exchanges if fragment in e.get("url", "")]

    def _make_order(self, *, label: str, cod: bool, pincode: str, mobile: str):
        """A clearly-marked UAT order (inactive product, UAT- number) to book with."""
        from django.contrib.auth.models import User

        from accounts.services import ensure_customer_profile_for_user
        from catalog.models import Category, Product
        from core.selectors import get_default_currency
        from orders.models import Order, OrderItem, OrderStatus
        from payments.models import PaymentStatus, PaymentTransaction

        user, _ = User.objects.get_or_create(
            username="uat-runner",
            defaults={"email": "uat-runner@yarnguy.invalid", "first_name": "UAT", "last_name": "Runner"},
        )
        profile = ensure_customer_profile_for_user(user=user)
        category, _ = Category.objects.get_or_create(slug="uat-category", defaults={"name": "UAT Category"})
        product, _ = Product.objects.get_or_create(
            sku="UAT-PRODUCT",
            defaults=dict(
                name="UAT Test Product", slug="uat-test-product", category=category, base_price="499.00",
                mrp="499.00", purchase_price="300.00", stock_quantity=1000, weight="0.400",
                length="25", width="20", height="3", is_active=False,
            ),
        )
        currency = get_default_currency()
        self._order_seq += 1
        order = Order.objects.create(
            customer_profile=profile,
            order_number=f"{UAT_ORDER_PREFIX}{self._run_stamp}-{label}{self._order_seq}"[:32],
            order_status=OrderStatus.PLACED_COD if cod else OrderStatus.CONFIRMED,
            success_at=timezone.now(),
            subtotal=Decimal("499.00"),
            total_amount=Decimal("549.00") if cod else Decimal("499.00"),
            currency=currency,
            delivery_address_snapshot={
                "name": "UAT Test Customer",
                "phone": mobile,
                "email": "uat-customer@yarnguy.invalid",
                "line1": "UAT Test Address, MG Road",
                "line2": "Near Test Junction",
                "city": "Kochi",
                "state": "Kerala",
                "pincode": pincode,
            },
        )
        OrderItem.objects.create(order=order, product=product, quantity=1, unit_price=Decimal("499.00"))
        PaymentTransaction.objects.create(
            order=order,
            gateway_key="cod" if cod else "razorpay_upi",
            amount=order.total_amount,
            currency=currency,
            status=PaymentStatus.PENDING if cod else PaymentStatus.SUCCESS,
        )
        return order

    def _cancel_uat_orders(self) -> None:
        """Take the synthetic orders out of sales reporting once the run is over."""
        from orders.models import Order, OrderStatus

        Order.objects.filter(order_number__startswith=UAT_ORDER_PREFIX).update(order_status=OrderStatus.CANCELLED)

    def _need(self, key: str, scenario: str) -> Any:
        if key not in self.state:
            raise Skip(f"Skipped: depends on {scenario}, which did not complete.")
        return self.state[key]

    # ------------------------------------------------------------------ scenarios

    def login(self) -> Outcome:
        cache.delete(self.client._token_cache_key)
        token = self.client.get_access_token(force_refresh=True)
        return Outcome(bool(token), f"Token received ({len(token)} characters, masked in evidence).")

    def login_invalid(self) -> Outcome:
        bad = IndiaPostClient(password="wrong-password-for-uat", recorder=self._exchanges.append)
        try:
            bad.get_access_token(force_refresh=True)
        except IndiaPostAuthError as exc:
            return Outcome(True, f"Rejected as expected: {exc}")
        return Outcome(False, "Login with a wrong password was NOT rejected.")

    def token_refresh(self) -> Outcome:
        cache.set(self.client._token_cache_key, "expired-token-for-uat", 60)
        offices = self.client.search_pincode(self.destination_pincode)
        statuses = [(e.get("method"), e.get("url", "").rsplit("/", 1)[-1], e.get("status_code")) for e in self._exchanges]
        first_call = next((e for e in self._exchanges if "pincode-search" in e.get("url", "")), {})
        relogged = any("/v1/access/login" in e.get("url", "") for e in self._exchanges)
        passed = first_call.get("status_code") == 401 and relogged and bool(offices)
        return Outcome(passed, f"Exchange sequence (method, endpoint, status): {statuses}; offices returned: {len(offices)}.")

    def pincode_valid(self) -> Outcome:
        offices = self.client.search_pincode(self.destination_pincode)
        return Outcome(bool(offices), f"{len(offices)} post office(s) returned for {self.destination_pincode}.")

    def pincode_invalid(self) -> Outcome:
        try:
            offices = self.client.search_pincode("000000")
        except IndiaPostAPIError as exc:
            return Outcome(True, f"Request rejected by India Post: {exc}")
        return Outcome(not offices, f"{len(offices)} post office(s) returned for pincode 000000.")

    def origin_office(self) -> Outcome:
        office_id = self.client.get_eligible_office_id(self.config.origin_pincode)
        configured = self.config.origin_office_id
        note = "matches" if office_id == configured else f"differs from configured INDIA_POST_ORIGIN_OFFICE_ID ({configured})"
        return Outcome(office_id is not None, f"Eligible delivery office id {office_id} for {self.config.origin_pincode}; {note}.")

    def speed_post_tariff(self) -> Outcome:
        tariff = self.client.calculate_speed_post_tariff(500, self.config.origin_pincode, self.destination_pincode, 25, 20, 3)
        return Outcome(bool(tariff), f"Tariff response: {json.dumps(tariff, default=str)[:300]}")

    def business_parcel_tariff(self) -> Outcome:
        if not settings.INDIA_POST_CONTRACT_ID_BP:
            raise Skip("Skipped: INDIA_POST_CONTRACT_ID_BP is not configured (account has no Business Parcel contract).")
        tariff = self.client.calculate_business_parcel_tariff(500, self.config.origin_pincode, self.destination_pincode, 25, 20, 3)
        return Outcome(bool(tariff), f"Tariff response: {json.dumps(tariff, default=str)[:300]}")

    def _book(self, *, cod: bool, key: str) -> IndiaPostShipment:
        order = self._make_order(label="COD" if cod else "PRE", cod=cod, pincode=self.destination_pincode, mobile=self.receiver_mobile)
        shipment = book_shipment(order, client=self.client)
        self.state[key] = shipment
        return shipment

    def book_prepaid(self) -> Outcome:
        shipment = self._book(cod=False, key="prepaid")
        passed = shipment.tracking_status == ShipmentStatus.BOOKED and bool(shipment.barcode_number)
        return Outcome(
            passed,
            f"Status {shipment.tracking_status}; barcode {shipment.barcode_number}; batch {shipment.batch_id or '-'}; tariff {shipment.calculated_tariff}.",
        )

    def book_cod(self) -> Outcome:
        shipment = self._book(cod=True, key="cod")
        sent = next((e for e in self._calls_to("/process-articles/")), {})
        article = ((sent.get("request_body") or {}).get("articles") or [{}])[0]
        order_total = float(shipment.order.total_amount)
        cod_ok = article.get("codr_cod") == "COD" and float(article.get("value_for_codr_cod") or 0) == order_total
        passed = shipment.tracking_status == ShipmentStatus.BOOKED and cod_ok
        return Outcome(
            passed,
            f"Status {shipment.tracking_status}; barcode {shipment.barcode_number}; sent codr_cod={article.get('codr_cod')!r}, value_for_codr_cod={article.get('value_for_codr_cod')} (order total {order_total}).",
        )

    def label_pdf(self) -> Outcome:
        shipment = self._need("prepaid", "UAT-09")
        if not shipment.label_pdf:
            generate_and_save_shipping_label(shipment, client=self.client)
            shipment.refresh_from_db()
        if not shipment.label_pdf:
            return Outcome(False, f"No label was produced. {shipment.error_message or ''}".strip())
        with shipment.label_pdf.open("rb") as handle:
            content = handle.read()
        target = self.output_dir / f"label_{shipment.barcode_number}.pdf"
        target.write_bytes(content)
        return Outcome(content.startswith(b"%PDF"), f"{len(content)} bytes; starts with {content[:8]!r}; saved as {target.name}.")

    def tracking(self) -> Outcome:
        barcodes = [self.state[k].barcode_number for k in ("prepaid", "cod") if k in self.state]
        if not barcodes:
            raise Skip("Skipped: no booked barcodes (UAT-09/UAT-10 did not complete).")
        data = self.client.track_bulk(barcodes)
        items = extract_tracking_items(data)
        return Outcome(True, f"HTTP 2xx for {len(barcodes)} barcode(s); {len(items)} tracking record(s) parsed. Raw response in evidence.")

    def _expect_local_refusal(self, *, pincode: str, mobile: str, label: str, marker: str) -> Outcome:
        order = self._make_order(label=label, cod=False, pincode=pincode, mobile=mobile)
        try:
            book_shipment(order, client=self.client)
        except IndiaPostBookingError as exc:
            sent = self._calls_to("/process-articles/")
            shipment = IndiaPostShipment.objects.get(order=order)
            passed = marker in str(exc).lower() and not sent and shipment.tracking_status == ShipmentStatus.FAILED
            return Outcome(passed, f"Refused: {exc} Booking requests sent: {len(sent)}. Shipment status: {shipment.tracking_status}.")
        return Outcome(False, "Booking was NOT refused.")

    def fail_bad_pincode(self) -> Outcome:
        return self._expect_local_refusal(pincode="12345", mobile=self.receiver_mobile, label="BADPIN", marker="pin")

    def fail_bad_phone(self) -> Outcome:
        return self._expect_local_refusal(pincode=self.destination_pincode, mobile="1234567890", label="BADTEL", marker="mobile")

    def fail_unknown_pincode(self) -> Outcome:
        order = self._make_order(label="NOPIN", cod=False, pincode="999999", mobile=self.receiver_mobile)
        try:
            shipment = book_shipment(order, client=self.client)
        except IndiaPostBookingError as exc:
            shipment = IndiaPostShipment.objects.get(order=order)
            reached_api = bool(self._calls_to("/process-articles/"))
            passed = reached_api and shipment.tracking_status == ShipmentStatus.FAILED
            where = "by India Post" if reached_api else "locally, BEFORE reaching India Post"
            return Outcome(passed, f"Rejected {where}: {exc} Shipment status: {shipment.tracking_status}.")
        return Outcome(False, f"India Post ACCEPTED a booking to non-existent pincode 999999 (barcode {shipment.barcode_number}).")

    def fail_duplicate_barcode(self) -> Outcome:
        booked = self._need("prepaid", "UAT-09")
        order = self._make_order(label="DUP", cod=False, pincode=self.destination_pincode, mobile=self.receiver_mobile)
        payload = build_article_payload(
            barcode=booked.barcode_number,
            config=self.config,
            parcel=plan_parcel(order_lines(order)),
            sender=store_party(self.config),
            receiver=resolve_recipient(order),
            reference=order.order_number,
        )
        try:
            response = self.client.book_articles(self.config.customer_id, [payload])
        except IndiaPostAPIError as exc:
            return Outcome(True, f"Rejected with HTTP {exc.status_code}: {exc.body}")
        accepted = bool(response.get("success")) and not response.get("error_articles") and (response.get("summary") or {}).get("success_count", 1) > 0
        return Outcome(not accepted, f"HTTP 2xx; success={response.get('success')}; error_articles={response.get('error_articles')}.")

    def idempotent_rebook(self) -> Outcome:
        shipment = self._need("prepaid", "UAT-09")
        again = book_shipment(shipment.order, client=self.client)
        sent = self._calls_to("/process-articles/")
        return Outcome(not sent and again.pk == shipment.pk, f"Same shipment returned ({again.barcode_number}); booking requests sent: {len(sent)}.")


    def reverse_pickup(self) -> Outcome:
        from datetime import timedelta

        order = self._make_order(label="RET", cod=False, pincode=self.destination_pincode, mobile=self.receiver_mobile)
        barcode = IndiaPostBarcodePool.allocate_next_barcode()
        booking = book_reverse_pickup(
            barcode=barcode,
            reference=order.order_number,
            pickup_from=resolve_recipient(order),
            lines=order_lines(order),
            day=timezone.localdate() + timedelta(days=1),
            slot=next(iter(PICKUP_SLOTS)),
            client=self.client,
        )
        sent = (next(iter(self._calls_to("/process-articles/")), {}).get("request_body") or {}).get("articles", [{}])[0]
        shape_ok = sent.get("pickup_or_dropoff") == "PICKUP" and sent.get("pickup_address_flag") == "TRUE"
        return Outcome(
            shape_ok,
            f"Accepted: barcode {booking.barcode}, batch {booking.batch_id or '-'}, tariff {booking.tariff}; "
            f"sent pickup_or_dropoff={sent.get('pickup_or_dropoff')!r}, pickup_schedule_date={sent.get('pickup_schedule_date')!r}.",
        )


# ---------------------------------------------------------------------- reporting

def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5, cwd=settings.BASE_DIR
        ).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def write_report(results: list[ScenarioResult], output_dir: Path) -> Path:
    """Write ``results.json`` and ``UAT_TEST_REPORT.md`` into ``output_dir``."""
    output_dir = Path(output_dir)
    host = urlparse(settings.INDIA_POST_BASE_URL).netloc or "(not configured)"
    counts = {s: sum(1 for r in results if r.status == s) for s in (PASS, FAIL, ERROR, SKIPPED)}
    verdict = (
        "ALL EXECUTED SCENARIOS PASSED" if not counts[FAIL] and not counts[ERROR] and counts[PASS]
        else "NOT ALL SCENARIOS PASSED - see failures below"
    )

    meta = {
        "generated_at": timezone.now().isoformat(),
        "environment_host": host,
        "application_commit": _git_commit(),
        "summary": counts,
        "verdict": verdict,
    }
    (output_dir / "results.json").write_text(
        json.dumps({"meta": meta, "scenarios": [{k: v for k, v in r.__dict__.items() if k != "exchanges"} for r in results]}, indent=2),
        encoding="utf-8",
    )

    lines = [
        "# India Post API Integration - UAT Test Report",
        "",
        "| | |",
        "|---|---|",
        f"| Application | Yarn Guy storefront (Django) |",
        f"| Application version | commit `{meta['application_commit']}` |",
        f"| India Post environment | `{host}` |",
        f"| Executed at | {meta['generated_at']} |",
        f"| Result | **{verdict}** |",
        f"| Passed / Failed / Errors / Skipped | {counts[PASS]} / {counts[FAIL]} / {counts[ERROR]} / {counts[SKIPPED]} |",
        "",
        "All results below come from live calls to the environment above. Request and response",
        "evidence for each scenario is in the JSON file named in the table (credentials and",
        "tokens are masked).",
        "",
        "## Results",
        "",
        "| ID | Scenario | Expected | Actual | Result | Evidence |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        actual = r.actual.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r.id} | {r.name} | {r.expected} | {actual} | **{r.status}** | `{r.evidence_file}` |")

    lines += ["", "## HTTP exchanges", ""]
    for r in results:
        lines.append(f"### {r.id} - {r.name} ({r.status}, {r.duration_ms} ms)")
        lines.append("")
        if not r.exchanges:
            lines += ["_No HTTP request was made._", ""]
            continue
        lines += ["| Method | URL | HTTP status | Time (ms) |", "|---|---|---|---|"]
        for e in r.exchanges:
            if "url" in e:
                lines.append(f"| {e['method']} | `{e['url']}` | {e.get('status_code', e.get('error', '-'))} | {e.get('elapsed_ms', '-')} |")
        lines.append("")

    problems = [r for r in results if r.status in (FAIL, ERROR)]
    if problems:
        lines += ["## Failures and errors", ""]
        lines += [f"- **{r.id} {r.name}** - {r.actual}" for r in problems]
        lines.append("")

    report = output_dir / "UAT_TEST_REPORT.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report
