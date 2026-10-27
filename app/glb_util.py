# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026~2028 深圳乘恺科技有限公司
#
# 本文件是 CK-CADForge 的一部分，依据 GNU 宽通用公共许可证第 3 版或更高版本发布。
# 完整条款见仓库根目录：LICENSE（LGPL-3.0）、COPYING（GPL-3.0，LGPL-3.0 以其为补充）。

"""
GLB 读写与后处理。

存在的原因：OCCT 的 RWGltf_CafWriter 写出的 glTF，其 node name 来自 XCAF label
的零件名 —— 而真实装配体里同名零件极其常见（几十颗 M6 螺钉共用同一个名字）。
前端靠 name 反查三维对象，重名会让结构树联动错乱。

所以转换完成后统一做一次后处理：按遍历顺序把 node name 改写成结构树 id
（n0 / n1 / ...），保证锚点唯一。这一步必须在压缩之前做，
且压缩必须带 -kn -km，否则 name 又会被清掉。
"""

import json
import struct

GLB_MAGIC = 0x46546C67
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942


def read_glb(path: str):
    """解析 GLB，返回 (gltf_dict, bin_bytes)。"""
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) < 20:
        raise ValueError("文件过短，不是合法 GLB")
    magic, version, _total = struct.unpack_from("<III", raw, 0)
    if magic != GLB_MAGIC:
        raise ValueError(f"不是 GLB 文件（magic=0x{magic:08X}）")

    gltf = None
    bin_data = b""
    offset = 12
    while offset + 8 <= len(raw):
        chunk_len, chunk_type = struct.unpack_from("<II", raw, offset)
        body_start = offset + 8
        body = raw[body_start:body_start + chunk_len]
        if chunk_type == CHUNK_JSON:
            gltf = json.loads(body.decode("utf-8"))
        elif chunk_type == CHUNK_BIN:
            bin_data = body
        offset = body_start + chunk_len
        # chunk 按 4 字节对齐，这里按规范推进
        if offset % 4 != 0:
            offset += 4 - (offset % 4)
    if gltf is None:
        raise ValueError("GLB 缺少 JSON chunk")
    return gltf, bin_data


def write_glb(gltf: dict, bin_data: bytes, path: str) -> None:
    """按 GLB 规范打包落盘。"""
    json_bytes = json.dumps(gltf, ensure_ascii=False).encode("utf-8")
    while len(json_bytes) % 4 != 0:
        json_bytes += b" "  # JSON chunk 用空格补齐

    bin_padded = bin_data
    while len(bin_padded) % 4 != 0:
        bin_padded += b"\x00"  # BIN chunk 用零补齐

    total = 12 + 8 + len(json_bytes) + 8 + len(bin_padded)
    out = bytearray()
    out += struct.pack("<III", GLB_MAGIC, 2, total)
    out += struct.pack("<II", len(json_bytes), CHUNK_JSON)
    out += json_bytes
    out += struct.pack("<II", len(bin_padded), CHUNK_BIN)
    out += bin_padded

    with open(path, "wb") as f:
        f.write(bytes(out))


def rename_nodes(gltf: dict, names: list) -> int:
    """
    按索引顺序重命名 glTF 节点。返回实际改写的节点数。

    names[i] 对应 nodes[i]。names 短于 nodes 时，多余节点保持原名。
    """
    if not isinstance(gltf.get("nodes"), list):
        return 0
    count = 0
    for i, node in enumerate(gltf["nodes"]):
        if i < len(names) and names[i]:
            node["name"] = names[i]
            count += 1
    return count


def node_names(gltf: dict) -> list:
    return [n.get("name") for n in (gltf.get("nodes") or [])]


def scale_positions(gltf: dict, bin_data: bytes, factor: float) -> bytes:
    """
    把所有顶点坐标与节点平移按 factor 缩放，返回新的 bin 数据。

    为什么不只依赖 OCCT 的坐标转换器：那套 API 在不同版本上行为不一致
    （7.7.2 上只设 InputLengthUnit 会让 IsEmpty() 仍为 true，变换静默失效）。
    单位错了模型会大 1000 倍，而症状是"相机拉到底还是看不全"——
    极易被误判成相机问题。所以这里再守一道，用包围盒反查、必要时强制缩放。
    """
    if factor == 1.0:
        return bin_data
    buf = bytearray(bin_data)
    accessors = gltf.get("accessors") or []
    views = gltf.get("bufferViews") or []
    components = {"VEC3": 3, "VEC2": 2, "SCALAR": 1, "VEC4": 4}

    scaled = 0
    for mesh in gltf.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            acc_idx = (prim.get("attributes") or {}).get("POSITION")
            if acc_idx is None:
                continue
            acc = accessors[acc_idx]
            view = views[acc["bufferView"]]
            stride = view.get("byteStride") or (components.get(acc["type"], 3) * 4)
            start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
            count = acc["count"]
            ncomp = components.get(acc["type"], 3)
            for i in range(count):
                base = start + i * stride
                for c in range(ncomp):
                    off = base + c * 4
                    (v,) = struct.unpack_from("<f", buf, off)
                    struct.pack_into("<f", buf, off, v * factor)
            # 同步包围盒，否则视锥剔除和自动居中会算错
            if "min" in acc:
                acc["min"] = [v * factor for v in acc["min"]]
            if "max" in acc:
                acc["max"] = [v * factor for v in acc["max"]]
            scaled += 1

    for node in gltf.get("nodes") or []:
        if node.get("translation"):
            node["translation"] = [v * factor for v in node["translation"]]
        # matrix 列主序，平移在第 13/14/15 位
        m = node.get("matrix")
        if m and len(m) == 16:
            m[12] *= factor
            m[13] *= factor
            m[14] *= factor
    gltf["_scaledAccessors"] = scaled
    return bytes(buf)


def bounding_box(gltf: dict):
    """返回整体包围盒 (min, max)；没有顶点数据时返回 None。"""
    mins, maxs = [], []
    accessors = gltf.get("accessors") or []
    for mesh in gltf.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            acc_idx = (prim.get("attributes") or {}).get("POSITION")
            if acc_idx is None:
                continue
            acc = accessors[acc_idx]
            if "min" in acc and "max" in acc:
                mins.append(acc["min"])
                maxs.append(acc["max"])
    if not mins:
        return None
    return (
        [min(m[i] for m in mins) for i in range(3)],
        [max(m[i] for m in maxs) for i in range(3)],
    )


def count_triangles(gltf: dict) -> int:
    """粗算三角面数（仅用于指标展示，不参与渲染）。"""
    total = 0
    accessors = gltf.get("accessors") or []
    for mesh in gltf.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            try:
                if "indices" in prim:
                    total += accessors[prim["indices"]]["count"] // 3
                else:
                    pos = prim.get("attributes", {}).get("POSITION")
                    total += accessors[pos]["count"] // 3
            except Exception:
                continue
    return total
