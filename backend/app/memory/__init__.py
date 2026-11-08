"""记忆子系统。

两层，职责不同：

1. **静态层（对齐 Pi）**：`memory/AGENTS.md` + `memory/agents/<name>.md`。
   Pi 的做法是把仓库里的 `AGENTS.md` 按目录就近注入上下文；这里保留「分层 +
   就近覆盖 + 只在提示词里出现索引/正文」的语义，并把**来源路径**一起注入，
   让「模型为什么这么说」可以被反查到某个文件。
2. **动态层（本项目扩展）**：`memories` 表。Pi 没有内置的可撤销记忆库，它的
   「记忆」就是会话树 + 文件。动态层补的是跨会话的用户偏好/事实，代价是必须
   自己回答「谁写进来的、从哪句话来的、怎么撤回、被投毒了怎么办」。
"""
from app.memory.guard import MemoryRejected, check_content, check_scope
from app.memory.layers import MemoryLayer, load_layers, render_layers
from app.memory.recall import pinned, recall, score
from app.memory.store import MemoryStore

__all__ = [
    "MemoryLayer",
    "MemoryRejected",
    "MemoryStore",
    "check_content",
    "check_scope",
    "load_layers",
    "pinned",
    "recall",
    "render_layers",
    "score",
]
