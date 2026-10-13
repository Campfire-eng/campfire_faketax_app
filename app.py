"""
FakeTax: a demo Campfire app that puts made-up sales tax on invoices, using every app-extension
capability (link, webhook and iframe slots, event subscriptions, install config).
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from flask.typing import ResponseReturnValue

from campfire import Campfire, NotConnected, WriteOutcome
from security import ContextTokenError, decode_context_token, token_covers, verify_context_token, verify_signature
from store import SIMULATE_OPTIONS, Store
from tax import TaxResult, compute_invoice_tax, is_self_echo, parse_rate

load_dotenv()

CAMPFIRE_BASE_URL: str = os.environ.get("CAMPFIRE_BASE_URL", "http://localhost:8001").rstrip("/")
CAMPFIRE_FRONTEND_ORIGIN: str = os.environ.get("CAMPFIRE_FRONTEND_ORIGIN", "http://localhost:3000").rstrip("/")
CLIENT_ID: str = os.environ["CLIENT_ID"]
CLIENT_SECRET: str = os.environ["CLIENT_SECRET"]
SIGNING_SECRETS: list[str] = [s for s in (os.environ.get("SIGNING_SECRET"), os.environ.get("SIGNING_SECRET_PREVIOUS")) if s]
PORT: int = int(os.environ.get("PORT", "3999"))
PUBLIC_BASE_URL: str = os.environ.get("PUBLIC_BASE_URL", f"http://localhost:{PORT}").rstrip("/")
REDIRECT_URI: str = f"{PUBLIC_BASE_URL}/callback"
DB_PATH: str = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "faketax.sqlite3"))

DEFAULT_INSTALL_CONFIG: dict[str, str] = {"default_rate": "5.00", "auto_calculate": "false", "base_url": PUBLIC_BASE_URL}
MAX_BULK: int = 50
SLOW_SECONDS: int = 12
TAXABLE_STATUSES: frozenset[str] = frozenset({"draft", "open"})

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log: logging.Logger = logging.getLogger("faketax")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))
store: Store = Store(path=DB_PATH)
campfire: Campfire = Campfire(store=store, base_url=CAMPFIRE_BASE_URL, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)


@dataclass(frozen=True)
class InvoiceOutcome:
    invoice_id: int
    kind: str  # written | draft | refused | needs_rate | missing | unchanged | skipped
    detail: str = ""
    tax: Decimal = Decimal("0")


def _pkce_pair() -> tuple[str, str]:
    verifier: str = secrets.token_urlsafe(48)
    challenge: str = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _default_rate(*, customer_id: int) -> str:
    grant = store.grant(customer_id=customer_id)
    return (grant.install_config if grant else DEFAULT_INSTALL_CONFIG).get("default_rate", "")


def calculate_invoice(
    *, customer_id: int, invoice_id: int, rate_override: Decimal | None = None, only_if_changed: bool = False
) -> InvoiceOutcome:
    """Fetch, compute, write. Refusals are counted, never retried: Campfire's answer is final."""
    invoice: dict[str, Any] | None = campfire.get_invoice(customer_id=customer_id, invoice_id=invoice_id)
    if invoice is None:
        return InvoiceOutcome(invoice_id=invoice_id, kind="missing")
    if only_if_changed and invoice.get("payment_status") not in TAXABLE_STATUSES:
        return InvoiceOutcome(invoice_id=invoice_id, kind="skipped", detail="not draft or open")
    result: TaxResult = compute_invoice_tax(
        invoice=invoice, default_rate=_default_rate(customer_id=customer_id), rate_override=rate_override
    )
    if result.needs_rate:
        return InvoiceOutcome(invoice_id=invoice_id, kind="needs_rate")
    if not result.lines:
        return InvoiceOutcome(invoice_id=invoice_id, kind="skipped", detail="no lines")
    if only_if_changed and result.already_applied:
        return InvoiceOutcome(invoice_id=invoice_id, kind="unchanged")
    outcome: WriteOutcome = campfire.write_tax(customer_id=customer_id, invoice=invoice, result=result)
    return InvoiceOutcome(invoice_id=invoice_id, kind=outcome.kind, detail=outcome.detail, tax=result.total_tax)


