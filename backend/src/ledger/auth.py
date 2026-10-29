import asyncio
import logging
import secrets
import time
from collections import defaultdict, deque
from urllib.parse import urlencode

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.config import get_settings
from ledger.db.engine import get_session
from ledger.models import HouseholdMember

log = logging.getLogger(__name__)
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALLBACK_PATH = "/api/auth/google/callback"

_hasher = PasswordHasher()
_failures: dict[str, deque[float]] = defaultdict(deque)
MAX_FAILURES = 5
WINDOW_SECONDS = 300


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def check_login(request: Request, password: str) -> None:
    key = _client_key(request)
    now = time.monotonic()
    attempts = _failures[key]
    while attempts and now - attempts[0] > WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= MAX_FAILURES:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts; try again later")

    stored = get_settings().app_password_hash
    if not stored:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "APP_PASSWORD_HASH is not configured")
    try:
        _hasher.verify(stored, password)
    except (VerifyMismatchError, InvalidHashError):
        attempts.append(now)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect password") from None
    attempts.clear()
    request.session.clear()
    request.session["auth"] = True
    request.session["sid"] = secrets.token_urlsafe(16)


def logout(request: Request) -> None:
    request.session.clear()


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("auth"))


async def signed_in_member(request: Request, session: AsyncSession) -> HouseholdMember | None:
    """The member a Google sign-in belongs to (None for a password sign-in).

    Unticking "Can sign in", changing the member's email or deleting the member ends their session on its next request.
    """
    member_id = request.session.get("member_id")
    if member_id is None:
        return None
    member = await session.get(HouseholdMember, member_id)
    if member is None or not member.can_sign_in or (member.email or "").lower() != request.session.get("member_email"):
        request.session.clear()
        return None
    return member


async def require_auth(request: Request, session: AsyncSession = Depends(get_session)) -> None:
    if "member_id" in request.session:
        await signed_in_member(request, session)
    if not is_authenticated(request):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")


# ---- Google sign-in (OAuth authorization-code flow) ----
class GoogleSignInError(Exception):
    """A refused Google sign-in; `code` tells the login page what to say."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def google_enabled() -> bool:
    s = get_settings()
    return bool(s.google_oauth_client_id and s.google_oauth_client_secret)


def google_redirect_uri(request: Request) -> str:
    # Behind a TLS proxy (tailscale serve) the request itself looks like plain http, so APP_BASE_URL wins.
    base = get_settings().app_base_url or str(request.base_url)
    return base.rstrip("/") + GOOGLE_CALLBACK_PATH


def google_start_url(request: Request) -> str:
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    request.session["google_oauth"] = {"state": state, "nonce": nonce}
    params = {
        "client_id": get_settings().google_oauth_client_id,
        "redirect_uri": google_redirect_uri(request),
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def _google_identity(code: str, redirect_uri: str) -> dict:
    """Exchange the authorization code for an ID token and return its verified claims."""
    s = get_settings()
    assert s.google_oauth_client_secret is not None
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": s.google_oauth_client_id,
                "client_secret": s.google_oauth_client_secret.get_secret_value(),
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    token = r.json().get("id_token") if r.status_code == 200 else None
    if not token:
        log.warning("Google token exchange failed: HTTP %s %s", r.status_code, r.text[:300])
        raise GoogleSignInError("google_failed")

    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2 import id_token

    try:
        # Checks the signature, issuer, audience (our client id) and expiry.
        return await asyncio.to_thread(id_token.verify_oauth2_token, token, GoogleRequest(), s.google_oauth_client_id)
    except ValueError as exc:
        log.warning("Google ID token rejected: %s", exc)
        raise GoogleSignInError("google_failed") from None


async def finish_google(request: Request, session: AsyncSession, code: str | None, state: str | None) -> HouseholdMember:
    pending = request.session.pop("google_oauth", None)
    if not pending or not state or not secrets.compare_digest(pending["state"], state):
        raise GoogleSignInError("expired")
    if not code:
        raise GoogleSignInError("cancelled")
    claims = await _google_identity(code, google_redirect_uri(request))
    if claims.get("nonce") != pending["nonce"]:
        raise GoogleSignInError("google_failed")
    email = (claims.get("email") or "").lower()
    if not email or claims.get("email_verified") is not True:
        raise GoogleSignInError("unverified")
    members = (
        await session.scalars(
            select(HouseholdMember).where(HouseholdMember.can_sign_in.is_(True), func.lower(HouseholdMember.email) == email)
        )
    ).all()
    if len(members) != 1:
        log.warning("Google sign-in refused for %s: %d members can sign in with it", email, len(members))
        raise GoogleSignInError("ambiguous" if members else "not_allowed")
    member = members[0]
    request.session.clear()
    request.session["auth"] = True
    request.session["sid"] = secrets.token_urlsafe(16)
    request.session["member_id"] = member.id
    request.session["member_email"] = email
    log.info("%s signed in with Google", member.name)
    return member
