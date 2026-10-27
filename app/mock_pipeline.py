# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026~2028 深圳乘恺科技有限公司
#
# 本文件是 CK-CADForge 的一部分，依据 GNU 宽通用公共许可证第 3 版或更高版本发布。
# 完整条款见仓库根目录：LICENSE（LGPL-3.0）、COPYING（GPL-3.0，LGPL-3.0 以其为补充）。

"""
演示管线 —— 不依赖 OCCT，程序生成一套可渲染的装配体（GLB + 结构树）。

存在的意义：OCCT 要装 conda 包（几百 MB），而 viewer 与编排链路的联调
不需要等它。先把"提交 → 回调 → 渲染 → 结构树联动"整条链路跑通，
再换真实内核，排查范围就只剩几何本身。

产物结构与真实管线完全一致（同一个 structure.json schema、同样的 node 命名约定），
所以前端不需要为 mock 做任何特殊处理。
"""

import array
import json
import math
import os
import struct
import time

MM_TO_M = 0.001  # glTF 规范以米为单位，STEP 源通常是毫米


def build_demo(job: dict, out_dir: str) -> dict:
    started = time.time()
    req = job.get("request") or {}
    job_id = job.get("jobId", "demo")

    # ---- 演示装配体：一块底板 + 一个端盖 + 四颗螺栓 + 一根主轴 ----
    # 单位按 mm 设计，落盘前统一乘 MM_TO_M。
    base = _box(240, 160, 20)
    cover = _box(200, 120, 12)
    bolt = _cylinder(6, 26, seg=20)
    shaft = _cylinder(14, 180, seg=24)

    # 几何去重演示：四颗螺栓共用同一个 mesh（真实装配体里螺钉常常几十上百颗，
    # 这一刀是体积优化里最有效的一刀）
    meshes = [
        {"key": "base", "geo": base, "color": [0.62, 0.66, 0.70, 1.0]},
        {"key": "cover", "geo": cover, "color": [0.35, 0.55, 0.75, 1.0]},
        {"key": "bolt", "geo": bolt, "color": [0.75, 0.72, 0.60, 1.0]},
        {"key": "shaft", "geo": shaft, "color": [0.85, 0.55, 0.25, 1.0]},
    ]

    # ---- 装配树 ----
    # node id 必须与 glTF 里 node 的 name 一致 —— 这是结构树与三维模型联动的唯一锚点。
    # 真实管线里这个约定同样成立：压缩时必须保留 node name（gltfpack 的 -kn）。
    nodes = [
        {"id": "n0", "parentId": None, "name": "演示减速箱总成", "number": "ASM-DEMO-001",
         "type": "assembly", "gltfNodeIndex": 0, "color": None},
        {"id": "n1", "parentId": "n0", "name": "底板", "number": "PLATE-001",
         "type": "part", "gltfNodeIndex": 1, "translate": [0, -95, 0]},
        {"id": "n2", "parentId": "n0", "name": "端盖", "number": "COVER-001",
         "type": "part", "gltfNodeIndex": 2, "translate": [0, 10, 0]},
        {"id": "n3", "parentId": "n0", "name": "内六角螺栓 M6", "number": "BOLT-M6",
         "type": "part", "gltfNodeIndex": 3, "translate": [-80, -82, -55]},
        {"id": "n4", "parentId": "n0", "name": "内六角螺栓 M6", "number": "BOLT-M6",
         "type": "part", "gltfNodeIndex": 4, "translate": [80, -82, -55]},
        {"id": "n5", "parentId": "n0", "name": "内六角螺栓 M6", "number": "BOLT-M6",
         "type": "part", "gltfNodeIndex": 5, "translate": [-80, -82, 55], "instanceOf": "n3"},
        {"id": "n6", "parentId": "n0", "name": "内六角螺栓 M6", "number": "BOLT-M6",
         "type": "part", "gltfNodeIndex": 6, "translate": [80, -82, 55], "instanceOf": "n3"},
        {"id": "n7", "parentId": "n0", "name": "主轴", "number": "SHAFT-001",
         "type": "part", "gltfNodeIndex": 7, "translate": [0, 0, 0]},
    ]

    glb_bytes, triangles = _build_glb(meshes, nodes)
    source_name = req.get("sourceFileName") or "unknown"

    glb_path = os.path.join(out_dir, "model.glb")
    with open(glb_path, "wb") as f:
        f.write(glb_bytes)

    structure = {
        "version": "1.0",
        "generator": "CK-CADForge mock pipeline",
        "sourceFileName": source_name,
        "units": {"length": "mm", "scaleToMeters": MM_TO_M},
        "rootId": "n0",
        "nodes": nodes,
    }
    structure_path = os.path.join(out_dir, "structure.json")
    with open(structure_path, "w", encoding="utf-8") as f:
        json.dump(structure, f, ensure_ascii=False, indent=2)

    duration_ms = int((time.time() - started) * 1000)
    return {
        "artifacts": [
            {"kind": "glb", "path": glb_path, "fileName": f"{job_id}.glb", "size": len(glb_bytes)},
            {"kind": "structure", "path": structure_path, "fileName": f"{job_id}.structure.json",
             "size": os.path.getsize(structure_path)},
        ],
        "metrics": {
            "units": "mm",
            "triangleCount": triangles,
            "partCount": len(nodes) - 1,
            "outputSize": len(glb_bytes),
            "durationMs": duration_ms,
            "converterVersion": "mock-0.1.0",
        },
    }


