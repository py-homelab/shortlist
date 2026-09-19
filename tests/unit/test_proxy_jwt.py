"""Reading a person out of a proxy's JWT: the claim path, the optional signature check, and failing
closed on everything else."""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from shortlist.server import proxy_jwt
from shortlist.server.proxy_jwt import account_id_from_jwt

CLAIM = "ak_proxy.user_attributes.additionalHeaders.X-Plex-Account-Id"
JWKS = "https://auth.example/application/o/picks/jwks/"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _num(n: int) -> str:
    return _b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))


def _payload(account: object = "555000100", **extra) -> dict:
    return {"sub": "x", "ak_proxy": {"user_attributes": {"additionalHeaders": {"X-Plex-Account-Id": account}}}, **extra}


def _rs256(payload: dict, key, kid="k1") -> str:
    head = _b64(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
    body = _b64(json.dumps(payload).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64(sig)}"


@pytest.fixture(autouse=True)
def _fresh_cache():
    proxy_jwt._jwks_cache.clear()
    yield
    proxy_jwt._jwks_cache.clear()


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _serve_jwks(key, kid="k1"):
    pub = key.public_key().public_numbers()
    respx.get(JWKS).mock(
        return_value=httpx.Response(
            200, json={"keys": [{"kty": "RSA", "kid": kid, "n": _num(pub.n), "e": _num(pub.e)}]}
        )
    )


class TestUnverified:
    def test_reads_the_claim_by_its_dotted_path(self, rsa_key):
        assert account_id_from_jwt(_rs256(_payload(), rsa_key), CLAIM) == 555000100

    def test_an_int_claim_works_too(self, rsa_key):
        assert account_id_from_jwt(_rs256(_payload(555000100), rsa_key), CLAIM) == 555000100

    @pytest.mark.parametrize("bad", [None, "", "abc", True, {"nested": 1}])
    def test_anything_that_is_not_an_account_id_is_nobody(self, rsa_key, bad):
        assert account_id_from_jwt(_rs256(_payload(bad), rsa_key), CLAIM) is None

    def test_a_missing_claim_is_nobody(self, rsa_key):
        assert account_id_from_jwt(_rs256({"sub": "x"}, rsa_key), CLAIM) is None

    @pytest.mark.parametrize("token", ["", "a.b", "a.b.c.d", "!!!.###.$$$"])
    def test_garbage_is_nobody(self, token):
        assert account_id_from_jwt(token, CLAIM) is None


class TestVerified:
    @respx.mock
    def test_a_token_the_proxy_signed_is_believed(self, rsa_key):
        _serve_jwks(rsa_key)
        assert account_id_from_jwt(_rs256(_payload(), rsa_key), CLAIM, JWKS) == 555000100

    @respx.mock
    def test_a_token_signed_by_anyone_else_is_not(self, rsa_key):
        _serve_jwks(rsa_key)
        forger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        assert account_id_from_jwt(_rs256(_payload(), forger), CLAIM, JWKS) is None

    @respx.mock
    def test_a_tampered_payload_is_not(self, rsa_key):
        _serve_jwks(rsa_key)
        head, _, sig = _rs256(_payload(), rsa_key).split(".")
        forged = _b64(json.dumps(_payload("555000001")).encode())
        assert account_id_from_jwt(f"{head}.{forged}.{sig}", CLAIM, JWKS) is None

    @respx.mock
    def test_an_expired_token_is_not(self, rsa_key):
        _serve_jwks(rsa_key)
        token = _rs256(_payload(exp=int(time.time()) - 60), rsa_key)
        assert account_id_from_jwt(token, CLAIM, JWKS) is None

    @respx.mock
    def test_an_unreachable_jwks_is_nobody_not_an_error(self, rsa_key):
        respx.get(JWKS).mock(side_effect=httpx.ConnectError("down"))
        assert account_id_from_jwt(_rs256(_payload(), rsa_key), CLAIM, JWKS) is None

    @respx.mock
    def test_es256_is_supported(self):
        key = ec.generate_private_key(ec.SECP256R1())
        pub = key.public_key().public_numbers()
        respx.get(JWKS).mock(
            return_value=httpx.Response(
                200, json={"keys": [{"kty": "EC", "crv": "P-256", "kid": "e1", "x": _num(pub.x), "y": _num(pub.y)}]}
            )
        )
        head = _b64(json.dumps({"alg": "ES256", "kid": "e1"}).encode())
        body = _b64(json.dumps(_payload()).encode())
        r, s = decode_dss_signature(key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
        sig = _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        assert account_id_from_jwt(f"{head}.{body}.{sig}", CLAIM, JWKS) == 555000100

    @respx.mock
    def test_an_unsigned_alg_none_token_is_refused(self, rsa_key):
        _serve_jwks(rsa_key)
        head = _b64(json.dumps({"alg": "none", "kid": "k1"}).encode())
        body = _b64(json.dumps(_payload()).encode())
        assert account_id_from_jwt(f"{head}.{body}.", CLAIM, JWKS) is None
