# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026~2028 深圳乘恺科技有限公司
#
# 本文件是 CK-CADForge 的一部分，依据 GNU 宽通用公共许可证第 3 版或更高版本发布。
# 完整条款见仓库根目录：LICENSE（LGPL-3.0）、COPYING（GPL-3.0，LGPL-3.0 以其为补充）。

"""
转换管线 —— 有 OCCT 就走真实转换，没有就降级到演示管线。

真实管线的阶段：
  读入(XCAF) → 网格化 → 写出 GLB → 从 GLB 反推结构树 → 重命名锚点 → 压缩

## 关于结构树：为什么从 glTF 反推，而不是直接遍历 XCAF

直觉做法是遍历 XCAF label 树拿层级和名称。但 pythonocc 7.7.2 的这套 SWIG 绑定里，
读 label 属性必须走 Handle（TDataStd_Name / Quantity_ColorRGBA），而 Handle 的
GetObject() 恒为 None —— 拿不到值。

换一条路：OCCT 写出的 glTF 本身就带完整的 node 层级、node name（来自 label）
和 material 颜色。直接从 GLB 反推结构树有三个好处：
  1. 与三维模型天然对齐，不需要猜测遍历顺序是否一致
  2. 不依赖那套坏的 Handle 绑定
  3. 层级关系来自渲染数据本身 —— 所见即所得，不会出现"树上有、画面里没有"

## API 校准记录（pythonocc-core 7.7.2 / OCCT 7.7.2，本机实测）

  - TDocStd_Document 必须传 str；传 TCollection_ExtendedString 会崩溃（0xC0000409）
  - RWGltf_CafWriter 同理，传 TCollection_AsciiString 会崩溃
  - STEPCAFControl_Reader.ReadFile 传 str 即可
  - BRepBndLib 用 brepbndlib_Add(shape, box) 两参数形式
  - 网格化必须在 writer.Perform 之前，否则 OCCT 会跳过无三角化的节点
"""

import json
import os
import subprocess
import time

from . import glb_util, mock_pipeline, progress

# glTF 规范以米为单位；STEP 源绝大多数是毫米。忘了换算模型会差 1000 倍。
DEFAULT_INPUT_UNIT_M = 0.001

# 演示几何（mock 管线）默认关闭。
# 这里踩过一次线上事故：服务被起在没有 OCCT 的解释器上，转换"成功"但产物是
# 一个与源文件毫无关系的演示减速箱 —— 前端指标正常、状态 SUCCESS，只有人眼
# 才看得出"轻量化图明显有问题"。宁可让任务失败并说清原因，也不产出假模型。
ALLOW_MOCK = os.environ.get("CADFORGE_ALLOW_MOCK", "").strip().lower() in ("1", "true", "yes", "on")

# 输出格式。README 的「输出格式」表与这里必须是同一份事实 ——
# 新增格式时：在这里加一个 key，并接上对应的写出器（计划统一收在 app/formats.py）。
DEFAULT_TARGET_FORMAT = "glb"
SUPPORTED_TARGET_FORMATS = {"glb"}


def resolve_target_format(options: dict) -> str:
    """
    解析并校验 options.targetFormat，返回规范化后的格式名。

    为什么不支持的值要直接抛错、而不是降级成 glb：
    调用方明确要 stl，却拿回一个 glb 且状态 SUCCESS —— 这正是演示管线那次
    事故的形状（指标正常、状态成功，只有人眼能看出不对）。宁可失败并说清原因，
    也不产出"看着成功了但其实不是请求的东西"。
    """
    fmt = str((options or {}).get("targetFormat") or DEFAULT_TARGET_FORMAT).strip().lower()
    if fmt not in SUPPORTED_TARGET_FORMATS:
        raise RuntimeError(
            f"暂不支持的输出格式：{fmt}。当前仅支持 {sorted(SUPPORTED_TARGET_FORMATS)}，"
            f"省略 options.targetFormat 即默认 {DEFAULT_TARGET_FORMAT}。"
        )
    return fmt


def occt_available() -> bool:
    try:
        from OCC.Core.STEPCAFControl import STEPCAFControl_Reader  # noqa: F401
        return True
    except Exception:
        return False


def occt_version() -> str:
    try:
        import OCC
        return getattr(OCC, "VERSION", "unknown")
    except Exception:
        return "unknown"


