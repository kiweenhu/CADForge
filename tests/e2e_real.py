# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026~2028 深圳乘恺科技有限公司
#
# 本文件是 CK-CADForge 的一部分，依据 GNU 宽通用公共许可证第 3 版或更高版本发布。
# 完整条款见仓库根目录：LICENSE（LGPL-3.0）、COPYING（GPL-3.0，LGPL-3.0 以其为补充）。

"""
真实内核端到端测试：用 tests/sample_gearbox.step 跑通完整管线并校验产物。

校验重点不是"生成了文件"，而是**结构树与三维模型的联动锚点是否成立** ——
即每个结构树 id 都能在 glTF 里找到同名 node。这个锚点断了，
前端点结构树就选不中模型，是最容易悄悄坏掉又最难发现的地方。

用法：
    python tests/e2e_real.py
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

STEP = os.path.join(HERE, "sample_gearbox.step")


def main() -> int:
    from app import glb_util, pipeline

    ok = True
    print(f"OCCT 可用: {pipeline.occt_available()}  版本: {pipeline.occt_version()}")

    if not os.path.isfile(STEP):
        print(f"缺少测试件 {STEP}，请先运行 tests/make_sample_step.py")
        return 1

    out_dir = tempfile.mkdtemp(prefix="cadforge-e2e-")
    job = {
        "jobId": "e2e-real",
        "request": {
            "sourcePath": STEP,
            "sourceFileName": "sample_gearbox.step",
            "callbackUrl": "",
            "options": {},
        },
    }

    try:
        result = pipeline.convert(job, out_dir)
        print(f"\n状态: {result['metrics'].get('converterVersion')}")
        for k, v in result["metrics"].items():
            print(f"  {k}: {v}")

        glb_item = next(a for a in result["artifacts"] if a["kind"] == "glb")
        st_item = next(a for a in result["artifacts"] if a["kind"] == "structure")

        print(f"\n产物: glb={glb_item['size']}B  structure={st_item['size']}B")
        if glb_item["size"] <= 0:
            print("  [FAIL] GLB 为空")
            ok = False

        # ---- 核心校验：锚点一致性 ----
        with open(st_item["path"], encoding="utf-8") as f:
            structure = json.load(f)
        nodes = structure["nodes"]
        print(f"\n结构树节点数: {len(nodes)}")
        for n in nodes[:12]:
            print(f"  {n['id']:>4} parent={str(n['parentId']):>5} "
                  f"{n['type']:<8} {n['name']}{'  <- 复用 ' + n['instanceOf'] if n.get('instanceOf') else ''}")

        gltf, _ = glb_util.read_glb(glb_item["path"])
        gltf_names = [n.get("name") for n in gltf.get("nodes") or []]
        missing = [n["id"] for n in nodes if n["id"] not in gltf_names]
        if missing:
            print(f"  [FAIL] {len(missing)} 个结构树 id 在 glTF 里找不到: {missing[:10]}")
            ok = False
        else:
            print(f"  [OK] 全部 {len(nodes)} 个结构树 id 都能在 glTF 中定位（联动锚点成立）")

        # ---- 校验几何确实被三角化 ----
        tris = glb_util.count_triangles(gltf)
        print(f"  三角面数: {tris}")
        if tris <= 0:
            print("  [FAIL] 没有任何三角面 —— 网格化没生效")
            ok = False

        # ---- 校验单位换算：模型应在合理米制尺寸内 ----
        # 测试件按 mm 设计（底板 240x160x20），换算后包围盒应在 ~0.24m 量级
        maxs = []
        for mesh in gltf.get("meshes") or []:
            for prim in mesh.get("primitives") or []:
                acc = gltf["accessors"][prim["attributes"]["POSITION"]]
                maxs.append(max(acc["max"]))
        if maxs:
            biggest = max(maxs)
            print(f"  最大坐标: {biggest:.4f} m")
            if biggest > 10:
                print("  [FAIL] 尺寸异常偏大 —— 单位换算（mm→m）可能没生效")
                ok = False
            elif biggest < 0.001:
                print("  [WARN] 尺寸异常偏小")
        return 0 if ok else 1
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
