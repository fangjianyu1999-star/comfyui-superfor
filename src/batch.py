"""
批量处理节点（本地文件夹 → 修复工作流 → 按原结构保存）
=====================================================

解决场景：
    桌面有一个根文件夹（例如 `图像a`），里面又套了多个子文件夹
    （`文件1`、`文件2`、`文件3` ...），每个子文件夹里有很多图。
    希望把它们逐张送进修复（API）工作流，结果保存到下载目录，
    并且**保留子目录结构和原文件名**。

ComfyUI 的执行模型是「一次队列跑一遍图」，而 API 修复节点一次只处理一张、
且每张图尺寸可能不同（无法堆成同一个 batch 张量）。因此批量的正确做法是：

    [批量遍历加载] --image--> [修复 API 节点] --image--> [按路径保存]
          |  filename / relative_dir                         ^
          +--------------------------------------------------+

加载器每次队列执行「自动取下一张」，配合 ComfyUI 的 Auto Queue / 队列 N 次，
即可把整个文件夹跑完；保存器按加载器给出的相对子目录 + 原文件名落盘。

两个节点：
- LoadImageBatchV3   aiaiartist 批量遍历加载（递归扫描，逐张输出 + 路径信息）
- SaveImageToDirV3   aiaiartist 按路径保存（保留子目录结构与原文件名）
"""
from __future__ import annotations

import logging
import os
from typing import Any

import torch
from PIL import Image

from .utils import (
    comfy_tensor_to_pil_list,
    make_error_image,
    pil_to_comfy_tensor,
    pretty_error,
)

log = logging.getLogger("comfyui-superfor")

CATEGORY_BATCH = "SuperFor/批量"

# 支持的图片扩展名（小写，含点）
IMAGE_EXTS: tuple[str, ...] = (
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif",
)

MODE_INCREMENTAL = "incremental"
MODE_SINGLE = "single"

SORT_NAME = "name"
SORT_MTIME = "mtime"

# 加载器游标状态：缓存键 -> {"start": 起始序号, "cursor": 下一张序号}
# 改变 start_index 会自动重置游标，方便重新从头跑。
_CURSORS: dict[str, dict[str, int]] = {}


def _expand_dir(directory: str) -> str:
    """把用户输入的目录展开成绝对路径（支持 ~ 和环境变量）。"""
    d = (directory or "").strip().strip('"').strip("'")
    return os.path.normpath(os.path.abspath(os.path.expanduser(os.path.expandvars(d))))


def _normalize_relative_dir(relative_dir: str) -> str:
    """规范化相对子目录；误接「相对路径」（含文件名）时自动只保留目录部分。"""
    rel = (relative_dir or "").strip().strip("/\\")
    if not rel:
        return ""
    base = os.path.basename(rel)
    if os.path.splitext(base)[1].lower() in IMAGE_EXTS:
        log.warning(
            "[SuperFor_SaveImageToDir] 「相对子目录」收到了文件路径（%s），"
            "已自动改为目录部分。请改接循环开始节点的「相对子目录」，不要接「相对路径」。",
            relative_dir,
        )
        rel = os.path.dirname(rel)
    return rel.strip("/\\")


def _is_under_root(root: str, target: str) -> bool:
    """判断 target 是否在 root 之下（兼容 Windows 不同盘符）。"""
    root_n = os.path.normcase(os.path.normpath(root))
    target_n = os.path.normcase(os.path.normpath(target))
    try:
        return os.path.commonpath([root_n, target_n]) == root_n
    except ValueError:
        return False


