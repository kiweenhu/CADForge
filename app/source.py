# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026~2028 深圳乘恺科技有限公司
#
# 本文件是 CK-CADForge 的一部分，依据 GNU 宽通用公共许可证第 3 版或更高版本发布。
# 完整条款见仓库根目录：LICENSE（LGPL-3.0）、COPYING（GPL-3.0，LGPL-3.0 以其为补充）。

"""
源件获取 —— 三条路，按优先级：

  1. sourcePath 本机可读            → 同机部署 / 共享卷且挂载路径一致（零改动）
  2. sourceRelativePath + 根目录     → 有共享存储但**两边挂载点不同**
  3. sourceUrl 流式下载              → **完全没有共享存储**（中小企业最常见的分布式形态）

为什么必须有第 2、3 条：
CK-PLM 提交的 sourcePath 是"它视角"的绝对路径，本服务原来直接 os.path.isfile 判断。
同机联调没问题，一旦分布式部署且没有共享卷，这个判断恒为 false —— 所有任务都会
以"源文件不在本机"失败。而中小企业往往就是两台机器各挂各的盘，NFS/Ceph 那套
运维门槛太高，不该成为使用前提。

下载为什么放在父进程、且必须在拿到车道槽位之后：
  - 放父进程：子进程只管转换，超时口径不互相污染（下载慢不会被判成"转换超时"）；
  - 拿到槽位之后：否则排队任务会一起下载，把磁盘和带宽同时打满；
  - 流式写盘：几百 MB 的 STEP 整读进内存会直接 OOM。
"""

import hashlib
import os
import time
from urllib.parse import urlparse

# 源件根目录：配合 sourceRelativePath 使用，解决"两边挂载点不同"的问题。
SOURCE_ROOT = os.environ.get("CADFORGE_SOURCE_ROOT", "").strip()

# 下载超时。与 CADFORGE_JOB_TIMEOUT 分开：后者只管子进程转换。
# 几百 MB 的 STEP 在慢网络上不该被判成"转换超时"——那是两种完全不同的故障。
SOURCE_TIMEOUT = float(os.environ.get("CADFORGE_SOURCE_TIMEOUT", "300"))

# 下载鉴权：与回调共用同一把服务间令牌，不引入第二套凭据。
SOURCE_TOKEN = (os.environ.get("CADFORGE_SOURCE_TOKEN")
                or os.environ.get("CADFORGE_CALLBACK_TOKEN") or "").strip()

# 白名单（host 或 host:port，逗号分隔）。
# 8090 没有入站鉴权 —— 能提交任务就能让本服务去拉任意 URL，
# 不设防的话这里就是一块探内网的跳板。配了才校验，不配则不拦（默认放开，便于联调）。
ALLOWED_HOSTS = {h.strip().lower()
                 for h in os.environ.get("CADFORGE_SOURCE_ALLOWED_HOSTS", "").split(",")
                 if h.strip()}

# 转换成功后是否保留下载的源件。默认删（磁盘不该为一个任务的中间文件翻倍），
# 失败时无论如何都保留 —— 排查线上问题必须有原件。
KEEP_SOURCE = os.environ.get("CADFORGE_KEEP_SOURCE", "").strip().lower() in ("1", "true", "yes", "on")

CHUNK = 1 << 20          # 1MB 分块
_PROGRESS_STEP = 16 << 20  # 每 16MB 报一次进度，避免快网络下刷屏


def url_allowed(url: str) -> bool:
    """URL 是否命中白名单。未配置白名单时一律放行。"""
    if not ALLOWED_HOSTS:
        return True
    p = urlparse(url or "")
    host = (p.hostname or "").lower()
    if not host:
        return False
    if p.port:
        candidates = {host, f"{host}:{p.port}"}
    else:
        candidates = {host, f"{host}:{443 if p.scheme == 'https' else 80}"}
    return bool(candidates & ALLOWED_HOSTS)


