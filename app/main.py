# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026~2028 深圳乘恺科技有限公司
#
# 本文件是 CK-CADForge 的一部分，依据 GNU 宽通用公共许可证第 3 版或更高版本发布。
# 完整条款见仓库根目录：LICENSE（LGPL-3.0）、COPYING（GPL-3.0，LGPL-3.0 以其为补充）。

"""
CK-CADForge —— CAD 轻量化转换服务。

独立进程、独立仓库（LGPL-3.0）。与 CK-PLM（Apache-2.0）只通过 HTTP 协作：
两者许可证不兼容，绝不能把本项目的代码或产物打进 CK-PLM 的镜像。

对外接口：
  POST   /api/v1/jobs                       提交任务（立即返回，异步执行）
  GET    /api/v1/jobs/{jobId}               查状态（回调丢失时的补偿通道）
  DELETE /api/v1/jobs/{jobId}               取消在途任务（释放 Worker）
  GET    /api/v1/jobs/{jobId}/artifacts/{k} 下载产物（共享卷不可用时的兜底）
  GET    /healthz                           健康检查
"""

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import pipeline, source, worker

app = FastAPI(title="CK-CADForge", version="0.2.0")

# 启动即自曝家门：起错解释器（没装 OCCT）时，日志里必须第一眼能看出来
if not pipeline.occt_available():
    print("[forge] 警告：当前解释器没有 OCCT（pythonocc-core），真实转换不可用；"
          "除非设置 CADFORGE_ALLOW_MOCK=1，所有任务会直接失败。")

# 产物输出目录。与 CK-PLM 共享文件卷时，直接把这里指向卷上的目录，
# 回调里带的就是 CK-PLM 能直接读到的绝对路径（零拷贝）。
OUTPUT_DIR = os.environ.get("CADFORGE_OUTPUT_DIR", "/data/ck-plm/cad/visualizations")
CALLBACK_TOKEN = os.environ.get("CADFORGE_CALLBACK_TOKEN", "")
CALLBACK_TIMEOUT = float(os.environ.get("CADFORGE_CALLBACK_TIMEOUT", "10"))
JOB_TIMEOUT = int(os.environ.get("CADFORGE_JOB_TIMEOUT", "300"))


# 并发与车道配额统一由 worker 计算：main 的池大小必须与之同口径，
# 否则会出现"池里能跑 3 个、槽位只放 1 个"的假并发（env 默认值只有一处可信）
MAX_CONCURRENCY = worker.MAX_CONCURRENCY
FAST_CONCURRENCY = worker.FAST_CONCURRENCY
HEAVY_CONCURRENCY = worker.HEAVY_CONCURRENCY

# ---- 分道调度 ----
# 一个 200MB 的整机装配会把 Worker 占满几分钟，后面排队的 50 个零件图只能干等。
# 所以把大件单独关进"慢车道"，占用固定且很少的槽位 —— 它的存在不再影响小件的等待时间。
HEAVY_SIZE_MB = float(os.environ.get("CADFORGE_HEAVY_SIZE_MB", "20"))
HEAVY_BYTES = int(HEAVY_SIZE_MB * 1024 * 1024)
# 过程进度上报的最小间隔（秒）。网格化是逐零件上报的，不节流的话一个 300 零件的
# 装配体会打出上百次 HTTP —— 对 CK-PLM 是纯粹的写库噪声。
PROGRESS_MIN_INTERVAL = float(os.environ.get("CADFORGE_PROGRESS_INTERVAL", "1.0"))

# MVP 用内存存任务。生产换成 Redis —— 转换器本身可以是无状态的，
# 这样才能随意扩 Worker。
JOBS: dict = {}
_LOCK = threading.Lock()
# 两条车道各一个池：每个线程负责 wait 一个子进程，超出的任务在各自队列里排队。
# 快车道永远有槽位留给小件 —— 这是"分道"的全部意义，共用一个大池做不到。
_FAST_POOL = ThreadPoolExecutor(max_workers=FAST_CONCURRENCY, thread_name_prefix="forge-fast")
_HEAVY_POOL = ThreadPoolExecutor(max_workers=HEAVY_CONCURRENCY, thread_name_prefix="forge-heavy")


def _lane_of(size_bytes: int) -> str:
    """按源文件大小分道。转换前拿不到零件数，文件大小是唯一"提交时就知道"的成本信号。"""
    return "heavy" if (size_bytes or 0) >= HEAVY_BYTES else "fast"


