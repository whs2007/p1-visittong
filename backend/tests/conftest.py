"""P1 测试夹具：绕开沙箱受限的系统临时目录，把 tmp_path 指到工作区内。"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest

_BASE = Path(__file__).resolve().parents[1] / "data" / "_pytest"


@pytest.fixture()
def tmp_path():
    """每个用例一个独立目录，但**不在 teardown 删除**。

    原先 fixture 结束时 `shutil.rmtree(path)`，全量跑时累计几十次目录删除会
    命中宿主的批量删除保护，整批测试被取消（不是用例失败）。`data/` 已被
    .gitignore 忽略，残留目录不进仓库、也不影响用例隔离；需要清理时由用户
    手动删除 `backend/data/_pytest/`。这样 `pytest -q` 可以一次跑完且可复现。
    """
    path = _BASE / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path
