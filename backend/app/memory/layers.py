"""静态记忆层：对齐 Pi 的 `AGENTS.md` 分层注入。

Pi 在启动时把仓库里的 `AGENTS.md` 按目录层级注入上下文，越靠近工作目录的
越优先。本项目的业务是「单一工作区 + 多 Agent 装配」，因此把层级改成
**项目级 → Agent 级**两级，覆盖规则与 Pi 一致：同名小节，后加载者胜。

与 Pi 的差异（要能说清）：
- Pi 是「文件系统层级 + 按需读取」；这里是「两级固定层级 + 全量注入」，
  因为两级内容都很短，按需读取只会多花一次工具调用；
- 渲染时把**来源路径**写在每个小节标题上。Pi 不显式标注来源，模型无从知道
  「这条约定来自哪里」；面试被问「你怎么证明这是记忆而不是模型编的」时，
  这一行就是答案。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_AGENT_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,40}$")
_SECTION = re.compile(r"^##\s+(?P<title>.+?)\s*$")


@dataclass(frozen=True)
class MemoryLayer:
    """一层记忆文件。`path` 是相对项目根的路径，用于溯源。"""

    path: str
    scope: str          # project | agent
    sections: tuple[tuple[str, str], ...]   # (小节标题, 正文)

    @property
    def chars(self) -> int:
        return sum(len(title) + len(body) for title, body in self.sections)


def _safe_read(base: Path, rel: str, max_chars: int) -> str | None:
    """读 `base/rel`；越界、符号链接逃逸、超大文件一律不读。"""
    candidate = (base / rel).resolve()
    root = base.resolve()
    if os.path.commonpath([str(root), str(candidate)]) != str(root):
        return None
    if not candidate.is_file():
        return None
    try:
        text = candidate.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（已截断）"
    return text


def _split_sections(text: str) -> tuple[tuple[str, str], ...]:
    """按 `## 标题` 切小节。

    第一个 `##` 之前的内容是**文件级说明**（标题、引用块、写给维护者的话），
    不进提示词 —— 否则每个 Agent 都会带上一段「这个文件会被注入…」的元说明，
    既费 token，也容易被模型当成规则复述。整篇没有小节时才整体归到「全局约定」。
    """
    if "## " not in text:
        body = "\n".join(line for line in text.splitlines() if not line.startswith("# ")).strip()
        return (("全局约定", body),) if body else ()

    sections: list[tuple[str, str]] = []
    title = ""
    body: list[str] = []
    started = False
    for raw in text.splitlines():
        match = _SECTION.match(raw)
        if match:
            if started and (title or any(line.strip() for line in body)):
                sections.append((title or "全局约定", "\n".join(body).strip()))
            title, body = match.group("title"), []
            started = True
        elif not started:
            continue
        elif raw.startswith("# "):
            continue      # 一级标题只作文档名，不进记忆
        else:
            body.append(raw)
    if title or any(line.strip() for line in body):
        sections.append((title or "全局约定", "\n".join(body).strip()))
    return tuple((name, text) for name, text in sections if text)


def load_layers(
    root: str | Path,
    agent_name: str,
    *,
    memory_dir: str = "memory",
    max_chars: int = 1600,
) -> list[MemoryLayer]:
    """按「项目级 → Agent 级」顺序加载；后加载者在渲染时覆盖同名小节。"""
    base = Path(root) / memory_dir
    layers: list[MemoryLayer] = []

    project_text = _safe_read(base, "AGENTS.md", max_chars)
    if project_text:
        layers.append(MemoryLayer(f"{memory_dir}/AGENTS.md", "project", _split_sections(project_text)))

    if _AGENT_NAME.match(agent_name or ""):
        agent_rel = f"agents/{agent_name}.md"
        agent_text = _safe_read(base, agent_rel, max_chars)
        if agent_text:
            layers.append(MemoryLayer(f"{memory_dir}/{agent_rel}", "agent", _split_sections(agent_text)))
    return layers


def render_layers(layers: list[MemoryLayer]) -> str:
    """渲染成提示词片段；同名小节后到覆盖，并标注被覆盖的来源。"""
    if not layers:
        return ""

    winner: dict[str, tuple[str, str]] = {}      # title -> (body, source)
    overridden: dict[str, str] = {}              # title -> 被覆盖的来源
    order: list[str] = []
    for layer in layers:
        for title, body in layer.sections:
            if title in winner:
                overridden[title] = winner[title][1]
            else:
                order.append(title)
            winner[title] = (body, layer.path)

    lines = ["# 记忆（静态层，就近覆盖，来源可追溯）"]
    for title in order:
        body, source = winner[title]
        shadow = f" ← 覆盖 {overridden[title]}" if title in overridden else ""
        lines.append(f"## {title} [来源: {source}{shadow}]")
        lines.append(body)
    return "\n".join(lines)
