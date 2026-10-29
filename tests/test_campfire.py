from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import requests

from campfire import ACCOUNT_PATH, TAX_RATE_PATH, Campfire
from store import Store
from tax import TaxResult, compute_invoice_tax

CUSTOMER_ID = 42
ACCOUNTS = [
    {"id": 101, "name": "FakeTax State Tax Payable", "number": "236120"},
    {"id": 102, "name": "FakeTax County Tax Payable", "number": "236130"},
    {"id": 103, "name": "FakeTax Supplemental Tax Payable", "number": "236140"},
]
SALES_TAX_PAYABLE = {"id": 7, "name": "Sales Tax Payable", "number": "236110"}
CA_LINES = [
    {"id": 1, "description": "FakeTax CA 7.25% state", "rate": "0.060000", "tax_account": 101},
    {"id": 2, "description": "FakeTax CA 7.25% county", "rate": "0.002500", "tax_account": 102},
    {"id": 3, "description": "FakeTax CA 7.25% supplemental", "rate": "0.010000", "tax_account": 103},
]


class _Response:
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def _ca_result() -> TaxResult:
    return compute_invoice_tax(invoice={"shipping_address": "Oakland, CA 94607", "lines": [{"amount": "100"}]}, default_rate="")


def _campfire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: Callable[[str, str, Any], _Response]) -> tuple[Campfire, list[dict[str, Any]]]:
    store = Store(path=str(tmp_path / "s.sqlite3"))
    store.save_tokens(customer_id=CUSTOMER_ID, tokens={"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    campfire = Campfire(store=store, base_url="http://campfire.test", client_id="c", client_secret="s")
    calls: list[dict[str, Any]] = []

    def request(*, customer_id: int, method: str, path: str, json: Any = None, timeout: float = 15) -> _Response:
        calls.append({"method": method, "path": path, "json": json, "timeout": timeout})
        return reply(method, path.split("?")[0], json)

    monkeypatch.setattr(campfire, "request", request)
    return campfire, calls


def test_creates_the_accounts_and_the_three_line_rate_on_first_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created_accounts = iter([101, 102, 103])

    def reply(method: str, path: str, body: Any) -> _Response:
        if (method, path) == ("GET", TAX_RATE_PATH):
            return _Response(200, {"results": [{"id": 5, "code": "FAKETAX-NY-8.000", "lines": []}]})
        if (method, path) == ("GET", ACCOUNT_PATH):
            return _Response(200, [SALES_TAX_PAYABLE])
        if (method, path) == ("POST", ACCOUNT_PATH):
            return _Response(201, {"id": next(created_accounts), **body})
        if (method, path) == ("POST", TAX_RATE_PATH):
            return _Response(201, {"id": 9, **body})
        raise AssertionError((method, path))

    campfire, calls = _campfire(tmp_path, monkeypatch, reply)

    assert campfire.ensure_tax_rate(customer_id=CUSTOMER_ID, result=_ca_result(), timeout=2) == 9

    assert [(c["method"], c["path"].split("?")[0]) for c in calls] == [
        ("GET", TAX_RATE_PATH), ("GET", ACCOUNT_PATH),
        ("POST", ACCOUNT_PATH), ("POST", ACCOUNT_PATH), ("POST", ACCOUNT_PATH), ("POST", TAX_RATE_PATH),
    ]
    assert all(c["timeout"] == 2 for c in calls)
    assert [c["json"]["name"] for c in calls[2:5]] == [a["name"] for a in ACCOUNTS]
    assert [c["json"]["number"] for c in calls[2:5]] == ["236120", "236130", "236140"]
    assert calls[2]["json"]["account_type"] == "LIABILITY" and calls[2]["json"]["account_subtype"] == "OTHER_CURRENT_LIABILITY"
    rate = calls[5]["json"]
    assert rate["name"] == "FakeTax CA 7.25%" and rate["code"] == "FAKETAX-CA-7.250" and rate["type"] == "receivable"
    assert [(line["description"], line["rate"], line["tax_account"]) for line in rate["lines"]] == [
        ("FakeTax CA 7.25% state", "0.06", 101),
        ("FakeTax CA 7.25% county", "0.0025", 102),
        ("FakeTax CA 7.25% supplemental", "0.01", 103),
    ]
    assert sum(Decimal(line["rate"]) for line in rate["lines"]) == Decimal("0.0725")


def test_reuses_a_rate_whose_lines_already_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def reply(method: str, path: str, body: Any) -> _Response:
        assert (method, path) == ("GET", TAX_RATE_PATH)
        return _Response(200, {"results": [{"id": 9, "code": "FAKETAX-CA-7.250", "lines": CA_LINES}]})

    campfire, calls = _campfire(tmp_path, monkeypatch, reply)

    assert campfire.ensure_tax_rate(customer_id=CUSTOMER_ID, result=_ca_result()) == 9
    assert len(calls) == 1


def test_brings_a_drifted_rate_back_in_line_keeping_its_line_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    drifted = [{**CA_LINES[0], "rate": "0.050000"}, CA_LINES[1], CA_LINES[2]]

    def reply(method: str, path: str, body: Any) -> _Response:
        if (method, path) == ("GET", TAX_RATE_PATH):
            return _Response(200, {"results": [{"id": 9, "code": "FAKETAX-CA-7.250", "lines": drifted}]})
        if (method, path) == ("GET", ACCOUNT_PATH):
            return _Response(200, {"results": ACCOUNTS})
        if (method, path) == ("PATCH", f"{TAX_RATE_PATH}/9"):
            return _Response(200, {"id": 9, **body})
        raise AssertionError((method, path))

    campfire, calls = _campfire(tmp_path, monkeypatch, reply)

    assert campfire.ensure_tax_rate(customer_id=CUSTOMER_ID, result=_ca_result()) == 9

    assert [c["method"] for c in calls] == ["GET", "GET", "PATCH"]
    assert [(line["id"], line["rate"]) for line in calls[2]["json"]["lines"]] == [(1, "0.06"), (2, "0.0025"), (3, "0.01")]


def test_refuses_to_book_a_result_without_components(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    campfire, _calls = _campfire(tmp_path, monkeypatch, lambda *a: _Response(500, {}))
    untaxed = compute_invoice_tax(invoice={"shipping_address": "Portland, OR 97201", "lines": [{"amount": "100"}]}, default_rate="")

    with pytest.raises(ValueError):
        campfire.ensure_tax_rate(customer_id=CUSTOMER_ID, result=untaxed)
