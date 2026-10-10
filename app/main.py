from __future__ import annotations

import hashlib
import logging
import math
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

import ulid
from fastapi import Depends, FastAPI, File, Form, Query, Request, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import PROJECT_ROOT, get_settings
from app.database import get_db
from app.errors import ApiError, install_exception_handlers, validation_detail
from app import gitea_oauth
from app.image_processing import process_upload
from app.models import Pack, PackSprite, Sprite, User
from app.schemas import (
    ErrorResponse,
    FavoriteFolderListResponse,
    FavoriteFolderResponse,
    FavoriteMembershipResponse,
    PackListResponse,
    PackResponse,
    PublicUser,
    SpriteListResponse,
    SpriteResponse,
)
from app.security import (
    create_access_token,
    reject_revoked_token,
    revoke_access_token,
    token_from_request,
)
from app.validation import (
    FavoriteFolderCreateRequest,
    FavoriteFolderPatchRequest,
    FavoriteMembershipRequest,
    PackCreateRequest,
    PackPatchRequest,
    UserPatchRequest,
    escape_like,
    normalize_name,
    normalize_search,
    normalize_search_tags,
    normalize_tags,
    ascii_lower,
)
from app.models import FavoriteFolder, FavoriteFolderSprite


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("grid_platform")
settings = get_settings()

# Configure the HTTP application and optional development CORS.
app = FastAPI(
    title="Grid++ 素材庫平台 API",
    version="1.0.0",
    description="32×32 RGBA8888 素材及素材包管理平台",
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = f"req_{ulid.new()}"
    request.state.request_id = request_id
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled middleware exception request_id=%s", request_id)
        response = JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_SERVER_ERROR",
                    "message": "伺服器發生未預期的錯誤",
                    "details": None,
                    "request_id": request_id,
                }
            },
        )
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request_id=%s method=%s path=%s status=%s",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
    )
    return response


install_exception_handlers(app)


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok"}


# Reuse the same documented error responses across endpoints.
COMMON_ERRORS = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    415: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
    502: {"model": ErrorResponse},
}


def iso_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def require_json_content_type(request: Request) -> None:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE")


def reject_extra_query(request: Request, allowed: set[str]) -> None:
    extras = sorted(set(request.query_params.keys()) - allowed)
    if extras:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            details=[validation_detail(name, "EXTRA_FIELD") for name in extras],
        )


async def reject_extra_form(request: Request, allowed: set[str]) -> None:
    form = await request.form()
    extras = sorted(set(form.keys()) - allowed)
    if extras:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            details=[validation_detail(name, "EXTRA_FIELD") for name in extras],
        )


def current_user(request: Request, db: Session) -> User:
    claims = token_from_request(request, required=True)
    reject_revoked_token(db, claims.jti)
    user = db.get(User, claims.user_id)
    if user is None:
        raise ApiError(401, "AUTH_TOKEN_INVALID")
    return user


def optional_user(request: Request, db: Session) -> User | None:
    claims = token_from_request(request, required=False)
    if claims is None:
        return None
    reject_revoked_token(db, claims.jti)
    user = db.get(User, claims.user_id)
    if user is None:
        raise ApiError(401, "AUTH_TOKEN_INVALID")
    return user


def validate_image_options(
    image_mode: str,
    focus_x: float,
    focus_y: float,
) -> None:
    details = []
    if image_mode not in {"pixel", "fit", "smooth"}:
        details.append(validation_detail("image_mode", "INVALID_IMAGE_MODE"))
    if not math.isfinite(focus_x) or not 0 <= focus_x <= 1:
        details.append(validation_detail("focus_x", "INVALID_IMAGE_FOCUS"))
    if not math.isfinite(focus_y) or not 0 <= focus_y <= 1:
        details.append(validation_detail("focus_y", "INVALID_IMAGE_FOCUS"))
    if details:
        raise ApiError(422, "VALIDATION_ERROR", details=details)


def pagination_payload(page: int, page_size: int, total_items: int) -> dict[str, Any]:
    total_pages = math.ceil(total_items / page_size) if total_items else 0
    return {
        "page": page,
        "page_size": page_size,
        "total_items": total_items,
        "total_pages": total_pages,
        "has_previous": page > 1 and total_pages > 0,
        "has_next": page < total_pages,
    }