class JobRequest(BaseModel):
    externalId: Optional[str] = None
    sourcePath: Optional[str] = None
    # 跨机部署且两边挂载点不同时用：与本服务的 CADFORGE_SOURCE_ROOT 拼接
    sourceRelativePath: Optional[str] = None
    # 跨机部署且**没有共享存储**时用：CK-PLM 给的带签名下载地址，本服务流式拉取
    sourceUrl: Optional[str] = None
    sourceFileName: Optional[str] = None
    sourceSha1: Optional[str] = None
    sourceSize: int = 0
    callbackUrl: Optional[str] = None
    options: Optional[dict] = None


@app.get("/healthz")
def healthz():
    with _LOCK:
        queued = sum(1 for j in JOBS.values() if j["status"] == "QUEUED")
        running = sum(1 for j in JOBS.values() if j["status"] == "RUNNING")
    return {
        "ok": True,
        "engine": "occt" if pipeline.occt_available() else "mock",
        "inflight": queued + running,
        "queued": queued,
        "running": running,
        "activeWorkers": len(worker.active_jobs()),
        # 并发与分道配置外露：排查"为什么排这么久"时，第一件事就是确认它到底开了几个槽
        "maxConcurrency": MAX_CONCURRENCY,
        "lanes": {
            "fast": FAST_CONCURRENCY,
            "heavy": HEAVY_CONCURRENCY,
            "heavyThresholdMb": HEAVY_SIZE_MB,
        },
    }


@app.post("/api/v1/jobs")
def submit_job(req: JobRequest):
    # 格式不支持就在提交时拒掉：让调用方立刻知道，而不是排队占一个槽位
    # 再在子进程里失败（大装配体排队几分钟才报错，排查成本完全不同）。
    # 与演示管线同一原则：宁可明确失败，也不产出与请求不符的产物。
    try:
        pipeline.resolve_target_format(req.options or {})
    except RuntimeError as e:
        raise HTTPException(400, str(e))

    # 源件地址白名单：8090 没有入站鉴权，能提交任务就能让本服务去拉任意 URL ——
    # 不设防这里就是一块探内网的跳板。提交时就拒，别等下载时才报。
    if req.sourceUrl and not source.url_allowed(req.sourceUrl):
        raise HTTPException(
            400,
            f"源件地址不在白名单内：{req.sourceUrl}。"
            f"请配置 CADFORGE_SOURCE_ALLOWED_HOSTS（host 或 host:port，逗号分隔）。"
        )

    job_id = str(uuid.uuid4())
    with _LOCK:
        JOBS[job_id] = {
            "jobId": job_id,
            "status": "QUEUED",
            "progress": 0,
            # stage 是给人看的一句话（「网格化零件 42/118」）——
            # 只有百分比的话，卡在 85% 时用户完全不知道在等什么
            "stage": "排队中",
            "request": req.dict(),
            "artifacts": [],
            "metrics": {},
            "errorMessage": None,
            "createdAt": time.time(),
            "startedAt": None,
            "lane": "fast",
            "_lastReportTs": 0.0,
        }
    # 子进程隔离执行（OCCT 段错误会直接杀进程，线程扛不住）
    lane = _lane_of(req.sourceSize)
    with _LOCK:
        JOBS[job_id]["lane"] = lane
    (_HEAVY_POOL if lane == "heavy" else _FAST_POOL).submit(_run_job, job_id, lane)
    return {"jobId": job_id, "status": "QUEUED", "accepted": True}


@app.get("/api/v1/jobs/{job_id}")
def query_job(job_id: str):
    with _LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return {
        "jobId": job["jobId"],
        "status": job["status"],
        "progress": job.get("progress"),
        "stage": job.get("stage"),
        "lane": job.get("lane"),
        "errorMessage": job.get("errorMessage"),
        "artifacts": job.get("artifacts") or [],
        "metrics": job.get("metrics") or {},
    }


@app.delete("/api/v1/jobs/{job_id}")
def cancel_job(job_id: str):
    """
    取消在途任务。CK-PLM 的看门狗在判定超时后可调它释放 Worker。
    注意：只能杀掉子进程，已在子进程里跑完的任务会照常回调。
    """
    with _LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    killed = worker.cancel(job_id)
    if killed:
        with _LOCK:
            job["status"] = "CANCELLED"
            job["errorMessage"] = "已被取消"
            job["progress"] = 100
    return {"jobId": job_id, "cancelled": killed}


@app.get("/api/v1/jobs/{job_id}/artifacts/{kind}")
def download_artifact(job_id: str, kind: str):
    with _LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    for item in job.get("artifacts") or []:
        if item.get("kind") == kind and item.get("path"):
            path = item["path"]
            if os.path.isfile(path):
                return FileResponse(path, filename=item.get("fileName"))
    raise HTTPException(404, "产物不存在")


# ==================== 执行 ====================


