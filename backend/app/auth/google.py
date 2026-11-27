"""Google OAuth 2.0 / OpenID Connect helpers.

Only identity is used. We intentionally never request offline access and never
persist Google access/refresh tokens.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import jwt
import requests

from app import config

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
JWKS_ENDPOINT = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}

_jwks_lock = threading.Lock()
_jwks: dict[str, Any] | None = None
_jwks_at = 0.0
_JWKS_TTL = 3600.0


class OAuthError(Exception):
    """A user-visible OAuth failure; details stay in server logs."""


@dataclass(frozen=True)
class GoogleProfile:
    subject: str
    email: str
    email_verified: bool
    name: str | None
    picture: str | None


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorization_url(state: str, nonce: str, code_challenge: str, redirect_uri: str) -> str:
    params = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_code(code: str, code_verifier: str, redirect_uri: str) -> dict[str, Any]:
    if not config.AUTH_ENABLED:
        raise OAuthError("Google login is not configured")
    try:
        response = requests.post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": config.GOOGLE_CLIENT_ID,
                "client_secret": config.GOOGLE_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
            headers={"Accept": "application/json"},
            timeout=15,
        )
    except requests.RequestException as e:
        raise OAuthError("Could not reach Google") from e
    if response.status_code != 200:
        raise OAuthError("Google rejected the authorization code")
    try:
        data = response.json()
    except ValueError as e:
        raise OAuthError("Google returned an invalid token response") from e
    if not data.get("id_token"):
        raise OAuthError("Google did not return an ID token")
    return data


def _load_jwks(force: bool = False) -> dict[str, Any]:
    global _jwks, _jwks_at
    now = time.time()
    with _jwks_lock:
        if not force and _jwks is not None and now - _jwks_at < _JWKS_TTL:
            return _jwks
        try:
            response = requests.get(JWKS_ENDPOINT, timeout=10)
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as e:
            if _jwks is not None:
                return _jwks  # stale keys are safer than a login outage
            raise OAuthError("Could not load Google signing keys") from e
        _jwks = data
        _jwks_at = now
        return data


def _key_for(kid: str):
    jwks = _load_jwks()
    for jwk in jwks.get("keys", []):
        if jwk.get("kid") == kid:
            return jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
    # Key rotation: refresh once before giving up.
    jwks = _load_jwks(force=True)
    for jwk in jwks.get("keys", []):
        if jwk.get("kid") == kid:
            return jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
    raise OAuthError("Google signing key not found")


def verify_id_token(id_token: str, nonce: str | None = None) -> GoogleProfile:
    if not config.AUTH_ENABLED:
        raise OAuthError("Google login is not configured")
    try:
        header = jwt.get_unverified_header(id_token)
        if header.get("alg") != "RS256":
            raise OAuthError("Unexpected ID token algorithm")
        kid = header.get("kid")
        if not kid:
            raise OAuthError("ID token has no key id")
        payload = jwt.decode(
            id_token,
            _key_for(kid),
            algorithms=["RS256"],
            audience=config.GOOGLE_CLIENT_ID,
            # 服务器时钟比 Google 慢几秒时，刚签发的 token 会因为 iat 在"未来"被拒（ImmatureSignatureError）
            leeway=10,
            options={"verify_iss": False, "require": ["exp", "iat", "aud", "sub"]},
        )
    except jwt.PyJWTError as e:
        raise OAuthError("Invalid Google ID token") from e

    if payload.get("iss") not in ISSUERS:
        raise OAuthError("Invalid Google token issuer")
    # nonce 只在"我们发起授权时预生成了随机数"的流程里校验（OAuth 跳转流）；
    # One Tap 的 ID Token 直接由页面上的 GIS 回调送来，没有预交换的 nonce，传 None 跳过。
    if nonce is not None and payload.get("nonce") != nonce:
        raise OAuthError("Invalid Google token nonce")
    if payload.get("azp") and payload["azp"] != config.GOOGLE_CLIENT_ID:
        raise OAuthError("Invalid Google token authorized party")

    verified = payload.get("email_verified")
    if verified not in (True, "true", 1):
        raise OAuthError("Google account email is not verified")
    email = payload.get("email")
    subject = payload.get("sub")
    if not isinstance(email, str) or not email:
        raise OAuthError("Google account has no email")
    if not isinstance(subject, str) or not subject:
        raise OAuthError("Google account has no subject")

    return GoogleProfile(
        subject=subject,
        email=email,
        email_verified=True,
        name=payload.get("name") if isinstance(payload.get("name"), str) else None,
        picture=payload.get("picture") if isinstance(payload.get("picture"), str) else None,
    )
