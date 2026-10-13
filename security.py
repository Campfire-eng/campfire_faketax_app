"""Checking that a request really came from Campfire: body signatures and ``cf_ctx`` context tokens."""

import hashlib
import hmac
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import jwt

TIMESTAMP_HEADER: str = "Campfire-Webhook-Request-Timestamp"
SIGNATURE_HEADER: str = "Campfire-Webhook-Signature-v1"
PREVIOUS_SIGNATURE_HEADER: str = "Campfire-Webhook-Signature-v1-Previous"
MAX_AGE_SECONDS: int = 300
CONTEXT_TOKEN_ISSUER: str = "campfire"


class ContextTokenError(Exception):
    pass


def sign(*, secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()


def verify_signature(
    *, secrets: Sequence[str], headers: Mapping[str, str], body: bytes, now: float, max_age: int = MAX_AGE_SECONDS
) -> bool:
    """
    Accept when any signature header matches any of our secrets, so a rotation works from
    either side: Campfire signs with old and new for a day, and we may hold either.
    """
    lowered: dict[str, str] = {key.lower(): value for key, value in headers.items()}
    timestamp: str = lowered.get(TIMESTAMP_HEADER.lower(), "")
    if not timestamp.isdigit() or abs(now - int(timestamp)) > max_age:
        return False
    candidates: list[str] = [
        value
        for value in (lowered.get(SIGNATURE_HEADER.lower()), lowered.get(PREVIOUS_SIGNATURE_HEADER.lower()))
        if value
    ]
    for secret in secrets:
        if not secret:
            continue
        expected: str = sign(secret=secret, timestamp=timestamp, body=body)
        if any(hmac.compare_digest(expected, candidate) for candidate in candidates):
            return True
    return False


def decode_context_token(*, token: str, secrets: Sequence[str], client_id: str) -> dict[str, Any]:
    """Signature, expiry, audience and issuer only; replay is the caller's job."""
    last_error: Exception | None = None
    for secret in secrets:
        if not secret:
            continue
        try:
            return jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience=client_id,
                issuer=CONTEXT_TOKEN_ISSUER,
                options={"require": ["exp", "iat", "jti", "aud", "iss"]},
            )
        except jwt.InvalidSignatureError as e:
            last_error = e
        except jwt.PyJWTError as e:
            raise ContextTokenError(str(e)) from e
    raise ContextTokenError(f"bad signature: {last_error}")


def verify_context_token(
    *, token: str, secrets: Sequence[str], client_id: str, claim_jti: Callable[[str, float], bool]
) -> dict[str, Any]:
    """
    A fully verified, single-use context token. ``claim_jti(jti, exp)`` records the id and returns
    False when it was already seen.
    """
    claims: dict[str, Any] = decode_context_token(token=token, secrets=secrets, client_id=client_id)
    if not claim_jti(str(claims["jti"]), float(claims["exp"])):
        raise ContextTokenError("context token already used")
    return claims


def token_covers(*, claims: Mapping[str, Any], object_type: str, object_id: str) -> bool:
    return claims.get("object_type") == object_type and object_id in [str(i) for i in claims.get("object_ids", [])]
