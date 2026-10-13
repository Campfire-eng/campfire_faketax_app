"""Talking to Campfire with a workspace's OAuth token, refreshing it when it expires or is refused."""

import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from store import Grant, Store
from tax import TaxResult, lines_payload

INVOICE_PATH: str = "/coa/api/v1/invoice/{id}/"
INSTALL_CONFIG_PATH: str = "/ca/api/v1/oauth-app-install/config"


class NotConnected(Exception):
    """No usable token for the workspace: never connected, revoked, or refresh refused."""


@dataclass(frozen=True)
class WriteOutcome:
    kind: str  # "written" | "draft" | "refused"
    detail: str = ""


class Campfire:
    def __init__(self, *, store: Store, base_url: str, client_id: str, client_secret: str) -> None:
        self.store: Store = store
        self.base_url: str = base_url.rstrip("/")
        self.client_id: str = client_id
        self.client_secret: str = client_secret
        self._refresh_lock: threading.Lock = threading.Lock()

    @property
    def token_url(self) -> str:
        return f"{self.base_url}/ca/oauth/token"

    @property
    def authorize_url(self) -> str:
        return f"{self.base_url}/ca/oauth/authorize"

    def exchange_code(self, *, code: str, redirect_uri: str, verifier: str) -> requests.Response:
        return requests.post(
            self.token_url,
            auth=(self.client_id, self.client_secret),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
            timeout=10,
        )

    def refresh(self, *, customer_id: int) -> None:
        """Rotate the refresh token. Serialized so two threads never spend the same refresh token."""
        with self._refresh_lock:
            grant: Grant | None = self.store.grant(customer_id=customer_id)
            if grant is None or not grant.refresh_token:
                raise NotConnected(f"workspace {customer_id} is not connected")
            r = requests.post(
                self.token_url,
                auth=(self.client_id, self.client_secret),
                data={"grant_type": "refresh_token", "refresh_token": grant.refresh_token},
                timeout=10,
            )
            if r.status_code != 200:
                self.store.delete_grant(customer_id=customer_id)
                raise NotConnected(f"refresh failed: {r.status_code} {r.text[:200]}")
            self.store.save_tokens(customer_id=customer_id, tokens=r.json())

    def request_with_token(self, *, access_token: str, method: str, path: str, json: Any = None) -> requests.Response:
        return requests.request(
            method,
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {access_token}"},
            json=json,
            timeout=15,
        )

    def _grant(self, *, customer_id: int) -> Grant:
        grant: Grant | None = self.store.grant(customer_id=customer_id)
        if grant is None:
            raise NotConnected(f"workspace {customer_id} is not connected")
        return grant

    def request(self, *, customer_id: int, method: str, path: str, json: Any = None) -> requests.Response:
        grant: Grant = self._grant(customer_id=customer_id)
        if grant.expires_at - 30 < time.time():
            self.refresh(customer_id=customer_id)
            grant = self._grant(customer_id=customer_id)
        r = self.request_with_token(access_token=grant.access_token, method=method, path=path, json=json)
        if r.status_code != 401:
            return r
        self.refresh(customer_id=customer_id)
        grant = self._grant(customer_id=customer_id)
        return self.request_with_token(access_token=grant.access_token, method=method, path=path, json=json)

    def get_invoice(self, *, customer_id: int, invoice_id: int) -> dict[str, Any] | None:
        r = self.request(customer_id=customer_id, method="GET", path=INVOICE_PATH.format(id=invoice_id))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        invoice: dict[str, Any] = r.json()
        self.store.cache_invoice(customer_id=customer_id, invoice=invoice)
        return invoice

    def write_tax(self, *, customer_id: int, invoice: dict[str, Any], result: TaxResult) -> WriteOutcome:
        """PATCH every line back with FakeTax's tax. A 202 means Campfire filed it for approval."""
        r = self.request(
            customer_id=customer_id,
            method="PATCH",
            path=INVOICE_PATH.format(id=invoice["id"]),
            json={"lines": lines_payload(invoice=invoice, result=result)},
        )
        if r.status_code == 202 or _is_draft_queue(r):
            return WriteOutcome(kind="draft")
        if 200 <= r.status_code < 300:
            self.store.cache_invoice(customer_id=customer_id, invoice=r.json())
            return WriteOutcome(kind="written")
        return WriteOutcome(kind="refused", detail=_error_text(r))

    def get_install_config(self, *, access_token: str) -> dict[str, Any]:
        r = self.request_with_token(access_token=access_token, method="GET", path=INSTALL_CONFIG_PATH)
        r.raise_for_status()
        body: dict[str, Any] = r.json()
        return body

    def put_install_config(self, *, customer_id: int, config: dict[str, str]) -> dict[str, str]:
        r = self.request(
            customer_id=customer_id, method="PUT", path=INSTALL_CONFIG_PATH, json={"install_config": config}
        )
        r.raise_for_status()
        saved: dict[str, str] = r.json()["install_config"]
        self.store.save_install_config(customer_id=customer_id, config=saved)
        return saved


def _is_draft_queue(r: requests.Response) -> bool:
    try:
        body: Any = r.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("status") == "pending" and "new_payload" in body


def _error_text(r: requests.Response) -> str:
    try:
        body: Any = r.json()
    except ValueError:
        return f"HTTP {r.status_code}"
    if isinstance(body, dict):
        for key in ("error", "detail", "lines", "non_field_errors"):
            if key in body:
                value: Any = body[key]
                return str(value[0] if isinstance(value, list) and value else value)[:120]
    return f"HTTP {r.status_code}"
