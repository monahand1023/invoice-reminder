"""Real OAuth2 + HTTPS transport for QuickBooks Online. Stdlib only (``urllib``).

This is the credentialed edge. It is imported lazily by ``QuickBooksOnlineSource``
only when no client is injected, so the offline test suite never touches it and the
``anthropic``-style "import is free, use needs creds" rule holds. The adapter's
mapping/paging logic is tested separately with an injected fake.
"""
from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from decimal import Decimal

from reminders.config import QBOConfig

_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
_BASE_URL = {
    "production": "https://quickbooks.api.intuit.com",
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
}


class QBOHttpClient:
    """Refreshes an OAuth2 access token, then runs QBO query statements."""

    def __init__(self, config: QBOConfig):
        missing = [
            name for name in ("client_id", "client_secret", "refresh_token", "realm_id")
            if not getattr(config, name)
        ]
        if missing:
            raise ValueError(
                "QuickBooksOnlineSource needs Intuit OAuth2 credentials before it can "
                f"call the API; missing: {', '.join(missing)}. Set them in config/.env "
                "(register an Intuit app, complete the OAuth2 flow, store the refresh "
                "token and the company realm_id)."
            )
        self.config = config
        self._access_token: str | None = None

    def _refresh_access_token(self) -> str:
        creds = f"{self.config.client_id}:{self.config.client_secret}".encode()
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self.config.refresh_token,
        }).encode()
        req = urllib.request.Request(
            _TOKEN_URL, data=body, method="POST",
            headers={
                "Authorization": "Basic " + base64.b64encode(creds).decode(),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted host)
            return json.loads(resp.read().decode())["access_token"]

    def query(self, statement: str) -> dict:
        if self._access_token is None:
            self._access_token = self._refresh_access_token()
        base = _BASE_URL.get(self.config.environment, _BASE_URL["production"])
        url = (
            f"{base}/v3/company/{self.config.realm_id}/query"
            f"?query={urllib.parse.quote(statement)}"
            f"&minorversion={self.config.minor_version}"
        )
        req = urllib.request.Request(url, method="GET", headers={
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted host)
            # parse_float=Decimal keeps money exact end-to-end.
            return json.loads(resp.read().decode(), parse_float=Decimal)
