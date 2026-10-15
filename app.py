"""
FakeTax: a demo Campfire app that puts made-up sales tax on invoices and credit memos, using every
app-extension capability (link, webhook and iframe buttons, panels, event subscriptions, install
config, a bespoke onboarding flow and a manage screen Campfire links into).
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import jwt
import requests
from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, make_response, redirect, render_template, request, session, url_for
from flask.typing import ResponseReturnValue
from itsdangerous import BadData, URLSafeTimedSerializer

from campfire import Campfire, NotConnected, WriteOutcome
from jurisdictions import DEFAULT_TAX_CODE, JURISDICTIONS, TAX_CODES, normalize_tax_code
from security import (
    ContextTokenError,
    IdTokenError,
    MAX_AGE_SECONDS,
    decode_context_token,
    token_covers,
    verify_context_token,
    verify_id_token,
    verify_signature,
)
from store import SIMULATE_OPTIONS, FrameSession, Store, WorkspaceTax
from tax import (
    TaxResult,
    compute_credit_memo_tax,
    compute_invoice_tax,
    is_self_echo,
    parse_rate,
    state_from_address,
    vendor_address,
)

load_dotenv()

CAMPFIRE_BASE_URL: str = os.environ.get("CAMPFIRE_BASE_URL", "https://api.meetcampfire.com").rstrip("/")
CAMPFIRE_FRONTEND_ORIGIN: str = os.environ.get("CAMPFIRE_FRONTEND_ORIGIN", "https://app.meetcampfire.com").rstrip("/")
CLIENT_ID: str = os.environ["CLIENT_ID"]
CLIENT_SECRET: str = os.environ["CLIENT_SECRET"]
SIGNING_SECRETS: list[str] = [s for s in (os.environ.get("SIGNING_SECRET"), os.environ.get("SIGNING_SECRET_PREVIOUS")) if s]
PORT: int = int(os.environ.get("PORT", "3999"))
PUBLIC_BASE_URL: str = os.environ.get("PUBLIC_BASE_URL", f"http://localhost:{PORT}").rstrip("/")
REDIRECT_URI: str = f"{PUBLIC_BASE_URL}/callback"
DB_PATH: str = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "faketax.sqlite3"))
with open(os.path.join(os.path.dirname(__file__), "manifest.json")) as _manifest:
    # Ask for exactly the manifest's ceiling, which includes openid so Campfire returns an ID token.
    SCOPES: str = " ".join(json.load(_manifest)["app"]["allowed_scopes"])

DEFAULT_INSTALL_CONFIG: dict[str, str] = {"default_rate": "5.00", "auto_calculate": "false", "base_url": PUBLIC_BASE_URL}
MAX_BULK: int = 50
SLOW_SECONDS: int = 12
TAXABLE_STATUSES: frozenset[str] = frozenset({"draft", "open"})
TAX_CODE_FIELD: str = "tax_code"
# Set on a framed page after its ``cf_ctx`` checks out; ``Partitioned`` keeps it in the jar for
# "FakeTax embedded in Campfire", which modern browsers keep apart from FakeTax opened directly.
FRAME_COOKIE: str = "faketax_frame"
TAX_CODE_MODELS: tuple[str, ...] = ("accountinginvoiceline", "accountingcreditmemoline")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log: logging.Logger = logging.getLogger("faketax")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(32))
store: Store = Store(path=DB_PATH)
frame_cookies: URLSafeTimedSerializer = URLSafeTimedSerializer(app.secret_key, salt="frame-cookie")
campfire: Campfire = Campfire(store=store, base_url=CAMPFIRE_BASE_URL, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)


def _current_user() -> dict[str, Any] | None:
    """Who this browser signed in as: set by the OAuth callback or a manage link from Campfire."""
    user: Any = session.get("user")
    return user if isinstance(user, dict) else None


@app.context_processor
def _inject_current_user() -> dict[str, Any]:
    return {"current_user": _current_user()}


def login_required(view: Callable[..., ResponseReturnValue]) -> Callable[..., ResponseReturnValue]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> ResponseReturnValue:
        if _current_user() is None:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def _sign_in(
    *,
    customer_id: int,
    whoami: dict[str, Any] | None,
    user_id: int | None = None,
    identity: dict[str, Any] | None = None,
) -> None:
    """``identity`` is a verified ID token; its name and email win over the install-config lookup."""
    person: dict[str, Any] = (whoami or {}).get("user") or {}
    claims: dict[str, Any] = identity or {}
    session["user"] = {
        "customer_id": customer_id,
        "customer_name": (whoami or {}).get("customer_name") or f"Workspace {customer_id}",
        "user_id": person.get("id", user_id),
        "sub": claims.get("sub"),
        "name": claims.get("name") or person.get("name") or "",
        "email": claims.get("email") or person.get("email") or "",
    }
    _remember_workspace(customer_id)


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
        invoice=invoice,
        default_rate=_default_rate(customer_id=customer_id),
        rate_override=rate_override,
        workspace=store.workspace(customer_id=customer_id),
    )
    if result.needs_rate:
        return InvoiceOutcome(invoice_id=invoice_id, kind="needs_rate")
    if not result.lines:
        return InvoiceOutcome(invoice_id=invoice_id, kind="skipped", detail="no lines")
    if only_if_changed and result.already_applied:
        return InvoiceOutcome(invoice_id=invoice_id, kind="unchanged")
    workspace: WorkspaceTax | None = store.workspace(customer_id=customer_id)
    outcome: WriteOutcome = campfire.write_tax(
        customer_id=customer_id,
        invoice=invoice,
        result=result,
        tax_code_field=workspace.tax_code_field if workspace else None,
    )
    return InvoiceOutcome(invoice_id=invoice_id, kind=outcome.kind, detail=outcome.detail, tax=result.total_tax)


def calculate_credit_memo(*, customer_id: int, memo_id: int) -> InvoiceOutcome:
    """Credit memos carry no ship-to address, so the client's address decides the jurisdiction."""
    memo: dict[str, Any] | None = campfire.get_credit_memo(customer_id=customer_id, memo_id=memo_id)
    if memo is None:
        return InvoiceOutcome(invoice_id=memo_id, kind="missing")
    state: str | None = None
    if memo.get("client"):
        vendor: dict[str, Any] | None = campfire.get_vendor(customer_id=customer_id, vendor_id=int(memo["client"]))
        state = state_from_address(vendor_address(vendor)) if vendor else None
    result: TaxResult = compute_credit_memo_tax(
        memo=memo,
        state=state,
        default_rate=_default_rate(customer_id=customer_id),
        workspace=store.workspace(customer_id=customer_id),
    )
    if result.needs_rate:
        return InvoiceOutcome(invoice_id=memo_id, kind="needs_rate")
    if not result.lines:
        return InvoiceOutcome(invoice_id=memo_id, kind="skipped", detail="no lines")
    if result.already_applied:
        return InvoiceOutcome(invoice_id=memo_id, kind="unchanged")
    workspace: WorkspaceTax | None = store.workspace(customer_id=customer_id)
    outcome: WriteOutcome = campfire.write_credit_memo_tax(
        customer_id=customer_id, memo=memo, result=result, tax_code_field=workspace.tax_code_field if workspace else None
    )
    return InvoiceOutcome(invoice_id=memo_id, kind=outcome.kind, detail=outcome.detail, tax=result.total_tax)


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
    A frame makes several calls with the one context token Campfire gave it: on the frame URL as
    ``cf_ctx`` and again over the bridge. The first call claims the jti and opens a short session
    bound to it; later calls are let through only for that open session, until the token expires.
    A call with no token at all is accepted when it carries the frame cookie instead. A list-page
    panel has no record, so it passes ``invoice_id=None`` and the token must carry no record either.
    """
    if not token:
        return _frame_cookie_claims(invoice_id=invoice_id)
    try:
        claims: dict[str, Any] = decode_context_token(token=token, secrets=SIGNING_SECRETS, client_id=CLIENT_ID)
    except ContextTokenError as e:
        abort(401, str(e))
    customer_id: int = int(claims["customer_id"])
    jti: str = str(claims["jti"])
    object_ids: list[str] = [str(i) for i in claims.get("object_ids", [])]
    _require_frame_covers(invoice_id=invoice_id, object_ids=object_ids)
    opened: FrameSession | None = store.frame_session(jti=jti)
    if opened is not None and opened.customer_id == customer_id:
        return claims
    if not store.claim_jti(jti, float(claims["exp"])):
        abort(401, "context token already used")
    store.open_frame_session(jti=jti, customer_id=customer_id, exp=float(claims["exp"]), object_ids=object_ids)
    return claims


def _require_frame_covers(*, invoice_id: str | None, object_ids: list[str]) -> None:
    if invoice_id is None:
        if object_ids:
            abort(403, "token is for a record, not the list page")
    elif invoice_id not in object_ids:
        abort(403, "token is not for this invoice")


def _frame_cookie_claims(*, invoice_id: str | None) -> dict[str, Any]:
    """The frame session the page's own cookie names, checked against the record being asked for."""
    try:
        jti: str = str(frame_cookies.loads(request.cookies.get(FRAME_COOKIE, ""), max_age=MAX_AGE_SECONDS))
    except BadData:
        abort(401, "context token or frame cookie required")
    opened: FrameSession | None = store.frame_session(jti=jti)
    if opened is None:
        abort(401, "frame session expired")
    _require_frame_covers(invoice_id=invoice_id, object_ids=opened.object_ids)
    return {"jti": jti, "customer_id": opened.customer_id, "object_ids": opened.object_ids}