def summarize(*, outcomes: list[InvoiceOutcome]) -> tuple[str, str]:
    """One toast (level, message) for a run over one or many invoices."""
    by_kind: dict[str, list[InvoiceOutcome]] = {}
    for outcome in outcomes:
        by_kind.setdefault(outcome.kind, []).append(outcome)
    written: list[InvoiceOutcome] = by_kind.get("written", [])
    parts: list[str] = []
    if written:
        total: Decimal = sum((o.tax for o in written), Decimal("0"))
        noun: str = "invoice" if len(written) == 1 else "invoices"
        parts.append(f"Tax ${total:,.2f} written on {len(written)} {noun}")
    if by_kind.get("draft"):
        parts.append(f"Submitted for approval ({len(by_kind['draft'])})")
    refused: list[InvoiceOutcome] = by_kind.get("refused", [])
    if refused:
        parts.append(f"{len(refused)} refused by Campfire: {refused[0].detail}")
    for kind, label in (("needs_rate", "no rate for the address"), ("missing", "not found"), ("skipped", "skipped")):
        if by_kind.get(kind):
            parts.append(f"{len(by_kind[kind])} {label}")
    message: str = "FakeTax: " + ("; ".join(parts) if parts else "nothing to do")
    level: str = "success" if written and not refused else "warning" if (written or by_kind.get("draft")) else "info"
    if refused and not written:
        level = "error"
    return level, message[:200]


def _verified_body() -> dict[str, Any]:
    raw: bytes = request.get_data(cache=True)
    if not verify_signature(secrets=SIGNING_SECRETS, headers=dict(request.headers), body=raw, now=time.time()):
        abort(401, "bad signature")
    try:
        body: Any = json.loads(raw)
    except ValueError:
        abort(400, "not JSON")
    if not isinstance(body, dict):
        abort(400, "expected an object")
    return body


def _claim_frame_token(*, token: str, invoice_id: str | None) -> dict[str, Any]:
    """
    A frame makes several calls with the one context token Campfire gave it. The first call
    claims the jti and opens a short session bound to it; later calls are let through only for
    that open session, until the token expires. A list-page panel has no record, so it passes
    ``invoice_id=None`` and the token must carry no record either.
    """
    try:
        claims: dict[str, Any] = decode_context_token(token=token, secrets=SIGNING_SECRETS, client_id=CLIENT_ID)
    except ContextTokenError as e:
        abort(401, str(e))
    customer_id: int = int(claims["customer_id"])
    jti: str = str(claims["jti"])
    if invoice_id is None:
        if claims.get("object_ids"):
            abort(403, "token is for a record, not the list page")
    elif not token_covers(claims=claims, object_type="invoice", object_id=invoice_id):
        abort(403, "token is not for this invoice")
    if store.frame_session_open(jti=jti, customer_id=customer_id):
        return claims
    if not store.claim_jti(jti, float(claims["exp"])):
        abort(401, "context token already used")
    store.open_frame_session(jti=jti, customer_id=customer_id, exp=float(claims["exp"]))
    return claims


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
def _json_error(error: Any) -> ResponseReturnValue:
    return jsonify({"error": getattr(error, "description", str(error))}), getattr(error, "code", 400)


@app.get("/")
def home() -> ResponseReturnValue:
    workspaces: list[dict[str, Any]] = [
        {
            "grant": grant,
            "simulate_failure": store.simulate_failure(customer_id=grant.customer_id),
            "cached": store.cached_invoice_count(customer_id=grant.customer_id),
            "expires_in": int(grant.expires_at - time.time()),
        }
        for grant in store.grants()
    ]
    return render_template(
        "home.html",
        workspaces=workspaces,
        simulate_options=SIMULATE_OPTIONS,
        error=request.args.get("error"),
        notice=request.args.get("notice"),
        signing_configured=bool(SIGNING_SECRETS),
    )


