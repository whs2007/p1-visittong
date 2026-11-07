"""CRM 适配器：MVP 直接查本地库，生产替换为 HTTP/RPC。"""
from __future__ import annotations

from typing import Any

from app.domain.db import Database


def search_customers(db: Database, name: str) -> list[dict[str, Any]]:
    return db.search_customers(name)


def get_customer(db: Database, customer_id: str) -> dict[str, Any] | None:
    return db.get_customer(customer_id)


def list_stores(db: Database, customer_id: str) -> list[dict[str, Any]]:
    return db.list_stores(customer_id)


def match_customer(db: Database, text: str) -> list[dict[str, Any]]:
    """从自然语言里找客户：用库里的名称/简称做包含匹配。"""
    hits: dict[str, dict[str, Any]] = {}
    for customer in db._rows("SELECT * FROM customers"):  # noqa: SLF001
        for token in (customer["name"], customer["alias"] or ""):
            if token and token in text:
                hits[customer["customer_id"]] = customer
    return list(hits.values())