def _frame_page(*, template: str, record_id: str | None, context: dict[str, Any] | None = None) -> Response:
    """
    A page Campfire frames. Campfire puts the context token on the frame URL as ``cf_ctx``, so the
    very first request can be verified and answered with a partitioned cookie; the page's own
    calls then need no token. Opened without one (a direct visit), the page still works over the
    bridge, which hands it the same token.
    """
    response: Response = make_response(
        render_template(template, host_origin=CAMPFIRE_FRONTEND_ORIGIN, **(context or {}))
    )
    token: str = request.args.get("cf_ctx", "")
    if token:
        claims: dict[str, Any] = _claim_frame_token(token=token, invoice_id=record_id)
        response.set_cookie(
            FRAME_COOKIE,
            frame_cookies.dumps(str(claims["jti"])),
            max_age=max(1, int(float(claims["exp"]) - time.time())),
            secure=True,
            httponly=True,
            samesite="None",
            partitioned=True,
        )
    return response


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
def _json_error(error: Any) -> ResponseReturnValue:
    return jsonify({"error": getattr(error, "description", str(error))}), getattr(error, "code", 400)


@app.errorhandler(NotConnected)
def _not_connected(_error: NotConnected) -> ResponseReturnValue:
    return jsonify({"error": "FakeTax is not connected to this workspace. Reconnect it in FakeTax."}), 409


