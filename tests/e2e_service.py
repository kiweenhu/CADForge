# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026~2028 深圳乘恺科技有限公司
#
# 本文件是 CK-CADForge 的一部分，依据 GNU 宽通用公共许可证第 3 版或更高版本发布。
# 完整条款见仓库根目录：LICENSE（LGPL-3.0）、COPYING（GPL-3.0，LGPL-3.0 以其为补充）。

"""
服务级端到端测试：验证异步提交、真实转换、以及**崩溃隔离**。

崩溃隔离是这里最关键的用例：OCCT 遇到畸形几何会直接段错误，
如果隔离失效，整个服务会跟着挂掉，所有在途任务一起失败。
这里提交一个"必崩"任务，然后确认服务依然活着、还能接新任务。

用法：
    python tests/e2e_service.py [--python <解释器路径>]
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STEP = os.path.join(HERE, "sample_gearbox.step")
PORT = int(os.environ.get("E2E_PORT", "8099"))
BASE = f"http://127.0.0.1:{PORT}"


def http(method: str, path: str, body=None, timeout=120, raw=False):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            return r.status, (payload if raw else json.loads(payload.decode("utf-8")))
    except urllib.error.HTTPError as e:
        payload = e.read()
        return e.code, (payload if raw else json.loads(payload.decode("utf-8")))


def wait_job(job_id: str, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, job = http("GET", f"/api/v1/jobs/{job_id}")
        if code == 200 and job.get("status") in ("SUCCESS", "FAILED", "CANCELLED"):
            return job
        time.sleep(1)
    return {"status": "TIMEOUT"}


def main() -> int:
    py = sys.executable
    if len(sys.argv) > 2 and sys.argv[1] == "--python":
        py = sys.argv[2]

    out_dir = tempfile.mkdtemp(prefix="cadforge-svc-")
    env = dict(os.environ)
    env["CADFORGE_OUTPUT_DIR"] = out_dir
    env["CADFORGE_CALLBACK_TOKEN"] = "e2e-token"
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.Popen(
        [py, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )

    ok = True
    try:
        # ---- 等服务起来 ----
        for _ in range(60):
            try:
                code, h = http("GET", "/healthz", timeout=5)
                if code == 200:
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            print("[FAIL] 服务未启动")
            print((proc.stdout.read() if proc.stdout else "")[:2000])
            return 1
        print(f"healthz: engine={h.get('engine')} maxConcurrency={h.get('maxConcurrency')}")

        # ---- 1. 真实 STEP 转换 ----
        print("\n[1] 提交真实 STEP 转换")
        code, sub = http("POST", "/api/v1/jobs", {
            "externalId": "e2e-service-1",
            "sourcePath": STEP,
            "sourceFileName": "sample_gearbox.step",
            "callbackUrl": "",
        })
        print(f"    提交 -> {sub.get('status')}")
        job = wait_job(sub["jobId"])
        print(f"    结果 -> {job.get('status')}")
        m = job.get("metrics") or {}
        print(f"    三角面={m.get('triangleCount')} 零件={m.get('partCount')} "
              f"节点={m.get('nodeCount')} 尺寸={m.get('outputSize')}B 命名={m.get('namedNodeCount')}")
        if job.get("status") != "SUCCESS":
            print(f"    [FAIL] 转换未成功: {job.get('errorMessage')}")
            ok = False
        else:
            kinds = [a["kind"] for a in job.get("artifacts") or []]
            print(f"    产物: {kinds}")
            code, blob = http("GET", f"/api/v1/jobs/{sub['jobId']}/artifacts/glb", raw=True)
            magic = int.from_bytes(blob[:4], "little") if len(blob) >= 4 else 0
            print(f"    下载 glb -> HTTP {code}, {len(blob)}B, magic=0x{magic:08X} "
                  f"({'合法 GLB' if magic == 0x46546C67 else '非法'})")
            if magic != 0x46546C67:
                ok = False

        # ---- 2. 崩溃隔离：提交必崩任务 ----
        print("\n[2] 提交必崩任务（模拟 OCCT 段错误）")
        code, sub2 = http("POST", "/api/v1/jobs", {
            "externalId": "e2e-crash",
            "sourcePath": STEP,
            "sourceFileName": "crash.step",
            "callbackUrl": "",
            "options": {"__crashTest": True},
        })
        job2 = wait_job(sub2["jobId"], timeout=120)
        print(f"    结果 -> {job2.get('status')}")
        print(f"    错误 -> {(job2.get('errorMessage') or '')[:110]}")
        if job2.get("status") != "FAILED":
            print("    [FAIL] 崩溃任务没有被正确判定为失败")
            ok = False

        # ---- 3. 服务是否还活着（隔离的价值所在）----
        print("\n[3] 崩溃之后服务是否仍可用")
        try:
            code, h2 = http("GET", "/healthz", timeout=10)
            if code == 200:
                print(f"    [OK] 服务存活，engine={h2.get('engine')} inflight={h2.get('inflight')}")
            else:
                print(f"    [FAIL] 服务无响应 HTTP {code}")
                ok = False
        except Exception as e:
            print(f"    [FAIL] 服务已死: {e}")
            ok = False
            return 1

        # ---- 4. 崩溃之后还能正常干活 ----
        print("\n[4] 崩溃之后继续转换新任务")
        code, sub3 = http("POST", "/api/v1/jobs", {
            "externalId": "e2e-after-crash",
            "sourcePath": STEP,
            "sourceFileName": "sample_gearbox.step",
            "callbackUrl": "",
        })
        job3 = wait_job(sub3["jobId"])
        print(f"    结果 -> {job3.get('status')}")
        if job3.get("status") != "SUCCESS":
            print(f"    [FAIL] 崩溃后无法继续服务: {job3.get('errorMessage')}")
            ok = False
        else:
            print("    [OK] 崩溃被隔离在子进程内，服务不受影响")

        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
