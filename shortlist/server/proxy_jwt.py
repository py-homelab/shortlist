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


def _jwks(url: str, *, fresh: bool = False) -> dict:
    """The proxy's keys by `kid`. `fresh` skips the READ, not the write: a check that asks "can this
    URL verify anything" must not be answered from an hour-old copy — in either direction."""
    now = time.monotonic()
    if not fresh:
        with _jwks_lock:
            hit = _jwks_cache.get(url)
            if hit and now - hit[0] < JWKS_TTL_S:
                return hit[1]
    r = httpx.get(url, timeout=10)
    r.raise_for_status()
    keys = {k.get("kid"): k for k in _key_list(r.json()) if isinstance(k, dict)}
    with _jwks_lock:
        _jwks_cache[url] = (now, keys)
    return keys


def _key_list(body: object) -> list:
    """The `keys` array of a JWKS document, or a raise for anything that is not one.

    A body that is not a key set at all — an SSO login page, a proxy error page that happens to be
    JSON, `{"keys": {...}}` — is a fault of the moment dressed as a document. Reading it as "this
    provider publishes no keys" would call a transient outage a permanent misconfiguration, and the
    caller treats those two very differently.
    """
    if not isinstance(body, dict) or not isinstance(body.get("keys"), list):
        raise ValueError("not a JWKS document")
    return body["keys"]


class UnusableJwks(Exception):
    """The JWKS URL answered, and carries no key that could verify anything."""


def check_jwks(url: str) -> None:
    """Raise `UnusableJwks` if that URL serves an empty key set, reading it FRESH.

    An empty key set is not a transient fault, it is a permanent misconfiguration: an authentik PROXY
    provider cannot hold a signing key at all, so its `/jwks/` is `{}` for ever and its tokens are
    signed symmetrically. Point verified mode at one and every person loses their session at the next
    request — all at once, and only on a path nobody exercises until they try to sign in.

    A URL that cannot be REACHED raises whatever httpx raised: unreachable is a fault of the moment
    and belongs to the caller to weigh, which is why the two cases are not collapsed here.
    """
    if not _jwks(url, fresh=True):
        raise UnusableJwks(f"{url} serves no keys, so no token could ever be verified against it")


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