@app.get("/login")
def login() -> ResponseReturnValue:
    if _current_user() is not None:
        return redirect(url_for("home"))
    return render_template("login.html", signing_configured=bool(SIGNING_SECRETS))


@app.get("/logout")
def logout() -> ResponseReturnValue:
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def home() -> ResponseReturnValue:
    mine: list[int] = [int(i) for i in session.get("workspaces", [])]
    workspaces: list[dict[str, Any]] = [
        {
            "grant": grant,
            "simulate_failure": store.simulate_failure(customer_id=grant.customer_id),
            "cached": store.cached_invoice_count(customer_id=grant.customer_id),
            "expires_in": int(grant.expires_at - time.time()),
            "tax": store.workspace(customer_id=grant.customer_id),
        }
        for grant in store.grants()
        if grant.customer_id in mine
    ]
    return render_template(
        "home.html",
        workspaces=workspaces,
        simulate_options=SIMULATE_OPTIONS,
        error=request.args.get("error"),
        notice=request.args.get("notice"),
        signing_configured=bool(SIGNING_SECRETS),
    )


def _campfire_return_url(candidate: str | None) -> str | None:
    """Only Campfire's own origin may be a return target; anything else is dropped."""
    if candidate and candidate.startswith(CAMPFIRE_FRONTEND_ORIGIN + "/"):
        return candidate
    return None