def convert(job: dict, out_dir: str) -> dict:
    """
    执行转换，返回 {artifacts, metrics}。

    artifacts 里的 path 是磁盘绝对路径 —— CK-PLM 与转换服务共享文件卷时
    可以直接读（零拷贝）；读不到时 CK-PLM 会退回到 HTTP 下载。
    """
    req = job.get("request") or {}
    src = req.get("sourcePath")
    options = req.get("options") or {}
    # 格式校验放在最前面：不支持的格式不该占住 Worker 槽位再失败，
    # 也不该在跑完网格化之后才告诉调用方"这个格式我不认"。
    # 当前唯一支持的 glb 就是管线写死的产物，所以这里只分派给真实管线做记录，
    # 真正的校验意义在于"不支持的值不许悄悄变成 glb"。
    target_format = resolve_target_format(options)

    # 故障注入：仅供自动化验证子进程隔离是否真的挡得住段错误。
    # OCCT 遇到畸形几何会直接 SIGSEGV，这里用一个已知必崩的调用来复现。
    if options.get("__crashTest"):
        from OCC.Core.TCollection import TCollection_ExtendedString
        from OCC.Core.TDocStd import TDocStd_Document
        TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))  # 必崩（0xC0000409）
        raise RuntimeError("没有崩 —— 故障注入失效")

    progress.report(out_dir, "准备转换环境", 5)

    if occt_available() and src and os.path.isfile(src):
        return _convert_with_occt(job, out_dir, target_format)

    # 不能再"默默降级"了：演示几何会被当成真实转换结果发给 CK-PLM，
    # 而它的指标（面片数/零件数）看起来完全正常，只有肉眼能发现不对。
    if not ALLOW_MOCK:
        reason = ("当前解释器没有 OCCT（pythonocc-core），无法执行真实转换"
                  if not occt_available() else f"源文件不在本机：{src}")
        raise RuntimeError(
            f"{reason}。已禁用演示管线 —— 请用装了 OCCT 的解释器启动服务"
            f"（/healthz 的 engine 应为 occt）；仅本地联调可设 CADFORGE_ALLOW_MOCK=1 放行）。"
        )
    return mock_pipeline.build_demo(job, out_dir)


# ==================== 真实管线 ====================