@app.get("/connect")
def connect() -> ResponseReturnValue:
    verifier, challenge = _pkce_pair()
    state: str = secrets.token_urlsafe(24)
    session["oauth"] = {"state": state, "verifier": verifier}
    query: dict[str, str] = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return redirect(f"{campfire.authorize_url}?{urlencode(query)}")


@app.get("/callback")
def callback() -> ResponseReturnValue:
    pending: dict[str, str] | None = session.pop("oauth", None)
    if request.args.get("error"):
        return redirect(url_for("home", error=f"Campfire returned {request.args['error']}"))
    if not pending or request.args.get("state") != pending["state"]:
        return redirect(url_for("home", error="state mismatch: refusing the code"))
    r = campfire.exchange_code(code=request.args.get("code", ""), redirect_uri=REDIRECT_URI, verifier=pending["verifier"])
    if r.status_code != 200:
        return redirect(url_for("home", error=f"token exchange failed: {r.status_code} {r.text}"))
    tokens: dict[str, Any] = r.json()
    # The token response does not name the workspace; the install config endpoint does.
    current: dict[str, Any] = campfire.get_install_config(access_token=tokens["access_token"])
    customer_id: int = int(current["customer_id"])
    store.save_tokens(customer_id=customer_id, tokens=tokens)
    config: dict[str, str] = {**DEFAULT_INSTALL_CONFIG, **(current.get("install_config") or {}), "base_url": PUBLIC_BASE_URL}
    campfire.put_install_config(customer_id=customer_id, config=config)
    return redirect(url_for("home", notice=f"Connected workspace {customer_id}"))


@app.get("/refresh/<int:customer_id>")
def refresh(customer_id: int) -> ResponseReturnValue:
    try:
        campfire.refresh(customer_id=customer_id)
    except NotConnected as e:
        return redirect(url_for("home", error=str(e)))
    return redirect(url_for("home", notice=f"Refreshed token for workspace {customer_id}"))


@app.get("/disconnect/<int:customer_id>")
def disconnect(customer_id: int) -> ResponseReturnValue:
    store.delete_grant(customer_id=customer_id)
    return redirect(url_for("home", notice=f"Forgot tokens for workspace {customer_id}"))


@app.post("/workspaces/<int:customer_id>/settings")
def save_settings(customer_id: int) -> ResponseReturnValue:
    default_rate: str = request.form.get("default_rate", "").strip()
    if default_rate and parse_rate(default_rate) is None:
        return redirect(url_for("home", error="Default rate must be a number between 0 and 100, or blank"))
    store.set_simulate_failure(customer_id=customer_id, value=request.form.get("simulate_failure", "none"))
    grant = store.grant(customer_id=customer_id)
    config: dict[str, str] = {
        **(grant.install_config if grant else {}),
        "default_rate": default_rate,
        "auto_calculate": "true" if request.form.get("auto_calculate") else "false",
        "base_url": PUBLIC_BASE_URL,
    }
    try:
        campfire.put_install_config(customer_id=customer_id, config=config)
    except (NotConnected, requests.HTTPError) as e:
        return redirect(url_for("home", error=f"Could not save to Campfire: {e}"))
    return redirect(url_for("home", notice=f"Saved settings for workspace {customer_id}"))