@app.get("/open/<int:customer_id>/<string:screen>")
@login_required
def open_screen(customer_id: int, screen: str) -> ResponseReturnValue:
    if store.grant(customer_id=customer_id) is None:
        abort(404, "not connected")
    _require_workspace(customer_id)
    if screen == "setup":
        return redirect(url_for("setup_nexus", customer_id=customer_id))
    return redirect(url_for("manage_workspace", customer_id=customer_id))


@app.get("/connect")
def connect() -> ResponseReturnValue:
    verifier, challenge = _pkce_pair()
    state: str = secrets.token_urlsafe(24)
    nonce: str = secrets.token_urlsafe(24)
    session["oauth"] = {"state": state, "verifier": verifier, "nonce": nonce}
    session["return_to"] = _campfire_return_url(request.args.get("return_to"))
    query: dict[str, str] = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return redirect(f"{campfire.authorize_url}?{urlencode(query)}")


def _connect_failed(message: str) -> ResponseReturnValue:
    """Shown in place, not via a redirect home: a person who is not signed in would be bounced to /login and lose it."""
    return render_template("message.html", title="Could not connect to Campfire", message=message), 400


@app.get("/callback")
def callback() -> ResponseReturnValue:
    pending: dict[str, str] | None = session.pop("oauth", None)
    if request.args.get("error"):
        detail: str = request.args.get("error_description") or ""
        return _connect_failed(f"Campfire returned {request.args['error']}" + (f": {detail}" if detail else ""))
    if not pending or request.args.get("state") != pending["state"]:
        return _connect_failed("state mismatch: refusing the code")
    r = campfire.exchange_code(code=request.args.get("code", ""), redirect_uri=REDIRECT_URI, verifier=pending["verifier"])
    if r.status_code != 200:
        return _connect_failed(f"token exchange failed: {r.status_code} {r.text}")
    tokens: dict[str, Any] = r.json()
    try:
        identity: dict[str, Any] = _verified_identity(id_token=tokens.get("id_token"), nonce=pending["nonce"])
    except IdTokenError as e:
        return _connect_failed(f"ID token rejected: {e}")
    # The token response does not name the workspace; the install config endpoint does.
    current: dict[str, Any] = campfire.get_install_config(access_token=tokens["access_token"])
    customer_id: int = int(current["customer_id"])
    store.save_tokens(customer_id=customer_id, tokens=tokens)
    config: dict[str, str] = {**DEFAULT_INSTALL_CONFIG, **(current.get("install_config") or {}), "base_url": PUBLIC_BASE_URL}
    campfire.put_install_config(customer_id=customer_id, config=config)
    _sign_in(customer_id=customer_id, whoami=current, identity=identity)
    workspace: WorkspaceTax | None = store.workspace(customer_id=customer_id)
    if workspace is None or workspace.onboarded_at is None:
        return redirect(url_for("setup_nexus", customer_id=customer_id))
    return redirect(session.get("return_to") or url_for("home"))


def _verified_identity(*, id_token: str | None, nonce: str) -> dict[str, Any]:
    """Who signed in, from the ID token Campfire returns because FakeTax asks for openid."""
    if not id_token:
        raise IdTokenError("Campfire returned no id_token; add openid to the app's scopes")
    try:
        key: Any = campfire.id_token_signing_key(id_token=id_token)
    except jwt.PyJWTError as e:
        raise IdTokenError(f"could not load Campfire's signing key: {e}") from e
    return verify_id_token(id_token=id_token, signing_key=key, issuer=campfire.issuer, client_id=CLIENT_ID, nonce=nonce)


def _remember_workspace(customer_id: int) -> None:
    """This browser proved it may act for the workspace, by authorizing or by arriving with a token."""
    allowed: list[int] = [int(i) for i in session.get("workspaces", [])]
    if customer_id not in allowed:
        allowed.append(customer_id)
    session["workspaces"] = allowed


def _require_workspace(customer_id: int) -> None:
    if customer_id not in [int(i) for i in session.get("workspaces", [])]:
        abort(403, "open this workspace from Campfire first")


def _nexus_from_form() -> list[str]:
    return [code for code in JURISDICTIONS if code in request.form.getlist("nexus")]


def _tax_code_from_form() -> str:
    value: str = request.form.get("default_tax_code", DEFAULT_TAX_CODE)
    return value if value in TAX_CODES else DEFAULT_TAX_CODE


