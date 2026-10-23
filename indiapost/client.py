"""HTTP client for the Department of Posts (India Post) customer APIs."""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Callable, Optional

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_TOKEN_TTL_SECONDS = 780  # 13 minutes (India Post token expires in 15 mins)
_SECRET_FIELDS = {"password", "access_token", "token", "authorization"}


class IndiaPostClientError(Exception):
    """Base exception for India Post API failures."""

    #a transient failure is worth retrying later; everything else needs a human or new data
    retryable = False

    def __init__(self, message: str, *, status_code: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class IndiaPostConfigError(IndiaPostClientError):
    """Required India Post settings are missing."""


class IndiaPostAuthError(IndiaPostClientError):
    """Credentials were rejected (login failed, or a fresh token was still refused)."""


class IndiaPostAPIError(IndiaPostClientError):
    """India Post answered 4xx: the request itself was rejected. Retrying it unchanged won't help."""


class IndiaPostTransientError(IndiaPostClientError):
    """Network error, timeout or 5xx from India Post. Safe to retry."""

    retryable = True


def _redact(value: Any) -> Any:
    """Copy of ``value`` with credentials/tokens masked, safe to write to logs and evidence files."""
    if isinstance(value, dict):
        return {
            k: ("***" if str(k).lower() in _SECRET_FIELDS else _redact(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


class IndiaPostClient:
    """
    Token authentication, pincode lookup, tariffs, booking, labels and tracking.

    Every request goes through ``_request``, which refreshes an expired token once and
    classifies failures (``IndiaPostTransientError`` vs ``IndiaPostAPIError`` ...).
    An optional ``recorder`` callable receives a redacted description of each HTTP
    exchange — the UAT runner uses it to save real request/response evidence.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: int = 25,
        recorder: Optional[Callable[[dict[str, Any]], None]] = None,
    ):
        self.base_url = (base_url if base_url is not None else settings.INDIA_POST_BASE_URL).rstrip("/")
        self.username = username if username is not None else settings.INDIA_POST_USERNAME
        self.password = password if password is not None else settings.INDIA_POST_PASSWORD
        self.timeout = timeout
        self.recorder = recorder

    # ------------------------------------------------------------------ plumbing

    @property
    def _token_cache_key(self) -> str:
        #scoped per host + user so test and production tokens can never be mixed up
        scope = hashlib.sha1(f"{self.base_url}|{self.username}".encode()).hexdigest()[:12]
        return f"indiapost_access_token:{scope}"

    def _require_config(self) -> None:
        missing = [
            name
            for name, value in (
                ("INDIA_POST_BASE_URL", self.base_url),
                ("INDIA_POST_USERNAME", self.username),
                ("INDIA_POST_PASSWORD", self.password),
            )
            if not value
        ]
        if missing:
            raise IndiaPostConfigError(f"India Post is not configured: set {', '.join(missing)}.")

    def _record(self, *, method: str, url: str, params, body, response, started: float, error: str = "") -> None:
        if self.recorder is None:
            return
        entry: dict[str, Any] = {
            "method": method,
            "url": url,
            "params": _redact(params),
            "request_body": _redact(body),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
        if response is not None:
            entry["status_code"] = response.status_code
            try:
                entry["response_body"] = _redact(response.json())
            except Exception:
                entry["response_body"] = None
                entry["response_bytes"] = len(response.content or b"")
        if error:
            entry["error"] = error
        self.recorder(entry)

    def _send(self, method: str, url: str, *, params=None, body=None, headers=None):
        started = time.monotonic()
        try:
            response = requests.request(
                method, url, params=params, json=body, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as exc:
            self._record(method=method, url=url, params=params, body=body, response=None, started=started, error=str(exc))
            raise IndiaPostTransientError(f"Could not reach India Post: {exc}") from exc
        self._record(method=method, url=url, params=params, body=body, response=response, started=started)
        return response

    def _request(self, method: str, path: str, *, params=None, body=None):
        """Send one authenticated API call and return the successful ``requests.Response``."""
        self._require_config()
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json"}

        refreshed = False
        while True:
            headers["Authorization"] = f"Bearer {self.get_access_token(force_refresh=refreshed)}"
            response = self._send(method, url, params=params, body=body, headers=headers)
            if response.status_code == 401 and not refreshed:
                logger.warning("India Post returned 401; logging in again and retrying once.")
                refreshed = True
                continue
            break

        status = response.status_code
        if status < 400:
            return response

        try:
            detail = response.json()
        except Exception:
            detail = None
        if status in (401, 403):
            raise IndiaPostAuthError(f"India Post refused the credentials (HTTP {status}).", status_code=status, body=detail)
        if status >= 500 or status in (408, 429):
            raise IndiaPostTransientError(f"India Post is unavailable (HTTP {status}).", status_code=status, body=detail)
        raise IndiaPostAPIError(f"India Post rejected the request (HTTP {status}).", status_code=status, body=detail)

    # ------------------------------------------------------------------ auth

    def get_access_token(self, force_refresh: bool = False) -> str:
        """Return a valid Bearer token, cached for 13 minutes; log in again when it expires."""
        self._require_config()
        if not force_refresh:
            cached_token = cache.get(self._token_cache_key)
            if cached_token:
                return cached_token

        url = f"{self.base_url}/v1/access/login"
        body = {"username": self.username, "password": self.password}
        response = self._send("POST", url, body=body, headers={"Content-Type": "application/json"})

        if response.status_code in (400, 401, 403):
            raise IndiaPostAuthError(
                f"India Post login was rejected (HTTP {response.status_code}).", status_code=response.status_code
            )
        if response.status_code >= 400:
            raise IndiaPostTransientError(
                f"India Post login failed (HTTP {response.status_code}).", status_code=response.status_code
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise IndiaPostTransientError("India Post login returned a non-JSON response.") from exc

        if not data.get("success"):
            raise IndiaPostAuthError(f"India Post login failed: {data.get('message') or 'no details given'}")
        token = (data.get("data") or {}).get("access_token")
        if not token:
            raise IndiaPostAuthError("India Post login response did not contain an access_token.")

        cache.set(self._token_cache_key, token, timeout=CACHE_TOKEN_TTL_SECONDS)
        logger.info("Fetched and cached a new India Post access token.")
        return token

    # ------------------------------------------------------------------ endpoints

    def search_pincode(self, pincode: str | int) -> list[dict[str, Any]]:
        """GET /v1/pincode-search — post offices serving a pincode (empty list when unknown)."""
        response = self._request(
            "GET", "/v1/pincode-search", params={"pincode": str(pincode).strip(), "office-type": "post"}
        )
        data = response.json()
        return data if isinstance(data, list) else []

    def get_eligible_office_id(self, pincode: str | int) -> Optional[int]:
        """First delivery office (not a branch office) for ``pincode``, else the first office listed."""
        offices = self.search_pincode(pincode)
        for office in offices:
            if office.get("delivery_office_flag") is True and office.get("office_type_code") != "BPO":
                try:
                    return int(office.get("office_id"))
                except (ValueError, TypeError):
                    continue
        if offices:
            try:
                return int(offices[0]["office_id"])
            except (KeyError, ValueError, TypeError):
                pass
        return None

    def calculate_speed_post_tariff(
        self,
        weight_grams: int,
        source_pincode: str | int,
        destination_pincode: str | int,
        length: float = 0,
        width: float = 0,
        height: float = 0,
        ins_amount: int = 0,
        pod: bool = False,
    ) -> dict[str, Any]:
        """GET /v1/speed-post/tariffs"""
        params = {
            "product-code": "SP",
            "weight": int(weight_grams),
            "source-pincode": str(source_pincode),
            "destination-pincode": str(destination_pincode),
            "length": length or 0,
            "width": width or 0,
            "height": height or 0,
            "INS": ins_amount,
            "POD": "YES" if pod else "NO",
        }
        return self._request("GET", "/v1/speed-post/tariffs", params=params).json()

    def calculate_business_parcel_tariff(
        self,
        weight_grams: int,
        source_pincode: str | int,
        destination_pincode: str | int,
        length: float = 0,
        width: float = 0,
        height: float = 0,
        ins_amount: int = 0,
    ) -> dict[str, Any]:
        """GET /v1/business-parcel-tariff/calculate"""
        params = {
            "product-code": "BP",
            "weight": int(weight_grams),
            "source-pincode": str(source_pincode),
            "destination-pincode": str(destination_pincode),
            "length": length or 0,
            "width": width or 0,
            "height": height or 0,
            "ins": ins_amount,
        }
        return self._request("GET", "/v1/business-parcel-tariff/calculate", params=params).json()

    def book_articles(self, customer_id: str, articles: list[dict[str, Any]]) -> dict[str, Any]:
        """POST /process-articles/:customerId — bulk booking. 4xx raises ``IndiaPostAPIError``."""
        return self._request("POST", f"/process-articles/{customer_id}", body={"articles": articles}).json()

    def generate_domestic_label(self, label_articles: list[dict[str, Any]]) -> bytes:
        """POST /v1/label/create/domestic — returns the label PDF bytes."""
        return self._request("POST", "/v1/label/create/domestic", body=label_articles).content

    def track_bulk(self, barcodes: list[str]) -> Any:
        """POST /v1/tracking/bulk — up to 500 articles per call."""
        return self._request("POST", "/v1/tracking/bulk", body={"bulk": barcodes}).json()
