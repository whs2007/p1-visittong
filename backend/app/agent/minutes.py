"""拜访纪要生成：确定性结构化摘要（离线可用），live 模式可再交给 LLM 精炼。"""
from __future__ import annotations

import re
from typing import Any


def generate_minutes(transcript: str) -> dict[str, Any]:
    text = (transcript or "").strip()
    sentences = [s.strip() for s in re.split(r"[。；;！？!?\n]", text) if s.strip()]
    points = sentences[:6]
    next_action = next(
        (s for s in sentences if any(k in s for k in ("下周", "下次", "后续", "跟进", "提供", "试用", "约定"))),
        "",
    )
    return {
        "title": "拜访纪要",
        "summary": text[:120],
        "points": points,
        "next_action": next_action,
        "transcript_chars": len(text),
    }


def render_markdown(minutes: dict[str, Any]) -> str:
    lines = [f"## {minutes.get('title', '拜访纪要')}", "", f"**摘要**：{minutes.get('summary', '')}", "", "**要点**"]
    for point in minutes.get("points") or []:
        lines.append(f"- {point}")
    if minutes.get("next_action"):
        lines += ["", f"**下一步**：{minutes['next_action']}"]
    return "\n".join(lines)