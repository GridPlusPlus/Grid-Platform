from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError


logger = logging.getLogger("grid_platform")

# Stable messages shared by all API error responses.
MESSAGES: dict[str, str] = {
    "VALIDATION_ERROR": "提交的資料格式不正確",
    "BAD_REQUEST": "請求內容不正確",
    "JSON_INVALID": "JSON 內容無法解析",
    "UNSUPPORTED_MEDIA_TYPE": "請求媒體格式不受支援",
    "INVALID_CREDENTIALS": "Email 或密碼錯誤",
    "AUTH_TOKEN_MISSING": "未提供登入憑證",
    "AUTH_TOKEN_INVALID": "登入憑證無效",
    "AUTH_TOKEN_EXPIRED": "登入憑證已過期",
    "GITEA_OAUTH_STATE_INVALID": "Gitea 登入狀態無效或已逾時，請重新登入",
    "GITEA_OAUTH_UNAVAILABLE": "目前無法連線至 Gitea 登入服務",
    "GITEA_OAUTH_INVALID_RESPONSE": "Gitea 登入服務回傳了無效資料",
    "GITEA_ACCOUNT_CONFLICT": "Gitea 帳號與既有平台帳號衝突，請聯絡管理員",
    "EMAIL_ALREADY_REGISTERED": "此 Email 已註冊",
    "USERNAME_ALREADY_REGISTERED": "此使用者名稱已被使用",
    "FILE_TOO_LARGE": "上傳檔案不可超過 5 MiB",
    "INVALID_IMAGE_FORMAT": "圖片格式不受支援或內容無法解碼",
    "ANIMATED_IMAGE_NOT_SUPPORTED": "不支援多幀圖片",
    "IMAGE_DIMENSIONS_TOO_LARGE": "圖片尺寸或總像素超過限制",
    "SPRITE_NOT_FOUND": "找不到指定的素材",
    "SPRITE_FORBIDDEN": "無權刪除此素材",
    "PACK_NOT_FOUND": "找不到指定的素材包",
    "PACK_FORBIDDEN": "無權修改此素材包",
    "FAVORITE_FOLDER_NOT_FOUND": "找不到指定的收藏夾",
    "FAVORITE_FOLDER_NAME_CONFLICT": "此收藏夾名稱已存在",
    "FAVORITE_FOLDER_LIMIT_REACHED": "每個帳號最多建立 5 個收藏夾",
    "FAVORITE_FOLDER_FULL": "收藏夾最多包含 100 個素材",
    "INTERNAL_SERVER_ERROR": "伺服器發生未預期的錯誤",
    "NOT_FOUND": "找不到指定的資源",
}

DETAIL_MESSAGES: dict[str, str] = {
    "EMAIL_INVALID": "Email 格式不正確",
    "USERNAME_REQUIRED": "使用者名稱不可為空",
    "USERNAME_TOO_LONG": "使用者名稱權重不可超過 40",
    "USERNAME_CONTROL_CHARACTER": "使用者名稱不可包含控制字元",
    "USERNAME_INVALID_TYPE": "使用者名稱必須是字串",
    "PASSWORD_TOO_SHORT": "密碼長度不可少於 8 個字元",
    "PASSWORD_TOO_LONG": "密碼長度不可超過 128 個字元",
    "NAME_REQUIRED": "名稱不可為空",
    "NAME_TOO_LONG": "名稱權重不可超過 40",
    "NAME_CONTROL_CHARACTER": "名稱不可包含控制字元",
    "NAME_INVALID_TYPE": "名稱必須是字串",
    "TAG_REQUIRED": "標籤不可為空",
    "TAG_TOO_LONG": "每個標籤權重不可超過 20",
    "TAG_CONTROL_CHARACTER": "標籤不可包含控制字元",
    "TOO_MANY_TAGS": "每個素材最多 20 個標籤",
    "TAGS_INVALID_TYPE": "標籤必須是逗號分隔字串",
    "DUPLICATE_SPRITE_IN_PACK": "同一素材不可在素材包中重複",
    "SPRITE_IDS_NOT_FOUND": "部分素材 ID 不存在",
    "SPRITE_IDS_REQUIRED": "sprite_ids 不可為 null",
    "FOLDER_IDS_NOT_FOUND": "部分收藏夾不存在",
    "DUPLICATE_FOLDER_ID": "收藏夾 ID 不可重複",
    "PATCH_FIELDS_REQUIRED": "至少須提供一個可更新欄位",
    "INVALID_PAGE": "page 必須是大於等於 1 的整數",
    "INVALID_PAGE_SIZE": "page_size 必須是 1 至 100 的整數",
    "INVALID_TAG_MODE": "tag_mode 僅允許 and 或 or",
    "INVALID_SORT": "sort 值不受支援",
    "INVALID_MINE": "mine 必須是布林值",
    "INVALID_IMAGE_MODE": "image_mode 僅允許 pixel、fit 或 smooth",
    "INVALID_IMAGE_FOCUS": "focus_x 與 focus_y 必須介於 0 和 1",
    "EXTRA_FIELD": "包含規格未定義的欄位",
    "MISSING_FIELD": "缺少必填欄位",
    "INVALID_TYPE": "欄位型別不正確",
    "INVALID_INTEGER": "必須是整數",
}


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str | None = None,
        details: Any = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message or MESSAGES.get(code, "請求失敗")
        self.details = details
        super().__init__(self.message)


