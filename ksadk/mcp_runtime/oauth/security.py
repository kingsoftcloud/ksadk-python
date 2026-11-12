"""Purpose-bound signed messages."""

from __future__ import annotations

import base64
import hashlib
import json
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


IDENTITY_FIELDS = ("identity_namespace", "tenant_id", "subject_type", "subject_id")


def identity(value: dict) -> dict:
    result = {key: value.get(key) for key in IDENTITY_FIELDS}
    if not all(isinstance(v, str) and 0 < len(v) <= 128 for v in result.values()):
        raise ValueError("IDENTITY_REQUIRED")
    return result


def sign(claims: dict, key: Ed25519PrivateKey, *, issuer: str, audience: str, ttl=120) -> str:
    now = int(time.time())
    body = {**claims, "iss": issuer, "aud": audience, "iat": now, "exp": now + ttl}
    message = b64(canonical({"alg": "EdDSA", "typ": "JWT"})) + "." + b64(canonical(body))
    return message + "." + b64(key.sign(message.encode()))


def verify(token: str, key: Ed25519PublicKey, *, issuer: str, audience: str) -> dict:
    try:
        if len(token) > 16384:
            raise ValueError()
        header, body, signature = token.split(".")
        parsed = json.loads(unb64(header))
        if (
            not isinstance(parsed, dict)
            or parsed.get("alg") != "EdDSA"
            or parsed.get("typ") != "JWT"
            or set(parsed) - {"alg", "typ", "kid"}
        ):
            raise ValueError()
        key.verify(unb64(signature), (header + "." + body).encode())
        claims = json.loads(unb64(body))
        now = time.time()
        if (
            claims["iss"] != issuer
            or claims["aud"] != audience
            or not isinstance(claims["iat"], int)
            or not isinstance(claims["exp"], int)
            or not claims["iat"] <= now < claims["exp"]
            or not 0 < claims["exp"] - claims["iat"] <= 300
        ):
            raise ValueError()
        return claims
    except Exception:
        raise ValueError("INVALID_SERVICE_CREDENTIAL") from None
