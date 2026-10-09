from __future__ import annotations

from datetime import datetime

from app.models import FavoriteFolderSprite, Sprite
from tests.helpers import auth, login, upload_sprite


def setup_account(client, email="owner@example.com", username="owner"):
    return login(client, email=email, username=username)


def test_profile_update_and_mine_sprites(client):
    token = setup_account(client)
    sprite = upload_sprite(client, token, "owned", "player").json()

    me = client.get("/users/me", headers=auth(token))
    assert me.status_code == 200
    assert me.json()["username"] == "owner"

    updated = client.patch(
        "/users/me",
        headers=auth(token),
        json={"username": "New Owner"},
    )
    assert updated.status_code == 200
    assert updated.json()["username"] == "New Owner"

    listing = client.get("/sprites", params={"mine": "true"}, headers=auth(token))
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["items"]] == [sprite["id"]]
    assert listing.json()["items"][0]["owner_name"] == "New Owner"
    assert client.get("/sprites", params={"mine": "true"}).status_code == 401


def test_username_conflict_is_case_insensitive(client):
    first = setup_account(client, "first@example.com", "Artist")
    setup_account(client, "second@example.com", "Other")
    conflict = client.patch(
        "/users/me",
        headers=auth(first),
        json={"username": "artist"},
    )
    assert conflict.status_code == 200

    second = login(client, "second@example.com")
    conflict = client.patch(
        "/users/me",
        headers=auth(second),
        json={"username": "ARTIST"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "USERNAME_ALREADY_REGISTERED"


def test_favorite_folders_are_private_and_support_multiple_memberships(client):
    owner_token = setup_account(client)
    sprite = upload_sprite(client, owner_token, "shared").json()
    first = client.post(
        "/favorites/folders",
        headers=auth(owner_token),
        json={"name": "角色"},
    ).json()
    second = client.post(
        "/favorites/folders",
        headers=auth(owner_token),
        json={"name": "常用"},
    ).json()

    membership = client.put(
        f"/favorites/sprites/{sprite['id']}",
        headers=auth(owner_token),
        json={"folder_ids": [first["id"], second["id"]]},
    )
    assert membership.status_code == 200
    assert membership.json()["folder_ids"] == [first["id"], second["id"]]
    assert (
        client.get(
            f"/favorites/folders/{first['id']}",
            headers=auth(owner_token),
        ).json()["sprites"][0]["id"]
        == sprite["id"]
    )

    other_token = setup_account(client, "other@example.com", "other")
    hidden = client.get(
        f"/favorites/folders/{first['id']}",
        headers=auth(other_token),
    )
    assert hidden.status_code == 404
    invalid_membership = client.put(
        f"/favorites/sprites/{sprite['id']}",
        headers=auth(other_token),
        json={"folder_ids": [first["id"]]},
    )
    assert invalid_membership.status_code == 422
    assert invalid_membership.json()["error"]["details"][0]["code"] == "FOLDER_IDS_NOT_FOUND"


def test_folder_name_limit_and_folder_count_limit(client):
    token = setup_account(client)
    first = client.post(
        "/favorites/folders",
        headers=auth(token),
        json={"name": "Enemy"},
    )
    assert first.status_code == 201
    duplicate = client.post(
        "/favorites/folders",
        headers=auth(token),
        json={"name": "enemy"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "FAVORITE_FOLDER_NAME_CONFLICT"

    for index in range(2, 6):
        response = client.post(
            "/favorites/folders",
            headers=auth(token),
            json={"name": f"folder-{index}"},
        )
        assert response.status_code == 201
    overflow = client.post(
        "/favorites/folders",
        headers=auth(token),
        json={"name": "folder-6"},
    )
    assert overflow.status_code == 422
    assert overflow.json()["error"]["code"] == "FAVORITE_FOLDER_LIMIT_REACHED"


def test_folder_capacity_and_sprite_delete_cascade(client, db_session):
    token = setup_account(client)
    folder = client.post(
        "/favorites/folders",
        headers=auth(token),
        json={"name": "滿收藏"},
    ).json()
    user_id = client.get("/users/me", headers=auth(token)).json()["id"]
    sprites = [
        Sprite(
            name=f"sprite-{index}",
            tags="",
            image_data=b"\x00" * 4096,
            owner_id=user_id,
        )
        for index in range(101)
    ]
    db_session.add_all(sprites)
    db_session.flush()
    db_session.add_all(
        [
            FavoriteFolderSprite(
                folder_id=folder["id"],
                sprite_id=sprite.id,
                created_at=datetime(2026, 6, 25, 0, 0, index % 60),
            )
            for index, sprite in enumerate(sprites[:100])
        ]
    )
    db_session.commit()

    full = client.put(
        f"/favorites/sprites/{sprites[100].id}",
        headers=auth(token),
        json={"folder_ids": [folder["id"]]},
    )
    assert full.status_code == 422
    assert full.json()["error"]["code"] == "FAVORITE_FOLDER_FULL"

    delete_response = client.delete(
        f"/sprites/{sprites[0].id}",
        headers=auth(token),
    )
    assert delete_response.status_code == 204
    detail = client.get(
        f"/favorites/folders/{folder['id']}",
        headers=auth(token),
    )
    assert detail.json()["sprite_count"] == 99


def test_replacing_membership_rejects_duplicates_without_partial_update(client):
    token = setup_account(client)
    sprite = upload_sprite(client, token, "sprite").json()
    folder = client.post(
        "/favorites/folders",
        headers=auth(token),
        json={"name": "folder"},
    ).json()
    duplicate = client.put(
        f"/favorites/sprites/{sprite['id']}",
        headers=auth(token),
        json={"folder_ids": [folder["id"], folder["id"]]},
    )
    assert duplicate.status_code == 422
    membership = client.get(
        f"/favorites/sprites/{sprite['id']}",
        headers=auth(token),
    )
    assert membership.json()["folder_ids"] == []
