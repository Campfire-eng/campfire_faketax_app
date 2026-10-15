import importlib
import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, urlparse
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
    monkeypatch.setattr(app_module.campfire, "get_invoice", lambda **kw: _invoice(invoice_id=kw["invoice_id"], address="Boise, ID 83702"))
    client = app_module.app.test_client()
    payload = _slot_payload(object_ids=["11"])

    response = _signed_post(client, "/slots/calculate", payload)
    frame = client.post("/frame/api/breakdown", json={"context_token": payload["context_token"], "invoice_id": 11})

    assert response.get_json()["effects"][1]["url"].endswith("/frame/breakdown/11")
    assert frame.status_code == 200 and frame.get_json()["rate"] is None


def test_breakdown_frame_returns_json_when_workspace_is_not_connected(app_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    def _disconnected(**_kw: Any) -> None:
        raise app_module.NotConnected("workspace is not connected")

    monkeypatch.setattr(app_module.campfire, "get_invoice", _disconnected)
    client = app_module.app.test_client()
    payload = _slot_payload(object_ids=["11"])

    frame = client.post("/frame/api/breakdown", json={"context_token": payload["context_token"], "invoice_id": 11})

    assert frame.status_code == 409 and "not connected" in frame.get_json()["error"]


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


def test_settings_panel_reads_and_saves_install_config(app_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[dict[str, str]] = []
    monkeypatch.setattr(app_module.campfire, "put_install_config", lambda *, customer_id, config: saved.append(config) or config)
    client = app_module.app.test_client()
    token = _token(object_ids=[], surface="panel:list")

    loaded = client.post("/frame/api/settings", json={"context_token": token})
    assert loaded.status_code == 200 and loaded.get_json()["install_config"]["default_rate"] == "5.00"

    result = client.post("/frame/api/settings/save", json={"context_token": token, "default_rate": "7.25", "auto_calculate": False})
    assert result.status_code == 200 and result.get_json()["level"] == "success"
    assert saved[0]["default_rate"] == "7.25" and saved[0]["auto_calculate"] == "false"

    record_token = _token(object_ids=["12"], surface="panel:detail")
    assert client.post("/frame/api/settings", json={"context_token": record_token}).status_code == 403


def _authorize_browser(client: Any, customer_id: int = CUSTOMER_ID) -> None:
    with client.session_transaction() as browser:
        browser["workspaces"] = [customer_id]
        browser["user"] = {"customer_id": customer_id, "customer_name": "Acme", "user_id": 7, "name": "Pat", "email": "pat@acme.example"}
        browser["return_to"] = "https://app.meetcampfire.com/v2/settings/connections/apps/abc"


def test_onboarding_sets_nexus_creates_the_line_field_and_returns_to_campfire(app_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, Any]] = []
    monkeypatch.setattr(app_module.campfire, "create_custom_field", lambda **kw: created.append(kw) or {"id": 5, "name": kw["name"]})
    client = app_module.app.test_client()
    _authorize_browser(client)

    step1 = client.post(f"/setup/{CUSTOMER_ID}", data={"nexus": ["CA", "TX"], "default_tax_code": "FT-SVC-300"})
    step2 = client.post(f"/setup/{CUSTOMER_ID}/field")
    done = client.get(f"/setup/{CUSTOMER_ID}/done")

    assert step1.status_code == 302 and step1.headers["Location"].endswith(f"/setup/{CUSTOMER_ID}/field")
    assert step2.status_code == 302 and step2.headers["Location"].endswith(f"/setup/{CUSTOMER_ID}/done")
    assert [c["model"] for c in created] == ["accountinginvoiceline", "accountingcreditmemoline"]
    assert created[0]["options"][:2] == ["FT-GOODS-100", "FT-SOFT-200"] and len(created[0]["options"]) == 10
    workspace = app_module.store.workspace(customer_id=CUSTOMER_ID)
    assert workspace.nexus == ["CA", "TX"] and workspace.default_tax_code == "FT-SVC-300"
    assert workspace.tax_code_field == "tax_code" and workspace.onboarded_at is not None
    assert done.status_code == 200 and b"/v2/settings/connections/apps/abc" in done.data


def test_setup_refuses_a_browser_signed_in_to_another_workspace(app_module: ModuleType) -> None:
    client = app_module.app.test_client()
    _authorize_browser(client, customer_id=99)

    assert client.get(f"/setup/{CUSTOMER_ID}").status_code == 403


def test_manage_link_opens_the_workspace_once_and_saves_overrides(app_module: ModuleType) -> None:
    client = app_module.app.test_client()
    token = _token(object_ids=[], surface="manage")

    entry = client.get(f"/manage?cf_ctx={token}")
    page = client.get(f"/manage/{CUSTOMER_ID}")
    saved = client.post(
        f"/manage/{CUSTOMER_ID}",
        data={"nexus": ["NY"], "override_NY": "8.875", "default_tax_code": "FT-GOODS-100"},
    )
    replay = client.get(f"/manage?cf_ctx={token}")

    assert entry.status_code == 302 and entry.headers["Location"].endswith(f"/manage/{CUSTOMER_ID}")
    assert page.status_code == 200 and b"New York" in page.data
    assert saved.status_code == 200 and b"Saved." in saved.data
    workspace = app_module.store.workspace(customer_id=CUSTOMER_ID)
    assert workspace.nexus == ["NY"] and workspace.overrides == {"NY": "8.875"}
    assert replay.status_code == 401


def test_manage_refuses_a_button_token(app_module: ModuleType) -> None:
    response = app_module.app.test_client().get(f"/manage?cf_ctx={_token(object_ids=['11'])}")

    assert response.status_code == 403


def test_credit_memo_event_taxes_lines_by_the_clients_state(app_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    app_module.store.save_workspace(customer_id=CUSTOMER_ID, nexus=["CA"], onboarded=True)
    writes: list[dict[str, Any]] = []
    monkeypatch.setattr(app_module.campfire, "get_credit_memo", lambda **kw: {"id": kw["memo_id"], "client": 77, "lines": [{"id": 1, "amount": "40.00", "tax": "0", "account": 3, "tags": []}]})
    monkeypatch.setattr(app_module.campfire, "get_vendor", lambda **kw: {"id": 77, "city": "Oakland", "state": "CA", "zip_code": "94607"})
    monkeypatch.setattr(app_module.campfire, "write_credit_memo_tax", lambda **kw: writes.append(kw) or WriteOutcome(kind="written"))

    app_module._handle_event(event={"kind": "event", "topic": "CreditMemo.created", "id": 9, "customer": {"id": CUSTOMER_ID}})

    assert len(writes) == 1 and str(writes[0]["result"].lines[0].tax) == "2.90"


def test_home_opens_a_workspaces_manage_screen(app_module: ModuleType) -> None:
    client = app_module.app.test_client()
    _authorize_browser(client)

    home = client.get("/")
    opened = client.get(f"/open/{CUSTOMER_ID}/manage")
    page = client.get(f"/manage/{CUSTOMER_ID}")

    assert home.status_code == 200 and b"Manage jurisdictions" in home.data and b"Run onboarding" in home.data
    assert b"Signed in as <strong>Pat</strong>" in home.data and b"Acme" in home.data
    assert opened.status_code == 302 and opened.headers["Location"].endswith(f"/manage/{CUSTOMER_ID}")
    assert page.status_code == 200
    assert client.get("/open/999/manage").status_code == 404


def test_signed_out_browsers_land_on_the_login_page(app_module: ModuleType) -> None:
    client = app_module.app.test_client()

    home = client.get("/")
    login = client.get("/login")
    manage = client.get(f"/manage/{CUSTOMER_ID}")

    assert home.status_code == 302 and "/login" in home.headers["Location"]
    assert login.status_code == 200 and b"Login with Campfire" in login.data
    assert manage.status_code == 302 and "/login" in manage.headers["Location"]


def test_sign_out_clears_the_session(app_module: ModuleType) -> None:
    client = app_module.app.test_client()
    _authorize_browser(client)

    out = client.get("/logout")
    after = client.get("/")

    assert out.status_code == 302 and out.headers["Location"].endswith("/login")
    assert after.status_code == 302


def test_manage_link_signs_in_with_the_connections_person_when_it_matches(app_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module.campfire, "get_install_config", lambda **kw: {"customer_id": CUSTOMER_ID, "customer_name": "Acme", "user": {"id": 7, "name": "Pat", "email": "pat@acme.example"}, "install_config": {}})
    client = app_module.app.test_client()

    client.get(f"/manage?cf_ctx={_token(object_ids=[], surface='manage')}")
    page = client.get(f"/manage/{CUSTOMER_ID}")

    assert b"Signed in as <strong>Pat</strong>" in page.data and b"Acme" in page.data


def test_embedded_frames_have_no_navbar(app_module: ModuleType) -> None:
    client = app_module.app.test_client()

    breakdown = client.get("/frame/breakdown/11")
    settings = client.get("/frame/settings")
    login = client.get("/login")

    assert b"<nav>" not in breakdown.data and b"<nav>" not in settings.data and b"<nav>" not in login.data


def test_onboarding_refreshes_the_options_of_an_existing_field(app_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, Any]] = []

    class Response:
        status_code = 200

        def __init__(self, body: Any) -> None:
            self._body = body

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            return self._body

    def fake_request(*, customer_id: int, method: str, path: str, json: Any = None) -> Response:
        calls.append((method, path, json))
        if method == "GET":
            return Response({"results": [{"id": 5, "name": "tax_code", "options": ["TAXABLE", "SAAS"]}]})
        return Response({"id": 5, "name": "tax_code", "options": json["options"]})

    monkeypatch.setattr(app_module.campfire, "request", fake_request)

    field = app_module.campfire.create_custom_field(
        customer_id=CUSTOMER_ID, model="accountinginvoiceline", name="tax_code", label="Tax code", options=["FT-GOODS-100", "FT-SOFT-200"]
    )

    assert field["options"] == ["FT-GOODS-100", "FT-SOFT-200"]
    assert [(m, p) for m, p, _ in calls] == [("GET", "/ca/api/v1/custom-fields?app=chart_of_accounts&model=accountinginvoiceline"), ("PATCH", "/ca/api/v1/custom-fields/5")]


def test_sidebar_pages_read_and_save_jurisdictions_with_a_nav_token(app_module: ModuleType) -> None:
    client = app_module.app.test_client()
    token = _token(object_ids=[], surface="nav")

    page = client.get("/frame/manage")
    codes = client.get("/frame/codes")
    loaded = client.post("/frame/api/manage", json={"context_token": token})
    saved = client.post(
        "/frame/api/manage/save",
        json={"context_token": token, "nexus": ["CA", "XX"], "overrides": {"CA": "8.5", "NY": ""}, "default_tax_code": "FT-SVC-300"},
    )

    assert page.status_code == 200 and b"<nav>" not in page.data
    assert codes.status_code == 200 and b"FT-GOODS-100" in codes.data
    assert loaded.status_code == 200 and len(loaded.get_json()["jurisdictions"]) == 17
    assert saved.status_code == 200 and saved.get_json()["workspace"] == {
        "nexus": ["CA"], "overrides": {"CA": "8.5"}, "default_tax_code": "FT-SVC-300", "tax_code_field": None, "onboarded": False,
    }


def _connect_and_call_back(
    app_module: ModuleType, monkeypatch: pytest.MonkeyPatch, *, id_token_claims: dict[str, Any] | None
) -> tuple[Any, Any, dict[str, str]]:
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client = app_module.app.test_client()
    connect = client.get("/connect")
    authorize = parse_qs(urlparse(connect.headers["Location"]).query)
    tokens: dict[str, Any] = {"access_token": "a2", "refresh_token": "r2", "expires_in": 3600}
    if id_token_claims is not None:
        now = int(time.time())
        claims = {"iss": app_module.CAMPFIRE_BASE_URL, "aud": CLIENT_ID, "iat": now, "exp": now + 3600, **id_token_claims}
        tokens["id_token"] = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})

    class Exchanged:
        status_code = 200
        text = ""

        def json(self) -> dict[str, Any]:
            return tokens

    monkeypatch.setattr(app_module.campfire, "exchange_code", lambda **kw: Exchanged())
    monkeypatch.setattr(app_module.campfire, "id_token_signing_key", lambda **kw: key.public_key())
    monkeypatch.setattr(app_module.campfire, "get_install_config", lambda **kw: {"customer_id": CUSTOMER_ID, "customer_name": "Acme", "user": {"id": 7, "name": "Stale", "email": "old@acme.example"}, "install_config": {}})
    monkeypatch.setattr(app_module.campfire, "put_install_config", lambda **kw: {})
    callback = client.get(f"/callback?code=c&state={authorize['state'][0]}")
    return client, callback, {key: values[0] for key, values in authorize.items()}


