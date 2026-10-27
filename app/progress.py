# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026~2028 深圳乘恺科技有限公司
#
# 本文件是 CK-CADForge 的一部分，依据 GNU 宽通用公共许可证第 3 版或更高版本发布。
# 完整条款见仓库根目录：LICENSE（LGPL-3.0）、COPYING（GPL-3.0，LGPL-3.0 以其为补充）。

"""
转换进度上报 —— 子进程写，父进程读。

## 为什么另开一条通道

转换执行在 `worker.run_isolated` 派生的**子进程**里（OCCT 段错误会带走整个进程，
线程隔离挡不住）。父子之间原本只有两个文件：`.job.json`（输入）和 `.result.json`
（结束才写）。`stdout` 走的是 `PIPE`，被 `communicate()` 独占 ——
转换过程中打任何日志都要等进程结束才拿得到，所以「边跑边报进度」不能用 print。

这里补第三个文件：`.progress.json`。子进程每到一个阶段就覆盖写一次，
父进程在 wait 期间定时读。文件很小（一个 JSON 对象），开销可以忽略。

## 为什么不干脆让子进程自己回调 CK-PLM

回调要有唯一出口：token、超时、节流、以及「终态回调一定在过程回调之后」这个顺序约束，
全都收在 `main._post_status` 一处才好维护。子进程只负责说「我到哪一步了」这一个事实，
网络的事留给父进程。

## 原子性

覆盖写用「先写临时文件再 os.replace」：Windows 上目标文件已存在时 `os.rename` 会失败，
`os.replace` 可以覆盖。读侧容忍半截文件（返回 None 当作本轮没读到），
绝不让一个读失败搞崩转换。
"""

import json
import os
import time

PROGRESS_FILENAME = ".progress.json"


def path_of(out_dir: str) -> str:
    return os.path.join(out_dir, PROGRESS_FILENAME)


def report(out_dir: str, stage: str, progress: int) -> None:
    """
    子进程侧：记一次进度。**永不抛异常** —— 报进度失败不能影响转换本身。

    :param stage: 人话阶段名，直接展示给用户（如「网格化 42/118」）
    :param progress: 0-100
    """
    if not out_dir:
        return
    try:
        value = max(0, min(100, int(progress)))
    except (TypeError, ValueError):
        value = 0
    path = path_of(out_dir)
    tmp = path + ".tmp"
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"stage": stage or "", "progress": value, "ts": time.time()}, f,
                      ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 —— 进度是锦上添花，不能因为它失败
        pass


def read(out_dir: str):
    """父进程侧：读最近一次进度。没有/读坏了都返回 None。"""
    if not out_dir:
        return None
    try:
        with open(path_of(out_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:  # noqa: BLE001
        return None


def clear(out_dir: str) -> None:
    """开跑前清掉上一轮残留，避免把旧进度当成本轮的第一个事件。"""
    try:
        p = path_of(out_dir)
        if os.path.exists(p):
            os.remove(p)
    except Exception:  # noqa: BLE001
        pass