@app.route("/setup/<int:customer_id>", methods=["GET", "POST"])
@login_required
def setup_nexus(customer_id: int) -> ResponseReturnValue:
    _require_workspace(customer_id)
    workspace: WorkspaceTax | None = store.workspace(customer_id=customer_id)
    if request.method == "POST":
        store.save_workspace(customer_id=customer_id, nexus=_nexus_from_form(), default_tax_code=_tax_code_from_form())
        return redirect(url_for("setup_field", customer_id=customer_id))
    return render_template(
        "setup_nexus.html",
        step=1,
        customer_id=customer_id,
        jurisdictions=list(JURISDICTIONS.values()),
        nexus=workspace.nexus if workspace else [],
        tax_codes=list(TAX_CODES.values()),
        default_tax_code=workspace.default_tax_code if workspace else DEFAULT_TAX_CODE,
    )


@app.route("/setup/<int:customer_id>/field", methods=["GET", "POST"])
@login_required
def setup_field(customer_id: int) -> ResponseReturnValue:
    _require_workspace(customer_id)
    workspace: WorkspaceTax = store.workspace(customer_id=customer_id) or store.save_workspace(customer_id=customer_id)
    error: str | None = None
    if request.method == "POST":
        try:
            for model in TAX_CODE_MODELS:
                campfire.create_custom_field(
                    customer_id=customer_id,
                    model=model,
                    name=TAX_CODE_FIELD,
                    label="Tax code",
                    options=[code.code for code in TAX_CODES.values()],
                )
            store.save_workspace(customer_id=customer_id, tax_code_field=TAX_CODE_FIELD)
            return redirect(url_for("setup_done", customer_id=customer_id))
        except (NotConnected, requests.RequestException) as e:
            error = f"Campfire refused the custom field: {e}"
    return render_template(
        "setup_field.html",
        step=2,
        customer_id=customer_id,
        tax_codes=list(TAX_CODES.values()),
        default_label=TAX_CODES[normalize_tax_code(workspace.default_tax_code)].label,
        error=error,
    )


@app.get("/setup/<int:customer_id>/done")
@login_required
def setup_done(customer_id: int) -> ResponseReturnValue:
    _require_workspace(customer_id)
    workspace: WorkspaceTax = store.save_workspace(customer_id=customer_id, onboarded=True)
    return render_template(
        "setup_done.html",
        step=3,
        customer_id=customer_id,
        workspace=workspace,
        return_to=session.get("return_to") or f"{CAMPFIRE_FRONTEND_ORIGIN}/v2/settings/connections",
    )


@app.get("/manage")
def manage_entry() -> ResponseReturnValue:
    """Campfire's "Manage in FakeTax" link: a single-use token names the person and workspace."""
    try:
        claims: dict[str, Any] = verify_context_token(
            token=request.args.get("cf_ctx", ""), secrets=SIGNING_SECRETS, client_id=CLIENT_ID, claim_jti=store.claim_jti
        )
    except ContextTokenError as e:
        return render_template("message.html", title="Link expired", message=f"Open FakeTax from Campfire again ({e})."), 401
    if claims.get("surface") != "manage":
        return render_template("message.html", title="Wrong link", message="This token is not a manage link."), 403
    customer_id: int = int(claims["customer_id"])
    user_id: int | None = int(claims["user_id"]) if claims.get("user_id") is not None else None
    whoami: dict[str, Any] | None = None
    grant = store.grant(customer_id=customer_id)
    if grant is not None:
        try:
            # The connection token names the person who connected; only trust it for the same person.
            candidate: dict[str, Any] = campfire.get_install_config(access_token=grant.access_token)
            if (candidate.get("user") or {}).get("id") == user_id:
                whoami = candidate
            else:
                whoami = {"customer_name": candidate.get("customer_name")}
        except requests.RequestException:
            whoami = None
    _sign_in(customer_id=customer_id, whoami=whoami, user_id=user_id)
    session["manage_claims"] = {"user_id": user_id, "customer_id": customer_id}
    return redirect(url_for("manage_workspace", customer_id=customer_id))


