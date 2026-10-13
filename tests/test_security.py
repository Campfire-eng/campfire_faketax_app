import time
import uuid

import jwt
import pytest

from security import (
    PREVIOUS_SIGNATURE_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    ContextTokenError,
    sign,
    token_covers,
    verify_context_token,
    verify_signature,
)

SECRET = "whsec_current_" + "c" * 32
OLD_SECRET = "whsec_old_" + "o" * 32
CLIENT_ID = "client-123"
BODY = b'{"kind":"event","topic":"Invoice.updated","id":5}'


def _headers(*, secret: str = SECRET, timestamp: int | None = None, body: bytes = BODY) -> dict[str, str]:
    ts = str(timestamp if timestamp is not None else int(time.time()))
    return {TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: sign(secret=secret, timestamp=ts, body=body)}


def _token(*, secret: str = SECRET, aud: str = CLIENT_ID, exp_in: int = 300, jti: str | None = None) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "campfire",
            "aud": aud,
            "jti": jti or str(uuid.uuid4()),
            "iat": now,
            "exp": now + exp_in,
            "customer_id": 42,
            "user_id": 7,
            "slot_key": "calculate",
            "object_type": "invoice",
            "object_ids": ["101", "102"],
            "surface": "detail_header",
        },
        secret,
        algorithm="HS256",
    )


class _JtiStore:
    def __init__(self) -> None:
        self.seen: set[str] = set()

    def claim(self, jti: str, exp: float) -> bool:
        if jti in self.seen:
            return False
        self.seen.add(jti)
        return True


class TestVerifySignature:
    def test_accepts_matching_signature(self) -> None:
        assert verify_signature(secrets=[SECRET], headers=_headers(), body=BODY, now=time.time())

    def test_rejects_tampered_body(self) -> None:
        assert not verify_signature(secrets=[SECRET], headers=_headers(), body=BODY + b" ", now=time.time())

    def test_rejects_wrong_secret(self) -> None:
        assert not verify_signature(secrets=["whsec_other"], headers=_headers(), body=BODY, now=time.time())

    def test_rejects_stale_timestamp(self) -> None:
        stale = int(time.time()) - 301
        assert not verify_signature(secrets=[SECRET], headers=_headers(timestamp=stale), body=BODY, now=time.time())

    def test_rejects_missing_timestamp(self) -> None:
        headers = _headers()
        del headers[TIMESTAMP_HEADER]
        assert not verify_signature(secrets=[SECRET], headers=headers, body=BODY, now=time.time())

    def test_accepts_previous_signature_during_rotation(self) -> None:
        ts = str(int(time.time()))
        headers = {
            TIMESTAMP_HEADER: ts,
            SIGNATURE_HEADER: sign(secret="whsec_brand_new_" + "n" * 32, timestamp=ts, body=BODY),
            PREVIOUS_SIGNATURE_HEADER: sign(secret=OLD_SECRET, timestamp=ts, body=BODY),
        }
        assert verify_signature(secrets=[OLD_SECRET], headers=headers, body=BODY, now=time.time())


class TestVerifyContextToken:
    def test_accepts_valid_token_once(self) -> None:
        store = _JtiStore()
        token = _token()

        claims = verify_context_token(token=token, secrets=[SECRET], client_id=CLIENT_ID, claim_jti=store.claim)

        assert claims["customer_id"] == 42
        with pytest.raises(ContextTokenError, match="already used"):
            verify_context_token(token=token, secrets=[SECRET], client_id=CLIENT_ID, claim_jti=store.claim)

    def test_rejects_other_audience(self) -> None:
        with pytest.raises(ContextTokenError):
            verify_context_token(token=_token(aud="someone-else"), secrets=[SECRET], client_id=CLIENT_ID, claim_jti=_JtiStore().claim)

    def test_rejects_expired(self) -> None:
        with pytest.raises(ContextTokenError):
            verify_context_token(token=_token(exp_in=-10), secrets=[SECRET], client_id=CLIENT_ID, claim_jti=_JtiStore().claim)

    def test_rejects_wrong_secret(self) -> None:
        with pytest.raises(ContextTokenError):
            verify_context_token(token=_token(secret="whsec_x_" + "x" * 32), secrets=[SECRET], client_id=CLIENT_ID, claim_jti=_JtiStore().claim)

    def test_accepts_token_signed_with_previous_secret(self) -> None:
        claims = verify_context_token(
            token=_token(secret=OLD_SECRET), secrets=[SECRET, OLD_SECRET], client_id=CLIENT_ID, claim_jti=_JtiStore().claim
        )
        assert claims["slot_key"] == "calculate"

    def test_token_covers_listed_ids_only(self) -> None:
        claims = {"object_type": "invoice", "object_ids": ["101", "102"]}
        assert token_covers(claims=claims, object_type="invoice", object_id="102")
        assert not token_covers(claims=claims, object_type="invoice", object_id="103")
        assert not token_covers(claims=claims, object_type="bill", object_id="101")