# ==================== 几何 ====================


def _box(w, h, d):
    """长方体：24 顶点（每面 4 个），带面法线。单位为 mm。"""
    x, y, z = w / 2.0, h / 2.0, d / 2.0
    faces = [
        (((x, -y, -z), (x, y, -z), (x, y, z), (x, -y, z)), (1, 0, 0)),
        (((-x, -y, z), (-x, y, z), (-x, y, -z), (-x, -y, -z)), (-1, 0, 0)),
        (((-x, y, -z), (-x, y, z), (x, y, z), (x, y, -z)), (0, 1, 0)),
        (((-x, -y, z), (-x, -y, -z), (x, -y, -z), (x, -y, z)), (0, -1, 0)),
        (((-x, -y, z), (x, -y, z), (x, y, z), (-x, y, z)), (0, 0, 1)),
        (((x, -y, -z), (-x, -y, -z), (-x, y, -z), (x, y, -z)), (0, 0, -1)),
    ]
    pos, nrm, idx = [], [], []
    for i, (quad, n) in enumerate(faces):
        b = i * 4
        pos.extend(quad)
        nrm.extend([n] * 4)
        idx.extend([b, b + 1, b + 2, b, b + 2, b + 3])
    return pos, nrm, idx


def _cylinder(r, h, seg=24):
    """圆柱（侧面 + 两端盖），单位为 mm。"""
    pos, nrm, idx = [], [], []
    for i in range(seg):
        a0 = 2 * math.pi * i / seg
        a1 = 2 * math.pi * (i + 1) / seg
        c0, s0 = math.cos(a0), math.sin(a0)
        c1, s1 = math.cos(a1), math.sin(a1)
        b = len(pos)
        pos += [(r * c0, -h / 2, r * s0), (r * c1, -h / 2, r * s1),
                (r * c1, h / 2, r * s1), (r * c0, h / 2, r * s0)]
        nrm += [(c0, 0, s0), (c1, 0, s1), (c1, 0, s1), (c0, 0, s0)]
        idx += [b, b + 1, b + 2, b, b + 2, b + 3]

    for sign in (1, -1):
        ny = (0, sign, 0)
        center = len(pos)
        pos.append((0, sign * h / 2, 0))
        nrm.append(ny)
        start = len(pos)
        for i in range(seg + 1):
            a = 2 * math.pi * i / seg
            pos.append((r * math.cos(a), sign * h / 2, r * math.sin(a)))
            nrm.append(ny)
        for i in range(seg):
            if sign > 0:
                idx += [center, start + i, start + i + 1]
            else:
                idx += [center, start + i + 1, start + i]
    return pos, nrm, idx


# ==================== GLB 组装 ====================


