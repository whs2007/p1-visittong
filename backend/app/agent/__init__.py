"""Agent Runtime 层（对齐 Pi agent-core 的分层思想）。

模块：
  events.py      统一事件包络（seq/ts）+ Pi 事件名映射
  prompt.py      系统提示词组装（技能索引 + 当前技能 + 运行时状态）
  compaction.py  上下文压缩（超预算时折叠为 summary entry）
  hooks.py       工具扩展点（before/after，等价于 Pi extension 的拦截）
  pipeline.py    工具执行流水线（策略 -> 钩子 -> 审批 -> 执行 -> 观测）
"""