def validation_detail(field: str, code: str, **extra: Any) -> dict[str, Any]:
    return {
        "field": field,
        "code": code,
        "message": DETAIL_MESSAGES.get(code, "欄位內容不正確"),
        **extra,
    }


def error_response(request: Request, error: ApiError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "req_unknown")
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "details": error.details,
                "request_id": request_id,
            }
        },
    )


def _field_name(location: tuple[Any, ...]) -> str:
    filtered = [str(part) for part in location if part not in {"body", "query", "path"}]
    return ".".join(filtered) or "request"


def _extract_custom_code(error: Mapping[str, Any]) -> str | None:
    ctx = error.get("ctx") or {}
    raw = ctx.get("error")
    code = getattr(raw, "code", None)
    if code:
        return str(code)
    message = str(error.get("msg", ""))
    for known in DETAIL_MESSAGES:
        if known in message:
            return known
    return None


def _map_validation_error(error: Mapping[str, Any]) -> dict[str, Any]:
    field = _field_name(tuple(error.get("loc", ())))
    error_type = str(error.get("type", ""))
    custom_code = _extract_custom_code(error)
    if custom_code:
        code = custom_code
    elif field == "page":
        code = "INVALID_PAGE"
    elif field == "page_size":
        code = "INVALID_PAGE_SIZE"
    elif field == "mine":
        code = "INVALID_MINE"
    elif error_type == "extra_forbidden":
        code = "EXTRA_FIELD"
    elif error_type == "missing":
        code = "MISSING_FIELD"
    elif "int_" in error_type:
        code = "INVALID_INTEGER"
    elif field == "email":
        code = "EMAIL_INVALID"
    else:
        code = "INVALID_TYPE"
    return validation_detail(field, code)


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        if any(error.get("type") == "json_invalid" for error in errors):
            return error_response(request, ApiError(400, "JSON_INVALID"))
        details = [_map_validation_error(error) for error in errors]
        return error_response(request, ApiError(422, "VALIDATION_ERROR", details=details))

    @app.exception_handler(ValidationError)
    async def handle_validation(request: Request, exc: ValidationError) -> JSONResponse:
        return error_response(
            request,
            ApiError(
                422,
                "VALIDATION_ERROR",
                details=[_map_validation_error(error) for error in exc.errors()],
            ),
        )

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return error_response(request, ApiError(404, "NOT_FOUND"))
        return error_response(
            request,
            ApiError(exc.status_code, "BAD_REQUEST", details={"reason": str(exc.detail)}),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "req_unknown")
        logger.exception("Unhandled exception request_id=%s", request_id)
        return error_response(request, ApiError(500, "INTERNAL_SERVER_ERROR"))
