from __future__ import annotations

import io
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse

from PIL import Image


def login(
    client,
    email: str = "user@example.com",
    password: str = "password123",
    username: str | None = None,
):
    del password
    normalized_email = email.strip().lower()
    profile = {
        "sub": hashlib.sha256(normalized_email.encode()).hexdigest()[:20],
        "preferred_username": username or normalized_email.split("@", 1)[0],
        "email": normalized_email,
    }
    encoded_profile = base64.urlsafe_b64encode(
        json.dumps(profile).encode("utf-8")
    ).decode("ascii").rstrip("=")

    start = client.get("/auth/gitea/login", follow_redirects=False)
    assert start.status_code == 307, start.text
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    callback = client.get(
        "/auth/gitea/callback",
        params={"code": encoded_profile, "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    fragment = parse_qs(urlparse(callback.headers["location"]).fragment)
    return fragment["access_token"][0]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def image_bytes(
    size: tuple[int, int] = (32, 32),
    color: tuple[int, int, int, int] = (255, 0, 0, 255),
    fmt: str = "PNG",
) -> bytes:
    image = Image.new("RGBA", size, color)
    if fmt == "JPEG":
        image = image.convert("RGB")
    output = io.BytesIO()
    image.save(output, format=fmt)
    return output.getvalue()


def upload_sprite(
    client,
    token: str,
    name: str,
    tags: str = "",
    content: bytes | None = None,
    filename: str = "sprite.png",
    content_type: str = "image/png",
    image_mode: str | None = None,
    trim_transparent: bool | None = None,
    focus_x: float | None = None,
    focus_y: float | None = None,
):
    data = {"name": name, "tags": tags}
    if image_mode is not None:
        data["image_mode"] = image_mode
    if trim_transparent is not None:
        data["trim_transparent"] = "true" if trim_transparent else "false"
    if focus_x is not None:
        data["focus_x"] = str(focus_x)
    if focus_y is not None:
        data["focus_y"] = str(focus_y)
    return client.post(
        "/sprites",
        headers=auth(token),
        data=data,
        files={"file": (filename, content or image_bytes(), content_type)},
    )
