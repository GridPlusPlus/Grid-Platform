from __future__ import annotations

import io

from PIL import Image

from tests.helpers import auth, image_bytes, login, upload_sprite


def setup_user(client):
    return login(client)


def test_upload_normalizes_name_tags_and_scales_to_rgba(client):
    token = setup_user(client)
    response = upload_sprite(
        client,
        token,
        "  幽靈  ",
        " Player, player ,CHARACTER ",
        image_bytes((64, 32), (255, 0, 0, 255)),
        image_mode="fit",
    )
    assert response.status_code == 201, response.text
    sprite = response.json()
    assert sprite["name"] == "幽靈"
    assert sprite["tags"] == "player,character"
    assert sprite["owner_name"] == "user"

    raw = client.get(sprite["image_url"])
    assert raw.status_code == 200
    assert raw.headers["content-type"] == "application/octet-stream"
    assert raw.headers["content-length"] == "4096"
    assert len(raw.content) == 4096
    pixels = raw.content
    assert pixels[:4] == bytes((0, 0, 0, 0))
    center_start = (8 * 32) * 4
    assert pixels[center_start:center_start + 4] == bytes((255, 0, 0, 255))
    bottom_start = (24 * 32) * 4
    assert pixels[bottom_start:bottom_start + 4] == bytes((0, 0, 0, 0))


def test_image_declarations_must_match_and_extra_form_is_rejected(client):
    token = setup_user(client)
    mismatch = upload_sprite(
        client,
        token,
        "bad",
        content=image_bytes(),
        filename="sprite.jpg",
        content_type="image/jpeg",
    )
    assert mismatch.status_code == 415
    assert mismatch.json()["error"]["code"] == "INVALID_IMAGE_FORMAT"

    extra = client.post(
        "/sprites",
        headers=auth(token),
        data={"name": "valid", "tags": "", "unexpected": "x"},
        files={"file": ("sprite.png", image_bytes(), "image/png")},
    )
    assert extra.status_code == 422
    assert extra.json()["error"]["details"][0]["code"] == "EXTRA_FIELD"


def test_png_transparent_border_is_trimmed_without_losing_small_content(client):
    token = setup_user(client)
    source = Image.new("RGBA", (1200, 1200), (0, 0, 0, 0))
    source.paste((255, 0, 0, 255), (584, 584, 616, 616))
    output = io.BytesIO()
    source.save(output, format="PNG")

    trimmed = upload_sprite(client, token, "trimmed", content=output.getvalue())
    assert trimmed.status_code == 201, trimmed.text
    trimmed_pixels = client.get(trimmed.json()["image_url"]).content
    trimmed_alpha = trimmed_pixels[3::4]
    assert sum(alpha > 0 for alpha in trimmed_alpha) == 32 * 32

    original_canvas = upload_sprite(
        client,
        token,
        "not trimmed",
        content=output.getvalue(),
        trim_transparent=False,
    )
    assert original_canvas.status_code == 201, original_canvas.text
    original_pixels = client.get(original_canvas.json()["image_url"]).content
    assert sum(alpha > 0 for alpha in original_pixels[3::4]) < 10


def test_pixel_mode_recovers_enlarged_grid_and_supports_focus_crop(client):
    token = setup_user(client)
    logical = Image.new("RGBA", (39, 39), (0, 0, 0, 255))
    for y in range(39):
        for x in range(39):
            logical.putpixel(
                (x, y),
                ((x * 37) % 256, (y * 53) % 256, ((x + y) * 29) % 256, 255),
            )
    for x in range(39):
        logical.putpixel((x, 19), (255, 0, 0, 255))
    logical.putpixel((0, 18), (0, 255, 0, 255))
    logical.putpixel((38, 20), (0, 0, 255, 255))
    enlarged = logical.resize((510, 510), Image.Resampling.NEAREST)
    output = io.BytesIO()
    enlarged.save(output, format="PNG")

    preview = client.post(
        "/sprites/preview",
        headers=auth(token),
        data={
            "image_mode": "pixel",
            "trim_transparent": "false",
            "focus_x": "0",
            "focus_y": "0.5",
        },
        files={"file": ("sprite.png", output.getvalue(), "image/png")},
    )
    assert preview.status_code == 200, preview.text
    assert preview.headers["x-logical-width"] == "39"
    assert preview.headers["x-logical-height"] == "39"
    assert preview.headers["x-max-crop-x"] == "7"
    assert preview.headers["x-max-crop-y"] == "7"
    assert preview.headers["x-pixel-grid-detected"] == "true"
    assert preview.content[(14 * 32) * 4 : (14 * 32) * 4 + 4] == bytes(
        (0, 255, 0, 255)
    )

    left = upload_sprite(
        client,
        token,
        "left crop",
        content=output.getvalue(),
        image_mode="pixel",
        trim_transparent=False,
        focus_x=0,
        focus_y=0.5,
    )
    assert left.status_code == 201, left.text
    assert client.get(left.json()["image_url"]).content == preview.content

    right = upload_sprite(
        client,
        token,
        "right crop",
        content=output.getvalue(),
        image_mode="pixel",
        trim_transparent=False,
        focus_x=1,
        focus_y=0.5,
    )
    assert right.status_code == 201, right.text
    right_pixels = client.get(right.json()["image_url"]).content
    blue_index = 16 * 32 * 4 + 31 * 4
    assert right_pixels[blue_index : blue_index + 4] == bytes((0, 0, 255, 255))


