from __future__ import annotations

import base64
import hmac
import os
import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from engine import AGENT_IDS, WorldEngine
from storage import PALETTE, create_store
from world import redact_secrets


SERVICE_TOKEN = os.getenv("RUNTIME_SERVICE_TOKEN", "").strip()


class CommandInput(BaseModel):
    agentId: str
    command: str = Field(min_length=1, max_length=800)
    actorId: str = Field(min_length=1, max_length=180)
    idempotencyKey: str = Field(min_length=8, max_length=100)


class PlayerInput(BaseModel):
    x: float = Field(ge=0, le=100)
    y: float = Field(ge=0, le=100)
    facing: str = Field(pattern="^(north|east|south|west)$")
    actorId: str = Field(min_length=1, max_length=180)


class ControlInput(BaseModel):
    agentsPaused: bool
    actorId: str = Field(min_length=1, max_length=180)


class ReviewInput(BaseModel):
    cycleId: str = Field(max_length=80)
    individual: int = Field(ge=1, le=5)
    team: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=1200)
    actorId: str = Field(min_length=1, max_length=180)


class BoardInput(BaseModel):
    title: str = Field(min_length=1, max_length=40)
    palette: list[str] = Field(min_length=1, max_length=32)
    pixelsBase64: str = Field(min_length=10, max_length=100_000)
    textBlocks: list[dict[str, Any]] = Field(max_length=32)
    expectedVersion: int = Field(ge=1)
    changeSummary: str = Field(default="", max_length=180)
    actorId: str = Field(min_length=1, max_length=180)
    actorName: str = Field(min_length=1, max_length=80)


def authorize(x_cashcow_service_token: str | None = Header(default=None)) -> None:
    if not SERVICE_TOKEN:
        raise HTTPException(503, "RUNTIME_SERVICE_TOKEN is not configured")
    if not x_cashcow_service_token or not hmac.compare_digest(x_cashcow_service_token, SERVICE_TOKEN):
        raise HTTPException(401, "invalid service token")


def validate_board(value: BoardInput) -> dict:
    if any(not re.fullmatch(r"#[0-9a-fA-F]{6}", color) for color in value.palette):
        raise HTTPException(400, "팔레트는 HEX 색상만 사용할 수 있습니다.")
    try:
        pixels = base64.b64decode(value.pixelsBase64, validate=True)
    except Exception as error:
        raise HTTPException(400, "도트 데이터가 올바른 base64가 아닙니다.") from error
    if len(pixels) != 256 * 256 or any(pixel >= len(value.palette) for pixel in pixels):
        raise HTTPException(400, "도트 데이터는 256×256 팔레트 인덱스여야 합니다.")
    safe_blocks = []
    for raw in value.textBlocks:
        try:
            x, y = int(raw.get("x", 0)), int(raw.get("y", 0))
            width, height = int(raw.get("width", 80)), int(raw.get("height", 40))
            font_size = int(raw.get("fontSize", 10))
        except (TypeError, ValueError) as error:
            raise HTTPException(400, "텍스트 영역 좌표가 올바르지 않습니다.") from error
        text = redact_secrets(str(raw.get("text", "")))[:1200]
        color = str(raw.get("color", "#26394f"))
        background = str(raw.get("background", "#f9f4dfef"))
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color) or not re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", background):
            raise HTTPException(400, "텍스트 색상이 올바르지 않습니다.")
        safe_blocks.append({
            "id": re.sub(r"[^a-zA-Z0-9_-]", "", str(raw.get("id", "")))[:80] or os.urandom(8).hex(),
            "x": max(0, min(248, x)), "y": max(0, min(248, y)),
            "width": max(24, min(256 - max(0, min(248, x)), width)),
            "height": max(20, min(256 - max(0, min(248, y)), height)),
            "text": text, "color": color.lower(), "background": background.lower(),
            "fontSize": max(6, min(28, font_size)),
        })
    return {
        "title": redact_secrets(value.title), "palette": [item.lower() for item in value.palette],
        "pixelsBase64": value.pixelsBase64, "textBlocks": safe_blocks,
        "expectedVersion": value.expectedVersion, "changeSummary": redact_secrets(value.changeSummary),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = WorldEngine(create_store())
    await engine.start()
    app.state.engine = engine
    yield
    await engine.stop()


app = FastAPI(title="Cashcow Persistent World Runtime", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.middleware("http")
async def no_store(request: Request, call_next):
    response = await call_next(request)
    response.headers["cache-control"] = "no-store"
    response.headers["x-cashcow-runtime"] = "persistent-world-v1"
    return response


def engine(request: Request) -> WorldEngine:
    return request.app.state.engine


@app.get("/cashcow/health")
async def health(request: Request):
    snapshot = await engine(request).snapshot()
    return {"status": "ok", "worldVersion": snapshot["worldVersion"], "paused": snapshot["agentsPaused"]}


@app.get("/cashcow/api/bootstrap", dependencies=[Depends(authorize)])
async def bootstrap(request: Request, actorId: str = Query(default="owner", max_length=180)):
    return await engine(request).snapshot(actorId)


@app.post("/cashcow/api/commands", status_code=202, dependencies=[Depends(authorize)])
async def command(request: Request, value: CommandInput):
    if value.agentId not in AGENT_IDS:
        raise HTTPException(400, "알 수 없는 에이전트입니다.")
    try:
        return await engine(request).enqueue(value.agentId, value.command, value.actorId, value.idempotencyKey)
    except PermissionError as error:
        raise HTTPException(409, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(423, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.patch("/cashcow/api/player", dependencies=[Depends(authorize)])
async def move_player(request: Request, value: PlayerInput):
    return await engine(request).move_player(value.actorId, {"x": value.x, "y": value.y}, value.facing)


@app.patch("/cashcow/api/control", dependencies=[Depends(authorize)])
async def control(request: Request, value: ControlInput):
    return await engine(request).set_paused(value.agentsPaused, value.actorId)


@app.patch("/cashcow/api/whiteboard", dependencies=[Depends(authorize)])
async def whiteboard(request: Request, value: BoardInput):
    safe = validate_board(value)
    try:
        return await engine(request).save_board(safe, value.actorId, value.actorName)
    except FileExistsError as error:
        raise HTTPException(409, str(error)) from error


@app.get("/cashcow/api/whiteboard/{version}", dependencies=[Depends(authorize)])
async def whiteboard_revision(request: Request, version: int):
    try:
        return await engine(request).board_revision(version)
    except KeyError as error:
        raise HTTPException(404, str(error).strip("'")) from error


@app.get("/cashcow/api/jobs/{job_id}", dependencies=[Depends(authorize)])
async def job_detail(request: Request, job_id: str):
    try:
        return await engine(request).job_detail(job_id)
    except KeyError as error:
        raise HTTPException(404, str(error).strip("'")) from error


@app.post("/cashcow/api/reviews", dependencies=[Depends(authorize)])
async def review(request: Request, value: ReviewInput):
    return await engine(request).apply_review(value.model_dump(), value.actorId)


@app.exception_handler(Exception)
async def unexpected_error(_request: Request, error: Exception):
    print("runtime_request_failed", type(error).__name__, redact_secrets(str(error))[:300], flush=True)
    return JSONResponse({"detail": "영속 월드 런타임에서 요청을 처리하지 못했습니다."}, status_code=500)