def _build_glb(meshes, nodes):
    """
    把若干几何打包成一个 GLB。

    meshes: [{key, geo=(pos,nrm,idx), color}]
    nodes:  结构树；index 0 是装配根（无 mesh），其后依次对应 meshes 的引用顺序
    """
    bin_data = bytearray()
    buffer_views = []
    accessors = []

    def add_view(payload: bytes, target=None):
        while len(bin_data) % 4 != 0:
            bin_data.append(0)
        off = len(bin_data)
        bin_data.extend(payload)
        view = {"buffer": 0, "byteOffset": off, "byteLength": len(payload)}
        if target:
            view["target"] = target
        buffer_views.append(view)
        return len(buffer_views) - 1

    gltf_meshes = []
    for m in meshes:
        pos, nrm, idx = m["geo"]
        # mm → m：glTF 规范以米为单位
        pos = [(x * MM_TO_M, y * MM_TO_M, z * MM_TO_M) for x, y, z in pos]

        flat_pos = array.array("f")
        for p in pos:
            flat_pos.extend(p)
        flat_nrm = array.array("f")
        for n in nrm:
            flat_nrm.extend(n)
        flat_idx = array.array("I", idx)

        pos_view = add_view(flat_pos.tobytes(), 34962)  # ARRAY_BUFFER
        nrm_view = add_view(flat_nrm.tobytes(), 34962)
        idx_view = add_view(flat_idx.tobytes(), 34963)  # ELEMENT_ARRAY_BUFFER

        xs = [p[0] for p in pos]
        ys = [p[1] for p in pos]
        zs = [p[2] for p in pos]
        accessors.append({"bufferView": pos_view, "componentType": 5126, "count": len(pos),
                          "type": "VEC3", "min": [min(xs), min(ys), min(zs)],
                          "max": [max(xs), max(ys), max(zs)]})
        pos_acc = len(accessors) - 1
        accessors.append({"bufferView": nrm_view, "componentType": 5126, "count": len(nrm),
                          "type": "VEC3"})
        nrm_acc = len(accessors) - 1
        accessors.append({"bufferView": idx_view, "componentType": 5125, "count": len(idx),
                          "type": "SCALAR"})
        idx_acc = len(accessors) - 1

        gltf_meshes.append({
            "name": m["key"],
            "primitives": [{"attributes": {"POSITION": pos_acc, "NORMAL": nrm_acc},
                            "indices": idx_acc, "material": len(gltf_meshes)}],
        })

    materials = [{
        "name": m["key"],
        "pbrMetallicRoughness": {
            "baseColorFactor": m["color"],
            "metallicFactor": 0.35,
            "roughnessFactor": 0.65,
        },
    } for m in meshes]

    # ---- 场景节点 ----
    # 每个节点的 name = 结构树 id —— 前端靠 scene.getObjectByName(id) 反查，
    # 这是结构树与三维模型联动的唯一锚点。
    mesh_order = ["base", "cover", "bolt", "bolt", "bolt", "bolt", "shaft"]
    mesh_key_to_index = {m["key"]: i for i, m in enumerate(meshes)}

    gltf_nodes = [{
        "name": nodes[0]["id"],
        "children": list(range(1, len(nodes))),
    }]
    for i, node in enumerate(nodes[1:]):
        key = mesh_order[i] if i < len(mesh_order) else "base"
        t = node.get("translate") or [0, 0, 0]
        gltf_nodes.append({
            "name": node["id"],
            "mesh": mesh_key_to_index[key],
            "translation": [t[0] * MM_TO_M, t[1] * MM_TO_M, t[2] * MM_TO_M],
        })

    triangles = sum(len(m["geo"][2]) // 3 for m in meshes) * len(mesh_order)

    gltf = {
        "asset": {"version": "2.0", "generator": "CK-CADForge mock pipeline"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": gltf_nodes,
        "meshes": gltf_meshes,
        "materials": materials,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(bin_data)}],
    }

    return _pack_glb(gltf, bytes(bin_data)), triangles


def _pack_glb(gltf: dict, bin_data: bytes) -> bytes:
    """按 GLB 规范打包：12 字节头 + JSON chunk + BIN chunk（均按 4 字节对齐）"""
    json_bytes = json.dumps(gltf, ensure_ascii=False).encode("utf-8")
    while len(json_bytes) % 4 != 0:
        json_bytes += b" "  # JSON chunk 用空格补齐

    bin_padded = bin_data
    while len(bin_padded) % 4 != 0:
        bin_padded += b"\x00"  # BIN chunk 用零补齐

    total = 12 + 8 + len(json_bytes) + 8 + len(bin_padded)
    out = bytearray()
    out += struct.pack("<III", 0x46546C67, 2, total)          # magic 'glTF', version, length
    out += struct.pack("<II", len(json_bytes), 0x4E4F534A)    # JSON chunk
    out += json_bytes
    out += struct.pack("<II", len(bin_padded), 0x004E4942)    # BIN chunk
    out += bin_padded
    return bytes(out)