def test_image_mode_and_focus_validation(client):
    token = setup_user(client)
    invalid_mode = upload_sprite(
        client,
        token,
        "bad mode",
        image_mode="unknown",
    )
    assert invalid_mode.status_code == 422
    assert invalid_mode.json()["error"]["details"][0]["code"] == "INVALID_IMAGE_MODE"

    invalid_focus = upload_sprite(
        client,
        token,
        "bad focus",
        focus_x=1.5,
    )
    assert invalid_focus.status_code == 422
    assert invalid_focus.json()["error"]["details"][0]["code"] == "INVALID_IMAGE_FOCUS"


def test_name_tag_validation_and_file_limit(client):
    token = setup_user(client)
    invalid = upload_sprite(client, token, "bad\nname", "a,,b")
    assert invalid.status_code == 422
    codes = {item["code"] for item in invalid.json()["error"]["details"]}
    assert {"NAME_CONTROL_CHARACTER", "TAG_REQUIRED"} <= codes

    too_large = upload_sprite(
        client,
        token,
        "large",
        content=b"x" * (5 * 1024 * 1024 + 1),
    )
    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_search_escapes_like_wildcards_and_stable_pagination(client):
    token = setup_user(client)
    assert upload_sprite(client, token, "100% hero", "enemy").status_code == 201
    assert upload_sprite(client, token, "plain hero", "enemy").status_code == 201
    assert upload_sprite(client, token, "under_score", "friend").status_code == 201

    percent = client.get("/sprites", params={"name": "%", "sort": "name_asc"})
    assert [item["name"] for item in percent.json()["items"]] == ["100% hero"]
    underscore = client.get("/sprites", params={"name": "_"})
    assert [item["name"] for item in underscore.json()["items"]] == ["under_score"]
    by_id = client.get(
        "/sprites",
        params={"id": upload_sprite(client, token, "exact id", "utility").json()["id"]},
    )
    assert [item["name"] for item in by_id.json()["items"]] == ["exact id"]
    missing_id = client.get("/sprites", params={"id": 99999})
    assert missing_id.status_code == 200
    assert missing_id.json()["items"] == []
    beyond = client.get("/sprites", params={"page": 9, "page_size": 2})
    assert beyond.status_code == 200
    assert beyond.json()["items"] == []

    extra = client.get("/sprites", params={"unknown": "x"})
    assert extra.status_code == 422


def test_multi_tag_and_or_search(client):
    token = setup_user(client)
    upload_sprite(client, token, "both", "enemy,character")
    upload_sprite(client, token, "enemy", "enemy")
    upload_sprite(client, token, "friend", "friend")
    and_result = client.get(
        "/sprites", params={"tags": "enemy,character", "tag_mode": "and"}
    )
    assert [item["name"] for item in and_result.json()["items"]] == ["both"]
    or_result = client.get(
        "/sprites", params={"tags": "character,friend", "tag_mode": "or", "sort": "name_asc"}
    )
    assert [item["name"] for item in or_result.json()["items"]] == ["both", "friend"]


def test_only_owner_can_delete_sprite(client):
    token = setup_user(client)
    sprite = upload_sprite(client, token, "owned").json()
    other = login(client, "other@example.com")
    forbidden = client.delete(f"/sprites/{sprite['id']}", headers=auth(other))
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "SPRITE_FORBIDDEN"
