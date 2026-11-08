"""Phase 7：存储方言适配（SQLite / MySQL）单元测试。

说明：本机无 MySQL 实例，MySQL 路径只做「SQL 翻译 + 驱动缺失报错」的契约测试；
真实 MySQL 端到端仍需在部署机验证（见 docs/04 与 README 的"未验证"说明）。
"""
from __future__ import annotations

import pytest
from app.domain.db import Database


def test_translate_placeholders_and_insert_ignore() -> None:
    assert Database.translate_sql("SELECT * FROM t WHERE a=?") == "SELECT * FROM t WHERE a=%s"
    assert Database.translate_sql("INSERT OR IGNORE INTO t (a) VALUES (?)") == "INSERT IGNORE INTO t (a) VALUES (%s)"


def test_translate_on_conflict_to_duplicate_key() -> None:
    sql = (
        "INSERT INTO knowledge_docs (doc_id,title) VALUES (?,?)"
        " ON CONFLICT(doc_id) DO UPDATE SET title=excluded.title"
    )
    translated = Database.translate_sql(sql)
    assert "ON DUPLICATE KEY UPDATE" in translated
    assert "title=VALUES(title)" in translated
    assert "?" not in translated


def test_sqlite_default_and_mysql_requires_driver() -> None:
    sqlite_db = Database("unused.db")
    assert sqlite_db.dialect == "sqlite"
    assert sqlite_db.translate_sql("SELECT 1") == "SELECT 1"

    mysql_db = Database("", backend="mysql", dsn="mysql://root:root@127.0.0.1:1/demo")
    assert mysql_db.dialect == "mysql"
    try:
        import pymysql  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="pymysql"):
            mysql_db.init()
    else:  # pragma: no cover - 部署机若装了 pymysql，则预期是连接失败而非静默成功
        with pytest.raises(Exception):
            mysql_db.init()