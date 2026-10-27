# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026~2028 深圳乘恺科技有限公司
#
# 本文件是 CK-CADForge 的一部分，依据 GNU 宽通用公共许可证第 3 版或更高版本发布。
# 完整条款见仓库根目录：LICENSE（LGPL-3.0）、COPYING（GPL-3.0，LGPL-3.0 以其为补充）。

"""
生成真实 STEP 测试件（AP242，含装配层级 / 零件名 / 颜色 / 多次引用）。

为什么自带生成器：不能指望 CI 或新同事手边正好有一个带层级的 STEP。
用 OCCT 自己造一个，测试才能随时跑。

用法：
    python tests/make_sample_step.py [输出路径]

校准记录（pythonocc 7.7.2 / OCCT 7.7.2，实测）：
  - TDocStd_Document 必须传 str，传 TCollection_ExtendedString 会崩（0xC0000409）
  - 其余 XCAF 调用正常
"""

import os
import sys


def build(output_path: str) -> str:
    from OCC.Core.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCC.Core.gp import gp_Trsf, gp_Vec, gp_Pnt, gp_Dir, gp_Ax2
    from OCC.Core.Quantity import Quantity_Color, Quantity_TOC_RGB
    from OCC.Core.STEPCAFControl import STEPCAFControl_Writer
    from OCC.Core.TDataStd import TDataStd_Name
    from OCC.Core.TDocStd import TDocStd_Document
    from OCC.Core.TopLoc import TopLoc_Location
    from OCC.Core.XCAFApp import XCAFApp_Application
    from OCC.Core.XCAFDoc import XCAFDoc_DocumentTool, XCAFDoc_ColorSurf

    # 注意：构造参数传 str。传 TCollection_ExtendedString 在这套绑定里会段错误。
    doc = TDocStd_Document("MDTV-XCAF")
    app = XCAFApp_Application.GetApplication()
    app.InitDocument(doc)

    shape_tool = XCAFDoc_DocumentTool.ShapeTool(doc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool(doc.Main())

    def add_part(shape, name, color_rgb) -> "object":
        label = shape_tool.AddShape(shape, False)
        TDataStd_Name.Set(label, name)
        color_tool.SetColor(label, Quantity_Color(*color_rgb, Quantity_TOC_RGB), XCAFDoc_ColorSurf)
        return label

    def trsf(x=0.0, y=0.0, z=0.0):
        t = gp_Trsf()
        t.SetTranslation(gp_Vec(x, y, z))
        return TopLoc_Location(t)

    # ---- 零件（单位：mm）----
    base = add_part(BRepPrimAPI_MakeBox(240.0, 160.0, 20.0).Shape(), "底板", (0.62, 0.66, 0.70))
    cover = add_part(BRepPrimAPI_MakeBox(200.0, 120.0, 12.0).Shape(), "端盖", (0.35, 0.55, 0.75))
    bolt = add_part(BRepPrimAPI_MakeCylinder(6.0, 26.0).Shape(), "内六角螺栓 M6", (0.75, 0.72, 0.60))
    shaft = add_part(
        BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 14.0, 180.0).Shape(),
        "主轴", (0.85, 0.55, 0.25),
    )

    # ---- 装配：根 + 子装配（模拟真实产品的两级结构）----
    root = shape_tool.NewShape()
    TDataStd_Name.Set(root, "减速箱总成")

    fasteners = shape_tool.NewShape()
    TDataStd_Name.Set(fasteners, "紧固件组")

    shape_tool.AddComponent(root, base, trsf(0, -95, 0))
    shape_tool.AddComponent(root, cover, trsf(0, 10, 30))
    shape_tool.AddComponent(root, shaft, trsf(0, 0, 0))
    shape_tool.AddComponent(root, fasteners, trsf(0, 0, 0))

    # 同一零件被引用 4 次 —— 真实装配里螺钉常常几十上百颗，
    # 这是几何去重最主要的收益来源，测试件必须覆盖
    for (x, z) in ((-80, -55), (80, -55), (-80, 55), (80, 55)):
        shape_tool.AddComponent(fasteners, bolt, trsf(x, -82, z))

    shape_tool.UpdateAssemblies()

    writer = STEPCAFControl_Writer()
    writer.SetNameMode(True)
    writer.SetColorMode(True)
    writer.SetLayerMode(True)
    writer.SetPropsMode(True)
    writer.Transfer(doc, False)
    status = writer.Write(output_path)
    return output_path, status


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "sample_gearbox.step")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        path, status = build(out)
    except Exception as e:
        print(f"生成失败: {type(e).__name__}: {e}")
        sys.exit(1)
    if os.path.isfile(path):
        print(f"已生成 {path}  ({os.path.getsize(path)} bytes, write status={status})")
    else:
        print(f"未生成文件（write status={status}）")
        sys.exit(1)
