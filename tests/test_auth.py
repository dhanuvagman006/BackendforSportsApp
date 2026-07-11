"""Auth & onboarding flow tests (spec §3)."""
from tests.conftest import register


async def test_register_and_login(client):
    acct = await register(client, "player")
    assert acct["user"]["role"] == "player"
    assert acct["user"]["onboarding_stage"] == "profile"
    assert acct["access_token"] and acct["refresh_token"]

    r = await client.post("/v1/auth/login", json={
        "email": acct["email"], "password": "Passw0rd1", "role": "player"})
    assert r.status_code == 200
    assert r.json()["user"]["onboarding_complete"] is False


async def test_register_weak_password(client):
    r = await client.post("/v1/auth/register", json={
        "full_name": "X", "email": "weak@example.com", "password": "short", "role": "player"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "WEAK_PASSWORD"
    assert r.json()["error"]["request_id"].startswith("req_")


async def test_register_email_taken(client):
    await register(client, "player", email="dup@example.com")
    r = await client.post("/v1/auth/register", json={
        "full_name": "X", "email": "DUP@example.com", "password": "Passw0rd1", "role": "player"})
    assert r.status_code == 409  # citext — case-insensitive uniqueness
    assert r.json()["error"]["code"] == "EMAIL_TAKEN"


async def test_login_role_mismatch(client):
    acct = await register(client, "player")
    r = await client.post("/v1/auth/login", json={
        "email": acct["email"], "password": "Passw0rd1", "role": "coach"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ROLE_MISMATCH"


async def test_login_bad_password(client):
    acct = await register(client, "player")
    r = await client.post("/v1/auth/login", json={
        "email": acct["email"], "password": "WrongPass1", "role": "player"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_refresh_rotation_and_reuse_detection(client):
    acct = await register(client, "player")
    old_refresh = acct["refresh_token"]

    r1 = await client.post("/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert r1.status_code == 200
    new_refresh = r1.json()["refresh_token"]
    assert new_refresh != old_refresh

    # Reusing the rotated (old) token must revoke the family
    r2 = await client.post("/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert r2.status_code == 401
    assert r2.json()["error"]["code"] == "TOKEN_REUSED"

    # …including the newest token in that family
    r3 = await client.post("/v1/auth/refresh", json={"refresh_token": new_refresh})
    assert r3.status_code == 401


async def test_otp_flow(client):
    acct = await register(client, "coach")
    r = await client.post("/v1/auth/otp/send", json={"phone": "+919876543210"},
                          headers=acct["headers"])
    assert r.status_code == 200
    body = r.json()
    assert body["masked_phone"] == "+91 98••••3210"
    assert body["expires_in"] == 300
    code = body["dev_code"]  # echoed outside production

    bad = await client.post("/v1/auth/otp/verify", json={"request_id": body["request_id"],
                                                         "code": "000000"},
                            headers=acct["headers"])
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_CODE"

    ok = await client.post("/v1/auth/otp/verify", json={"request_id": body["request_id"],
                                                        "code": code},
                           headers=acct["headers"])
    assert ok.status_code == 200
    assert ok.json()["verified"] is True
    assert ok.json()["next_step"] == "complete_coach_profile"

    me = await client.get("/v1/users/me", headers=acct["headers"])
    assert me.json()["phone"] == "+919876543210"


async def test_otp_rate_limit(client):
    for _ in range(3):
        r = await client.post("/v1/auth/otp/send", json={"phone": "+919812345678"})
        assert r.status_code == 200
    r = await client.post("/v1/auth/otp/send", json={"phone": "+919812345678"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


async def test_logout_revokes_refresh(client):
    acct = await register(client, "player")
    r = await client.post("/v1/auth/logout", json={"refresh_token": acct["refresh_token"]},
                          headers=acct["headers"])
    assert r.status_code == 204
    r = await client.post("/v1/auth/refresh", json={"refresh_token": acct["refresh_token"]})
    assert r.status_code == 401


async def test_account_soft_delete(client):
    acct = await register(client, "player")
    r = await client.delete("/v1/users/me", headers=acct["headers"])
    assert r.status_code == 202
    assert r.json()["grace_days"] == 30

    r = await client.post("/v1/auth/login", json={
        "email": acct["email"], "password": "Passw0rd1", "role": "player"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ACCOUNT_DELETED"