def _convert_with_occt(job: dict, out_dir: str, target_format: str = DEFAULT_TARGET_FORMAT) -> dict:
    started = time.time()
    req = job.get("request") or {}
    src = req["sourcePath"]
    job_id = job.get("jobId", "job")
    options = req.get("options") or {}

    from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
    from OCC.Core.Message import Message_ProgressRange
    from OCC.Core.RWGltf import RWGltf_CafWriter
    from OCC.Core.RWMesh import RWMesh_CoordinateSystemConverter
    from OCC.Core.STEPCAFControl import STEPCAFControl_Reader
    from OCC.Core.TColStd import TColStd_IndexedDataMapOfStringString
    from OCC.Core.TDocStd import TDocStd_Document
    from OCC.Core.TDF import TDF_LabelSequence
    from OCC.Core.XCAFApp import XCAFApp_Application
    from OCC.Core.XCAFDoc import XCAFDoc_DocumentTool

    # ---- 1. 读入：必须用 XCAF 框架 —— 它保留装配层级、零件名、颜色、材料。
    #         用 STEPControl_Reader 只会得到一个扁平 shape，结构信息全丢，
    #         而那些正是 PLM 要的东西。
    progress.report(out_dir, "读入 STEP 装配（名称 / 颜色 / 层级）", 10)
    doc = TDocStd_Document("MDTV-XCAF")
    app = XCAFApp_Application.GetApplication()
    app.InitDocument(doc)

    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(True)
    reader.SetLayerMode(True)
    reader.SetPropsMode(True)  # AP242 扩展属性（材料等）
    status = reader.ReadFile(src)
    if status != 1:  # IFSelect_RetDone
        raise RuntimeError(f"STEP 读取失败，返回状态 {status}")
    if not reader.Transfer(doc):
        raise RuntimeError("STEP 转入 XCAF 文档失败")

    shape_tool = XCAFDoc_DocumentTool.ShapeTool(doc.Main())

    # ---- 2. 网格化：按每个零件自身包围盒自适应弦差。
    #         全局统一弦差要么把螺钉糊成一坨，要么让大件产生几百万三角面。
    #         这一步必须在写出之前 —— OCCT 会跳过没有三角化数据的节点。
    linear_ratio = float(options.get("linearRatio", 0.002))
    angular = float(options.get("angularDeflection", 0.35))
    progress.report(out_dir, "网格化零件几何", 22)
    meshed = _tessellate(shape_tool, linear_ratio=linear_ratio, angular=angular, out_dir=out_dir)

    # ---- 3. 写出 GLB（含单位换算）----
    glb_path = os.path.join(out_dir, "model.glb")
    conv = RWMesh_CoordinateSystemConverter()
    # STEP 源绝大多数是 mm，glTF 规范是 m。漏了这步模型会大 1000 倍，
    # 表现为"相机拉到底还是看不全"，很容易被误判成相机问题。
    #
    # 坑：只设 InputLengthUnit 不够 —— 7.7.2 上 OutputLengthUnit 默认是 -1.0
    #（表示未设置），此时 IsEmpty() 仍为 true，OCCT 会静默放弃整个变换。
    # 必须两端都显式设置。
    input_unit = float(options.get("inputUnitM", DEFAULT_INPUT_UNIT_M))
    conv.SetInputLengthUnit(input_unit)
    conv.SetOutputLengthUnit(1.0)

    progress.report(out_dir, "写出 GLB", 65)
    writer = RWGltf_CafWriter(glb_path, True)  # True = 二进制 GLB
    writer.SetCoordinateSystemConverter(conv)
    if not writer.Perform(doc, TColStd_IndexedDataMapOfStringString(), Message_ProgressRange()):
        raise RuntimeError("GLB 写出失败（OCCT Perform 返回 false）")
    if not os.path.isfile(glb_path) or os.path.getsize(glb_path) == 0:
        raise RuntimeError("GLB 未生成或为空")
    progress.report(out_dir, "GLB 写出完成", 75)

    # ---- 4. 从 GLB 反推结构树，并把 node name 改写成结构树 id ----
    #         这一步是结构树与三维模型联动的关键：前端靠 name 反查对象，
    #         而 OCCT 给的 name 是零件名（同名零件在装配体里极其常见），必须换成唯一 id。
    gltf, bin_data = glb_util.read_glb(glb_path)

    # 单位守卫：OCCT 这套坐标转换在不同版本上行为不一致（7.7.2 只设一端就静默失效）。
    # 与其信任它，不如看实际包围盒 —— 模型大到超出常识就说明没换算。
    scale_applied = 1.0
    bbox = glb_util.bounding_box(gltf)
    if bbox:
        extent = max(abs(v) for v in bbox[0] + bbox[1])
        # 机械零件不该超过 100 米；超过基本可以断定是 mm 没换算成 m
        if extent > 100.0 and input_unit < 1.0:
            scale_applied = input_unit
            bin_data = glb_util.scale_positions(gltf, bin_data, input_unit)

    progress.report(out_dir, "反推装配结构树", 78)
    nodes = _build_structure_from_gltf(gltf)
    if not nodes:
        raise RuntimeError("GLB 中没有可用节点（可能所有几何都未被三角化）")
    renamed = glb_util.rename_nodes(gltf, [n["id"] for n in nodes])
    glb_util.write_glb(gltf, bin_data, glb_path)

    # ---- 5. 可选压缩：必须在重命名之后，且必须带 -kn -km，
    #         否则 gltfpack 会把 node name 清掉，联动锚点就没了。
    progress.report(out_dir, "压缩模型", 85)
    final_glb = _try_compress(glb_path, out_dir)

    structure = {
        "version": "1.0",
        "generator": f"CK-CADForge occt {occt_version()}",
        "sourceFileName": req.get("sourceFileName"),
        "units": {"length": "mm", "scaleToMeters": DEFAULT_INPUT_UNIT_M},
        "rootId": nodes[0]["id"],
        # 顶层可能不止一个（STEP 里常有游离零件与装配根并列），前端按森林渲染
        "rootIds": [n["id"] for n in nodes if n["parentId"] is None],
        "nodes": nodes,
    }
    structure_path = os.path.join(out_dir, "structure.json")
    with open(structure_path, "w", encoding="utf-8") as f:
        json.dump(structure, f, ensure_ascii=False, indent=2)

    size = os.path.getsize(final_glb)

    # ---- 6. 缩略图：在最终 GLB 上渲染 ----
    #         必须放在压缩之后 —— 列表里的小图不需要高精度网格，但必须与用户
    #         点开看到的模型是同一个东西（用压缩前的中间产物会得到"看着像但是另一个"的图）。
    progress.report(out_dir, "渲染缩略图", 92)
    thumb_path = _render_thumbnail(final_glb, out_dir, job_id, options)
    progress.report(out_dir, "收尾（登记产物）", 99)

    artifacts = [
        {"kind": "glb", "path": final_glb, "fileName": f"{job_id}.glb", "size": size},
        {"kind": "structure", "path": structure_path, "fileName": f"{job_id}.structure.json",
         "size": os.path.getsize(structure_path)},
    ]
    if thumb_path:
        artifacts.append({"kind": "thumbnail", "path": thumb_path, "fileName": f"{job_id}.png",
                          "size": os.path.getsize(thumb_path)})

    return {
        "artifacts": artifacts,
        "metrics": {
            "units": "mm",
            "triangleCount": glb_util.count_triangles(gltf),
            "partCount": sum(1 for n in nodes if n["type"] == "part"),
            "nodeCount": len(nodes),
            "meshedShapeCount": meshed,
            "anchorsRenamed": renamed,
            # 命名质量指标：pythonocc 7.7.2 上零件名常常拿不到（OCCT 只给了 label entry），
            # 这个值偏低说明需要在更高版本的 OCCT 上重跑，而不是转换器逻辑有问题
            "namedNodeCount": sum(1 for n in nodes if not n["name"].startswith("零件-")),
            "unitScaleApplied": scale_applied,
            "thumbnailGenerated": bool(thumb_path),
            "outputSize": size,
            "durationMs": int((time.time() - started) * 1000),
            "converterVersion": f"occt-{occt_version()}",
            # 回显请求要的格式：将来支持多种格式时，这是核对"产的是不是要的"的最省事依据
            "targetFormat": target_format,
        },
    }


