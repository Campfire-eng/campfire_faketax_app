import importlib
import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import jwt
import pytest

from campfire import WriteOutcome
from security import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign

SECRET = "whsec_" + "s" * 40
CLIENT_ID = "faketax-client"
CUSTOMER_ID = 42


@pytest.fixture
def app_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    monkeypatch.setenv("CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("CLIENT_SECRET", "secret")
    monkeypatch.setenv("SIGNING_SECRET", SECRET)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.sqlite3"))
    import app as module

    module = importlib.reload(module)
    module.store.save_tokens(customer_id=CUSTOMER_ID, tokens={"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    module.store.save_install_config(customer_id=CUSTOMER_ID, config={"default_rate": "5.00", "auto_calculate": "true"})
    yield module


def _invoice(*, invoice_id: int, address: str, tax: str = "0") -> dict[str, Any]:
    return {
        "id": invoice_id,
        "invoice_number": f"INV-{invoice_id}",
        "payment_status": "draft",
        "currency": "USD",
        "shipping_address": address,
        "lines": [{"id": 1, "product": 3, "amount": "100.00", "discount": "0", "tax": tax, "tags": []}],
    }


def _token(*, object_ids: list[str], surface: str = "detail_header") -> str:
    now = int(time.time())
    claims = {
        "iss": "campfire", "aud": CLIENT_ID, "jti": str(uuid.uuid4()), "iat": now, "exp": now + 300,
        "customer_id": CUSTOMER_ID, "user_id": 7, "slot_key": "calculate", "object_type": "invoice",
        "object_ids": object_ids, "surface": surface,
    }
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _signed_post(client: Any, path: str, payload: dict[str, Any]) -> Any:
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    headers = {TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: sign(secret=SECRET, timestamp=ts, body=body), "Content-Type": "application/json"}
    return client.post(path, data=body, headers=headers)


def _slot_payload(*, object_ids: list[str], surface: str = "detail_header") -> dict[str, Any]:
    return {
        "kind": "slot.invoked", "slot_key": "calculate", "object_type": "invoice", "object_ids": object_ids,
        "surface": surface, "user": {"id": 7}, "customer": {"id": CUSTOMER_ID},
        "context_token": _token(object_ids=object_ids, surface=surface),
    }


def test_calculate_writes_tax_and_replies_with_toast_and_refresh(app_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[dict[str, Any]] = []
    monkeypatch.setattr(app_module.campfire, "get_invoice", lambda **kw: _invoice(invoice_id=kw["invoice_id"], address="Oakland, CA 94607"))
    monkeypatch.setattr(app_module.campfire, "write_tax", lambda **kw: writes.append(kw) or WriteOutcome(kind="written"))

    response = _signed_post(app_module.app.test_client(), "/slots/calculate", _slot_payload(object_ids=["11"]))

    effects = response.get_json()["effects"]
    assert response.status_code == 200
    assert effects[0]["level"] == "success" and "$7.25" in effects[0]["message"]
    assert effects[1] == {"type": "refresh"}
    assert writes[0]["result"].description == "FakeTax CA 7.25%"


def test_bulk_counts_refusals_and_drafts(app_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = {11: WriteOutcome(kind="written"), 12: WriteOutcome(kind="refused", detail="Period is closed"), 13: WriteOutcome(kind="draft")}
    monkeypatch.setattr(app_module.campfire, "get_invoice", lambda **kw: _invoice(invoice_id=kw["invoice_id"], address="Austin, TX 78701"))
    monkeypatch.setattr(app_module.campfire, "write_tax", lambda **kw: outcomes[kw["invoice"]["id"]])

    response = _signed_post(app_module.app.test_client(), "/slots/calculate", _slot_payload(object_ids=["11", "12", "13"], surface="list_header"))

    message = response.get_json()["effects"][0]["message"]
    assert "1 invoice" in message and "Submitted for approval (1)" in message and "1 refused by Campfire: Period is closed" in message


def test_unknown_rate_opens_breakdown_frame_whose_token_still_works(app_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    app_module.store.save_install_config(customer_id=CUSTOMER_ID, config={"default_rate": ""})
    monkeypatch.setattr(app_module.campfire, "get_invoice", lambda **kw: _invoice(invoice_id=kw["invoice_id"], address="Portland, OR 97201"))
    client = app_module.app.test_client()
    payload = _slot_payload(object_ids=["11"])

    response = _signed_post(client, "/slots/calculate", payload)
    frame = client.post("/frame/api/breakdown", json={"context_token": payload["context_token"], "invoice_id": 11})

    assert response.get_json()["effects"][1]["url"].endswith("/frame/breakdown/11")
    assert frame.status_code == 200 and frame.get_json()["rate"] is None


def test_rejects_unsigned_slot_call(app_module: ModuleType) -> None:
    response = app_module.app.test_client().post("/slots/calculate", json=_slot_payload(object_ids=["11"]))

    assert response.status_code == 401


def test_simulated_500(app_module: ModuleType) -> None:
    app_module.store.set_simulate_failure(customer_id=CUSTOMER_ID, value="500")

    response = _signed_post(app_module.app.test_client(), "/slots/calculate", _slot_payload(object_ids=["11"]))

    assert response.status_code == 500


def test_report_link_is_single_use(app_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module.campfire, "get_invoice", lambda **kw: _invoice(invoice_id=kw["invoice_id"], address="Albany, NY 12207"))
    client = app_module.app.test_client()
    token = _token(object_ids=["11"])

    first = client.get(f"/report/11?cf_ctx={token}")
    second = client.get(f"/report/11?cf_ctx={token}")

    assert first.status_code == 200 and b"8.00" in first.data
    assert second.status_code == 401


def test_report_refuses_other_invoice(app_module: ModuleType) -> None:
    response = app_module.app.test_client().get(f"/report/99?cf_ctx={_token(object_ids=['11'])}")

    assert response.status_code == 403


def test_event_skips_when_tax_already_applied(app_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[Any] = []
    monkeypatch.setattr(app_module.campfire, "get_invoice", lambda **kw: _invoice(invoice_id=kw["invoice_id"], address="Oakland, CA", tax="7.25"))
    monkeypatch.setattr(app_module.campfire, "write_tax", lambda **kw: writes.append(kw))

    app_module._handle_event(event={"kind": "event", "topic": "Invoice.updated", "id": 11, "changed_fields": ["lines"], "customer": {"id": CUSTOMER_ID}})

    assert writes == []
