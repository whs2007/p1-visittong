"""P1 测试夹具：绕开沙箱受限的系统临时目录，把 tmp_path 指到工作区内。"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

# 测试用默认 AUTH_SECRET（进程启动即会检查；显式开关表示「我知道这是测试环境」）
os.environ.setdefault("AUTH_ALLOW_INSECURE_SECRET", "1")

# 本机 .env 不得影响测试结果。踩过的坑：开发者在 `.env` 里写了 AUTH_MODE=demo
# 后，匿名访问 /api/me 从 401 变成 200，一条断言「未登录必须 401」的用例直接失败——
# 同样的代码在不同机器上跑出不同结论，测试就不再是证据。这里把关键开关钉死：
# 鉴权走 strict（测试统一带 Bearer），模型走 mock（离线、可复现、不花额度）。
os.environ.setdefault("AUTH_MODE", "strict")
os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("LLM_API_KEY", "")

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
