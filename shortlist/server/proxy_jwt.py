"""Reading a person's identity out of a JWT a reverse proxy attaches (authentik's `X-authentik-jwt`).

The proxy authenticated the visitor and forwards a JWT whose claims name them; Shortlist reads one
claim — the Plex account id — by a dotted path (`ak_proxy.user_attributes.additionalHeaders.
X-Plex-Account-Id` for an authentik provider that maps it, as the picks app did). Two trust models:

* **Unverified** (no JWKS URL set): the token is trusted because only the proxy can reach Shortlist
  and its forward-auth REPLACES the header. That is only true when the container publishes no port
  and nothing else on its networks is hostile — the deployment's job, not this module's.
* **Verified** (`auth.proxy.jwks_url` set): the signature is checked against the proxy's published
  keys (RS256 / ES256), and an expired token is refused. The URL comes from Shortlist's OWN settings,
  never from a request header — a forger controls every header, so a key named by one is theirs.

Everything fails CLOSED to "nobody": a missing header, a claim that does not resolve, a bad signature,
an unreachable JWKS. Never an error the caller can read anything from.
"""

from __future__ import annotations

import base64
import json
import threading
import time

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from loguru import logger

JWKS_TTL_S = 3600
_jwks_cache: dict[str, tuple[float, dict]] = {}
_jwks_lock = threading.Lock()


def _b64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _int(value: str) -> int:
    return int.from_bytes(_b64(value), "big")


def _claim(payload: dict, path: str) -> object:
    """Walk a dotted path. Keys may themselves contain dashes (`X-Plex-Account-Id`), never dots."""
    node: object = payload
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _jwks(url: str) -> dict:
    now = time.monotonic()
    with _jwks_lock:
        hit = _jwks_cache.get(url)
        if hit and now - hit[0] < JWKS_TTL_S:
            return hit[1]
    r = httpx.get(url, timeout=10)
    r.raise_for_status()
    keys = {k.get("kid"): k for k in r.json().get("keys", []) if isinstance(k, dict)}
    with _jwks_lock:
        _jwks_cache[url] = (now, keys)
    return keys


def _verify(signing_input: bytes, signature: bytes, header: dict, jwks_url: str) -> bool:
    keys = _jwks(jwks_url)
    jwk = keys.get(header.get("kid")) or (next(iter(keys.values())) if len(keys) == 1 else None)
    if jwk is None:
        return False
    alg = header.get("alg")
    try:
        if alg == "RS256" and jwk.get("kty") == "RSA":
            key = rsa.RSAPublicNumbers(_int(jwk["e"]), _int(jwk["n"])).public_key()
            key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
            return True
        if alg == "ES256" and jwk.get("kty") == "EC" and jwk.get("crv") == "P-256":
            key = ec.EllipticCurvePublicNumbers(_int(jwk["x"]), _int(jwk["y"]), ec.SECP256R1()).public_key()
            if len(signature) != 64:
                return False
            der = encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
            key.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
            return True
    except (InvalidSignature, KeyError, ValueError):
        return False
    return False


def account_id_from_jwt(token: str, claim_path: str, jwks_url: str = "") -> int | None:
    """The Plex account id the token names, or None — for ANY failure (see the module docstring)."""
    try:
        head_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(_b64(head_b64))
        payload = json.loads(_b64(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    if jwks_url:
        try:
            ok = _verify(f"{head_b64}.{payload_b64}".encode(), _b64(sig_b64), header, jwks_url)
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("proxy JWT: could not read the signing keys ({}) — nobody signed in", type(e).__name__)
            return None
        if not ok:
            logger.warning("proxy JWT: signature did not verify — ignored")
            return None
        exp = payload.get("exp")
        if isinstance(exp, int | float) and exp < time.time():
            return None
    value = _claim(payload, claim_path)
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None