def _tessellate(shape_tool, linear_ratio=0.002, angular=0.35, out_dir=None) -> int:
    """
    逐零件自适应网格化。弦差按零件自身包围盒对角线取比例。返回成功网格化的数量。

    网格化是整条流水线里最久的一段（大装配体几十秒到几分钟），
    也是唯一天然带「第几个 / 共几个」的地方 —— 进度就从这里逐件上报，
    映射到 22~62 这一区间（前后各留出余量给读入与写出）。
    """
    from OCC.Core.BRepBndLib import brepbndlib_Add
    from OCC.Core.Bnd import Bnd_Box
    from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
    from OCC.Core.TDF import TDF_LabelSequence

    seq = TDF_LabelSequence()
    shape_tool.GetShapes(seq)
    total = seq.Length()
    # 零件太多时不必每件都报一次：文件写入本身也是一次 IO
    step = max(1, total // 40)
    done = 0
    for i in range(seq.Length()):
        if out_dir and (i % step == 0 or i == total - 1):
            progress.report(out_dir, f"网格化零件 {i + 1}/{total}", 22 + int(40 * i / max(1, total)))
        shape = shape_tool.GetShape(seq.Value(i + 1))
        if shape is None or shape.IsNull():
            continue
        box = Bnd_Box()
        try:
            brepbndlib_Add(shape, box)
        except Exception:
            pass
        diag = 1.0
        if not box.IsVoid():
            try:
                diag = box.CornerMax().Distance(box.CornerMin()) or 1.0
            except Exception:
                diag = 1.0
        # 机架和 M3 螺钉尺寸能差三个数量级，全局统一弦差必然两头不讨好
        linear = max(diag * linear_ratio, 1e-4)
        try:
            BRepMesh_IncrementalMesh(shape, linear, False, angular, True)
            done += 1
        except Exception:
            continue
    return done


def _is_label_entry(name: str) -> bool:
    """判断是不是 OCCT 的 label entry 占位名（形如 =>[0:1:1:3] 或 [0:1:1]）。"""
    if not name:
        return True
    s = name.strip()
    if s.startswith("=>["):
        return True
    # 纯标签路径：只有数字、冒号和方括号
    stripped = s.lstrip("[").rstrip("]")
    if stripped and all(c.isdigit() or c == ":" for c in stripped):
        return True
    return False


def _build_structure_from_gltf(gltf: dict) -> list:
    """
    从 glTF 节点反推结构树。

    id 用 n{索引} —— 与 glTF node 顺序严格一一对应，这是联动的唯一锚点。
    层级从 children 反推，颜色从 material 取，几何复用从 mesh 引用次数取。
    """
    raw_nodes = gltf.get("nodes") or []
    materials = gltf.get("materials") or []
    meshes = gltf.get("meshes") or []

    parent_of = {}
    for i, node in enumerate(raw_nodes):
        for child in node.get("children") or []:
            if isinstance(child, int) and 0 <= child < len(raw_nodes):
                parent_of[child] = i

    # 顶层节点（scene 里列出的，或被任何节点引用不到的）
    scene_roots = set()
    for scene in gltf.get("scenes") or []:
        for n in scene.get("nodes") or []:
            scene_roots.add(n)

    nodes = []
    mesh_first_use = {}
    for i, raw in enumerate(raw_nodes):
        node_id = f"n{i}"
        children = raw.get("children") or []
        has_mesh = raw.get("mesh") is not None
        node_type = "assembly" if children else ("part" if has_mesh else "node")

        raw_name = (raw.get("name") or "").strip()
        # OCCT 在没有名字属性时会拿 label entry 当 node name（形如 =>[0:1:1:3]）。
        # 那种东西给用户看毫无意义，识别出来换成可读的默认名。
        if _is_label_entry(raw_name):
            raw_name = ""
        display = raw_name if raw_name else f"零件-{i}"

        color = None
        if has_mesh:
            try:
                prim = meshes[raw["mesh"]]["primitives"][0]
                mat_idx = prim.get("material")
                if mat_idx is not None:
                    pbr = materials[mat_idx].get("pbrMetallicRoughness") or {}
                    f = pbr.get("baseColorFactor")
                    if f and len(f) >= 3:
                        color = "#%02x%02x%02x" % (
                            int(max(0.0, min(1.0, f[0])) * 255),
                            int(max(0.0, min(1.0, f[1])) * 255),
                            int(max(0.0, min(1.0, f[2])) * 255),
                        )
            except Exception:
                color = None

        node = {
            "id": node_id,
            "parentId": f"n{parent_of[i]}" if i in parent_of else None,
            "name": display,
            "number": None,
            "type": node_type,
            "color": color,
            "gltfNodeIndex": i,
        }
        # 几何复用：同一 mesh 被多个节点引用 = 装配里的重复零件。
        # 这是最有效的体积优化来源，也是后续生成 BOM 时合并数量的依据。
        if has_mesh:
            first = mesh_first_use.get(raw["mesh"])
            if first is None:
                mesh_first_use[raw["mesh"]] = node_id
            else:
                node["instanceOf"] = first
        nodes.append(node)

    # 顶层判定：scene 里显式列出的才算根；避免把游离零件误当根后又挂在别处
    for n in nodes:
        if n["parentId"] is None and n["gltfNodeIndex"] not in scene_roots:
            # 不在 scene 里且无父 —— 仍然当根处理，避免节点在树上消失
            pass
    return nodes


def _render_thumbnail(glb_path: str, out_dir: str, job_id: str, options: dict):
    """
    渲染列表用的缩略图 PNG，返回路径或 None。

    失败一律吞掉：少一张小图只是列表里显示占位，而已经成功的 GLB 不该
    因为出图失败被判整体失败 —— 两者的代价完全不对称。
    """
    if options.get("generateThumbnail") is False:
        return None
    try:
        from . import thumbnail

        path = os.path.join(out_dir, "thumbnail.png")
        size = int(options.get("thumbnailSize") or 512)
        info = thumbnail.render_glb(glb_path, path, size=size)
        print(f"[pipeline] 缩略图已生成: {os.path.basename(path)} "
              f"({info['width']}x{info['height']}, {info['triangles']} 三角形)")
        return path
    except Exception as e:  # noqa: BLE001
        print(f"[pipeline] 缩略图生成失败（不影响转换结果）: {type(e).__name__}: {e}")
        return None


def _try_compress(glb_path: str, out_dir: str) -> str:
    """
    调用 gltfpack 压缩。-kn / -km 必须带，否则 node name 会被清掉，
    结构树与三维模型的联动锚点就没了。压缩失败一律回退到未压缩产物。
    """
    out_path = os.path.join(out_dir, "model.opt.glb")
    try:
        subprocess.run(
            ["gltfpack", "-i", glb_path, "-o", out_path, "-cc", "-kn", "-km"],
            check=True, timeout=300,
        )
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            # 压缩后校验锚点是否存活 —— 清掉 name 的压缩比省下的体积贵得多
            try:
                gltf, _ = glb_util.read_glb(out_path)
                names = [n.get("name") for n in gltf.get("nodes") or []]
                if any(not nm for nm in names):
                    return glb_path
            except Exception:
                return glb_path
            return out_path
    except Exception:
        pass
    return glb_path
