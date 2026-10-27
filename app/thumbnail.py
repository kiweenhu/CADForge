# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026~2028 深圳乘恺科技有限公司
#
# 本文件是 CK-CADForge 的一部分，依据 GNU 宽通用公共许可证第 3 版或更高版本发布。
# 完整条款见仓库根目录：LICENSE（LGPL-3.0）、COPYING（GPL-3.0，LGPL-3.0 以其为补充）。

"""
缩略图渲染 —— 把 GLB 画成一张 PNG。纯 numpy，不依赖 GPU、窗口系统或额外图像库。

## 为什么不用 OCCT 自带的可视化

{@code V3d_View} 出图需要一个已绑定的窗口（Windows 上是 {@code WNT_Window}）。
转换器跑在后台子进程里，没有桌面会话，建窗口会失败或得到黑图；
pythonocc 也没有稳定的 headless offscreen 入口。用不上，就不硬蹭。

## 为什么自己光栅化

输入本来就是三角网格（GLB 里已经是剖分好的三角形），
正交等轴测 + z-buffer + 平面着色，几十行 numpy 就够，
一张 512×512 的图在万级三角形上几十毫秒 —— 对"列表里 48px 的小图"完全够用。

## 输出

RGBA PNG，背景透明。列表、卡片、详情页可以放在任意底色上，
不需要为深色/浅色主题各出一版。
"""

import struct
import zlib

import numpy as np

from . import glb_util

# 没材质时的默认灰（与 OCCT 给无颜色零件的默认灰接近）
DEFAULT_COLOR = (0.72, 0.75, 0.79)
# 光照方向（glTF 是 Y-up，从右上前方打光）；双面材质取绝对值，背面不会全黑
LIGHT_DIR = np.array([0.35, 0.86, 0.42])
AMBIENT = 0.45
DIFFUSE = 0.55


def render_glb(glb_path: str, out_path: str, size: int = 512, supersample: int = 2) -> dict:
    """
    把 GLB 渲染成 PNG。返回 {width, height, triangles}。

    三角形数很多时自动降低超采样倍数：缩略图只有几十像素宽，
    为了它把大装配体多算四遍不值得。
    """
    gltf, bin_data = glb_util.read_glb(glb_path)
    tris, normals, colors = _collect_triangles(gltf, bin_data)
    total = tris.shape[0]
    if total == 0:
        raise RuntimeError("GLB 中没有可用三角形，无法生成缩略图")
    if total > 60000:
        supersample = 1

    rgb, alpha = _rasterize(tris, normals, colors, size * supersample)
    if supersample > 1:
        rgb, alpha = _downsample(rgb, alpha, supersample)

    _write_png(out_path, rgb, alpha)
    return {"width": size, "height": size, "triangles": int(total)}


# ==================== 几何采集 ====================


