"""港口危险品配载预审 API。"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.reviews import store
from app.rules import assess, removal_impact
from app.validation import (
    ApiError,
    preparse_command_dedup,
    preparse_create_dedup,
    validate_payload,
    validate_review_command_payload,
    validate_review_create_payload,
)

app = FastAPI(title="Dangerous Goods Stowage Pre-review", version="1.0.0")


@app.exception_handler(ApiError)
async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


async def _load_json(request: Request) -> object:
    try:
        return json.loads(await request.body())
    except (UnicodeDecodeError, ValueError):
        raise ApiError("INVALID_JSON", "Request body must be valid JSON.")


def _stored_json_response(status_code: int, body: bytes) -> Response:
    """直接回放存储层渲染好的字节，重试与首次响应字节级一致。"""
    return Response(status_code=status_code, content=body, media_type="application/json")


def _replay_if_command_seen(
    dedup: tuple[str, tuple] | None,
) -> tuple[int, bytes] | None:
    """完整内容校验前的判重：命中已占用 commandId 则直接给出结果。

    - 标识未占用（或请求体尚不足以提取 commandId）→ ``None``，继续严格校验；
    - 同标识同内容 → 首次成功响应，字节级重放；
    - 同标识不同内容（含非法内容）→ 抛 ``COMMAND_ID_REUSED``（409）。
    """
    if dedup is None:
        return None
    return store.lookup_command(*dedup)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/stowage/assess")
async def assess_stowage(request: Request) -> JSONResponse:
    payload = await _load_json(request)
    hold, items = validate_payload(payload)
    result = assess(items)
    return JSONResponse(
        status_code=200,
        content={
            "hold": hold,
            "conclusion": result["conclusion"],
            "evidence": result["evidence"],
        },
    )


@app.post("/api/v1/stowage/removal-impact")
async def removal_impact_stowage(request: Request) -> JSONResponse:
    payload = await _load_json(request)
    hold, items = validate_payload(payload)
    analysis = removal_impact(items)
    return JSONResponse(
        status_code=200,
        content={
            "hold": hold,
            "original": analysis["original"],
            "removals": analysis["removals"],
            "recommendations": analysis["recommendations"],
        },
    )


@app.post("/api/v1/stowage/reviews", status_code=201)
async def create_review(request: Request) -> Response:
    payload = await _load_json(request)
    # 判重先于完整内容校验：已占用 commandId 的非法重提也要得到
    # COMMAND_ID_REUSED，而不是货项类别的 400。
    replay = _replay_if_command_seen(preparse_create_dedup(payload))
    if replay is not None:
        return _stored_json_response(*replay)
    hold, items, command_id = validate_review_create_payload(payload)
    status_code, body = store.create_review(hold, items, command_id)
    return _stored_json_response(status_code, body)


@app.post("/api/v1/stowage/reviews/{review_id}/commands")
async def review_command(review_id: str, request: Request) -> Response:
    payload = await _load_json(request)
    replay = _replay_if_command_seen(preparse_command_dedup(payload, review_id))
    if replay is not None:
        return _stored_json_response(*replay)
    command_id, action, expected_revision, items = validate_review_command_payload(
        payload
    )
    status_code, body = store.apply_command(
        review_id, command_id, action, expected_revision, items
    )
    return _stored_json_response(status_code, body)
