"""P1 测试夹具：绕开沙箱受限的系统临时目录，把 tmp_path 指到工作区内。"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

_BASE = Path(__file__).resolve().parents[1] / "data" / "_pytest"


@pytest.fixture()
def tmp_path():
    path = _BASE / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)