def _scan_images(root: str, include_subdir: bool, keyword: str, sort_mode: str) -> list[str]:
    """扫描根目录下所有图片，返回绝对路径列表（已排序）。"""
    keyword = (keyword or "").strip().lower()
    files: list[str] = []

    if include_subdir:
        for dirpath, _dirnames, names in os.walk(root):
            for name in names:
                if os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                    files.append(os.path.join(dirpath, name))
    else:
        try:
            for name in os.listdir(root):
                full = os.path.join(root, name)
                if os.path.isfile(full) and os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                    files.append(full)
        except FileNotFoundError:
            return []

    if keyword:
        files = [f for f in files if keyword in os.path.basename(f).lower()]

    if sort_mode == SORT_MTIME:
        files.sort(key=lambda p: (os.path.getmtime(p), p.lower()))
    else:
        # 按相对路径名排序，保证「文件1、文件2、文件3」这种顺序稳定
        files.sort(key=lambda p: os.path.normcase(p))

    return files


def directory_signature(
    directory: str,
    include_subdir: bool,
    filter_keyword: str,
    sort: str,
) -> str:
    """源目录指纹：换文件夹、增删图、改排序/筛选时自动变。"""
    root = _expand_dir(directory)
    if not os.path.isdir(root):
        return f"invalid|{root}"
    sub = include_subdir if isinstance(include_subdir, bool) else True
    kw = filter_keyword if isinstance(filter_keyword, str) else ""
    sm = sort if sort in (SORT_NAME, SORT_MTIME) else SORT_NAME
    files = _scan_images(root, sub, kw, sm)
    if not files:
        return f"empty|{root}|{sm}"
    latest_mtime = max(os.path.getmtime(f) for f in files)
    return f"{root}|{len(files)}|{latest_mtime:.6f}|{sm}|{sub}|{kw.strip().lower()}"


def _load_pil(path: str) -> Image.Image:
    """读取图片并做 EXIF 方向校正。"""
    img = Image.open(path)
    try:
        from PIL import ImageOps

        img = ImageOps.exif_transpose(img)
    except Exception:  # noqa: BLE001
        pass
    return img


try:
    from comfy_api.latest import io, ui

    _HAS_V3 = True
except ImportError:  # pragma: no cover
    _HAS_V3 = False


def _resize_max_side(pil: Image.Image, max_side: int) -> Image.Image:
    """按长边限制缩放；max_side<=0 时不缩放。"""
    if max_side <= 0:
        return pil
    w, h = pil.size
    longest = max(w, h)
    if longest <= max_side:
        return pil
    scale = max_side / float(longest)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    return pil.resize((nw, nh), Image.Resampling.LANCZOS)


def get_batch_v3_nodes() -> list[type]:
    """返回本模块的 V3 节点类列表（供 nodes.py 汇总注册）。"""
    if not _HAS_V3:
        return []
    return [LoadImageBatchV3, SaveImageToDirV3, CountImagesInDirV3, BatchFolderExportV3]


