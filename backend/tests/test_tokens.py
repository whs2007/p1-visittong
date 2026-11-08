"""token 估算与按 token 裁剪。

钉住三件事：
1. 估算器在任何文本上都不返回 0（空串除外），且对中文明显大于英文；
2. `clip_to_tokens` **不会**超出预算（用它自己的估算器量，误差另由脚本对 tiktoken 量化）；
3. 治理器在 token 模式下仍然保留引用化能力（超长 → 有 ref → 可回查），
   且 `max_tokens=0` 时退回字符模式（向后兼容，配置迁移不会静默变行为）。
"""
from __future__ import annotations

from app.agent.output_guard import OutputGovernor
from app.agent.tokens import clip_to_tokens, estimate_tokens
from app.domain.db import Database


def make_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "tokens.db"))
    db.init()
    return db


def test_estimator_handles_mixed_text():
    assert estimate_tokens("") == 0
    chinese = estimate_tokens("帮我安排明天下午拜访望京便利店")
    english = estimate_tokens("schedule a visit tomorrow afternoon")
    assert chinese > english
    assert estimate_tokens("a") >= 1


def test_clip_respects_token_budget():
    text = "客户档案：" * 2000
    clipped, truncated = clip_to_tokens(text, 200, ref="TO-demo")
    assert truncated is True
    assert estimate_tokens(clipped) <= 200
    assert "ref=TO-demo" in clipped


def test_clip_keeps_tail_for_error_codes():
    text = "detail line\n" * 500 + "ERROR_CODE=E1024 total=0\n"
    clipped, _ = clip_to_tokens(text, 120)
    assert "E1024" in clipped          # 错误码/汇总行在尾部，不能被裁掉


def test_short_text_is_untouched():
    text = "ok"
    clipped, truncated = clip_to_tokens(text, 500)
    assert clipped == text and truncated is False


def test_governor_token_mode_keeps_ref_lookup(tmp_path):
    db = make_db(tmp_path)
    governor = OutputGovernor(db, max_tokens=120)
    result = governor.govern("客户详情：北京市海淀区中关村大街 1 号。\n" * 200, tool="todo_query", trace_id="tr_tok")
    assert result.truncated is True
    assert result.kept_tokens <= 120
    assert result.ref
    stored = governor.fetch(result.ref)
    assert stored and stored["chars"] > result.kept_chars


def test_governor_falls_back_to_char_mode(tmp_path):
    db = make_db(tmp_path)
    governor = OutputGovernor(db, max_chars=400, max_tokens=0)
    result = governor.govern("x" * 3000, tool="todo_query")
    assert result.truncated is True
    assert result.kept_chars <= 400
    assert result.kept_tokens > 0        # 即使走字符模式也记录 token，便于对账
