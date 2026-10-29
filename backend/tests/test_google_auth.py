from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

from ledger import auth
from ledger.config import get_settings

CALLBACK = "http://test/api/auth/google/callback"


@pytest.fixture
def google(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "google_oauth_client_id", "client-123.apps.googleusercontent.com")
    monkeypatch.setattr(s, "google_oauth_client_secret", SecretStr("shh"))


@pytest.fixture
async def anon(database):
    from ledger.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def member(client):
    """Creates household members through the API and deletes them afterwards."""
    made = []

    async def make(name: str, email: str | None, can_sign_in: bool = True) -> dict:
        body = {"name": name, "initials": name[:2].upper(), "email": email, "can_sign_in": can_sign_in}
        r = await client.post("/api/members", json=body)
        assert r.status_code == 201, r.text
        made.append(r.json()["id"])
        return r.json()

    yield make
    for obj_id in made:
        await client.delete(f"/api/members/{obj_id}")


async def start(c: httpx.AsyncClient) -> dict[str, str]:
    r = await c.get("/api/auth/google/start")
    assert r.status_code == 303
    url = urlsplit(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == auth.GOOGLE_AUTH_URL
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q["client_id"] == "client-123.apps.googleusercontent.com"
    assert q["redirect_uri"] == CALLBACK
    assert q["scope"] == "openid email"
    return q


async def sign_in(c, monkeypatch, email: str, verified: bool = True, nonce: str | None = None) -> str:
    """Runs the redirect flow with Google's side faked; returns where the callback sends the browser."""
    q = await start(c)

    async def fake_identity(code: str, redirect_uri: str) -> dict:
        assert (code, redirect_uri) == ("auth-code", CALLBACK)
        return {"email": email, "email_verified": verified, "nonce": nonce or q["nonce"]}

    monkeypatch.setattr(auth, "_google_identity", fake_identity)
    r = await c.get("/api/auth/google/callback", params={"code": "auth-code", "state": q["state"]})
    assert r.status_code == 303
    return r.headers["location"]


async def test_not_configured(anon):
    assert (await anon.get("/api/auth/google/start")).status_code == 404
    assert (await anon.get("/api/auth/me")).json()["google"] is False
    r = await anon.get("/api/auth/google/callback", params={"code": "x", "state": "y"})
    assert r.headers["location"] == "/?login_error=not_configured"


async def test_member_signs_in(google, anon, member, monkeypatch):
    ann = await member("GA Ann", "ga-ann@example.com")
    assert (await anon.get("/api/auth/me")).json() == {"authenticated": False, "member": None, "google": True}

    assert await sign_in(anon, monkeypatch, "GA-Ann@Example.com") == "/"
    me = (await anon.get("/api/auth/me")).json()
    assert me["authenticated"] is True
    assert me["member"] == {"id": ann["id"], "name": "GA Ann", "initials": "GA"}
    assert (await anon.get("/api/transactions")).status_code == 200

    await anon.post("/api/auth/logout")
    assert (await anon.get("/api/transactions")).status_code == 401


@pytest.mark.parametrize(
    "email, verified, can_sign_in, error",
    [
        ("ga-nobody@example.com", True, True, "not_allowed"),
        ("ga-bob@example.com", True, False, "not_allowed"),
        ("ga-bob@example.com", False, True, "unverified"),
    ],
)
async def test_refused(google, anon, member, monkeypatch, email, verified, can_sign_in, error):
    await member("GA Bob", "ga-bob@example.com", can_sign_in)
    assert await sign_in(anon, monkeypatch, email, verified) == f"/?login_error={error}"
    assert (await anon.get("/api/auth/me")).json()["authenticated"] is False


async def test_nonce_must_match(google, anon, member, monkeypatch):
    await member("GA Cat", "ga-cat@example.com")
    assert await sign_in(anon, monkeypatch, "ga-cat@example.com", nonce="replayed") == "/?login_error=google_failed"


async def test_state_is_checked_and_single_use(google, anon, member, monkeypatch):
    await member("GA Dee", "ga-dee@example.com")
    q = await start(anon)
    r = await anon.get("/api/auth/google/callback", params={"code": "auth-code", "state": "forged"})
    assert r.headers["location"] == "/?login_error=expired"
    # The forged attempt used up the pending state, so the real one no longer works either.
    r = await anon.get("/api/auth/google/callback", params={"code": "auth-code", "state": q["state"]})
    assert r.headers["location"] == "/?login_error=expired"

    q = await start(anon)
    r = await anon.get("/api/auth/google/callback", params={"error": "access_denied", "state": q["state"]})
    assert r.headers["location"] == "/?login_error=cancelled"


async def test_shared_email_is_ambiguous(google, anon, client, member, monkeypatch):
    from ledger.db.engine import get_sessionmaker
    from ledger.models import HouseholdMember

    await member("GA Eve", "ga-eve@example.com")
    # The API refuses a second sign-in member with the same email, so add one behind its back.
    async with get_sessionmaker()() as s:
        twin = HouseholdMember(name="GA Eve Twin", initials="GT", email="GA-EVE@example.com", can_sign_in=True)
        s.add(twin)
        await s.commit()
    try:
        assert await sign_in(anon, monkeypatch, "ga-eve@example.com") == "/?login_error=ambiguous"
    finally:
        await client.delete(f"/api/members/{twin.id}")


async def test_member_api_guards_sign_in_emails(client, member):
    await member("GA Fay", "ga-fay@example.com")
    r = await client.post(
        "/api/members", json={"name": "GA Fay 2", "initials": "F2", "email": "GA-Fay@example.com", "can_sign_in": True}
    )
    assert r.status_code == 409
    assert "GA Fay" in r.json()["detail"]
    r = await client.post("/api/members", json={"name": "GA Gus", "initials": "GG", "email": " ", "can_sign_in": True})
    assert r.status_code == 422
    # The same email is fine for a member who only receives questions.
    other = await member("GA Fay 3", "ga-fay@example.com", can_sign_in=False)
    assert other["can_sign_in"] is False


@pytest.mark.parametrize("change", ["untick", "new_email", "delete"])
async def test_revoking_ends_session(google, anon, client, monkeypatch, change):
    r = await client.post(
        "/api/members", json={"name": "GA Hal", "initials": "GH", "email": "ga-hal@example.com", "can_sign_in": True}
    )
    hal = r.json()
    try:
        assert await sign_in(anon, monkeypatch, "ga-hal@example.com") == "/"
        assert (await anon.get("/api/transactions")).status_code == 200

        if change == "delete":
            assert (await client.delete(f"/api/members/{hal['id']}")).status_code == 204
        else:
            body = {
                **hal,
                "can_sign_in": change != "untick",
                "email": "ga-hal2@example.com" if change == "new_email" else hal["email"],
            }
            assert (await client.put(f"/api/members/{hal['id']}", json=body)).status_code == 200

        assert (await anon.get("/api/transactions")).status_code == 401
        assert (await anon.get("/api/auth/me")).json()["authenticated"] is False
        # The password session that made the change is unaffected.
        assert (await client.get("/api/transactions")).status_code == 200
    finally:
        await client.delete(f"/api/members/{hal['id']}")


@pytest.mark.parametrize("outcome", ["ok", "http_error", "bad_token"])
async def test_token_exchange(google, monkeypatch, outcome):
    from google.oauth2 import id_token

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == auth.GOOGLE_TOKEN_URL
        form = parse_qs(request.content.decode())
        assert form["code"] == ["auth-code"] and form["client_secret"] == ["shh"]
        assert form["redirect_uri"] == [CALLBACK] and form["grant_type"] == ["authorization_code"]
        if outcome == "http_error":
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"id_token": "signed.jwt"})

    def verify(token, request, audience):
        assert (token, audience) == ("signed.jwt", "client-123.apps.googleusercontent.com")
        if outcome == "bad_token":
            raise ValueError("Token expired")
        return {"email": "x@example.com"}

    real_client = httpx.AsyncClient
    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(id_token, "verify_oauth2_token", verify)
    if outcome == "ok":
        assert await auth._google_identity("auth-code", CALLBACK) == {"email": "x@example.com"}
    else:
        with pytest.raises(auth.GoogleSignInError) as exc:
            await auth._google_identity("auth-code", CALLBACK)
        assert exc.value.code == "google_failed"
