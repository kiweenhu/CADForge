# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026~2028 深圳乘恺科技有限公司
#
# 本文件是 CK-CADForge 的一部分，依据 GNU 宽通用公共许可证第 3 版或更高版本发布。
# 完整条款见仓库根目录：LICENSE（LGPL-3.0）、COPYING（GPL-3.0，LGPL-3.0 以其为补充）。

"""
子进程隔离执行器。

为什么必须是子进程、线程不够：
OCCT 在遇到畸形 STEP（非法拓扑、自相交面、损坏的 B-Rep）时可能直接触发
段错误（SIGSEGV）。段错误发生在 C 层，Python 的 try/except 完全接不住 ——
线程隔离拦不住，一次崩溃会带走整个服务进程，所有在途任务一起失败，
而且 FastAPI 也会跟着挂掉，CK-PLM 的看门狗会看到一批同时超时的任务。

所以：每个转换任务 fork 一个独立子进程，父进程 wait 并带硬超时，
超时/崩溃一律 kill 整棵进程树，把任务标记成 FAILED/TIMEOUT 后继续服务下一个。

结果通过 JSON 文件回传（不用 stdout pipe —— 子进程刷屏时会堵死管道）。
"""

import json
import os
import subprocess
import sys
import threading
import time

from . import source

# 硬超时：宁可让一个畸形文件失败，也不能让它占着 Worker 不放
DEFAULT_TIMEOUT = int(os.environ.get("CADFORGE_JOB_TIMEOUT", "300"))


