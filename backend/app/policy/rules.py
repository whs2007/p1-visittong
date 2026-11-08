"""安全规则：危险命令识别、参数 Schema 校验。"""
from __future__ import annotations

import re
from typing import Any

# (规则名, 正则, 说明) —— 只做“识别 + 升级审批”，不替代容器隔离
DANGEROUS_COMMAND_RULES: list[tuple[str, str, str]] = [
    ("rm_root", r"\brm\s+(-[a-zA-Z]*\s+)*-?\s*(/|/\*|~)\s*$", "删除根目录/家目录"),
    ("rm_rf", r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\b", "递归强制删除"),
    ("mkfs", r"\bmkfs(\.\w+)?\b", "格式化文件系统"),
    ("dd", r"\bdd\s+if=", "裸设备写入"),
    ("shutdown", r"\b(shutdown|reboot|halt|poweroff)\b", "关机/重启"),
    ("format_disk", r"\bformat\s+[a-zA-Z]:", "格式化磁盘"),
    ("del_force", r"\bdel\s+/[fsq]\b", "Windows 强制删除"),
    ("git_reset", r"\bgit\s+reset\s+--hard\b", "丢弃全部未提交改动"),
    ("git_clean", r"\bgit\s+clean\s+-[a-zA-Z]*[fdx]", "清理未跟踪文件"),
    ("curl_pipe_bash", r"\b(curl|wget)\b[^|]*\|\s*(ba)?sh\b", "远程脚本直接执行"),
    ("chmod_777_root", r"\bchmod\s+-R\s+777\s+/", "放开根目录权限"),
    ("fork_bomb", r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork 炸弹"),
    ("sudo", r"\bsudo\b", "提权执行"),
    ("drop_table", r"\b(drop\s+(table|database)|truncate\s+table)\b", "删表/删库"),
]

# 会被人工阅读 / 外发给客户的自由文本字段。危险命令规则只扫这些字段。
FREE_TEXT_FIELDS: set[str] = {
    "summary", "feedback", "next_action", "topic", "message",
    "target", "text", "content", "reason", "file_path", "question",
}

_TYPE_MAP: dict[str, tuple[type, ...]] = {    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


def match_dangerous(
    args: dict[str, Any], *, fields: set[str] | None = None
) -> tuple[str, str] | None:
    """返回 (规则名, 命中的文本片段)。

    **只扫描自由文本字段**（`FREE_TEXT_FIELDS`）。原因：本项目没有任何执行
    shell 的工具，这 14 条规则的真实用途是「文本内容风控」—— 销售写给客户的
    通知、拜访日志、跟进备注会经 outbox 外发，若里面夹带 `rm -rf /` 这类串，
    说明可能是误粘贴或提示词注入的残留物，值得升级人工确认。

    v0.7 之前它对**所有**字符串参数做匹配（`plan_id`、`planned_at` 也扫），
    既拦不住真攻击（没有执行面），又会对正常业务参数误报。审计把这条列为
    「装饰性治理」，本次按真实用途收窄，并在文档里写清它防的是什么。
    """
    fields = FREE_TEXT_FIELDS if fields is None else fields
    for key, value in args.items():
        if key not in fields:
            continue
        for text in _iter_strings(value):
            for name, pattern, _desc in DANGEROUS_COMMAND_RULES:
                if re.search(pattern, text, flags=re.IGNORECASE):
                    return name, text[:120]
    return None


def validate_args(schema: dict[str, Any], args: dict[str, Any]) -> str | None:
    """轻量 JSON Schema 校验：required + 基础类型。通过返回 None。"""
    if not isinstance(args, dict):
        return "参数必须是 JSON 对象"
    required = schema.get("required") or []
    missing = [key for key in required if args.get(key) in (None, "")]
    if missing:
        return f"缺少必填参数：{', '.join(missing)}"
    properties = schema.get("properties") or {}
    for key, spec in properties.items():
        if key not in args or args[key] is None:
            continue
        expected = spec.get("type")
        allowed = _TYPE_MAP.get(expected)
        if allowed and not isinstance(args[key], allowed):
            return f"参数 {key} 类型应为 {expected}"
    return None