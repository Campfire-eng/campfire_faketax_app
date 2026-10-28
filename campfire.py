"""Talking to Campfire with a workspace's OAuth token, refreshing it when it expires or is refused."""

import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import jwt
import requests

from store import Grant, Store
from tax import TaxResult, credit_memo_lines_payload, lines_payload

INVOICE_PATH: str = "/coa/api/v1/invoice/{id}/"
CREDIT_MEMO_PATH: str = "/coa/api/v1/credit-memo/{id}"
VENDOR_PATH: str = "/coa/api/vendor/{id}"
CUSTOM_FIELDS_PATH: str = "/ca/api/v1/custom-fields"
TAX_RATE_PATH: str = "/coa/api/tax_rate"
ACCOUNT_PATH: str = "/coa/api/account"
# One liability account per component, shared by every jurisdiction, so the ledger shows the split.
TAX_ACCOUNTS: dict[str, str] = {
    "state": "FakeTax State Tax Payable",
    "county": "FakeTax County Tax Payable",
    "supplemental": "FakeTax Supplemental Tax Payable",
}
# Account numbers are required; FakeTax takes the first free ones after the usual sales tax payable.
FIRST_TAX_ACCOUNT_NUMBER: int = 236110
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
        # Caches Campfire's public keys and refetches when an ID token names a key it has not seen.
        self._jwks: jwt.PyJWKClient = jwt.PyJWKClient(f"{self.base_url}/.well-known/jwks.json")

    @property
    def token_url(self) -> str:
        return f"{self.base_url}/ca/oauth/token"

    @property
    def authorize_url(self) -> str:
        return f"{self.base_url}/ca/oauth/authorize"

    @property
    def issuer(self) -> str:
        return self.base_url

    def id_token_signing_key(self, *, id_token: str) -> Any:
        return self._jwks.get_signing_key_from_jwt(id_token).key

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

    def request_with_token(
        self, *, access_token: str, method: str, path: str, json: Any = None, timeout: float = 15
    ) -> requests.Response:
        return requests.request(
            method,
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {access_token}"},
            json=json,
            timeout=timeout,
        )

    def _grant(self, *, customer_id: int) -> Grant:
        grant: Grant | None = self.store.grant(customer_id=customer_id)
        if grant is None:
            raise NotConnected(f"workspace {customer_id} is not connected")
        return grant

    def request(
        self, *, customer_id: int, method: str, path: str, json: Any = None, timeout: float = 15
    ) -> requests.Response:
        grant: Grant = self._grant(customer_id=customer_id)
        if grant.expires_at - 30 < time.time():
            self.refresh(customer_id=customer_id)
            grant = self._grant(customer_id=customer_id)
        r = self.request_with_token(
            access_token=grant.access_token, method=method, path=path, json=json, timeout=timeout
        )
        if r.status_code != 401:
            return r
        self.refresh(customer_id=customer_id)
        grant = self._grant(customer_id=customer_id)
        return self.request_with_token(
            access_token=grant.access_token, method=method, path=path, json=json, timeout=timeout
        )

    def get_invoice(self, *, customer_id: int, invoice_id: int) -> dict[str, Any] | None:
        r = self.request(customer_id=customer_id, method="GET", path=INVOICE_PATH.format(id=invoice_id))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        invoice: dict[str, Any] = r.json()
        self.store.cache_invoice(customer_id=customer_id, invoice=invoice)
        return invoice

    def write_tax(
        self, *, customer_id: int, invoice: dict[str, Any], result: TaxResult, tax_code_field: str | None = None
    ) -> WriteOutcome:
        """PATCH every line back with FakeTax's tax and codes. A 202 means Campfire filed it for approval."""
        r = self.request(
            customer_id=customer_id,
            method="PATCH",
            path=INVOICE_PATH.format(id=invoice["id"]),
            json={"lines": lines_payload(invoice=invoice, result=result, tax_code_field=tax_code_field)},
        )
        if r.status_code == 202 or _is_draft_queue(r):
            return WriteOutcome(kind="draft")
        if 200 <= r.status_code < 300:
            self.store.cache_invoice(customer_id=customer_id, invoice=r.json())
            return WriteOutcome(kind="written")
        return WriteOutcome(kind="refused", detail=_error_text(r))

    def get_credit_memo(self, *, customer_id: int, memo_id: int) -> dict[str, Any] | None:
        r = self.request(customer_id=customer_id, method="GET", path=CREDIT_MEMO_PATH.format(id=memo_id))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        memo: dict[str, Any] = r.json()
        return memo

    def write_credit_memo_tax(
        self, *, customer_id: int, memo: dict[str, Any], result: TaxResult, tax_code_field: str | None = None
    ) -> WriteOutcome:
        r = self.request(
            customer_id=customer_id,
            method="PATCH",
            path=CREDIT_MEMO_PATH.format(id=memo["id"]),
            json={"lines": credit_memo_lines_payload(memo=memo, result=result, tax_code_field=tax_code_field)},
        )
        if r.status_code == 202 or _is_draft_queue(r):
            return WriteOutcome(kind="draft")
        if 200 <= r.status_code < 300:
            return WriteOutcome(kind="written")
        return WriteOutcome(kind="refused", detail=_error_text(r))

    def get_vendor(self, *, customer_id: int, vendor_id: int) -> dict[str, Any] | None:
        """Credit memos name a client, which Campfire stores as a vendor record with an address."""
        r = self.request(customer_id=customer_id, method="GET", path=VENDOR_PATH.format(id=vendor_id))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        vendor: dict[str, Any] = r.json()
        return vendor

    def list_custom_fields(self, *, customer_id: int, model: str) -> list[dict[str, Any]]:
        r = self.request(
            customer_id=customer_id, method="GET", path=f"{CUSTOM_FIELDS_PATH}?app=chart_of_accounts&model={model}"
        )
        r.raise_for_status()
        body: Any = r.json()
        rows: Any = body.get("results", body) if isinstance(body, dict) else body
        return list(rows or [])

    def create_custom_field(
        self, *, customer_id: int, model: str, name: str, label: str, options: list[str]
    ) -> dict[str, Any]:
        """A single-select field on ``model`` lines, or the existing field of that name with its options brought up to date."""
        for field in self.list_custom_fields(customer_id=customer_id, model=model):
            if field.get("name") == name:
                if list(field.get("options") or []) == options:
                    return field
                r = self.request(
                    customer_id=customer_id,
                    method="PATCH",
                    path=f"{CUSTOM_FIELDS_PATH}/{field['id']}",
                    json={"options": options},
                )
                r.raise_for_status()
                updated: dict[str, Any] = r.json()
                return updated
        r = self.request(
            customer_id=customer_id,
            method="POST",
            path=CUSTOM_FIELDS_PATH,
            json={
                "app": "chart_of_accounts",
                "model": model,
                "name": name,
                "label": label,
                "field_type": "single_select",
                "options": options,
                "is_required": False,
                "is_active": True,
            },
        )
        r.raise_for_status()
        created: dict[str, Any] = r.json()
        return created

    def ensure_tax_accounts(self, *, customer_id: int, timeout: float = 15) -> dict[str, int]:
        """The three FakeTax liability accounts by component, created on first use."""
        r = self.request(
            customer_id=customer_id, method="GET", path=f"{ACCOUNT_PATH}?account_type=LIABILITY", timeout=timeout
        )
        r.raise_for_status()
        liabilities: list[dict[str, Any]] = _results(r.json())
        by_name: dict[str, int] = {str(row.get("name")): int(row["id"]) for row in liabilities}
        taken: set[str] = {str(row.get("number")) for row in liabilities}
        number: int = FIRST_TAX_ACCOUNT_NUMBER
        accounts: dict[str, int] = {}
        for component, name in TAX_ACCOUNTS.items():
            if name not in by_name:
                while str(number) in taken:
                    number += 10
                taken.add(str(number))
                created = self.request(
                    customer_id=customer_id,
                    method="POST",
                    path=ACCOUNT_PATH,
                    json={
                        "name": name,
                        "number": str(number),
                        "account_type": "LIABILITY",
                        "account_subtype": "OTHER_CURRENT_LIABILITY",
                        "description": f"Sales tax collected by FakeTax, {component} share.",
                    },
                    timeout=timeout,
                )
                created.raise_for_status()
                by_name[name] = int(created.json()["id"])
            accounts[component] = by_name[name]
        return accounts

    def ensure_tax_rate(self, *, customer_id: int, result: TaxResult, timeout: float = 15) -> int:
        """
        The Campfire tax rate for ``result``'s jurisdiction and percentage, with one line per
        component on its own account, created or brought up to date; the id goes on the lines.
        """
        if result.components is None or result.rate is None:
            raise ValueError("nothing to book")
        wanted: list[tuple[str, str, Decimal]] = [
            (component, f"{result.description} {component}", getattr(result.components, component))
            for component in TAX_ACCOUNTS
        ]
        listed = self.request(
            customer_id=customer_id, method="GET", path=f"{TAX_RATE_PATH}?q=FakeTax&limit=500", timeout=timeout
        )
        listed.raise_for_status()
        existing: dict[str, Any] | None = next(
            (row for row in _results(listed.json()) if row.get("code") == result.rate_code), None
        )
        current_lines: list[dict[str, Any]] = list((existing or {}).get("lines") or [])
        if existing is not None and _lines_match(current_lines=current_lines, wanted=wanted):
            return int(existing["id"])
        accounts: dict[str, int] = self.ensure_tax_accounts(customer_id=customer_id, timeout=timeout)
        lines: list[dict[str, Any]] = [
            {"description": description, "rate": str(rate / Decimal("100")), "tax_account": accounts[component]}
            for component, description, rate in wanted
        ]
        for line, current in zip(lines, current_lines, strict=False):
            line["id"] = current.get("id")
        body: dict[str, Any] = {
            "name": result.description,
            "code": result.rate_code,
            "type": "receivable",
            "description": f"FakeTax {result.state or 'default'} rate: state, county and supplemental shares.",
            "lines": lines,
        }
        if existing is not None:
            r = self.request(
                customer_id=customer_id,
                method="PATCH",
                path=f"{TAX_RATE_PATH}/{existing['id']}",
                json=body,
                timeout=timeout,
            )
        else:
            r = self.request(customer_id=customer_id, method="POST", path=TAX_RATE_PATH, json=body, timeout=timeout)
        r.raise_for_status()
        return int(r.json()["id"])

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


def _results(body: Any) -> list[dict[str, Any]]:
    rows: Any = body.get("results", body) if isinstance(body, dict) else body
    return list(rows or [])


def _lines_match(*, current_lines: list[dict[str, Any]], wanted: list[tuple[str, str, Decimal]]) -> bool:
    if len(current_lines) != len(wanted):
        return False
    return all(
        line.get("description") == description
        and line.get("tax_account") is not None
        and Decimal(str(line.get("rate"))) == rate / Decimal("100")
        for line, (_component, description, rate) in zip(current_lines, wanted, strict=True)
    )


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