@app.post("/slots/calculate")
def slot_calculate() -> ResponseReturnValue:
    body: dict[str, Any] = _verified_body()
    try:
        claims: dict[str, Any] = verify_context_token(
            token=str(body.get("context_token", "")),
            secrets=SIGNING_SECRETS,
            client_id=CLIENT_ID,
            claim_jti=store.claim_jti,
        )
    except ContextTokenError as e:
        abort(401, str(e))
    customer_id: int = int(body["customer"]["id"])
    if int(claims["customer_id"]) != customer_id or body.get("object_type") != "invoice":
        abort(400, "context token does not match the request")
    simulate: str = store.simulate_failure(customer_id=customer_id)
    if simulate == "500":
        return jsonify({"error": "simulated failure"}), 500
    if simulate == "slow":
        time.sleep(SLOW_SECONDS)
    ids: list[str] = [str(i) for i in claims.get("object_ids", [])][:MAX_BULK]
    try:
        outcomes: list[InvoiceOutcome] = [
            calculate_invoice(customer_id=customer_id, invoice_id=int(invoice_id)) for invoice_id in ids
        ]
    except NotConnected:
        return jsonify({"effects": [{"type": "toast", "level": "error", "message": "FakeTax is not connected to this workspace. Reconnect it in FakeTax."}]})
    if len(outcomes) == 1 and outcomes[0].kind == "needs_rate":
        store.open_frame_session(jti=str(claims["jti"]), customer_id=customer_id, exp=float(claims["exp"]))
        return jsonify(
            {
                "effects": [
                    {"type": "toast", "level": "info", "message": "FakeTax has no rate for this address. Pick one."},
                    {"type": "open_iframe", "url": f"{PUBLIC_BASE_URL}/frame/breakdown/{ids[0]}", "presentation": "drawer"},
                ]
            }
        )
    level, message = summarize(outcomes=outcomes)
    log.info("slot %s on %s: %s", body.get("slot_key"), ids, message)
    return jsonify({"effects": [{"type": "toast", "level": level, "message": message}, {"type": "refresh"}]})


@app.get("/report/<int:invoice_id>")
def report(invoice_id: int) -> ResponseReturnValue:
    try:
        claims: dict[str, Any] = verify_context_token(
            token=request.args.get("cf_ctx", ""), secrets=SIGNING_SECRETS, client_id=CLIENT_ID, claim_jti=store.claim_jti
        )
    except ContextTokenError as e:
        return render_template("message.html", title="Link expired", message=f"Open this report from Campfire again ({e})."), 401
    if not token_covers(claims=claims, object_type="invoice", object_id=str(invoice_id)):
        return render_template("message.html", title="Wrong invoice", message="This link was issued for another record."), 403
    customer_id: int = int(claims["customer_id"])
    try:
        invoice: dict[str, Any] | None = campfire.get_invoice(customer_id=customer_id, invoice_id=invoice_id)
    except NotConnected:
        return render_template("message.html", title="Not connected", message="Connect FakeTax to this workspace first."), 403
    if invoice is None:
        return render_template("message.html", title="Not found", message="Campfire has no such invoice."), 404
    result: TaxResult = compute_invoice_tax(invoice=invoice, default_rate=_default_rate(customer_id=customer_id))
    return render_template("report.html", invoice=invoice, result=result, claims=claims)


@app.get("/frame/breakdown/<int:invoice_id>")
def frame_breakdown(invoice_id: int) -> ResponseReturnValue:
    return render_template("breakdown.html", invoice_id=invoice_id, host_origin=CAMPFIRE_FRONTEND_ORIGIN)


def _frame_request() -> tuple[dict[str, Any], int, int]:
    body: Any = request.get_json(silent=True) or {}
    invoice_id: str = str(body.get("invoice_id", ""))
    if not invoice_id.isdigit():
        abort(400, "invoice_id required")
    claims: dict[str, Any] = _claim_frame_token(token=str(body.get("context_token", "")), invoice_id=invoice_id)
    return body, int(claims["customer_id"]), int(invoice_id)


@app.post("/frame/api/breakdown")
def frame_api_breakdown() -> ResponseReturnValue:
    _body, customer_id, invoice_id = _frame_request()
    invoice: dict[str, Any] | None = campfire.get_invoice(customer_id=customer_id, invoice_id=invoice_id)
    if invoice is None:
        return jsonify({"error": "not found"}), 404
    result: TaxResult = compute_invoice_tax(invoice=invoice, default_rate=_default_rate(customer_id=customer_id))
    return jsonify(
        {
            "invoice_number": invoice.get("invoice_number"),
            "payment_status": invoice.get("payment_status"),
            "currency": invoice.get("currency"),
            "shipping_address": invoice.get("shipping_address") or "",
            "state": result.state,
            "rate": str(result.rate) if result.rate is not None else None,
            "lines": [
                {"description": line.description, "base": str(line.base), "current_tax": str(line.current_tax)}
                for line in result.lines
            ],
        }
    )