def _collect_triangles(gltf: dict, bin_data: bytes):
    """把场景里所有节点引用的三角形展开到世界坐标，返回 (三角形数组, 颜色数组)。"""
    nodes = gltf.get("nodes") or []
    meshes = gltf.get("meshes") or []
    materials = gltf.get("materials") or []

    parent_of = {}
    for i, node in enumerate(nodes):
        for child in node.get("children") or []:
            parent_of[child] = i
    world_cache = {}

    tri_blocks, normal_blocks, color_blocks = [], [], []
    for i, node in enumerate(nodes):
        mesh_index = node.get("mesh")
        if mesh_index is None or not (0 <= mesh_index < len(meshes)):
            continue
        world = _world_matrix(i, nodes, parent_of, world_cache)
        for prim in meshes[mesh_index].get("primitives") or []:
            # 只要三角形图元（4）；点/线在缩略图里没有意义
            if prim.get("mode", 4) != 4:
                continue
            positions = _read_vec3(gltf, bin_data, (prim.get("attributes") or {}).get("POSITION"))
            if positions is None or len(positions) < 3:
                continue
            indices = _read_indices(gltf, bin_data, prim.get("indices"), len(positions))
            if indices is None:
                indices = np.arange(len(positions), dtype=np.int64)
            if len(indices) < 3:
                continue
            indices = indices[: len(indices) // 3 * 3].reshape(-1, 3)

            verts = positions[indices]                                  # (M,3,3)
            verts = _apply_rows(world[:3, :3], verts.reshape(-1, 3))
            verts = verts.reshape(-1, 3, 3) + world[:3, 3]
            # 退化三角形（面积≈0，常见于剖分边缘）会在光栅化里被自然剔除，
            # 这里先按包围盒对角线过滤一次，省掉大量无用的像素循环
            if not _has_area(verts):
                continue

            # 顶点法线：OCCT 的剖分结果自带 NORMAL，直接用它做 Gouraud 插值。
            # 只用面法线做平面着色的话，圆柱面会被剖成一圈窄三角形，
            # 每个面片一个亮度 —— 缩略图上就是明暗相间的竖条（实测踩过）。
            vertex_normals = _read_vec3(gltf, bin_data, (prim.get("attributes") or {}).get("NORMAL"))
            if vertex_normals is not None and len(vertex_normals) == len(positions):
                vn = _apply_rows(world[:3, :3], vertex_normals[indices].reshape(-1, 3))
                vn = vn.reshape(-1, 3, 3)
                length = np.sqrt((vn * vn).sum(axis=-1, keepdims=True))
                vn = vn / np.maximum(length, 1e-12)
            else:
                # 没有法线就用面法线兜底（等价平面着色，形状仍然正确）
                face = np.cross(verts[:, 1] - verts[:, 0], verts[:, 2] - verts[:, 0])
                face_length = np.sqrt((face * face).sum(axis=-1, keepdims=True))
                face = face / np.maximum(face_length, 1e-12)
                vn = np.repeat(face[:, None, :], 3, axis=1)

            tri_blocks.append(verts)
            normal_blocks.append(vn)
            color_blocks.append(np.tile(_material_color(materials, prim.get("material")),
                                        (verts.shape[0], 1)))

    if not tri_blocks:
        return np.zeros((0, 3, 3)), np.zeros((0, 3, 3)), np.zeros((0, 3))
    return (np.concatenate(tri_blocks), np.concatenate(normal_blocks),
            np.concatenate(color_blocks))


def _matmul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """
    矩阵乘法，逐元素展开实现。

    为什么不直接写 {@code a @ b}：pythonocc 环境里的 numpy 是 conda 构建，
    调用 BLAS 时会去延迟加载 MKL 线程库，缺库就<b>直接崩进程</b>
    （Windows 退出码 0xC06D007E / ERROR_DELAY_LOAD_FAILED，无任何 Python 栈）。
    {@code import numpy}、普通 ufunc 都正常，只有 matmul 这类 BLAS 入口会踩雷 ——
    转换器是子进程运行，这种崩法会表现成"任务莫名失败"，极难定位。
    """
    return (a[..., :, :, None] * b[..., None, :, :]).sum(axis=-2)


def _apply_rows(m: np.ndarray, v: np.ndarray) -> np.ndarray:
    """每个行向量左乘矩阵：out[i] = v[i] · m（等价 v @ m.T）。"""
    return _matmul(v, m.T)


def _has_area(verts: np.ndarray) -> bool:
    a = verts[:, 1] - verts[:, 0]
    b = verts[:, 2] - verts[:, 0]
    cross = np.cross(a, b)
    return bool((np.linalg.norm(cross, axis=1) > 1e-12).any()) if cross.size else False


def _world_matrix(index: int, nodes: list, parent_of: dict, cache: dict) -> np.ndarray:
    """节点局部矩阵逐级左乘父矩阵。装配体的零件位置全靠这一层累乘。"""
    if index in cache:
        return cache[index]
    local = _local_matrix(nodes[index])
    parent = parent_of.get(index)
    world = local if parent is None else _matmul(_world_matrix(parent, nodes, parent_of, cache), local)
    cache[index] = world
    return world


def _local_matrix(node: dict) -> np.ndarray:
    matrix = node.get("matrix")
    if matrix and len(matrix) == 16:
        # glTF 的 matrix 是列主序
        return np.array(matrix, dtype=np.float64).reshape(4, 4).T
    t = node.get("translation") or [0.0, 0.0, 0.0]
    r = node.get("rotation") or [0.0, 0.0, 0.0, 1.0]  # xyzw
    s = node.get("scale") or [1.0, 1.0, 1.0]
    m = np.eye(4)
    x, y, z, w = r
    rotation = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])
    m[:3, :3] = _matmul(rotation, np.diag(s))
    m[:3, 3] = t
    return m


def _read_vec3(gltf: dict, bin_data: bytes, accessor_index):
    if accessor_index is None:
        return None
    accessors = gltf.get("accessors") or []
    if not (0 <= accessor_index < len(accessors)):
        return None
    acc = accessors[accessor_index]
    if acc.get("type") != "VEC3" or acc.get("componentType") != 5126:
        return None
    return _read_accessor(gltf, bin_data, accessor_index, np.dtype("<f4"), 3).astype(np.float64)


def _read_indices(gltf: dict, bin_data: bytes, accessor_index, vertex_count: int):
    if accessor_index is None:
        return None
    accessors = gltf.get("accessors") or []
    if not (0 <= accessor_index < len(accessors)):
        return None
    component = accessors[accessor_index].get("componentType")
    dtype = {5121: np.dtype("u1"), 5123: np.dtype("<u2"), 5125: np.dtype("<u4")}.get(component)
    if dtype is None:
        return None
    data = _read_accessor(gltf, bin_data, accessor_index, dtype, 1).reshape(-1).astype(np.int64)
    if data.size == 0:
        return None
    # 越界索引会让 numpy 直接抛异常，这里裁掉更稳（畸形模型也要能出图）
    return data[data < vertex_count]


