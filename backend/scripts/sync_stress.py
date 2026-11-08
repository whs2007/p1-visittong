"""知识同步并发压测：把「先查后写」和「SQL 版本守卫」放在同一批乱序事件上跑。

为什么要有这个脚本：docs/10 §5 写着「没做真实多 consumer 压测」。而这类 bug 的
特点是——**单线程永远不复现**。所以这里跑两组：

- `naive`：v0.8 之前的写法，先 `SELECT source_version` 再决定是否 `UPDATE`；
- `guarded`：v0.9 起的写法，把版本比较压进 `ON CONFLICT ... WHERE excluded > current`。

事件流是同一批（每个文档 v1…vN，多线程乱序消费），线程数可调。中间人为 `sleep`
把「读—写窗口」撑开：真实环境里这个窗口来自网络往返和 Python 调度，
本地 SQLite 太快，不撑开就测不出来——这正是它值得写下来的原因。

用法：
    python backend/scripts/sync_stress.py
    python backend/scripts/sync_stress.py --threads 8 --docs 40 --versions 5
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.adapters.knowledge import LocalKnowledge  # noqa: E402
from app.domain.db import Database  # noqa: E402


def _make_db(docs: int) -> tuple[Database, LocalKnowledge]:
    workdir = Path(tempfile.mkdtemp(prefix="sync_stress_"))
    db = Database(str(workdir / "sync.db"))
    db.init()
    adapter = LocalKnowledge(db)
    for index in range(docs):
        adapter.upsert(
            doc_id=f"doc_{index:03d}", kind="customer", ref_id=f"c{index:03d}",
            title=f"客户{index}", text="v0", version=0, force=True,
        )
    return db, adapter


def _events(docs: int, versions: int, seed: int = 20261108) -> list[tuple[str, int]]:
    events = [(f"doc_{index:03d}", version) for index in range(docs) for version in range(1, versions + 1)]
    random.Random(seed).shuffle(events)      # 乱序到达是常态，不是异常
    return events


def _run_naive(db: Database, events: list[tuple[str, int]], threads: int, window_ms: float) -> dict:
    """先查后写：读和写之间有任何并发，就会互相覆盖。"""
    chunks = [events[index::threads] for index in range(threads)]
    stats = {"stale_skip": 0, "writes": 0, "lock_errors": 0}
    lock = threading.Lock()

    def worker(chunk: list[tuple[str, int]]) -> None:
        local = {"stale_skip": 0, "writes": 0, "lock_errors": 0}
        for doc_id, version in chunk:
            row = db._row(  # noqa: SLF001
                db._conn().execute("SELECT source_version FROM knowledge_docs WHERE doc_id=?", (doc_id,)).fetchone()
            )
            if row and int(row["source_version"] or 0) >= version:
                local["stale_skip"] += 1
                continue
            if window_ms:
                time.sleep(window_ms / 1000.0)   # 撑开读写窗口（真实环境是网络往返）
            for attempt in range(3):
                try:
                    db._exec(  # noqa: SLF001
                        "UPDATE knowledge_docs SET text=?, source_version=? WHERE doc_id=?",
                        (f"v{version}", version, doc_id),
                    )
                    local["writes"] += 1
                    break
                except sqlite3.OperationalError:
                    if attempt == 2:
                        local["lock_errors"] += 1
                    time.sleep(0.01)
        with lock:
            for key, value in local.items():
                stats[key] += value

    started = time.perf_counter()
    workers = [threading.Thread(target=worker, args=(chunk,)) for chunk in chunks]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()
    stats["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return stats


def _run_guarded(adapter: LocalKnowledge, events: list[tuple[str, int]], threads: int, window_ms: float) -> dict:
    """SQL 版本守卫：比较和写入在同一条语句里，SQLite 写锁保证原子。"""
    chunks = [events[index::threads] for index in range(threads)]
    stats = {"stale_skip": 0, "writes": 0, "lock_errors": 0}
    lock = threading.Lock()

    def worker(chunk: list[tuple[str, int]]) -> None:
        local = {"stale_skip": 0, "writes": 0, "lock_errors": 0}
        for doc_id, version in chunk:
            if window_ms:
                time.sleep(window_ms / 1000.0)
            for attempt in range(3):
                try:
                    changed = adapter.upsert(
                        doc_id=doc_id, kind="customer", ref_id=doc_id,
                        title=doc_id, text=f"v{version}", version=version,
                    )
                    local["writes" if changed else "stale_skip"] += 1
                    break
                except sqlite3.OperationalError:
                    if attempt == 2:
                        local["lock_errors"] += 1
                    time.sleep(0.01)
        with lock:
            for key, value in local.items():
                stats[key] += value

    started = time.perf_counter()
    workers = [threading.Thread(target=worker, args=(chunk,)) for chunk in chunks]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join()
    stats["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return stats


def _verify(db: Database, docs: int, versions: int) -> dict:
    wrong_version = 0
    wrong_content = 0
    for index in range(docs):
        doc_id = f"doc_{index:03d}"
        row = db._row(db._conn().execute("SELECT text, source_version FROM knowledge_docs WHERE doc_id=?", (doc_id,)).fetchone())  # noqa: SLF001
        if int(row["source_version"] or 0) != versions:
            wrong_version += 1
        if row["text"] != f"v{versions}":
            wrong_content += 1
    return {"docs": docs, "stale_overwrites": wrong_version, "content_mismatch": wrong_content}


def judge(naive_runs: list[int], guarded_runs: list[int], docs: int) -> tuple[int, list[str]]:
    """判定压测结论，返回 (退出码, 说明)。

    抽成纯函数是为了它自己能被单测覆盖——「防止门禁永远绿」的代码如果没人测，
    很容易再长出一个永远绿。判据是**双向**的：

    1) 守卫每一次都必须 0 次陈旧覆盖，否则守卫逻辑坏了；
    2) 对照组至少有一次非 0，否则说明竞争根本没发生（窗口被吃掉、线程退化成串行、
       事件恰好没乱序），此时「守卫 0/40」不能证明任何事。

    只查第 1 条是最容易踩的坑：压测一旦失效，门禁会永远绿，比没有压测更糟。
    """
    if not guarded_runs:
        return 1, ["失败：没有守卫组的样本，压测没跑起来。"]
    if any(value != 0 for value in guarded_runs):
        return 1, [f"失败：SQL 版本守卫出现陈旧覆盖 {guarded_runs} —— 守卫逻辑坏了。"]
    if not naive_runs or max(naive_runs) == 0:
        return 1, [
            "失败：对照组一次都没复现陈旧覆盖。压测本身失效了"
            "（读写窗口太小 / 线程退化成串行 / 事件没有乱序），"
            f"此时「守卫 0/{docs}」证明不了任何事。请调大 --window-ms 或 --threads 后重跑，"
            "而不是把它当绿灯放行。"
        ]
    return 0, []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--docs", type=int, default=40)
    parser.add_argument("--versions", type=int, default=10)
    parser.add_argument("--window-ms", type=float, default=3.0, help="人为撑开的读写窗口")
    parser.add_argument(
        "--repeat", type=int, default=3,
        help="多线程对照组重复次数。竞争是随机的，只跑一次可能恰好 0/40，"
             "把「没复现」当成「没问题」是最常见的自欺。",
    )
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    results: dict[str, dict] = {}
    print(f"\n事件数 = {args.docs} 文档 × {args.versions} 版本 = {args.docs * args.versions}；"
          f"线程 {args.threads}；读写窗口 {args.window_ms}ms；对照组重复 {args.repeat} 次\n")
    header = f"{'策略':<22}{'线程':>5}{'实际写入':>10}{'判为陈旧':>10}{'陈旧覆盖':>10}{'内容错乱':>10}{'耗时':>9}"
    print(header)
    print("-" * 80)

    # 单线程只跑一次（它永远干净，重复没有信息量）；多线程重复 args.repeat 次，
    # 因为「这次恰好没抢到」和「根本没有竞争」在单次输出里长得一模一样。
    plan: list[tuple[int, int]] = [(1, 1)]
    if args.threads > 1:
        plan.append((args.threads, max(1, args.repeat)))

    for label, runner in (("naive 先查后写", "naive"), ("guarded SQL 版本守卫", "guarded")):
        for threads, rounds in plan:
            runs: list[dict] = []
            for round_index in range(rounds):
                db, adapter = _make_db(args.docs)
                events = _events(args.docs, args.versions)
                if runner == "naive":
                    stats = _run_naive(db, events, threads, args.window_ms)
                else:
                    stats = _run_guarded(adapter, events, threads, args.window_ms)
                check = _verify(db, args.docs, args.versions)
                runs.append({**stats, **check})
                suffix = f" #{round_index + 1}" if rounds > 1 else ""
                print(
                    f"{label + suffix:<22}{threads:>5}{stats['writes']:>10}{stats['stale_skip']:>10}"
                    f"{check['stale_overwrites']:>10}{check['content_mismatch']:>10}{stats['elapsed_ms']:>8.0f}ms"
                )
            key = f"{runner}_{threads}t"
            results[key] = {
                **runs[-1],
                "rounds": rounds,
                "stale_runs": [item["stale_overwrites"] for item in runs],
            }
        print()

    naive_runs = results[f"naive_{args.threads}t"]["stale_runs"]
    guarded_runs = results[f"guarded_{args.threads}t"]["stale_runs"]
    print(
        f"结论：同一批乱序事件，{args.threads} 线程下「先查后写」每次覆盖 "
        f"{naive_runs}（共 {args.repeat} 次，最多 {max(naive_runs)}/{args.docs}）；"
        f"SQL 版本守卫 {guarded_runs}。"
    )
    print(
        "注意单线程那一行 naive 也是 0 —— 这就是这类 bug 的阴险之处："
        "本地/单实例跑一万次都正常，上多副本偶发丢数据。"
    )

    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {args.json}")

    # 门禁是双向的，细节见 judge() 的 docstring。
    code, messages = judge(naive_runs, guarded_runs, args.docs)
    for message in messages:
        print(f"\n{message}")
    if code:
        return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