@app.route("/manage/<int:customer_id>", methods=["GET", "POST"])
@login_required
def manage_workspace(customer_id: int) -> ResponseReturnValue:
    _require_workspace(customer_id)
    error: str | None = None
    notice: str | None = None
    if request.method == "POST":
        overrides: dict[str, str] = {}
        for code in JURISDICTIONS:
            raw: str = request.form.get(f"override_{code}", "").strip()
            if raw and parse_rate(raw) is None:
                error = f"{code}: an override must be a number between 0 and 100."
                break
            if raw:
                overrides[code] = raw
        if error is None:
            store.save_workspace(
                customer_id=customer_id,
                nexus=_nexus_from_form(),
                overrides=overrides,
                default_tax_code=_tax_code_from_form(),
            )
            notice = "Saved."
    workspace: WorkspaceTax = store.workspace(customer_id=customer_id) or store.save_workspace(customer_id=customer_id)
    grant = store.grant(customer_id=customer_id)
    claims: dict[str, Any] | None = session.get("manage_claims")
    return render_template(
        "manage.html",
        customer_id=customer_id,
        workspace=workspace,
        jurisdictions=list(JURISDICTIONS.values()),
        tax_codes=list(TAX_CODES.values()),
        install_config=grant.install_config if grant else {},
        cached=store.cached_invoice_count(customer_id=customer_id),
        claims=claims if claims and claims.get("customer_id") == customer_id else None,
        error=error,
        notice=notice,
    )


@app.get("/refresh/<int:customer_id>")
@login_required
def refresh(customer_id: int) -> ResponseReturnValue:
    _require_workspace(customer_id)
    try:
        campfire.refresh(customer_id=customer_id)
    except NotConnected as e:
        return redirect(url_for("home", error=str(e)))
    return redirect(url_for("home", notice=f"Refreshed token for workspace {customer_id}"))


@app.get("/disconnect/<int:customer_id>")
@login_required
def disconnect(customer_id: int) -> ResponseReturnValue:
    _require_workspace(customer_id)
    store.delete_grant(customer_id=customer_id)
    return redirect(url_for("home", notice=f"Forgot tokens for workspace {customer_id}"))


@app.post("/workspaces/<int:customer_id>/settings")
@login_required
def save_settings(customer_id: int) -> ResponseReturnValue:
    _require_workspace(customer_id)
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
        store.open_frame_session(jti=str(claims["jti"]), customer_id=customer_id, exp=float(claims["exp"]), object_ids=ids)
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
    result: TaxResult = compute_invoice_tax(
        invoice=invoice, default_rate=_default_rate(customer_id=customer_id), workspace=store.workspace(customer_id=customer_id)
    )
    return render_template("report.html", invoice=invoice, result=result, claims=claims)