def _read_accessor(gltf: dict, bin_data: bytes, accessor_index: int, dtype, components: int):
    """按 accessor 读二进制，支持带 byteStride 的交错布局。"""
    acc = gltf["accessors"][accessor_index]
    view = gltf["bufferViews"][acc["bufferView"]]
    count = int(acc["count"])
    start = int(view.get("byteOffset", 0)) + int(acc.get("byteOffset", 0))
    itemsize = dtype.itemsize * components
    stride = int(view.get("byteStride") or itemsize)
    if stride == itemsize:
        return np.frombuffer(bin_data, dtype=dtype, count=count * components,
                             offset=start).reshape(count, components)
    out = np.empty((count, components), dtype=dtype)
    for i in range(count):
        off = start + i * stride
        out[i] = np.frombuffer(bin_data, dtype=dtype, count=components, offset=off)
    return out


def _material_color(materials: list, material_index) -> np.ndarray:
    if material_index is None or not (0 <= material_index < len(materials)):
        return np.array(DEFAULT_COLOR)
    pbr = materials[material_index].get("pbrMetallicRoughness") or {}
    factor = pbr.get("baseColorFactor")
    if factor and len(factor) >= 3:
        # 提亮：真实的工程件多是 0.4 左右的深灰，直接按原色渲染在列表小图里
        # 糊成一块黑（实测 0.43 灰渲染出来几乎看不见轮廓）。
        # 往浅色区抬一档，既保留材质色相，又保证小图能看清形状。
        rgb = np.clip(np.array(factor[:3], dtype=np.float64), 0.0, 1.0)
        return 0.42 + 0.58 * rgb
    return np.array(DEFAULT_COLOR)


# ==================== 光栅化 ====================


