"""Player onboarding, ID allocation and Qo Score plumbing."""
from tests.conftest import onboard_coach, onboard_player, register


async def test_player_id_sequence(client):
    a = await onboard_player(client, name="Player One")
    b = await onboard_player(client, name="Player Two")
    assert a["player_id"].startswith("P")
    assert len(a["player_id"]) == 6  # P{YY}{NNN}
    year = a["player_id"][1:3]
    assert a["player_id"] == f"P{year}001"
    assert b["player_id"] == f"P{year}002"


async def test_sport_selection_idempotent(client):
    acct = await onboard_player(client)
    r = await client.post("/v1/users/me/sport", json={"sport": "Cricket"}, headers=acct["headers"])
    assert r.status_code == 201
    assert r.json()["player_id"] == acct["player_id"]  # same ID, no re-mint


async def test_age_group_derived_from_dob(client):
    acct = await onboard_player(client, dob="2011-03-02")
    me = await client.get("/v1/users/me", headers=acct["headers"])
    assert me.json()["profile"]["age_group"] in ("U16", "U19")  # depends on current date
    assert me.json()["onboarding_stage"] == "complete"


async def test_card_tiers_config(client):
    r = await client.get("/v1/config/card-tiers")
    assert r.status_code == 200
    tiers = r.json()["tiers"]
    assert len(tiers) == 8
    assert tiers[0] == {"level": 1, "tier": "purple", "label": "Purple Card",
                        "threshold": 1000, "hex": "#7B2FFF"}
    assert tiers[-1]["tier"] == "golden_pro"
    assert tiers[-1]["threshold"] == 100000


async def test_cricket_types_config(client):
    r = await client.get("/v1/config/cricket-types")
    values = [t["value"] for t in r.json()["types"]]
    assert values == ["gully", "professional", "box", "tennis_ball", "hard_ball", "corporate", "beach"]


async def test_qo_score_card_shape(client):
    acct = await onboard_player(client)
    r = await client.get("/v1/players/me/qo-score", headers=acct["headers"])
    assert r.status_code == 200
    body = r.json()
    assert body["score"] == 0
    assert body["card"]["tier"] == "purple"
    assert body["card"]["next_tier"]["label"] == "Green Card"
    assert body["card"]["next_tier"]["points_needed"] == 1000
    assert len(body["all_tiers"]) == 8
    assert body["all_tiers"][0]["unlocked"] is True
    assert body["all_tiers"][1]["unlocked"] is False


async def test_admin_point_correction_via_ledger(client):
    acct = await onboard_player(client)
    user_id = acct["user"]["id"]

    r = await client.post("/v1/admin/points/correction",
                          json={"user_id": user_id, "points": 1200, "reason": "migration backfill"},
                          headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code == 201

    score = await client.get("/v1/players/me/qo-score", headers=acct["headers"])
    body = score.json()
    assert body["score"] == 1200
    assert body["card"]["tier"] == "green"  # crossed the 1000 threshold

    # wrong admin key is rejected
    r = await client.post("/v1/admin/points/correction",
                          json={"user_id": user_id, "points": 1, "reason": "nope"},
                          headers={"X-Admin-Key": "wrong"})
    assert r.status_code == 403


async def test_player_dashboard(client):
    acct = await onboard_player(client)
    r = await client.get("/v1/players/me/dashboard", headers=acct["headers"])
    assert r.status_code == 200
    body = r.json()
    assert body["player"]["player_id"] == acct["player_id"]
    assert body["qo_score"]["current"] == 0
    assert len(body["qo_score"]["trend"]) == 8
    assert body["active_league"] is None


async def test_dashboard_requires_player_role(client):
    coach = await onboard_coach(client)
    r = await client.get("/v1/players/me/dashboard", headers=coach["headers"])
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ROLE_MISMATCH"


async def test_settings_roundtrip(client):
    acct = await register(client, "player")
    r = await client.get("/v1/users/me/settings", headers=acct["headers"])
    assert r.json()["notifications_enabled"] is True
    r = await client.patch("/v1/users/me/settings",
                           json={"dark_mode": True, "private_profile": True},
                           headers=acct["headers"])
    assert r.json()["dark_mode"] is True
    assert r.json()["private_profile"] is True
    assert r.json()["notifications_enabled"] is True
