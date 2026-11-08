"""并发压测的门禁判定本身要有测试。

为什么专门给「判定逻辑」写用例：这个脚本的作用是证明版本守卫有效，它一旦失效
（比如某次调度恰好没抢到、窗口参数被调小、线程退化成串行），结论会变成
「守卫 0/40」——看起来全绿，实际什么都没证明。项目里已经踩过一次同类坑
（verify_docs 的 CI 覆盖检查只认 `ast.Assign`，而 `STEPS` 是带注解赋值，
命令列表恒空 → 门禁永远绿）。所以这里钉住四条：

1. 守卫出现陈旧覆盖 → 必须红（真 bug）；
2. 对照组全 0 → 必须红（压测失效，不许放行）；
3. 对照组非 0 且守卫全 0 → 绿（正常）；
4. 没有样本 → 必须红（脚本没跑起来，不是「没有问题」）。
"""
from __future__ import annotations

from scripts.sync_stress import judge


def test_guard_regression_fails() -> None:
    code, messages = judge([5, 5, 3], [0, 2, 0], docs=40)
    assert code == 1
    assert any("守卫" in message for message in messages)


def test_no_contest_detected_fails() -> None:
    """对照组一次都没坏 → 压测没证明任何事，必须红。"""
    code, messages = judge([0, 0, 0], [0, 0, 0], docs=40)
    assert code == 1
    assert any("压测本身失效" in message for message in messages)


def test_healthy_run_passes() -> None:
    code, messages = judge([5, 5, 3], [0, 0, 0], docs=40)
    assert (code, messages) == (0, [])


def test_empty_samples_fail() -> None:
    assert judge([], [], docs=40)[0] == 1
    assert judge([], [0, 0, 0], docs=40)[0] == 1
    assert judge([5], [], docs=40)[0] == 1