@app.post("/frame/api/apply")
def frame_api_apply() -> ResponseReturnValue:
    body, customer_id, invoice_id = _frame_request()
    rate: Decimal | None = parse_rate(str(body.get("rate", "")))
    if rate is None:
        return jsonify({"level": "error", "message": "Enter a rate between 0 and 100."}), 400
    outcome: InvoiceOutcome = calculate_invoice(customer_id=customer_id, invoice_id=invoice_id, rate_override=rate)
    level, message = summarize(outcomes=[outcome])
    return jsonify({"ok": outcome.kind in ("written", "draft"), "level": level, "message": message})


@app.get("/frame/settings")
def frame_settings() -> ResponseReturnValue:
    """The list-page panel: this workspace's FakeTax settings, edited inside Campfire."""
    return render_template("settings.html", host_origin=CAMPFIRE_FRONTEND_ORIGIN)


def _panel_request() -> tuple[dict[str, Any], int]:
    body: Any = request.get_json(silent=True) or {}
    claims: dict[str, Any] = _claim_frame_token(token=str(body.get("context_token", "")), invoice_id=None)
    return body, int(claims["customer_id"])


@app.post("/frame/api/settings")
def frame_api_settings() -> ResponseReturnValue:
    _body, customer_id = _panel_request()
    grant = store.grant(customer_id=customer_id)
    if grant is None:
        return jsonify({"error": "not connected"}), 403
    return jsonify({"customer_id": customer_id, "install_config": grant.install_config})


@app.post("/frame/api/settings/save")
def frame_api_settings_save() -> ResponseReturnValue:
    body, customer_id = _panel_request()
    default_rate: str = str(body.get("default_rate", "")).strip()
    if default_rate and parse_rate(default_rate) is None:
        return jsonify({"level": "error", "message": "Enter a rate between 0 and 100, or leave it blank."}), 400
    grant = store.grant(customer_id=customer_id)
    config: dict[str, str] = {
        **(grant.install_config if grant else {}),
        "default_rate": default_rate,
        "auto_calculate": "true" if body.get("auto_calculate") else "false",
        "base_url": PUBLIC_BASE_URL,
    }
    try:
        saved: dict[str, str] = campfire.put_install_config(customer_id=customer_id, config=config)
    except (NotConnected, requests.HTTPError) as e:
        return jsonify({"level": "error", "message": f"Could not save to Campfire: {e}"}), 502
    return jsonify({"level": "success", "message": "FakeTax settings saved.", "install_config": saved})


@app.post("/events")
def events() -> ResponseReturnValue:
    body: dict[str, Any] = _verified_body()
    threading.Thread(target=_handle_event, kwargs={"event": body}, daemon=True).start()
    return jsonify({"ok": True})


def _handle_event(*, event: dict[str, Any]) -> None:
    topic: str = str(event.get("topic", ""))
    customer_id: int = int(event.get("customer", {}).get("id", 0))
    invoice_id: int = int(event.get("id") or 0)
    try:
        if topic == "Invoice.deleted":
            store.forget_invoice(customer_id=customer_id, invoice_id=invoice_id)
            return
        if topic not in ("Invoice.created", "Invoice.updated"):
            return
        grant = store.grant(customer_id=customer_id)
        if grant is None or grant.install_config.get("auto_calculate") != "true":
            return
        if topic == "Invoice.updated" and is_self_echo(event.get("changed_fields") or []):
            log.info("event %s %s: only tax fields changed, skipping", topic, invoice_id)
            return
        outcome: InvoiceOutcome = calculate_invoice(customer_id=customer_id, invoice_id=invoice_id, only_if_changed=True)
        log.info("event %s %s: %s %s", topic, invoice_id, outcome.kind, outcome.detail)
    except (NotConnected, requests.RequestException) as e:
        log.warning("event %s %s failed: %s", topic, invoice_id, e)


if __name__ == "__main__":
    if not SIGNING_SECRETS:
        log.warning("SIGNING_SECRET is not set: every slot webhook and event will be refused")
    # 0.0.0.0 because Campfire calls in from Docker via host.docker.internal.
    app.run(host="0.0.0.0", port=PORT, debug=True, threaded=True)
