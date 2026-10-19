import logging
from typing import Any, Optional
import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_TOKEN_KEY = "indiapost_access_token"
CACHE_TOKEN_TTL_SECONDS = 780  # 13 minutes (India Post token expires in 15 mins)


class IndiaPostClientError(Exception):
    """Base exception for India Post API failures."""
    pass


class IndiaPostClient:
    """
    HTTP client for interacting with the Department of Posts (India Post) APIs.
    Handles token authentication, pincode lookup, tariff calculation,
    article booking, label generation, and tracking.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: int = 25,
    ):
        self.base_url = (base_url or getattr(settings, "INDIA_POST_BASE_URL", "https://test.cept.gov.in/beextcustomer")).rstrip("/")
        self.username = username or getattr(settings, "INDIA_POST_USERNAME", "")
        self.password = password or getattr(settings, "INDIA_POST_PASSWORD", "")
        self.timeout = timeout

    def get_access_token(self, force_refresh: bool = False) -> str:
        """
        Retrieves a valid Bearer access token, caching it for 13 minutes.
        Auto-refreshes when expired.
        """
        if not force_refresh:
            cached_token = cache.get(CACHE_TOKEN_KEY)
            if cached_token:
                return cached_token

        login_url = f"{self.base_url}/v1/access/login"
        payload = {
            "username": self.username,
            "password": self.password,
        }

        try:
            resp = requests.post(
                login_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()

            if not data.get("success"):
                err_msg = data.get("message") or "Authentication failed without error details"
                raise IndiaPostClientError(f"India Post login failed: {err_msg}")

            token = data.get("data", {}).get("access_token")
            if not token:
                raise IndiaPostClientError("Missing access_token in India Post login response")

            # Store in cache
            cache.set(CACHE_TOKEN_KEY, token, timeout=CACHE_TOKEN_TTL_SECONDS)
            logger.info("Successfully fetched and cached new India Post access token.")
            return token

        except requests.RequestException as exc:
            logger.error(f"Error connecting to India Post login endpoint: {exc}")
            raise IndiaPostClientError(f"Failed to authenticate with India Post: {exc}") from exc

    def _get_auth_headers(self, force_refresh: bool = False) -> dict[str, str]:
        token = self.get_access_token(force_refresh=force_refresh)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def search_pincode(self, pincode: str | int) -> list[dict[str, Any]]:
        """
        Validates pincode and retrieves list of post office details.
        GET /v1/pincode-search?pincode=<pincode>&office-type=post
        """
        url = f"{self.base_url}/v1/pincode-search"
        params = {"pincode": str(pincode).strip(), "office-type": "post"}

        try:
            resp = requests.get(url, params=params, headers=self._get_auth_headers(), timeout=self.timeout)
            if resp.status_code == 401:
                resp = requests.get(url, params=params, headers=self._get_auth_headers(force_refresh=True), timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except requests.RequestException as exc:
            logger.error(f"Pincode search failed for {pincode}: {exc}")
            raise IndiaPostClientError(f"Error querying pincode {pincode}: {exc}") from exc

    def get_eligible_office_id(self, pincode: str | int) -> Optional[int]:
        """
        Finds the first office_id matching India Post's booking criteria:
        1. delivery_office_flag == True
        2. office_type_code != 'BPO'
        """
        offices = self.search_pincode(pincode)
        for off in offices:
            if off.get("delivery_office_flag") is True and off.get("office_type_code") != "BPO":
                try:
                    return int(off.get("office_id"))
                except (ValueError, TypeError):
                    continue
        # Fallback to any office_id in the list
        if offices and "office_id" in offices[0]:
            try:
                return int(offices[0]["office_id"])
            except (ValueError, TypeError):
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
        """
        Calculates Speed Post tariff.
        GET /v1/speed-post/tariffs
        """
        url = f"{self.base_url}/v1/speed-post/tariffs"
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
        try:
            resp = requests.get(url, params=params, headers=self._get_auth_headers(), timeout=self.timeout)
            if resp.status_code == 401:
                resp = requests.get(url, params=params, headers=self._get_auth_headers(force_refresh=True), timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.error(f"Tariff calculation failed: {exc}")
            raise IndiaPostClientError(f"Failed to calculate Speed Post tariff: {exc}") from exc

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
        """
        Calculates Business Parcel tariff.
        GET /v1/business-parcel-tariff/calculate
        """
        url = f"{self.base_url}/v1/business-parcel-tariff/calculate"
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
        try:
            resp = requests.get(url, params=params, headers=self._get_auth_headers(), timeout=self.timeout)
            if resp.status_code == 401:
                resp = requests.get(url, params=params, headers=self._get_auth_headers(force_refresh=True), timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.error(f"Business parcel tariff calculation failed: {exc}")
            raise IndiaPostClientError(f"Failed to calculate Business Parcel tariff: {exc}") from exc

    def book_articles(self, customer_id: str, articles: list[dict[str, Any]]) -> dict[str, Any]:
        """
        Processes bulk booking of postal articles with automatic 401 re-login retry.
        POST /process-articles/:customId
        """
        url = f"{self.base_url}/process-articles/{customer_id}"
        payload = {"articles": articles}

        try:
            resp = requests.post(url, json=payload, headers=self._get_auth_headers(), timeout=self.timeout)
            if resp.status_code == 401:
                logger.warning("India Post 401 received. Refreshing token and retrying booking...")
                cache.delete(CACHE_TOKEN_KEY)
                resp = requests.post(url, json=payload, headers=self._get_auth_headers(force_refresh=True), timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.error(f"Booking articles failed: {exc}")
            if hasattr(exc, "response") and exc.response is not None:
                if exc.response.status_code == 401:
                    cache.delete(CACHE_TOKEN_KEY)
                try:
                    return exc.response.json()
                except Exception:
                    pass
            raise IndiaPostClientError(f"Failed to book articles with India Post: {exc}") from exc

    def generate_domestic_label(self, label_articles: list[dict[str, Any]]) -> bytes:
        """
        Generates address label PDF for domestic articles with automatic 401 retry.
        POST /v1/label/create/domestic
        Returns binary PDF content.
        """
        url = f"{self.base_url}/v1/label/create/domestic"

        try:
            resp = requests.post(url, json=label_articles, headers=self._get_auth_headers(), timeout=self.timeout)
            if resp.status_code == 401:
                logger.warning("India Post 401 received. Refreshing token and retrying label generation...")
                cache.delete(CACHE_TOKEN_KEY)
                resp = requests.post(url, json=label_articles, headers=self._get_auth_headers(force_refresh=True), timeout=self.timeout)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            logger.error(f"Address label generation failed: {exc}")
            raise IndiaPostClientError(f"Failed to generate address label: {exc}") from exc

    def track_bulk(self, barcodes: list[str]) -> dict[str, Any]:
        """
        Queries tracking details for up to 500 articles with automatic 401 retry.
        POST /v1/tracking/bulk
        """
        url = f"{self.base_url}/v1/tracking/bulk"
        payload = {"bulk": barcodes}

        try:
            resp = requests.post(url, json=payload, headers=self._get_auth_headers(), timeout=self.timeout)
            if resp.status_code == 401:
                logger.warning("India Post 401 received. Refreshing token and retrying tracking query...")
                cache.delete(CACHE_TOKEN_KEY)
                resp = requests.post(url, json=payload, headers=self._get_auth_headers(force_refresh=True), timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.error(f"Bulk tracking failed: {exc}")
            raise IndiaPostClientError(f"Failed to track articles: {exc}") from exc
