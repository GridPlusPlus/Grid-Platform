from __future__ import annotations

import sqlite3

from tests.helpers import auth, login, upload_sprite


def setup_sprites(client):
    token = login(client)
    sprites = [
        upload_sprite(client, token, name, "character").json()
        for name in ("alpha", "beta", "gamma")
    ]
    return token, sprites


def test_create_update_list_and_export_pack(client, tmp_path):
    token, sprites = setup_sprites(client)
    created = client.post(
        "/packs",
        headers=auth(token),
        json={"name": " 角色素材 ", "sprite_ids": [sprites[2]["id"], sprites[0]["id"]]},
    )
    assert created.status_code == 201, created.text
    pack = created.json()
    assert [item["position"] for item in pack["sprites"]] == [0, 1]
    assert [item["id"] for item in pack["sprites"]] == [sprites[2]["id"], sprites[0]["id"]]

    listing = client.get("/packs", headers=auth(token), params={"mine": "true"})
    assert listing.status_code == 200
    assert listing.json()["items"][0]["sprite_count"] == 2

    updated = client.patch(
        f"/packs/{pack['id']}",
        headers=auth(token),
        json={"name": "新版", "sprite_ids": [sprites[1]["id"]]},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "新版"
    assert [item["id"] for item in updated.json()["sprites"]] == [sprites[1]["id"]]

    exported = client.get(f"/packs/{pack['id']}/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/octet-stream"
    assert "filename=\"assets.db\"" in exported.headers["content-disposition"]
    assert exported.content.startswith(b"SQLite format 3\x00")

    output = tmp_path / "exported.db"
    output.write_bytes(exported.content)
    with sqlite3.connect(output) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        assert tables == [("sprites",)]
        columns = connection.execute("PRAGMA table_info(sprites)").fetchall()
        assert [(column[1], column[2], column[5]) for column in columns] == [
            ("id", "INTEGER", 1),
            ("name", "TEXT", 0),
            ("tags", "TEXT", 0),
            ("image_data", "BLOB", 0),
        ]
        rows = connection.execute(
            "SELECT id, name, tags, length(image_data) FROM sprites ORDER BY id"
        ).fetchall()
        assert rows == [(1, "beta", "character", 4096)]


def test_duplicate_and_missing_sprite_ids_roll_back(client):
    token, sprites = setup_sprites(client)
    duplicate = client.post(
        "/packs",
        headers=auth(token),
        json={"name": "bad", "sprite_ids": [sprites[0]["id"], sprites[0]["id"]]},
    )
    assert duplicate.status_code == 422
    assert duplicate.json()["error"]["details"][0]["code"] == "DUPLICATE_SPRITE_IN_PACK"

    missing = client.post(
        "/packs",
        headers=auth(token),
        json={"name": "bad", "sprite_ids": [99999]},
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["details"][0]["code"] == "SPRITE_IDS_NOT_FOUND"
    assert client.get("/packs").json()["pagination"]["total_items"] == 0


def test_empty_patch_is_400_and_non_owner_is_forbidden(client):
    token, _sprites = setup_sprites(client)
    pack = client.post(
        "/packs", headers=auth(token), json={"name": "pack", "sprite_ids": []}
    ).json()
    empty = client.patch(f"/packs/{pack['id']}", headers=auth(token), json={})
    assert empty.status_code == 400

    other = login(client, "other@example.com")
    forbidden = client.patch(
        f"/packs/{pack['id']}", headers=auth(other), json={"name": "stolen"}
    )
    assert forbidden.status_code == 403


def test_deleting_sprite_reindexes_every_affected_pack(client):
    token, sprites = setup_sprites(client)
    first = client.post(
        "/packs",
        headers=auth(token),
        json={"name": "first", "sprite_ids": [item["id"] for item in sprites]},
    ).json()
    second = client.post(
        "/packs",
        headers=auth(token),
        json={"name": "second", "sprite_ids": [sprites[1]["id"], sprites[2]["id"]]},
    ).json()

    deleted = client.delete(f"/sprites/{sprites[1]['id']}", headers=auth(token))
    assert deleted.status_code == 204, deleted.text
    first_after = client.get(f"/packs/{first['id']}").json()
    second_after = client.get(f"/packs/{second['id']}").json()
    assert [(item["position"], item["id"]) for item in first_after["sprites"]] == [
        (0, sprites[0]["id"]),
        (1, sprites[2]["id"]),
    ]
    assert [(item["position"], item["id"]) for item in second_after["sprites"]] == [
        (0, sprites[2]["id"])
    ]


def test_mine_requires_token_and_invalid_optional_token_is_rejected(client):
    assert client.get("/packs", params={"mine": "true"}).status_code == 401
    invalid = client.get(
        "/packs",
        params={"mine": "false"},
        headers={"Authorization": "Bearer invalid"},
    )
    assert invalid.status_code == 401
