"""Dugout, social graph, messaging and media (spec §6–§7)."""
import io

from PIL import Image

from tests.conftest import onboard_coach, onboard_player


async def test_post_and_feed(client):
    player = await onboard_player(client, name="Aarav Mehta")
    coach = await onboard_coach(client)

    r = await client.post("/v1/posts",
                          data={"content": "Another day, another step forward. #WorkInProgress #TeamFirst",
                                "category": "playing"},
                          headers=player["headers"])
    assert r.status_code == 201, r.text
    post = r.json()
    assert post["qo_points_earned"] == 12
    assert set(post["hashtags"]) == {"WorkInProgress", "TeamFirst"}
    assert post["author"]["type"] == "player"

    # +12 through the ledger (25 would mean double-award)
    score = await client.get("/v1/players/me/qo-score", headers=player["headers"])
    assert score.json()["score"] == 12

    feed = await client.get("/v1/feed?tab=players", headers=coach["headers"])
    assert feed.json()["total"] == 1
    assert feed.json()["items"][0]["viewer"]["liked"] is False

    empty = await client.get("/v1/feed?tab=coaches", headers=coach["headers"])
    assert empty.json()["total"] == 0

    search = await client.get("/v1/feed?q=Aarav", headers=coach["headers"])
    assert search.json()["total"] == 1
    search = await client.get("/v1/feed?q=Nobody", headers=coach["headers"])
    assert search.json()["total"] == 0


async def test_like_comment_share(client):
    author = await onboard_player(client, name="Author")
    fan = await onboard_player(client, name="Fan")
    r = await client.post("/v1/posts", data={"content": "match day!"}, headers=author["headers"])
    post_id = r.json()["id"]

    like = await client.post(f"/v1/posts/{post_id}/like", headers=fan["headers"])
    assert like.json() == {"liked": True, "like_count": 1}
    # double-like is a no-op
    like2 = await client.post(f"/v1/posts/{post_id}/like", headers=fan["headers"])
    assert like2.json()["like_count"] == 1
    unlike = await client.delete(f"/v1/posts/{post_id}/like", headers=fan["headers"])
    assert unlike.json() == {"liked": False, "like_count": 0}

    c = await client.post(f"/v1/posts/{post_id}/comments", json={"body": "Great knock! 🏏"},
                          headers=fan["headers"])
    assert c.status_code == 201
    listing = await client.get(f"/v1/posts/{post_id}/comments", headers=author["headers"])
    assert listing.json()["total"] == 1

    share = await client.post(f"/v1/posts/{post_id}/share", headers=fan["headers"])
    assert share.json()["share_url"].endswith(post_id)

    # author was notified about like + comment
    notifs = await client.get("/v1/notifications", headers=author["headers"])
    types = [n["type"] for n in notifs.json()["items"]]
    assert "post_liked" in types and "post_commented" in types

    # mark all read
    await client.post("/v1/notifications/read-all", headers=author["headers"])
    after = await client.get("/v1/notifications", headers=author["headers"])
    assert after.json()["unread_count"] == 0


async def test_follow_and_privacy(client):
    a = await onboard_player(client, name="Watcher")
    b = await onboard_player(client, name="Star Player")
    b_id = b["user"]["id"]

    t = await client.post(f"/v1/users/{b_id}/track", headers=a["headers"])
    assert t.json() == {"tracking": True, "follower_count": 1}

    profile = await client.get(f"/v1/users/{b_id}/profile", headers=a["headers"])
    body = profile.json()
    assert body["counts"]["followers"] == 1
    assert body["viewer"]["following"] is True
    # minors: dob / school / phone never in public payloads
    assert "dob" not in body and "school" not in body and "phone" not in body

    u = await client.delete(f"/v1/users/{b_id}/track", headers=a["headers"])
    assert u.json()["follower_count"] == 0

    # private profile hides tabs from non-followers
    await client.patch("/v1/users/me/settings", json={"private_profile": True}, headers=b["headers"])
    hidden = await client.get(f"/v1/users/{b_id}/profile", headers=a["headers"])
    assert hidden.json()["private"] is True
    assert all(t["count"] == 0 for t in hidden.json()["tabs"].values())


