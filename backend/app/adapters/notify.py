"""通知适配器：MVP 写 outbox 表留痕；生产接短信/企微。"""
from __future__ import annotations

from app.domain.db import Database


def notify(db: Database, channel: str, target: str, content: str) -> bool:
    db.add_outbox(channel, target, content)
    return True