"""录音链路：分片上传 → 聚合 → ASR → 纪要（原生表单/实时录音都可复用）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel

from app.adapters import asr
from app.agent.minutes import generate_minutes, render_markdown
from app.api.deps import get_ctx, get_current_user
from app.context import AppContext
from app.errors import ApiError, not_found

router = APIRouter(prefix="/api", tags=["audio"])


class CompleteBody(BaseModel):
    save_log: bool = False
    plan_id: str | None = None


@router.post("/audio/{upload_id}/chunk")
async def upload_chunk(
    upload_id: str,
    seq: int = Form(0),
    file: UploadFile = File(...),
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    data = await file.read()
    if not data:
        raise ApiError(400, "EMPTY_CHUNK", "空音频分片")
    chunks = await ctx.audio.add_chunk(upload_id, seq, data)
    return {"upload_id": upload_id, "chunks": chunks, "backend": ctx.audio.backend}


@router.post("/audio/{upload_id}/complete")
async def complete_audio(
    upload_id: str,
    body: CompleteBody | None = None,
    ctx: AppContext = Depends(get_ctx),
    user: dict[str, Any] = Depends(get_current_user),
):
    """合并分片 -> ASR -> 纪要；可选落一条拜访日志（降级路径）。"""
    count = await ctx.audio.count(upload_id)
    if count == 0:
        raise not_found("AUDIO_NOT_FOUND", "没有已上传的音频分片")
    chunks = await ctx.audio.chunks(upload_id)
    audio_dir = Path(ctx.settings.audio_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)
    path = audio_dir / f"{upload_id}.bin"
    path.write_bytes(b"".join(chunks))

    result = asr.transcribe(
        str(path),
        backend=ctx.settings.asr_backend,
        model=ctx.settings.asr_model,
        language=ctx.settings.asr_language,
        command=ctx.settings.asr_command,
    )
    minutes = generate_minutes(result["text"])
    await ctx.audio.clear(upload_id)
    ctx.db.add_audit(
        actor=user["user_id"], action="audio.transcribe",
        target_type="audio", target_id=upload_id,
        after={"backend": result["backend"], "chars": len(result["text"]), "chunks": count},
    )

    log = None
    if body and body.save_log:
        log = ctx.db.create_log(
            plan_id=body.plan_id, customer_id=None,
            summary=minutes["summary"], feedback="", next_action=minutes["next_action"],
            follow_up_at=None, created_by=user["user_id"],
        )
    return {
        "upload_id": upload_id,
        "chunks": count,
        "asr_backend": result["backend"],
        "fallback_reason": result["fallback_reason"],
        "transcript": result["text"],
        "minutes": minutes,
        "minutes_markdown": render_markdown(minutes),
        "log": log,
    }