@app.get("/frame/breakdown/<int:invoice_id>")
def frame_breakdown(invoice_id: int) -> ResponseReturnValue:
    return _frame_page(template="breakdown.html", record_id=str(invoice_id), context={"invoice_id": invoice_id})


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
    result: TaxResult = compute_invoice_tax(
        invoice=invoice, default_rate=_default_rate(customer_id=customer_id), workspace=store.workspace(customer_id=customer_id)
    )
    return jsonify(
        {
            "invoice_number": invoice.get("invoice_number"),
            "payment_status": invoice.get("payment_status"),
            "currency": invoice.get("currency"),
            "shipping_address": invoice.get("shipping_address") or "",
            "state": result.state,
            "rate": str(result.rate) if result.rate is not None else None,
            "nexus": result.nexus,
            "lines": [
                {
                    "description": line.description,
                    "base": str(line.base),
                    "current_tax": str(line.current_tax),
                    "tax_code": line.tax_code,
                    "taxable": line.taxable,
                }
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
    return _frame_page(template="settings.html", record_id=None)


def _panel_request() -> tuple[dict[str, Any], int]:
    body: Any = request.get_json(silent=True) or {}
    claims: dict[str, Any] = _claim_frame_token(token=str(body.get("context_token", "")), invoice_id=None)
    return body, int(claims["customer_id"])


@app.get("/frame/manage")
def frame_manage() -> ResponseReturnValue:
    """The Jurisdictions page Campfire shows in its sidebar: the manage screen, driven by the bridge."""
    return _frame_page(template="frame_manage.html", record_id=None)


@app.get("/frame/codes")
def frame_codes() -> ResponseReturnValue:
    """The tax code catalog; the same for every workspace, so no token is needed."""
    return render_template("frame_codes.html", host_origin=CAMPFIRE_FRONTEND_ORIGIN, tax_codes=list(TAX_CODES.values()))


def _manage_payload(*, customer_id: int) -> dict[str, Any]:
    workspace: WorkspaceTax = store.workspace(customer_id=customer_id) or store.save_workspace(customer_id=customer_id)
    return {
        "customer_id": customer_id,
        "workspace": {
            "nexus": workspace.nexus,
            "overrides": workspace.overrides,
            "default_tax_code": normalize_tax_code(workspace.default_tax_code),
            "tax_code_field": workspace.tax_code_field,
            "onboarded": workspace.onboarded_at is not None,
        },
        "jurisdictions": [
            {
                "code": j.code,
                "name": j.name,
                "state_rate": str(j.state_rate),
                "local_rate": str(j.local_rate),
                "combined_rate": str(j.combined_rate),
                "has_sales_tax": j.has_sales_tax,
                "taxes_services": j.taxes_services,
                "taxes_saas": j.taxes_saas,
                "taxes_digital": j.taxes_digital,
                "taxes_shipping": j.taxes_shipping,
            }
            for j in JURISDICTIONS.values()
        ],
        "tax_codes": [{"code": c.code, "label": c.label, "description": c.description, "when": c.when} for c in TAX_CODES.values()],
    }


@app.post("/frame/api/manage")
def frame_api_manage() -> ResponseReturnValue:
    _body, customer_id = _panel_request()
    return jsonify(_manage_payload(customer_id=customer_id))


@app.post("/frame/api/manage/save")
def frame_api_manage_save() -> ResponseReturnValue:
    body, customer_id = _panel_request()
    nexus: Any = body.get("nexus", [])
    raw_overrides: Any = body.get("overrides", {})
    if not isinstance(nexus, list) or not isinstance(raw_overrides, dict):
        return jsonify({"level": "error", "message": "nexus must be a list and overrides an object."}), 400
    overrides: dict[str, str] = {}
    for code, raw in raw_overrides.items():
        value: str = str(raw or "").strip()
        if code in JURISDICTIONS and value:
            if parse_rate(value) is None:
                return jsonify({"level": "error", "message": f"{code}: an override must be a number between 0 and 100."}), 400
            overrides[code] = value
    default_code: str = normalize_tax_code(str(body.get("default_tax_code") or ""))
    store.save_workspace(
        customer_id=customer_id,
        nexus=[code for code in JURISDICTIONS if code in nexus],
        overrides=overrides,
        default_tax_code=default_code,
    )
    return jsonify({"level": "success", "message": "FakeTax jurisdictions saved.", **_manage_payload(customer_id=customer_id)})


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
        if topic not in ("Invoice.created", "Invoice.updated", "CreditMemo.created", "CreditMemo.updated"):
            return
        grant = store.grant(customer_id=customer_id)
        if grant is None or grant.install_config.get("auto_calculate") != "true":
            return
        if topic.endswith(".updated") and is_self_echo(event.get("changed_fields") or []):
            log.info("event %s %s: only tax fields changed, skipping", topic, invoice_id)
            return
        outcome: InvoiceOutcome = (
            calculate_credit_memo(customer_id=customer_id, memo_id=invoice_id)
            if topic.startswith("CreditMemo.")
            else calculate_invoice(customer_id=customer_id, invoice_id=invoice_id, only_if_changed=True)
        )
        log.info("event %s %s: %s %s", topic, invoice_id, outcome.kind, outcome.detail)
    except (NotConnected, requests.RequestException) as e:
        log.warning("event %s %s failed: %s", topic, invoice_id, e)


if __name__ == "__main__":
    if not SIGNING_SECRETS:
        log.warning("SIGNING_SECRET is not set: every slot webhook and event will be refused")
    # 0.0.0.0 because Campfire calls in from Docker via host.docker.internal.
    app.run(host="0.0.0.0", port=PORT, debug=True, threaded=True)