def _rasterize(tris: np.ndarray, vertex_normals: np.ndarray, colors: np.ndarray, canvas: int):
    """正交等轴测投影 + z-buffer + Gouraud 着色。返回 (rgb 0-255, alpha 0-255)。"""
    points = tris.reshape(-1, 3)
    center = (points.min(axis=0) + points.max(axis=0)) / 2.0

    # 相机：让包围盒最长轴横躺屏幕水平方向，再从侧上方约 38° 俯视。
    # 固定斜视角下，轴类/螺钉类零件会在 48px 的小图里斜成一根"斜棍"，方向都看不出来；
    # 按最长轴对齐后，"头在哪、杆朝哪"一眼可辨。
    extent = points.max(axis=0) - points.min(axis=0)
    longest = int(np.argmax(extent))
    axis = np.zeros(3)
    axis[longest] = 1.0
    up_ref = np.array([0.0, 0.0, 1.0]) if longest == 1 else np.array([0.0, 1.0, 0.0])
    side = np.cross(axis, up_ref)
    side = side / np.sqrt((side * side).sum())
    # 视向：从长轴的侧上方斜看约 60°。不能正侧视 —— 杆和头部会叠成一块方片，
    # 端面与圆弧全看不见；也不能沿轴看 —— 那就只剩一个圆。
    d = axis * 0.45 + side * 0.36 + up_ref * 0.82
    d = d / np.sqrt((d * d).sum())            # 指向相机（+d 侧朝模型看）
    # 屏幕水平轴取长轴在屏幕平面上的投影：长轴因此横躺，且与视向严格正交
    # （right 若与 d 不垂直，投影会变成斜投影，零件被拉歪 —— 实测会糊成一块方片）
    right = axis - d * (axis * d).sum()
    right = right / np.sqrt((right * right).sum())
    up = np.cross(d, right)

    rel = tris - center
    sx = (rel * right).sum(axis=-1)                               # (N,3)
    sy = (rel * up).sum(axis=-1)
    depth = (rel * d).sum(axis=-1)                                # 越大越靠近相机

    span = max(sx.max() - sx.min(), sy.max() - sy.min())
    if span <= 0:
        raise RuntimeError("模型退化（包围盒为 0），无法生成缩略图")
    # 留 6% 边距，避免零件轮廓贴边被裁
    half = span / 2.0 * 1.06
    mid_x = (sx.min() + sx.max()) / 2.0
    mid_y = (sy.min() + sy.max()) / 2.0

    px = (sx - mid_x) / half * (canvas / 2.0) + canvas / 2.0
    py = canvas / 2.0 - (sy - mid_y) / half * (canvas / 2.0)

    rgb = np.zeros((canvas, canvas, 3), dtype=np.float64)
    alpha = np.zeros((canvas, canvas), dtype=np.float64)
    zbuf = np.full((canvas, canvas), -np.inf)

    light = LIGHT_DIR / np.linalg.norm(LIGHT_DIR)
    valid = np.sqrt((np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]) ** 2)
                    .sum(axis=-1)) > 1e-12
    n0 = vertex_normals[:, 0]
    n1 = vertex_normals[:, 1]
    n2 = vertex_normals[:, 2]

    for i in range(tris.shape[0]):
        if not valid[i]:
            continue
        x0, x1, x2 = px[i]
        y0, y1, y2 = py[i]
        min_x = max(int(np.floor(min(x0, x1, x2))), 0)
        max_x = min(int(np.ceil(max(x0, x1, x2))), canvas - 1)
        min_y = max(int(np.floor(min(y0, y1, y2))), 0)
        max_y = min(int(np.ceil(max(y0, y1, y2))), canvas - 1)
        if min_x > max_x or min_y > max_y:
            continue

        # 重心坐标按像素中心采样：像素 (r,c) 的中心是 (c+0.5, r+0.5)
        xs = np.arange(min_x, max_x + 1) + 0.5
        ys = np.arange(min_y, max_y + 1) + 0.5
        gx, gy = np.meshgrid(xs, ys)

        det = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(det) < 1e-12:
            continue
        w0 = ((y1 - y2) * (gx - x2) + (x2 - x1) * (gy - y2)) / det
        w1 = ((y2 - y0) * (gx - x2) + (x0 - x2) * (gy - y2)) / det
        w2 = 1.0 - w0 - w1
        inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        if not inside.any():
            continue

        z = w0 * depth[i, 0] + w1 * depth[i, 1] + w2 * depth[i, 2]
        zbuf_view = zbuf[min_y:max_y + 1, min_x:max_x + 1]
        nearer = inside & (z > zbuf_view)
        if not nearer.any():
            continue

        zbuf_view[nearer] = z[nearer]

        # Gouraud：像素级插值顶点法线再点乘光照方向。
        # 只对最终写入的像素算（先过 z-buffer 和 inside 两道筛），开销可控。
        b0 = w0[nearer]
        b1 = w1[nearer]
        b2 = w2[nearer]
        nx = b0 * n0[i, 0] + b1 * n1[i, 0] + b2 * n2[i, 0]
        ny = b0 * n0[i, 1] + b1 * n1[i, 1] + b2 * n2[i, 1]
        nz = b0 * n0[i, 2] + b1 * n1[i, 2] + b2 * n2[i, 2]
        length = np.sqrt(nx * nx + ny * ny + nz * nz)
        lam = np.abs((nx * light[0] + ny * light[1] + nz * light[2])
                     / np.maximum(length, 1e-12))
        shade = AMBIENT + DIFFUSE * lam

        rgb_view = rgb[min_y:max_y + 1, min_x:max_x + 1]
        rgb_view[nearer] = colors[i] * shade[:, None]
        alpha_view = alpha[min_y:max_y + 1, min_x:max_x + 1]
        alpha_view[nearer] = 1.0

    return np.clip(rgb * 255.0, 0, 255), np.clip(alpha * 255.0, 0, 255)


def _downsample(rgb: np.ndarray, alpha: np.ndarray, factor: int):
    """超采样降采样。颜色按 alpha 加权，否则边缘会发黑（透明区颜色是 0）。"""
    h, w = alpha.shape
    h2, w2 = h // factor, w // factor
    a = alpha[: h2 * factor, : w2 * factor].reshape(h2, factor, w2, factor)
    c = rgb[: h2 * factor, : w2 * factor].reshape(h2, factor, w2, factor, 3)
    a_sum = a.sum(axis=(1, 3))
    out_alpha = a_sum / (factor * factor)
    weight = a[..., None].transpose(0, 1, 2, 3, 4)  # (h2,f,w2,f,1)
    weighted = (c * weight).sum(axis=(1, 3))
    with np.errstate(invalid="ignore", divide="ignore"):
        out_rgb = np.where(a_sum[..., None] > 0, weighted / np.maximum(a_sum, 1e-9)[..., None], 0.0)
    return np.clip(out_rgb, 0, 255), np.clip(out_alpha, 0, 255)


# ==================== PNG ====================


def _write_png(path: str, rgb: np.ndarray, alpha: np.ndarray) -> None:
    """写 8 位 RGBA PNG。只为一张图引一个图像库（Pillow）不值得。"""
    height, width = alpha.shape
    pixels = np.empty((height, width, 4), dtype=np.uint8)
    pixels[..., :3] = rgb.astype(np.uint8)
    pixels[..., 3] = alpha.astype(np.uint8)
    # 每行前置滤波类型 0（None）—— 缩略图体积小，不值得上滤波器
    raw = b"".join(b"\x00" + pixels[y].tobytes() for y in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)