def sprite_payload(sprite: Sprite) -> dict[str, Any]:
    return {
        "id": sprite.id,
        "name": sprite.name,
        "tags": sprite.tags,
        "owner_id": sprite.owner_id,
        "owner_name": sprite.owner.username if sprite.owner is not None else None,
        "created_at": iso_z(sprite.created_at),
        "image_url": f"/sprites/{sprite.id}/image",
    }


def pack_payload(pack: Pack) -> dict[str, Any]:
    links = sorted(pack.sprite_links, key=lambda link: link.position)
    return {
        "id": pack.id,
        "name": pack.name,
        "owner_id": pack.owner_id,
        "created_at": iso_z(pack.created_at),
        "sprites": [
            {
                "position": link.position,
                "id": link.sprite.id,
                "name": link.sprite.name,
                "tags": link.sprite.tags,
                "image_url": f"/sprites/{link.sprite.id}/image",
            }
            for link in links
        ],
    }


def pack_db_filename(name: str, pack_id: int) -> str:
    cleaned = "".join(
        "_" if character in '\\/:*?"<>|' or ord(character) < 32 else character
        for character in name.strip()
    )
    return f"{cleaned or f'pack-{pack_id}'}.db"


def build_pack_assets_db(links: list[PackSprite]) -> bytes:
    """Create the engine-facing SQLite file bytes for one pack."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temporary:
            temporary_path = Path(temporary.name)

        connection = sqlite3.connect(temporary_path)
        try:
            connection.execute(
                """
                CREATE TABLE sprites (
                    id         INTEGER PRIMARY KEY,
                    name       TEXT,
                    tags       TEXT,
                    image_data BLOB
                )
                """
            )
            for link in links:
                if len(link.sprite.image_data) != 4096:
                    raise ApiError(500, "INTERNAL_SERVER_ERROR")
                connection.execute(
                    """
                    INSERT INTO sprites (name, tags, image_data)
                    VALUES (?, ?, ?)
                    """,
                    (
                        link.sprite.name,
                        link.sprite.tags,
                        sqlite3.Binary(link.sprite.image_data),
                    ),
                )
            connection.commit()
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ApiError(500, "INTERNAL_SERVER_ERROR")
        finally:
            connection.close()
        return temporary_path.read_bytes()
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def load_pack(db: Session, pack_id: int) -> Pack:
    pack = db.scalar(
        select(Pack)
        .where(Pack.id == pack_id)
        .options(selectinload(Pack.sprite_links).selectinload(PackSprite.sprite))
    )
    if pack is None:
        raise ApiError(404, "PACK_NOT_FOUND", details={"pack_id": pack_id})
    return pack


def load_favorite_folder(db: Session, folder_id: int, owner_id: int) -> FavoriteFolder:
    folder = db.scalar(
        select(FavoriteFolder)
        .where(
            FavoriteFolder.id == folder_id,
            FavoriteFolder.owner_id == owner_id,
        )
        .options(
            selectinload(FavoriteFolder.sprite_links).selectinload(
                FavoriteFolderSprite.sprite
            ).selectinload(Sprite.owner)
        )
    )
    if folder is None:
        raise ApiError(
            404,
            "FAVORITE_FOLDER_NOT_FOUND",
            details={"folder_id": folder_id},
        )
    return folder


def favorite_folder_payload(folder: FavoriteFolder) -> dict[str, Any]:
    links = sorted(
        folder.sprite_links,
        key=lambda link: (link.created_at, link.sprite_id),
        reverse=True,
    )
    return {
        "id": folder.id,
        "name": folder.name,
        "created_at": iso_z(folder.created_at),
        "sprite_count": len(links),
        "sprites": [sprite_payload(link.sprite) for link in links],
    }


def favorite_integrity_error(exc: IntegrityError) -> ApiError | None:
    message = str(exc.orig)
    if "FAVORITE_FOLDER_LIMIT_REACHED" in message:
        return ApiError(422, "FAVORITE_FOLDER_LIMIT_REACHED")
    if "FAVORITE_FOLDER_FULL" in message:
        return ApiError(422, "FAVORITE_FOLDER_FULL")
    if (
        "favorite_folders.owner_id, favorite_folders.name" in message
        or "uq_favorite_folders_owner_name" in message
    ):
        return ApiError(409, "FAVORITE_FOLDER_NAME_CONFLICT")
    return None


def validate_sprite_ids(db: Session, sprite_ids: list[int]) -> None:
    duplicates = sorted(
        sprite_id for sprite_id, count in Counter(sprite_ids).items() if count > 1
    )
    details = []
    if duplicates:
        details.append(
            validation_detail(
                "sprite_ids",
                "DUPLICATE_SPRITE_IN_PACK",
                sprite_ids=duplicates,
            )
        )
    unique_ids = set(sprite_ids)
    existing_ids = (
        set(db.scalars(select(Sprite.id).where(Sprite.id.in_(unique_ids))).all())
        if unique_ids
        else set()
    )
    missing = sorted(unique_ids - existing_ids)
    if missing:
        details.append(
            validation_detail("sprite_ids", "SPRITE_IDS_NOT_FOUND", sprite_ids=missing)
        )
    if details:
        raise ApiError(422, "VALIDATION_ERROR", details=details)


def replace_pack_sprites(db: Session, pack_id: int, sprite_ids: Iterable[int]) -> None:
    db.execute(delete(PackSprite).where(PackSprite.pack_id == pack_id))
    db.flush()
    db.add_all(
        [
            PackSprite(pack_id=pack_id, sprite_id=sprite_id, position=position)
            for position, sprite_id in enumerate(sprite_ids)
        ]
    )


# Authentication is delegated exclusively to Gitea. The platform still issues
# its own short-lived JWT so the rest of the API does not need to know about the
# provider access token.
OAUTH_STATE_COOKIE = "grid_gitea_oauth_state"


def _unique_gitea_username(db: Session, preferred: str, subject: str) -> str:
    try:
        candidate = normalize_name(preferred)
    except (TypeError, ValueError):
        candidate = "gitea-user"
    if db.scalar(select(User.id).where(User.username == candidate)) is None:
        return candidate
    suffix = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:10]
    candidate = f"{candidate[:27]}-{suffix}"
    if db.scalar(select(User.id).where(User.username == candidate)) is None:
        return candidate
    raise ApiError(409, "GITEA_ACCOUNT_CONFLICT")


def _upsert_gitea_user(db: Session, profile: dict[str, Any]) -> User:
    subject_value = profile.get("sub", profile.get("id"))
    subject = str(subject_value) if isinstance(subject_value, (str, int)) else None
    preferred_username = (
        profile.get("preferred_username")
        or profile.get("login")
        or profile.get("name")
    )
    email = profile.get("email")
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        raise ApiError(502, "GITEA_OAUTH_INVALID_RESPONSE")
    if not isinstance(preferred_username, str) or not preferred_username.strip():
        raise ApiError(502, "GITEA_OAUTH_INVALID_RESPONSE")
    if not isinstance(email, str) or "@" not in email:
        raise ApiError(502, "GITEA_OAUTH_INVALID_RESPONSE")
    normalized_email = email.strip().lower()

    user = db.scalar(select(User).where(User.gitea_user_id == subject))
    email_owner = db.scalar(select(User).where(User.email == normalized_email))
    if user is not None:
        if email_owner is not None and email_owner.id != user.id:
            raise ApiError(409, "GITEA_ACCOUNT_CONFLICT")
        user.email = normalized_email
    elif email_owner is not None:
        # First OAuth login migrates an existing local account by matching its
        # unique email, preserving all uploaded assets, packs, and favorites.
        user = email_owner
        if user.gitea_user_id not in {None, subject}:
            raise ApiError(409, "GITEA_ACCOUNT_CONFLICT")
        user.gitea_user_id = subject
    else:
        user = User(
            gitea_user_id=subject,
            username=_unique_gitea_username(db, preferred_username, subject),
            email=normalized_email,
            password_hash="!gitea-oauth-only!",
        )
        db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, "GITEA_ACCOUNT_CONFLICT") from exc
    db.refresh(user)
    return user


@app.get("/auth/gitea/login", include_in_schema=False)
def gitea_login(request: Request) -> RedirectResponse:
    callback = urlsplit(settings.gitea_oauth_redirect_uri)
    canonical_origin = f"{callback.scheme}://{callback.netloc}"
    request_host = urlsplit(f"//{request.headers.get('host', '')}").hostname
    if (request_host or "").lower() != (callback.hostname or "").lower():
        return RedirectResponse(f"{canonical_origin}/auth/gitea/login", status_code=307)
    state_token = gitea_oauth.create_oauth_state()
    response = RedirectResponse(gitea_oauth.authorization_url(state_token), status_code=307)
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        state_token,
        max_age=gitea_oauth.STATE_TTL_SECONDS,
        httponly=True,
        secure=settings.oauth_cookie_secure,
        samesite="lax",
        path="/auth/gitea/callback",
    )
    return response


@app.get("/auth/gitea/callback", include_in_schema=False, responses=COMMON_ERRORS)
def gitea_callback(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    code: str | None = Query(None, min_length=1),
    state_token: str = Query(..., alias="state", min_length=1),
    error: str | None = None,
) -> RedirectResponse:
    gitea_oauth.validate_oauth_state(
        state_token, request.cookies.get(OAUTH_STATE_COOKIE)
    )
    if error:
        response = RedirectResponse("/#auth_error=gitea_denied", status_code=303)
        response.delete_cookie(
            OAUTH_STATE_COOKIE,
            path="/auth/gitea/callback",
            secure=settings.oauth_cookie_secure,
            httponly=True,
            samesite="lax",
        )
        return response
    if code is None:
        raise ApiError(400, "BAD_REQUEST")
    provider_token = gitea_oauth.exchange_code(code)
    user = _upsert_gitea_user(db, gitea_oauth.fetch_userinfo(provider_token))
    platform_token = create_access_token(user.id)
    response = RedirectResponse(
        f"/#access_token={quote(platform_token, safe='')}&auth=gitea",
        status_code=303,
    )
    response.delete_cookie(
        OAUTH_STATE_COOKIE,
        path="/auth/gitea/callback",
        secure=settings.oauth_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=COMMON_ERRORS,
)
def logout(request: Request, db: Annotated[Session, Depends(get_db)]) -> Response:
    claims = token_from_request(request, required=True)
    user = db.get(User, claims.user_id)
    if user is None:
        raise ApiError(401, "AUTH_TOKEN_INVALID")
    revoke_access_token(db, claims)
    return Response(status_code=204)


# Account profile endpoints.
@app.get("/users/me", response_model=PublicUser, responses=COMMON_ERRORS)
def get_me(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = current_user(request, db)
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "created_at": iso_z(user.created_at),
    }


@app.patch("/users/me", response_model=PublicUser, responses=COMMON_ERRORS)
def update_me(
    payload: UserPatchRequest,
    request: Request,
    _content_type: Annotated[None, Depends(require_json_content_type)],
    db: Annotated[Session, Depends(get_db)],
):
    user = current_user(request, db)
    user.username = payload.username
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(409, "USERNAME_ALREADY_REGISTERED") from exc
    db.refresh(user)
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "created_at": iso_z(user.created_at),
    }


# Public sprite browsing and owner management.
@app.get("/sprites", response_model=SpriteListResponse, responses=COMMON_ERRORS)
def list_sprites(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    sprite_id: int | None = Query(None, alias="id", ge=1),
    q: str | None = None,
    name: str | None = None,
    tags: str | None = None,
    tag_mode: str = "and",
    sort: str = "newest",
    mine: bool = False,
):
    reject_extra_query(
        request,
        {"page", "page_size", "id", "q", "name", "tags", "tag_mode", "sort", "mine"},
    )
    if tag_mode not in {"and", "or"}:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            details=[validation_detail("tag_mode", "INVALID_TAG_MODE")],
        )
    if sort not in {"newest", "oldest", "name_asc", "name_desc"}:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            details=[validation_detail("sort", "INVALID_SORT")],
        )

    user = optional_user(request, db)
    if mine and user is None:
        raise ApiError(401, "AUTH_TOKEN_MISSING")

    conditions = []
    if mine:
        conditions.append(Sprite.owner_id == user.id)
    if sprite_id is not None:
        conditions.append(Sprite.id == sprite_id)
    query_term = normalize_search(q)
    if query_term:
        lowered_query = ascii_lower(query_term)
        query_conditions = [
            func.lower(Sprite.name).like(
                f"%{escape_like(lowered_query)}%", escape="\\"
            ),
            func.lower(Sprite.tags).like(
                f"%{escape_like(lowered_query)}%", escape="\\"
            ),
        ]
        if query_term.isdecimal() and int(query_term) > 0:
            query_conditions.append(Sprite.id == int(query_term))
        conditions.append(or_(*query_conditions))
    name_term = normalize_search(name)
    if name_term:
        conditions.append(
            func.lower(Sprite.name).like(
                f"%{escape_like(ascii_lower(name_term))}%", escape="\\"
            )
        )
    tag_terms = normalize_search_tags(tags)
    if tag_terms:
        tag_conditions = [
            func.lower(Sprite.tags).like(
                f"%{escape_like(ascii_lower(term))}%", escape="\\"
            )
            for term in tag_terms
        ]
        conditions.append(
            and_(*tag_conditions) if tag_mode == "and" else or_(*tag_conditions)
        )

    base = select(Sprite).where(*conditions).options(selectinload(Sprite.owner))
    total_items = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    orderings = {
        "newest": (Sprite.created_at.desc(), Sprite.id.desc()),
        "oldest": (Sprite.created_at.asc(), Sprite.id.asc()),
        "name_asc": (Sprite.name.collate("NOCASE").asc(), Sprite.id.asc()),
        "name_desc": (Sprite.name.collate("NOCASE").desc(), Sprite.id.desc()),
    }
    items = db.scalars(
        base.order_by(*orderings[sort]).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {
        "items": [sprite_payload(sprite) for sprite in items],
        "pagination": pagination_payload(page, page_size, total_items),
    }


# Private favorite folder management.
@app.get(
    "/favorites/folders",
    response_model=FavoriteFolderListResponse,
    responses=COMMON_ERRORS,
)
def list_favorite_folders(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    reject_extra_query(request, set())
    user = current_user(request, db)
    sprite_count = (
        select(func.count(FavoriteFolderSprite.sprite_id))
        .where(FavoriteFolderSprite.folder_id == FavoriteFolder.id)
        .correlate(FavoriteFolder)
        .scalar_subquery()
    )
    rows = db.execute(
        select(FavoriteFolder, sprite_count.label("sprite_count"))
        .where(FavoriteFolder.owner_id == user.id)
        .order_by(FavoriteFolder.created_at.desc(), FavoriteFolder.id.desc())
    ).all()
    return {
        "items": [
            {
                "id": folder.id,
                "name": folder.name,
                "created_at": iso_z(folder.created_at),
                "sprite_count": count,
            }
            for folder, count in rows
        ],
        "folder_limit": 5,
    }


@app.post(
    "/favorites/folders",
    response_model=FavoriteFolderResponse,
    status_code=status.HTTP_201_CREATED,
    responses=COMMON_ERRORS,
)
def create_favorite_folder(
    payload: FavoriteFolderCreateRequest,
    request: Request,
    _content_type: Annotated[None, Depends(require_json_content_type)],
    db: Annotated[Session, Depends(get_db)],
):
    user = current_user(request, db)
    folder_count = db.scalar(
        select(func.count(FavoriteFolder.id)).where(FavoriteFolder.owner_id == user.id)
    ) or 0
    if folder_count >= 5:
        raise ApiError(422, "FAVORITE_FOLDER_LIMIT_REACHED")
    folder = FavoriteFolder(owner_id=user.id, name=payload.name)
    db.add(folder)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        mapped = favorite_integrity_error(exc)
        if mapped:
            raise mapped from exc
        raise
    db.refresh(folder)
    folder.sprite_links = []
    return favorite_folder_payload(folder)


@app.get(
    "/favorites/folders/{folder_id}",
    response_model=FavoriteFolderResponse,
    responses=COMMON_ERRORS,
)
def get_favorite_folder(
    folder_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    user = current_user(request, db)
    return favorite_folder_payload(load_favorite_folder(db, folder_id, user.id))


@app.patch(
    "/favorites/folders/{folder_id}",
    response_model=FavoriteFolderResponse,
    responses=COMMON_ERRORS,
)
def update_favorite_folder(
    folder_id: int,
    payload: FavoriteFolderPatchRequest,
    request: Request,
    _content_type: Annotated[None, Depends(require_json_content_type)],
    db: Annotated[Session, Depends(get_db)],
):
    user = current_user(request, db)
    folder = load_favorite_folder(db, folder_id, user.id)
    folder.name = payload.name
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        mapped = favorite_integrity_error(exc)
        if mapped:
            raise mapped from exc
        raise
    return favorite_folder_payload(load_favorite_folder(db, folder_id, user.id))


@app.delete(
    "/favorites/folders/{folder_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=COMMON_ERRORS,
)
def delete_favorite_folder(
    folder_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    user = current_user(request, db)
    folder = load_favorite_folder(db, folder_id, user.id)
    db.delete(folder)
    db.commit()
    return Response(status_code=204)


@app.get(
    "/favorites/sprites/{sprite_id}",
    response_model=FavoriteMembershipResponse,
    responses=COMMON_ERRORS,
)
def get_favorite_membership(
    sprite_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    user = current_user(request, db)
    if db.get(Sprite, sprite_id) is None:
        raise ApiError(404, "SPRITE_NOT_FOUND", details={"sprite_id": sprite_id})
    folder_ids = db.scalars(
        select(FavoriteFolderSprite.folder_id)
        .join(FavoriteFolder)
        .where(
            FavoriteFolder.owner_id == user.id,
            FavoriteFolderSprite.sprite_id == sprite_id,
        )
        .order_by(FavoriteFolderSprite.folder_id.asc())
    ).all()
    return {"sprite_id": sprite_id, "folder_ids": folder_ids}


@app.put(
    "/favorites/sprites/{sprite_id}",
    response_model=FavoriteMembershipResponse,
    responses=COMMON_ERRORS,
)
def replace_favorite_membership(
    sprite_id: int,
    payload: FavoriteMembershipRequest,
    request: Request,
    _content_type: Annotated[None, Depends(require_json_content_type)],
    db: Annotated[Session, Depends(get_db)],
):
    user = current_user(request, db)
    if db.get(Sprite, sprite_id) is None:
        raise ApiError(404, "SPRITE_NOT_FOUND", details={"sprite_id": sprite_id})

    counts = Counter(payload.folder_ids)
    duplicates = sorted(folder_id for folder_id, count in counts.items() if count > 1)
    if duplicates:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            details=[
                validation_detail(
                    "folder_ids",
                    "DUPLICATE_FOLDER_ID",
                    folder_ids=duplicates,
                )
            ],
        )

    requested_ids = set(payload.folder_ids)
    owned_ids = (
        set(
            db.scalars(
                select(FavoriteFolder.id).where(
                    FavoriteFolder.owner_id == user.id,
                    FavoriteFolder.id.in_(requested_ids),
                )
            ).all()
        )
        if requested_ids
        else set()
    )
    if requested_ids != owned_ids:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            details=[validation_detail("folder_ids", "FOLDER_IDS_NOT_FOUND")],
        )

    current_ids = set(
        db.scalars(
            select(FavoriteFolderSprite.folder_id)
            .join(FavoriteFolder)
            .where(
                FavoriteFolder.owner_id == user.id,
                FavoriteFolderSprite.sprite_id == sprite_id,
            )
        ).all()
    )
    ids_to_add = requested_ids - current_ids
    if ids_to_add:
        folder_counts = dict(
            db.execute(
                select(
                    FavoriteFolderSprite.folder_id,
                    func.count(FavoriteFolderSprite.sprite_id),
                )
                .where(FavoriteFolderSprite.folder_id.in_(ids_to_add))
                .group_by(FavoriteFolderSprite.folder_id)
            ).all()
        )
        full_ids = sorted(
            folder_id
            for folder_id in ids_to_add
            if folder_counts.get(folder_id, 0) >= 100
        )
        if full_ids:
            raise ApiError(
                422,
                "FAVORITE_FOLDER_FULL",
                details={"folder_ids": full_ids},
            )

    try:
        db.execute(
            delete(FavoriteFolderSprite).where(
                FavoriteFolderSprite.sprite_id == sprite_id,
                FavoriteFolderSprite.folder_id.in_(current_ids - requested_ids),
            )
        )
        db.add_all(
            [
                FavoriteFolderSprite(folder_id=folder_id, sprite_id=sprite_id)
                for folder_id in ids_to_add
            ]
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        mapped = favorite_integrity_error(exc)
        if mapped:
            raise mapped from exc
        raise
    return {"sprite_id": sprite_id, "folder_ids": sorted(requested_ids)}


@app.get("/sprites/{sprite_id}/image", responses=COMMON_ERRORS)
def get_sprite_image(sprite_id: int, db: Annotated[Session, Depends(get_db)]) -> Response:
    sprite = db.get(Sprite, sprite_id)
    if sprite is None:
        raise ApiError(404, "SPRITE_NOT_FOUND", details={"sprite_id": sprite_id})
    if len(sprite.image_data) != 4096:
        raise ApiError(500, "INTERNAL_SERVER_ERROR")
    return Response(
        content=sprite.image_data,
        media_type="application/octet-stream",
        headers={"Content-Length": "4096"},
    )


@app.post("/sprites/preview", responses=COMMON_ERRORS)
async def preview_sprite_upload(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(...),
    image_mode: str = Form("pixel"),
    trim_transparent: bool = Form(True),
    focus_x: float = Form(0.5),
    focus_y: float = Form(0.5),
) -> Response:
    await reject_extra_form(
        request,
        {"file", "image_mode", "trim_transparent", "focus_x", "focus_y"},
    )
    current_user(request, db)
    validate_image_options(image_mode, focus_x, focus_y)
    processed = await process_upload(
        file,
        image_mode=image_mode,
        trim_transparent=trim_transparent,
        focus_x=focus_x,
        focus_y=focus_y,
    )
    return Response(
        content=processed.data,
        media_type="application/octet-stream",
        headers={
            "Content-Length": "4096",
            "X-Logical-Width": str(processed.logical_width),
            "X-Logical-Height": str(processed.logical_height),
            "X-Content-Width": str(processed.content_width),
            "X-Content-Height": str(processed.content_height),
            "X-Max-Crop-X": str(processed.max_crop_x),
            "X-Max-Crop-Y": str(processed.max_crop_y),
            "X-Pixel-Grid-Detected": str(processed.pixel_grid_detected).lower(),
        },
    )


@app.post(
    "/sprites",
    response_model=SpriteResponse,
    status_code=status.HTTP_201_CREATED,
    responses=COMMON_ERRORS,
)
async def create_sprite(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(...),
    name: str = Form(...),
    tags: str = Form(""),
    image_mode: str = Form("pixel"),
    trim_transparent: bool = Form(True),
    focus_x: float = Form(0.5),
    focus_y: float = Form(0.5),
):
    await reject_extra_form(
        request,
        {
            "file",
            "name",
            "tags",
            "image_mode",
            "trim_transparent",
            "focus_x",
            "focus_y",
        },
    )
    user = current_user(request, db)
    validate_image_options(image_mode, focus_x, focus_y)
    details = []
    try:
        normalized_name = normalize_name(name)
    except ValueError as exc:
        details.append(validation_detail("name", str(exc)))
        normalized_name = ""
    try:
        normalized_tags = normalize_tags(tags)
    except ValueError as exc:
        details.append(validation_detail("tags", str(exc)))
        normalized_tags = ""
    if details:
        raise ApiError(422, "VALIDATION_ERROR", details=details)

    processed = await process_upload(
        file,
        image_mode=image_mode,
        trim_transparent=trim_transparent,
        focus_x=focus_x,
        focus_y=focus_y,
    )
    sprite = Sprite(
        name=normalized_name,
        tags=normalized_tags,
        image_data=processed.data,
        owner_id=user.id,
    )
    db.add(sprite)
    db.commit()
    db.refresh(sprite)
    sprite.owner = user
    return sprite_payload(sprite)


@app.delete(
    "/sprites/{sprite_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=COMMON_ERRORS,
)
def delete_sprite(
    sprite_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    user = current_user(request, db)
    sprite = db.get(Sprite, sprite_id)
    if sprite is None:
        raise ApiError(404, "SPRITE_NOT_FOUND", details={"sprite_id": sprite_id})
    if sprite.owner_id != user.id:
        raise ApiError(403, "SPRITE_FORBIDDEN", details={"sprite_id": sprite_id})

    affected_pack_ids = db.scalars(
        select(PackSprite.pack_id).where(PackSprite.sprite_id == sprite_id)
    ).all()
    try:
        db.delete(sprite)
        db.flush()
        for pack_id in affected_pack_ids:
            remaining_ids = db.scalars(
                select(PackSprite.sprite_id)
                .where(PackSprite.pack_id == pack_id)
                .order_by(PackSprite.position.asc())
            ).all()
            replace_pack_sprites(db, pack_id, remaining_ids)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return Response(status_code=204)


# Public pack browsing and owner management.
@app.get("/packs", response_model=PackListResponse, responses=COMMON_ERRORS)
def list_packs(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    name: str | None = None,
    mine: bool = False,
    sort: str = "newest",
):
    reject_extra_query(request, {"page", "page_size", "name", "mine", "sort"})
    if sort not in {"newest", "oldest", "name_asc", "name_desc"}:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            details=[validation_detail("sort", "INVALID_SORT")],
        )
    user = optional_user(request, db)
    if mine and user is None:
        raise ApiError(401, "AUTH_TOKEN_MISSING")

    conditions = []
    if mine:
        conditions.append(Pack.owner_id == user.id)
    name_term = normalize_search(name)
    if name_term:
        conditions.append(
            func.lower(Pack.name).like(
                f"%{escape_like(ascii_lower(name_term))}%", escape="\\"
            )
        )

    total_items = db.scalar(select(func.count(Pack.id)).where(*conditions)) or 0
    sprite_count = (
        select(func.count(PackSprite.sprite_id))
        .where(PackSprite.pack_id == Pack.id)
        .correlate(Pack)
        .scalar_subquery()
    )
    orderings = {
        "newest": (Pack.created_at.desc(), Pack.id.desc()),
        "oldest": (Pack.created_at.asc(), Pack.id.asc()),
        "name_asc": (Pack.name.collate("NOCASE").asc(), Pack.id.asc()),
        "name_desc": (Pack.name.collate("NOCASE").desc(), Pack.id.desc()),
    }
    rows = db.execute(
        select(Pack, sprite_count.label("sprite_count"))
        .where(*conditions)
        .order_by(*orderings[sort])
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [
            {
                "id": pack.id,
                "name": pack.name,
                "owner_id": pack.owner_id,
                "created_at": iso_z(pack.created_at),
                "sprite_count": count,
            }
            for pack, count in rows
        ],
        "pagination": pagination_payload(page, page_size, total_items),
    }


@app.post(
    "/packs",
    response_model=PackResponse,
    status_code=status.HTTP_201_CREATED,
    responses=COMMON_ERRORS,
)
def create_pack(
    payload: PackCreateRequest,
    request: Request,
    _content_type: Annotated[None, Depends(require_json_content_type)],
    db: Annotated[Session, Depends(get_db)],
):
    user = current_user(request, db)
    validate_sprite_ids(db, payload.sprite_ids)
    pack = Pack(name=payload.name, owner_id=user.id)
    try:
        db.add(pack)
        db.flush()
        db.add_all(
            [
                PackSprite(pack_id=pack.id, sprite_id=sprite_id, position=position)
                for position, sprite_id in enumerate(payload.sprite_ids)
            ]
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return pack_payload(load_pack(db, pack.id))


@app.get("/packs/{pack_id}", response_model=PackResponse, responses=COMMON_ERRORS)
def get_pack(pack_id: int, db: Annotated[Session, Depends(get_db)]):
    return pack_payload(load_pack(db, pack_id))


@app.patch("/packs/{pack_id}", response_model=PackResponse, responses=COMMON_ERRORS)
def update_pack(
    pack_id: int,
    payload: PackPatchRequest,
    request: Request,
    _content_type: Annotated[None, Depends(require_json_content_type)],
    db: Annotated[Session, Depends(get_db)],
):
    user = current_user(request, db)
    if not payload.model_fields_set:
        raise ApiError(
            400,
            "BAD_REQUEST",
            details=[validation_detail("request", "PATCH_FIELDS_REQUIRED")],
        )
    pack = db.get(Pack, pack_id)
    if pack is None:
        raise ApiError(404, "PACK_NOT_FOUND", details={"pack_id": pack_id})
    if pack.owner_id != user.id:
        raise ApiError(403, "PACK_FORBIDDEN", details={"pack_id": pack_id})
    if "sprite_ids" in payload.model_fields_set:
        validate_sprite_ids(db, payload.sprite_ids or [])

    try:
        if "name" in payload.model_fields_set:
            pack.name = payload.name or ""
        if "sprite_ids" in payload.model_fields_set:
            replace_pack_sprites(db, pack.id, payload.sprite_ids or [])
        db.commit()
    except Exception:
        db.rollback()
        raise
    return pack_payload(load_pack(db, pack.id))


@app.delete(
    "/packs/{pack_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=COMMON_ERRORS,
)
def delete_pack(
    pack_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    user = current_user(request, db)
    pack = db.get(Pack, pack_id)
    if pack is None:
        raise ApiError(404, "PACK_NOT_FOUND", details={"pack_id": pack_id})
    if pack.owner_id != user.id:
        raise ApiError(403, "PACK_FORBIDDEN", details={"pack_id": pack_id})
    db.delete(pack)
    db.commit()
    return Response(status_code=204)


@app.get(
    "/packs/{pack_id}/export",
    responses={
        200: {
            "description": "Download the pack as a Grid++ assets SQLite database.",
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        },
        **COMMON_ERRORS,
    },
)
def export_pack(pack_id: int, db: Annotated[Session, Depends(get_db)]):
    pack = load_pack(db, pack_id)
    links = sorted(pack.sprite_links, key=lambda link: link.position)
    filename = pack_db_filename(pack.name, pack.id)
    return Response(
        content=build_pack_assets_db(links),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": (
                "attachment; filename=\"assets.db\"; "
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )


STATIC_DIR = PROJECT_ROOT / "app" / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Serve the single-page frontend from the application root.
@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