def resolve_source(req: dict, out_dir: str, on_progress=None):
    """
    解析出本机可读的源件路径，返回 (路径, 信息字典)。

    信息字典会并进任务的 metrics —— 分布式下"到底慢在下载还是慢在转换"
    必须有据可查，否则只能靠猜。
    """
    src = str(req.get("sourcePath") or "").strip()
    rel = str(req.get("sourceRelativePath") or "").strip()
    url = str(req.get("sourceUrl") or "").strip()

    if src and os.path.isfile(src):
        return src, {"sourceFrom": "path"}

    if rel and SOURCE_ROOT:
        candidate = os.path.join(SOURCE_ROOT, rel)
        if os.path.isfile(candidate):
            return candidate, {"sourceFrom": "relative"}
        # 配了根却拼不出来，多半是挂载的不是同一份存储 —— 把拼出的路径直接给出来，
        # 运维照着改就行，不用再来回猜。
        raise RuntimeError(
            f"按 CADFORGE_SOURCE_ROOT 拼接后仍找不到源件：{candidate}"
            f"（sourceRelativePath={rel}）。请确认两边挂载的是同一份存储，且相对路径一致。"
        )

    if url:
        return _download(url, out_dir, req.get("sourceFileName"), req.get("sourceSha1"), on_progress)

    raise RuntimeError(
        "源件不可达：本机读不到 sourcePath，也没有可用的 sourceRelativePath + "
        "CADFORGE_SOURCE_ROOT 或 sourceUrl。三种解法任选其一 —— "
        "① 同机/共享卷部署且两边路径一致；"
        "② 配置 CADFORGE_SOURCE_ROOT 并由 CK-PLM 传 sourceRelativePath；"
        "③ 由 CK-PLM 传带签名的 sourceUrl（无需共享存储）。"
    )


def cleanup_source(path: str, info: dict, success: bool) -> None:
    """转换成功后删除下载的源件；失败时保留（排查线上问题必须有原件）。"""
    if not path or info.get("sourceFrom") != "url":
        return
    if not success or KEEP_SOURCE:
        return
    try:
        os.remove(path)
    except OSError:
        pass


# ==================== 下载 ====================


def _download(url: str, out_dir: str, file_name=None, expect_sha1=None, on_progress=None):
    if not url_allowed(url):
        raise RuntimeError(
            f"源件地址不在白名单内：{urlparse(url).netloc}。"
            f"若确需使用，请配置 CADFORGE_SOURCE_ALLOWED_HOSTS（host 或 host:port，逗号分隔）。"
        )

    import httpx  # 局部导入：worker 被 main 引入时不必为下载路径付出导入成本

    dest = os.path.join(out_dir, _safe_name(file_name) or _name_from_url(url) or "source.step")
    headers = {"X-Cad-Forge-Token": SOURCE_TOKEN} if SOURCE_TOKEN else {}
    # 连接超时给短一点（网络不通要早失败），读超时才是大文件该等的
    timeout = httpx.Timeout(10.0, read=SOURCE_TIMEOUT)

    digest = hashlib.sha1()
    total = 0
    next_report = _PROGRESS_STEP
    started = time.time()
    try:
        with httpx.stream("GET", url, headers=headers, timeout=timeout,
                          follow_redirects=False) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes(CHUNK):
                    f.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
                    if on_progress and total >= next_report:
                        next_report = total + _PROGRESS_STEP
                        on_progress(f"下载源件 {total // CHUNK} MB", 5)
    except Exception as e:  # noqa: BLE001 —— 网络/IO 的任何失败都要归一成"获取源件失败"
        # 半截文件比没有文件更糟：OCCT 读坏文件可能直接段错误，
        # 而症状是"转换器崩了"，很难想到是下载断在半路。
        _remove(dest)
        raise RuntimeError(f"源件下载失败：{type(e).__name__}: {e}")

    if total == 0:
        _remove(dest)
        raise RuntimeError(f"源件下载为空：{url}")

    expected = str(expect_sha1 or "").strip().lower()
    if expected and digest.hexdigest() != expected:
        _remove(dest)
        raise RuntimeError(
            f"源件 SHA1 校验不通过：期望 {expected}，实际 {digest.hexdigest()}（已删除不完整文件）。"
            f"多半是下载中断或缓存了旧版本。"
        )

    return dest, {
        "sourceFrom": "url",
        "sourceBytes": total,
        "sourceDownloadMs": int((time.time() - started) * 1000),
    }


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _safe_name(file_name) -> str:
    """只取文件名部分，挡掉 ../ 之类的路径穿越。"""
    name = os.path.basename(str(file_name or "").strip())
    return name if name and name not in (".", "..") else ""


def _name_from_url(url: str) -> str:
    name = _safe_name(urlparse(url).path.rsplit("/", 1)[-1])
    return name or ""