def _run_job(job_id: str, lane: str = "fast"):
    """
    执行一个任务。

    :param lane: fast / heavy —— 决定它等哪条车道的槽位（见 main 顶部的分道说明）。
        池已经限流，这里再传 lane 是为了让 worker 的槽位与池口径一致：
        两处都按车道算，才不会出现"池里能跑 3 个、槽位只放 1 个"的假并发。
    """
    with _LOCK:
        job = JOBS[job_id]
    out_dir = os.path.join(OUTPUT_DIR, job_id)
    os.makedirs(out_dir, exist_ok=True)

    try:
        with _LOCK:
            job["status"] = "RUNNING"
            job["startedAt"] = time.time()
            job["stage"] = "已领取任务"
            job["progress"] = 1
        _post_status(job, status="RUNNING", progress=1, stage="已领取任务")

        result = worker.run_isolated(
            job, out_dir, timeout=JOB_TIMEOUT,
            on_progress=lambda stage, value: _report_progress(job, stage, value),
            lane=lane,
        )

        with _LOCK:
            if result.status == "SUCCESS":
                job["status"] = "SUCCESS"
                job["artifacts"] = result.artifacts
                job["metrics"] = result.metrics
                # 成功后不再有"阶段"可言：留着最后一句「收尾」会让完成态看起来像还没结束
                job["stage"] = None
            elif result.status == "TIMEOUT":
                job["status"] = "FAILED"
                job["errorMessage"] = result.error
                job["metrics"] = {"timeoutSec": JOB_TIMEOUT}
                # 失败时保留 stage：卡在哪一步是有诊断价值的信息
            else:
                job["status"] = "FAILED"
                job["errorMessage"] = result.error
            job["progress"] = 100
    except Exception as e:  # noqa: BLE001 —— 兜底：任何异常都不能让任务悬在 RUNNING
        with _LOCK:
            job["status"] = "FAILED"
            job["errorMessage"] = f"{type(e).__name__}: {e}"
            job["progress"] = 100

    _callback(job)


def _report_progress(job: dict, stage: str, progress: int):
    """
    过程进度：先更新内存，再按节流上报给 CK-PLM。

    终态回调一定在它之后 —— 转换结束后的收尾回调由同一个线程顺序发出，
    但网络乱序仍可能让 RUNNING 迟到，所以 CK-PLM 侧另有终态保护（见
    CadConvertServiceImpl#handleCallback）。
    """
    now = time.time()
    with _LOCK:
        # 已被终态收口的任务不再接受过程进度
        if job.get("status") not in ("QUEUED", "RUNNING"):
            return
        job["status"] = "RUNNING"
        job["stage"] = stage
        try:
            job["progress"] = max(0, min(99, int(progress)))
        except (TypeError, ValueError):
            pass
        last = job.get("_lastReportTs") or 0.0
        if now - last < PROGRESS_MIN_INTERVAL:
            return
        job["_lastReportTs"] = now
        stage_value = job["stage"]
        progress_value = job["progress"]
    _post_status(job, status="RUNNING", progress=progress_value, stage=stage_value)


def _callback(job: dict):
    """把**终态**结果打回 CK-PLM（进度另走 _post_status 的 RUNNING 分支）。"""
    _post_status(job)


def _post_status(job: dict, status: str = None, progress: int = None, stage: str = None,
                 artifacts=None, metrics=None):
    """
    回调 CK-PLM 的唯一出口：终态结果与过程进度都从这里走。

    <p>为什么不给过程进度单独写一套 HTTP：token、超时、错误处理、以及
    「先 RUNNING 后 SUCCESS」的顺序，只维护一处才不容易漂。
    过程进度失败一律静默 —— CK-PLM 的下一轮进度或看门狗会补上，
    绝不能因为一次上报失败影响转换本身。
    """
    url = (job.get("request") or {}).get("callbackUrl")
    if not url:
        return
    payload = {
        "externalId": (job.get("request") or {}).get("externalId"),
        "jobId": job["jobId"],
        "status": status or job.get("status"),
        "progress": job.get("progress") if progress is None else progress,
        "stage": job.get("stage") if stage is None else stage,
        "artifacts": job.get("artifacts") or [] if artifacts is None else artifacts,
        "metrics": job.get("metrics") or {} if metrics is None else metrics,
        "errorMessage": job.get("errorMessage"),
    }
    headers = {"Content-Type": "application/json"}
    if CALLBACK_TOKEN:
        headers["X-Cad-Forge-Token"] = CALLBACK_TOKEN
    try:
        httpx.post(url, content=json.dumps(payload), headers=headers, timeout=CALLBACK_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        if payload["status"] == "RUNNING":
            return  # 过程进度丢了就丢了，不值得刷日志
        print(f"[callback] 回调失败（CK-PLM 看门狗会补偿）: {e}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8090")))