def test_connect_asks_for_openid_with_a_nonce(app_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, authorize = _connect_and_call_back(app_module, monkeypatch, id_token_claims=None)

    assert "openid" in authorize["scope"].split() and authorize["nonce"]


def test_callback_signs_in_from_the_verified_id_token(app_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module.secrets, "token_urlsafe", lambda n=None: "fixed-nonce")
    client, callback, _ = _connect_and_call_back(
        app_module, monkeypatch, id_token_claims={"sub": "31017", "name": "Pat", "email": "pat@acme.example", "nonce": "fixed-nonce"}
    )

    assert callback.status_code == 302 and "error" not in callback.headers["Location"]
    with client.session_transaction() as browser:
        assert browser["user"]["sub"] == "31017"
        assert browser["user"]["name"] == "Pat" and browser["user"]["email"] == "pat@acme.example"


def test_callback_refuses_an_id_token_with_the_wrong_nonce(app_module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    client, callback, _ = _connect_and_call_back(
        app_module, monkeypatch, id_token_claims={"sub": "31017", "nonce": "someone-elses"}
    )

    assert callback.status_code == 400 and b"ID token rejected" in callback.data
    with client.session_transaction() as browser:
        assert "user" not in browser


def test_callback_shows_campfires_error_to_a_signed_out_person(app_module: ModuleType) -> None:
    client = app_module.app.test_client()

    page = client.get("/callback?error=invalid_scope&error_description=This+app+may+not+request%3A+openid&state=x")

    assert page.status_code == 400
    assert b"invalid_scope: This app may not request: openid" in page.data