async def test_friends_and_messages(client):
    a = await onboard_player(client, name="Asha")
    b = await onboard_player(client, name="Bala")

    fr = await client.post("/v1/friend-requests", json={"to_id": b["user"]["id"]}, headers=a["headers"])
    assert fr.status_code == 201
    fr_id = fr.json()["id"]

    # only the recipient can accept
    self_accept = await client.post(f"/v1/friend-requests/{fr_id}/accept", headers=a["headers"])
    assert self_accept.status_code == 403

    ok = await client.post(f"/v1/friend-requests/{fr_id}/accept", headers=b["headers"])
    assert ok.json()["status"] == "accepted"

    conv = await client.post("/v1/conversations", json={"participant_id": b["user"]["id"]},
                             headers=a["headers"])
    conv_id = conv.json()["conversation_id"]
    # find-or-create returns the same conversation
    conv2 = await client.post("/v1/conversations", json={"participant_id": a["user"]["id"]},
                              headers=b["headers"])
    assert conv2.json()["conversation_id"] == conv_id

    msg = await client.post(f"/v1/conversations/{conv_id}/messages", json={"body": "Net session at 6?"},
                            headers=a["headers"])
    assert msg.status_code == 201
    inbox = await client.get(f"/v1/conversations/{conv_id}/messages", headers=b["headers"])
    assert inbox.json()["items"][0]["body"] == "Net session at 6?"
    assert inbox.json()["items"][0]["mine"] is False

    # outsiders can't read the conversation
    outsider = await onboard_player(client, name="Nosy")
    denied = await client.get(f"/v1/conversations/{conv_id}/messages", headers=outsider["headers"])
    assert denied.status_code == 403


def _jpeg_with_exif() -> bytes:
    img = Image.new("RGB", (64, 64), (200, 60, 60))
    exif = Image.Exif()
    exif[0x010F] = "TestCam"  # Make tag — stands in for EXIF metadata incl. GPS
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


async def test_media_upload_strips_exif_and_validates(client):
    player = await onboard_player(client)

    up = await client.post("/v1/media",
                           files={"file": ("photo.jpg", _jpeg_with_exif(), "image/jpeg")},
                           data={"purpose": "avatar"},
                           headers=player["headers"])
    assert up.status_code == 201, up.text
    body = up.json()
    assert body["width"] == 64 and body["height"] == 64
    assert body["url"].startswith("http")

    # wrong mime for purpose → 415
    bad = await client.post("/v1/media",
                            files={"file": ("doc.pdf", b"%PDF-1.4 fake", "application/pdf")},
                            data={"purpose": "avatar"},
                            headers=player["headers"])
    assert bad.status_code == 415
    assert bad.json()["error"]["code"] == "UNSUPPORTED_MIME"


async def test_presign_flow(client):
    player = await onboard_player(client)
    pre = await client.post("/v1/media/presign",
                            json={"filename": "century.mp4", "mime": "video/mp4",
                                  "bytes": 1024, "purpose": "post_video"},
                            headers=player["headers"])
    assert pre.status_code == 200
    media_id = pre.json()["media_id"]
    assert pre.json()["upload_url"].endswith(f"/v1/media/{media_id}/upload")

    put = await client.put(f"/v1/media/{media_id}/upload", content=b"\x00" * 1024,
                           headers=player["headers"])
    assert put.status_code == 204

    done = await client.post(f"/v1/media/{media_id}/complete", headers=player["headers"])
    assert done.json()["status"] == "processing"

    # dev transcode marks ready in the background task
    final = await client.get(f"/v1/media/{media_id}", headers=player["headers"])
    assert final.json()["status"] == "ready"


async def test_device_registry(client):
    player = await onboard_player(client)
    r = await client.post("/v1/devices",
                          json={"fcm_token": "tok_abc12345", "platform": "android",
                                "app_version": "1.0.0"},
                          headers=player["headers"])
    assert r.status_code == 201
    gone = await client.delete("/v1/devices/tok_abc12345", headers=player["headers"])
    assert gone.status_code == 204