if _HAS_V3:

    class LoadImageBatchV3(io.ComfyNode):
        """aiaiartist 批量遍历加载

        递归扫描一个根文件夹（可含多层子文件夹）里的所有图片，
        每次队列执行输出「下一张」图，并同时给出它的文件名、相对子目录、
        相对路径等信息，方便后续保存时保留原始目录结构。
        """

        @classmethod
        def define_schema(cls) -> io.Schema:
            return io.Schema(
                node_id="SuperFor_LoadImageBatch",
                display_name="批量遍历加载",
                category=CATEGORY_BATCH,
                not_idempotent=True,
                description=(
                    "递归扫描文件夹里的所有图片，逐张送入工作流。\n"
                    "用法：「加载模式」选「逐张」，把本节点 🖼️图像 接修复节点，\n"
                    "📝文件名 / 📂相对子目录 接「按路径保存」节点；\n"
                    "然后开启 ComfyUI 的 Auto Queue（或队列 N 次），即可跑完整个文件夹。\n"
                    "⚠ 配合「批量循环-开始」时不要接「当前序号」——请直接用开始节点的图像/路径输出。"
                ),
                inputs=[
                    io.String.Input(
                        "directory",
                        display_name="文件夹路径",
                        default="",
                        tooltip="根文件夹路径，支持 ~ 和子文件夹，例如 ~/Desktop/图像a",
                    ),
                    io.Combo.Input(
                        "mode",
                        display_name="加载模式",
                        options=[MODE_INCREMENTAL, MODE_SINGLE],
                        default=MODE_INCREMENTAL,
                        tooltip="逐张：每次队列自动取下一张（批量用）；指定序号：固定取第 N 张（调试用）",
                    ),
                    io.Int.Input(
                        "index",
                        display_name="序号",
                        default=0,
                        min=0,
                        max=999999,
                        tooltip="逐张模式：起始序号（改它会从该序号重新开始）；指定序号模式：取第几张",
                    ),
                    io.Boolean.Input(
                        "include_subdir",
                        display_name="含子文件夹",
                        default=True,
                        tooltip="是否递归扫描子文件夹（图像a 里套的 文件1/文件2/...）",
                    ),
                    io.Combo.Input(
                        "sort",
                        display_name="排序方式",
                        options=[SORT_NAME, SORT_MTIME],
                        default=SORT_NAME,
                        tooltip="遍历顺序",
                    ),
                    io.String.Input(
                        "filter_keyword",
                        display_name="文件名筛选",
                        default="",
                        optional=True,
                        tooltip="只加载文件名包含该关键字的图片，可留空",
                    ),
                ],
                outputs=[
                    io.Image.Output(display_name="图像"),
                    io.String.Output(display_name="文件名"),
                    io.String.Output(display_name="相对子目录"),
                    io.String.Output(display_name="相对路径"),
                    io.String.Output(display_name="源完整路径"),
                    io.Int.Output(display_name="当前序号"),
                    io.Int.Output(display_name="图片总数"),
                ],
            )

        @classmethod
        def fingerprint_inputs(cls, directory, mode, index, include_subdir, sort, filter_keyword="") -> Any:
            """逐张模式每次队列前进一张；指定序号按 index 失效缓存（不用 nan，避免整图重复执行）。"""
            if mode == MODE_INCREMENTAL:
                return float("nan")
            return f"{_expand_dir(directory)}|{include_subdir}|{sort}|{filter_keyword}|{index}"

        @classmethod
        def execute(cls, directory, mode, index, include_subdir, sort, filter_keyword=""):
            try:
                root = _expand_dir(directory)
                if not directory.strip():
                    return io.NodeOutput(
                        make_error_image("请填写 directory（要批量加载的文件夹路径）"),
                        "", "", "", "", 0, 0,
                    )
                if not os.path.isdir(root):
                    return io.NodeOutput(
                        make_error_image(f"目录不存在：\n{root}"),
                        "", "", "", "", 0, 0,
                    )

                files = _scan_images(root, include_subdir, filter_keyword, sort)
                total = len(files)
                if total == 0:
                    return io.NodeOutput(
                        make_error_image(f"目录里没找到图片：\n{root}\n（扩展名支持 {'/'.join(IMAGE_EXTS)}）"),
                        "", "", "", "", 0, 0,
                    )

                if mode == MODE_SINGLE:
                    cur = max(0, min(index, total - 1))
                else:
                    key = f"{root}|{include_subdir}|{sort}|{filter_keyword.strip().lower()}"
                    state = _CURSORS.get(key)
                    if state is None or state["start"] != index:
                        state = {"start": index, "cursor": index}
                        _CURSORS[key] = state
                    cur = state["cursor"] % total
                    state["cursor"] = cur + 1  # 下一次取下一张（到末尾自动绕回）

                src = files[cur]
                pil = _load_pil(src)
                tensor = pil_to_comfy_tensor(pil)

                rel_path = os.path.relpath(src, root)
                rel_dir = os.path.dirname(rel_path)
                filename = os.path.splitext(os.path.basename(src))[0]

                log.info(
                    "[SuperFor_LoadImageBatch] %d/%d -> %s",
                    cur + 1, total, rel_path,
                )

                return io.NodeOutput(
                    tensor,
                    filename,
                    rel_dir,
                    rel_path,
                    src,
                    cur,
                    total,
                )
            except Exception as e:  # noqa: BLE001
                log.exception("[SuperFor_LoadImageBatch] 失败")
                return io.NodeOutput(
                    make_error_image(pretty_error("批量加载失败", e)),
                    "", "", "", "", 0, 0,
                )

    class CountImagesInDirV3(io.ComfyNode):
        """aiaiartist 目录图片计数

        统计一个文件夹（可含子文件夹）里有多少张图片，输出 INT 数量。
        专门用来喂给 easy-use「For循环-开始」的 total（总量），
        这样循环次数就会自动等于图片总数，不用手动数。

        注意：参数要和「批量遍历加载」保持一致（同一个 directory / include_subdir /
        filter_keyword），算出来的总数才和加载顺序对得上。
        """

        @classmethod
        def define_schema(cls) -> io.Schema:
            return io.Schema(
                node_id="SuperFor_CountImagesInDir",
                display_name="目录图片计数",
                category=CATEGORY_BATCH,
                description=(
                    "统计文件夹里图片总数（可含子文件夹），输出整数。\n"
                    "接到 easy-use「For循环-开始」的 total，循环次数自动 = 图片数。\n"
                    "参数务必和「批量遍历加载」一致。"
                ),
                inputs=[
                    io.String.Input(
                        "directory",
                        display_name="文件夹路径",
                        default="",
                        tooltip="要统计的文件夹路径，需与「批量遍历加载」的文件夹路径相同",
                    ),
                    io.Boolean.Input(
                        "include_subdir",
                        display_name="含子文件夹",
                        default=True,
                        tooltip="是否递归子文件夹（与加载器保持一致）",
                    ),
                    io.String.Input(
                        "filter_keyword",
                        display_name="文件名筛选",
                        default="",
                        optional=True,
                        tooltip="只统计文件名含该关键字的图片（与加载器保持一致）",
                    ),
                ],
                outputs=[
                    io.Int.Output(display_name="图片总数"),
                ],
            )

        @classmethod
        def execute(cls, directory, include_subdir=True, filter_keyword=""):
            root = _expand_dir(directory)
            if not os.path.isdir(root):
                log.warning("[SuperFor_CountImagesInDir] 目录不存在：%s", root)
                return io.NodeOutput(0)
            total = len(_scan_images(root, include_subdir, filter_keyword, SORT_NAME))
            log.info("[SuperFor_CountImagesInDir] %s 共 %d 张", root, total)
            return io.NodeOutput(total)

    class SaveImageToDirV3(io.ComfyNode):
        """aiaiartist 按路径保存

        把图像保存到「输出根目录 / 相对子目录 / 文件名.扩展名」，
        自动创建目录、保留子目录结构与原文件名。
        通常 relative_dir 和 filename 直接接「批量遍历加载」节点的同名输出。
        """

        @classmethod
        def define_schema(cls) -> io.Schema:
            return io.Schema(
                node_id="SuperFor_SaveImageToDir",
                display_name="按路径保存",
                category=CATEGORY_BATCH,
                description=(
                    "把结果保存到指定根目录，保留子目录结构和原文件名。\n"
                    "把 📂相对子目录 / 📝文件名 接到「批量遍历加载」的同名输出即可。\n"
                    "最终路径 = 保存根目录 / 相对子目录 / 前缀+文件名+后缀.扩展名"
                ),
                inputs=[
                    io.Image.Input(
                        "images",
                        display_name="图像",
                        tooltip="要保存的图像（接修复节点输出）",
                    ),
                    io.String.Input(
                        "output_root",
                        display_name="保存根目录",
                        default="",
                        tooltip="保存的根目录，例如 ~/Downloads/修复结果",
                    ),
                    io.String.Input(
                        "relative_dir",
                        display_name="相对子目录",
                        default="",
                        tooltip="相对子目录（接加载器的 📂相对子目录，保留原结构），可留空",
                    ),
                    io.String.Input(
                        "filename",
                        display_name="文件名",
                        default="",
                        tooltip="文件名（不含扩展名，接加载器的 📝文件名）；留空则用时间戳",
                    ),
                    io.String.Input(
                        "filename_prefix",
                        display_name="文件名前缀",
                        default="",
                        optional=True,
                        tooltip="文件名前缀，可留空",
                    ),
                    io.String.Input(
                        "filename_suffix",
                        display_name="文件名后缀",
                        default="",
                        optional=True,
                        tooltip="文件名后缀，例如 _修复，可留空",
                    ),
                    io.Combo.Input(
                        "image_format",
                        display_name="保存格式",
                        options=["png", "jpg", "webp"],
                        default="png",
                        tooltip="保存格式",
                    ),
                    io.Int.Input(
                        "quality",
                        display_name="图片质量",
                        default=95,
                        min=1,
                        max=100,
                        tooltip="jpg/webp 的质量（png 忽略）",
                    ),
                    io.Boolean.Input(
                        "overwrite",
                        display_name="覆盖同名",
                        default=False,
                        tooltip="同名文件是否覆盖；关闭时自动加 _1 _2 ... 避免覆盖",
                    ),
                ],
                outputs=[
                    io.String.Output(display_name="已保存路径"),
                ],
                # 不可设为 output：循环子图每次展开都会把 OUTPUT 节点重新加入队列，导致同一张重复保存 N 次。
                # 预览由 execute 返回的 ui.PreviewImage 提供；队列出口只用「批量循环-结束」。
                is_output_node=False,
            )

        @classmethod
        def fingerprint_inputs(
            cls,
            images,
            output_root,
            relative_dir="",
            filename="",
            filename_prefix="",
            filename_suffix="",
            image_format="png",
            quality=95,
            overwrite=False,
        ) -> Any:
            # 换保存目录会自动重跑；循环体内每轮仍靠上游图像变化 + nan 写盘
            return (_expand_dir(output_root), filename, relative_dir, filename_suffix, float("nan"))

        @classmethod
        def execute(cls, images, output_root, relative_dir="", filename="",
                    filename_prefix="", filename_suffix="", image_format="png",
                    quality=95, overwrite=False):
            try:
                if not (output_root or "").strip():
                    raise ValueError("请填写 output_root（保存的根目录），例如 ~/Downloads/修复结果")

                root = _expand_dir(output_root)
                # 防止 relative_dir 里的绝对路径 / .. 越权写到根目录之外
                rel = _normalize_relative_dir(relative_dir)
                target_dir = os.path.normpath(os.path.join(root, rel))
                if not _is_under_root(root, target_dir):
                    log.warning("[SuperFor_SaveImageToDir] relative_dir 越权，已忽略：%s", relative_dir)
                    target_dir = root
                os.makedirs(target_dir, exist_ok=True)

                ext = {"png": ".png", "jpg": ".jpg", "webp": ".webp"}[image_format]
                def _safe_affix(value: object, *, role: str) -> str:
                    """过滤 widgets_values 错位产生的 95 / True 等非法前后缀。"""
                    if not isinstance(value, str):
                        return ""
                    s = value.strip()
                    if not s:
                        return ""
                    if s.lower() in {"true", "false"}:
                        log.warning("[SuperFor_SaveImageToDir] 忽略非法%s（布尔串 %r）", role, s)
                        return ""
                    if s.isdigit():
                        log.warning("[SuperFor_SaveImageToDir] 忽略非法%s（数字 %r）", role, s)
                        return ""
                    return s

                prefix = _safe_affix(filename_prefix, role="前缀")
                suffix = _safe_affix(filename_suffix, role="后缀")
                base_name = (filename or "").strip() if isinstance(filename, str) else ""
                if not base_name:
                    import time

                    base_name = time.strftime("%Y%m%d_%H%M%S")

                pil_list = comfy_tensor_to_pil_list(images)
                batch_n = len(pil_list)
                if batch_n > 1:
                    log.warning(
                        "[SuperFor_SaveImageToDir] 上游图像 batch=%d（通常应为 1）。"
                        "ComfyUI 若收到列表/大 batch，会对每张各跑一遍后续节点，表现为「重复多次」。",
                        batch_n,
                    )
                saved: list[str] = []

                for i, pil in enumerate(pil_list):
                    name = f"{prefix}{base_name}{suffix}"
                    if batch_n > 1:
                        name = f"{name}_{i:02d}"

                    out_path = os.path.join(target_dir, name + ext)
                    if not overwrite:
                        out_path = cls._dedupe(out_path)

                    save_img = pil
                    if image_format == "jpg" and save_img.mode != "RGB":
                        save_img = save_img.convert("RGB")

                    if image_format == "png":
                        save_img.save(out_path, format="PNG", compress_level=4)
                    elif image_format == "jpg":
                        save_img.save(out_path, format="JPEG", quality=quality)
                    else:  # webp
                        save_img.save(out_path, format="WEBP", quality=quality)

                    saved.append(out_path)
                    log.info("[SuperFor_SaveImageToDir] 已保存 %s", out_path)

                result = "\n".join(saved)
                # 循环场景下不要返回 PreviewImage UI，避免子图展开时重复触发执行
                return io.NodeOutput(result)
            except Exception as e:  # noqa: BLE001
                log.exception("[SuperFor_SaveImageToDir] 失败")
                msg = pretty_error("保存失败", e)
                # 抛出异常以中断工作流并在 ComfyUI 界面显示红框错误（避免静默继续循环）
                raise RuntimeError(msg) from e

        @staticmethod
        def _dedupe(path: str) -> str:
            """文件已存在时，自动追加 _1 _2 ... 直到不冲突。"""
            if not os.path.exists(path):
                return path
            stem, ext = os.path.splitext(path)
            i = 1
            while os.path.exists(f"{stem}_{i}{ext}"):
                i += 1
            return f"{stem}_{i}{ext}"

    class BatchFolderExportV3(io.ComfyNode):
        """SuperFor 文件夹批量导出（一次跑完，无图展开循环）

        在节点内部用 Python for 循环逐张处理，避免 ComfyUI 循环子图缓存导致重复保存。
        适合：A 文件夹 → 可选缩放 → B 文件夹（保留子目录结构）。
        """

        @classmethod
        def define_schema(cls) -> io.Schema:
            return io.Schema(
                node_id="SuperFor_BatchFolderExport",
                display_name="文件夹批量导出",
                category=CATEGORY_BATCH,
                description=(
                    "一次队列跑完整个文件夹：读取 A → 可选按长边缩放 → 保存到 B。\n"
                    "不经过图展开循环，每张只处理一遍，最稳定。\n"
                    "若需接修复 API 等节点，请用「批量循环-开始/结束」工作流。"
                ),
                inputs=[
                    io.String.Input(
                        "directory",
                        display_name="源文件夹",
                        default="",
                        tooltip="要读取的根文件夹，支持子文件夹",
                    ),
                    io.String.Input(
                        "output_root",
                        display_name="保存根目录",
                        default="",
                        tooltip="输出根目录，会保留源目录的子文件夹结构",
                    ),
                    io.Boolean.Input(
                        "include_subdir",
                        display_name="含子文件夹",
                        default=True,
                    ),
                    io.Combo.Input(
                        "sort",
                        display_name="排序方式",
                        options=[SORT_NAME, SORT_MTIME],
                        default=SORT_NAME,
                    ),
                    io.String.Input(
                        "filter_keyword",
                        display_name="文件名筛选",
                        default="",
                        optional=True,
                    ),
                    io.Int.Input(
                        "max_side",
                        display_name="长边上限",
                        default=0,
                        min=0,
                        max=8192,
                        tooltip="0=不缩放；例如 1920 表示长边不超过 1920 像素",
                    ),
                    io.String.Input(
                        "filename_suffix",
                        display_name="文件名后缀",
                        default="",
                        optional=True,
                        tooltip="例如 _修复，可留空",
                    ),
                    io.Combo.Input(
                        "image_format",
                        display_name="保存格式",
                        options=["png", "jpg", "webp"],
                        default="png",
                    ),
                    io.Int.Input(
                        "quality",
                        display_name="图片质量",
                        default=95,
                        min=1,
                        max=100,
                    ),
                    io.Boolean.Input(
                        "overwrite",
                        display_name="覆盖同名",
                        default=True,
                    ),
                ],
                outputs=[
                    io.String.Output(display_name="导出摘要"),
                    io.Int.Output(display_name="成功数量"),
                ],
                is_output_node=True,
            )

        @classmethod
        def fingerprint_inputs(
            cls,
            directory,
            output_root,
            include_subdir=True,
            sort=SORT_NAME,
            filter_keyword="",
            max_side=0,
            filename_suffix="",
            image_format="png",
            quality=95,
            overwrite=True,
        ) -> Any:
            sig = directory_signature(directory, include_subdir, filter_keyword, sort)
            return f"{sig}|{_expand_dir(output_root)}|{max_side}|{filename_suffix}"

        @classmethod
        def execute(
            cls,
            directory,
            output_root,
            include_subdir=True,
            sort=SORT_NAME,
            filter_keyword="",
            max_side=0,
            filename_suffix="",
            image_format="png",
            quality=95,
            overwrite=True,
        ):
            root = _expand_dir(directory)
            out_root = _expand_dir(output_root)
            if not os.path.isdir(root):
                raise RuntimeError(f"源文件夹不存在：{root}")
            if not (output_root or "").strip():
                raise RuntimeError("请填写保存根目录 output_root")

            files = _scan_images(root, include_subdir, filter_keyword, sort)
            total = len(files)
            if total == 0:
                raise RuntimeError(f"源文件夹里没有图片：{root}")

            suffix = (filename_suffix or "").strip()
            ext = {"png": ".png", "jpg": ".jpg", "webp": ".webp"}[image_format]
            saved: list[str] = []

            for idx, src in enumerate(files):
                rel_path = os.path.relpath(src, root)
                rel_dir = os.path.dirname(rel_path)
                base_name = os.path.splitext(os.path.basename(src))[0]
                target_dir = os.path.normpath(os.path.join(out_root, rel_dir))
                if not _is_under_root(out_root, target_dir):
                    target_dir = out_root
                os.makedirs(target_dir, exist_ok=True)

                pil = _load_pil(src)
                pil = _resize_max_side(pil, int(max_side or 0))
                out_name = f"{base_name}{suffix}{ext}"
                out_path = os.path.join(target_dir, out_name)
                if not overwrite:
                    out_path = SaveImageToDirV3._dedupe(out_path)

                save_img = pil
                if image_format == "jpg" and save_img.mode != "RGB":
                    save_img = save_img.convert("RGB")
                if image_format == "png":
                    save_img.save(out_path, format="PNG", compress_level=4)
                elif image_format == "jpg":
                    save_img.save(out_path, format="JPEG", quality=quality)
                else:
                    save_img.save(out_path, format="WEBP", quality=quality)

                saved.append(out_path)
                log.info(
                    "[SuperFor_BatchFolderExport] %d/%d -> %s",
                    idx + 1, total, out_path,
                )

            summary = f"共导出 {len(saved)} 张到 {out_root}"
            log.info("[SuperFor_BatchFolderExport] 完成：%s", summary)
            return io.NodeOutput(summary, len(saved))