def _default_concurrency() -> int:
    """
    默认并发 = 核数一半，夹在 [2, 6]。

    下限 2：单核机器也要能一边转一边响应；上限 6：每个 OCCT 子进程峰值 1~3GB，
    再往上不是更快而是换页（实测 31MB 装配体单进程峰值约 1.4GB）。
    """
    cores = os.cpu_count() or 4
    return max(2, min(6, cores // 2))


# 并发口径只在这里算一次：main 的池大小、这里的槽位数都取这三个常量，
# 两处各读一遍环境变量，迟早出现"池里能跑 3 个、槽位只放 1 个"的假并发。
MAX_CONCURRENCY = int(os.environ.get("CADFORGE_MAX_CONCURRENCY", str(_default_concurrency())))
HEAVY_CONCURRENCY = max(1, int(os.environ.get("CADFORGE_HEAVY_CONCURRENCY", "1")))
FAST_CONCURRENCY = max(1, MAX_CONCURRENCY - HEAVY_CONCURRENCY)
# 子进程输出截断：OCCT 某些版本遇到坏面会刷几万行告警
LOG_TAIL = int(os.environ.get("CADFORGE_LOG_TAIL", "2000"))

# 分道槽位：大件单独关进慢车道，它跑多久都不再挤占小件的等待时间
_LANE_SEMAPHORES = {
    "fast": threading.BoundedSemaphore(FAST_CONCURRENCY),
    "heavy": threading.BoundedSemaphore(HEAVY_CONCURRENCY),
}
_ACTIVE: dict = {}
_ACTIVE_LOCK = threading.Lock()


class JobResult:
    def __init__(self, status: str, artifacts=None, metrics=None, error=None):
        self.status = status
        self.artifacts = artifacts or []
        self.metrics = metrics or {}
        self.error = error

    def to_dict(self):
        return {
            "status": self.status,
            "artifacts": self.artifacts,
            "metrics": self.metrics,
            "error": self.error,
        }


def run_isolated(job: dict, out_dir: str, timeout: int = None, on_progress=None,
                 lane: str = "fast") -> JobResult:
    """
    在独立子进程里跑转换。返回 JobResult —— 不会抛异常给调用方，
    因为调用方需要的是「任务失败了」这个事实，而不是崩溃。

    :param on_progress: 可选回调 ``(stage, progress)``。子进程把阶段进度写进
        ``out_dir/.progress.json``，这里在 wait 期间起一个伴随线程读出来转给它 ——
        回调抛异常一律吞掉，进度问题不能影响转换。
    """
    timeout = timeout or DEFAULT_TIMEOUT
    job_id = job.get("jobId", "job")

    from . import progress

    result_path = os.path.join(out_dir, ".result.json")
    if os.path.exists(result_path):
        os.remove(result_path)
    progress.clear(out_dir)

    payload_path = os.path.join(out_dir, ".job.json")
    # 子进程入口用 -m，保证 app 包可被正常导入（不依赖当前工作目录）
    cmd = [sys.executable, "-m", "app.worker", payload_path, result_path, out_dir]

    semaphore = _LANE_SEMAPHORES.get(lane) or _LANE_SEMAPHORES["fast"]
    semaphore.acquire()
    proc = None
    watch_stop = threading.Event()
    watcher = None
    local_src = None
    source_info: dict = {}

    def failed(status: str, error: str) -> JobResult:
        # 失败时同样带上源件信息：分布式下"是下载慢/传坏了，还是转换崩了"全靠它区分，
        # 否则跨机排障只能看到一句"转换失败（退出码 1）"。
        return JobResult(status, metrics=dict(source_info), error=error)

    try:
        # ---- 取源：必须在拿到槽位之后（否则排队任务一起下载会打满磁盘和带宽），
        #      也必须在 fork 之前（子进程只管转换，超时口径互不污染）。
        #      分布式部署且无共享存储时，这一步就是"能不能跑起来"的分水岭。
        try:
            req = job.get("request") or {}
            # 先报一句：大文件下载要走网络，卡在这一步时用户不该看到"0% 一动不动"
            if on_progress:
                on_progress("准备源件", 4)
            local_src, source_info = source.resolve_source(req, out_dir, on_progress=on_progress)
            job = dict(job)
            job["request"] = dict(req)
            job["request"]["sourcePath"] = local_src
        except Exception as e:  # noqa: BLE001 —— 取源失败与转换失败是两回事，但都归一成 FAILED
            return failed("FAILED", f"获取源件失败：{e}")

        with open(payload_path, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False)

        started = time.time()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            )
        except Exception as e:  # noqa: BLE001 —— 连进程都起不来，直接判失败
            return failed("FAILED", f"无法启动转换子进程: {e}")

        with _ACTIVE_LOCK:
            _ACTIVE[job_id] = proc.pid

        if on_progress:
            watcher = threading.Thread(
                target=_watch_progress,
                args=(out_dir, proc, on_progress, watch_stop),
                daemon=True,
                name=f"forge-progress-{job_id[:8]}",
            )
            watcher.start()

        try:
            out, _ = proc.communicate(timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            out, _ = proc.communicate()
            return failed(
                "TIMEOUT",
                f"转换超时（{timeout}s），已终止子进程。"
                f"可能是超大装配体或几何反复愈合失败，可提高 CADFORGE_JOB_TIMEOUT 重试。",
            )
        finally:
            watch_stop.set()
            with _ACTIVE_LOCK:
                _ACTIVE.pop(job_id, None)

        elapsed_ms = int((time.time() - started) * 1000)

        # 段错误在 Windows 上是 0xC0000005（有符号即 -1073741819），
        # Linux 上是 -11 (SIGSEGV)。统一归一成 FAILED 并给出可诊断的信息。
        if _is_crash(code):
            return failed(
                "FAILED",
                f"转换器子进程异常终止（退出码 {code}，疑似段错误）。"
                f"该文件可能包含 OCCT 无法处理的几何。子进程已隔离，服务不受影响。",
            )

        # 子进程在抛异常退出前会先把原因写进结果文件，且退出码为 1。
        # 所以「退出码非 0」时也要先读结果文件 —— 只报"退出码 1"外加 stdout，
        # 用户看到的是一片空白，而真正的原因（比如"没有 OCCT"）就在那一行里。
        data = None
        if os.path.exists(result_path):
            try:
                with open(result_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:  # noqa: BLE001
                data = None
                if code == 0:
                    return failed("FAILED", f"结果文件无法解析: {e}")

        if code != 0:
            detail = (data or {}).get("error") or _tail(out) or "（子进程没有输出任何信息）"
            return failed("FAILED", f"转换失败（退出码 {code}）：{detail}")

        if data is None:
            tail = _tail(out)
            return failed("FAILED", f"子进程未产出结果文件（可能中途崩溃）：{tail}")

        if not data.get("ok"):
            return failed("FAILED", data.get("error") or "转换失败（未知原因）")

        metrics = data.get("metrics") or {}
        metrics.setdefault("durationMs", elapsed_ms)
        metrics["isolatedProcess"] = True
        # 源件怎么来的、下载花了多久：分布式下"慢在下载还是慢在转换"必须有据可查
        metrics.update(source_info)
        if out and out.strip():
            metrics["logTail"] = _tail(out)
        # 下载来的源件用完即删 —— 磁盘不该为一个任务的中间文件翻倍。
        # 失败时不删：排查线上问题必须有原件（见 source.cleanup_source）。
        source.cleanup_source(local_src, source_info, success=True)
        return JobResult("SUCCESS", artifacts=data.get("artifacts") or [], metrics=metrics)
    finally:
        semaphore.release()
        # 临时文件留着：排查线上问题时 .job.json 能还原当时的输入
        # （磁盘占用极小，清理交给运维定时任务）


def cancel(job_id: str) -> bool:
    """取消在途任务。CK-PLM 看门狗判定超时后可以主动调它释放 Worker。"""
    with _ACTIVE_LOCK:
        pid = _ACTIVE.get(job_id)
    if not pid:
        return False
    try:
        _kill_pid(pid)
        return True
    except Exception:
        return False


def active_jobs():
    with _ACTIVE_LOCK:
        return dict(_ACTIVE)


# 进度文件轮询间隔：0.5s 足够让用户看到"在动"，又不至于为了读一个小 JSON 忙起来
PROGRESS_POLL_SEC = float(os.environ.get("CADFORGE_PROGRESS_POLL", "0.5"))


def _watch_progress(out_dir: str, proc, on_progress, stop: threading.Event):
    """
    伴随线程：把子进程写下的进度文件转成回调。

    只在「子进程还活着」时轮询 —— 进程结束后由终态结果负责表达，
    迟到的过程进度没有任何意义（还可能把已完成的记录打回 RUNNING）。
    """
    from . import progress

    last = None
    while not stop.wait(PROGRESS_POLL_SEC):
        if proc.poll() is not None:
            break
        data = progress.read(out_dir)
        if not data:
            continue
        stage = data.get("stage") or ""
        try:
            value = int(data.get("progress") or 0)
        except (TypeError, ValueError):
            value = 0
        if (stage, value) == last:
            continue
        last = (stage, value)
        try:
            on_progress(stage, value)
        except Exception:  # noqa: BLE001 —— 上报失败不能影响转换
            pass


# ==================== 子进程入口 ====================


def _child_entry(payload_path: str, result_path: str, out_dir: str):
    """
    子进程主体。这里抛出的任何异常都要落到结果文件里 —— 但段错误不会走到这里，
    那种情况由父进程靠退出码识别。
    """
    data = {"ok": False}
    try:
        with open(payload_path, "r", encoding="utf-8") as f:
            job = json.load(f)
        from . import pipeline

        result = pipeline.convert(job, out_dir)
        data = {
            "ok": True,
            "artifacts": result.get("artifacts") or [],
            "metrics": result.get("metrics") or {},
        }
    except Exception as e:  # noqa: BLE001
        data = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    try:
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        print(f"[worker] 写结果文件失败: {e}", file=sys.stderr)
        raise
    # 显式退出：OCCT 有时会在 atexit 阶段崩，给个明确的 0 更可靠
    sys.stdout.flush()
    os._exit(0 if data.get("ok") else 1)


# ==================== 工具 ====================


def _is_crash(code) -> bool:
    if code is None:
        return False
    # Windows: 0xC0000005 访问违规；POSIX: -11 SIGSEGV / -6 SIGABRT
    return code in (-11, -6, -1073741819, -1073740791, 3221225477)


def _kill_tree(proc):
    try:
        _kill_pid(proc.pid)
    except Exception:
        pass


def _kill_pid(pid: int):
    if os.name == "nt":
        # /T 杀整棵进程树 —— OCCT 可能派生子进程或加载器残留
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            os.kill(pid, signal.SIGKILL)


def _tail(text: str) -> str:
    if not text:
        return ""
    lines = text.strip().splitlines()
    if len(lines) > 20:
        lines = ["...(省略 %d 行)" % (len(lines) - 20)] + lines[-20:]
    out = "\n".join(lines)
    return out[:LOG_TAIL]


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("usage: python -m app.worker <job.json> <result.json> <out_dir>")
        sys.exit(2)
    _child_entry(sys.argv[1], sys.argv[2], sys.argv[3])
