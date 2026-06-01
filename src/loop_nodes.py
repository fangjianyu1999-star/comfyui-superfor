"""
自动计数的批量循环节点（递归遍历子目录）
=========================================

背景：easy-use 的 `easy forLoopStart` 要求 `total`（总量）必须是**字面数字**，
不能用连线喂（`forLoopEnd` 会把它当字面值塞进内部展开的子图，连线会指向子图里
不存在的节点，导致循环只跑一次）。而 easy-use 自带的目录循环用 `os.listdir`，
**不递归子目录**，对「图像a/文件1、文件2...」这种套文件夹结构无效。

本模块提供一对循环节点，逐字复用 easy-use 已验证的循环展开逻辑
（`easy whileLoopStart/whileLoopEnd/mathInt/compare`），仅把「读 total 字面值」
换成「在 Python 里递归统计目录图片数」（仍是字面整数，规避连线问题）：

- Aiaiartist_DirForLoopStart  批量循环-开始（自动递归计数 + 直接输出当前图）
- Aiaiartist_DirForLoopEnd    批量循环-结束

⚠ 依赖：需要安装 comfyui-easy-use（提供底层 whileLoop / mathInt / compare 节点）。
"""
from __future__ import annotations

import logging
import os

from .batch import (
    SORT_MTIME,
    SORT_NAME,
    _expand_dir,
    _load_pil,
    _scan_images,
)
from .utils import make_error_image, pil_to_comfy_tensor

log = logging.getLogger("comfyui-superfor")

MAX_FLOW_NUM = 20  # 与 easy-use 保持一致


class AlwaysEqualProxy(str):
    """与任意类型都判等的代理（用于 any 类型输出）。"""

    def __eq__(self, _):
        return True

    def __ne__(self, _):
        return False


class TautologyStr(str):
    def __ne__(self, _other):
        return False


class ByPassTypeTuple(tuple):
    """让所有输出端都报告为「兼容任意类型」（照搬 easy-use 的写法）。"""

    def __getitem__(self, index):
        if index > 0:
            index = 0
        item = super().__getitem__(index)
        if isinstance(item, str):
            return TautologyStr(item)
        return item


any_type = AlwaysEqualProxy("*")

try:
    from comfy_execution.graph_utils import GraphBuilder

    _HAS_GRAPH = GraphBuilder is not None
except Exception:  # noqa: BLE001  # pragma: no cover
    GraphBuilder = None
    _HAS_GRAPH = False


def _count_recursive(directory, include_subdir, filter_keyword, sort) -> int:
    """递归统计目录图片数（字面整数）。参数可能来自原始 prompt，做容错。"""
    if not isinstance(directory, str):
        return 1  # directory 被连线（非字面）时无法统计，返回 1 保证至少跑一次
    root = _expand_dir(directory)
    if not os.path.isdir(root):
        return 1
    sub = include_subdir if isinstance(include_subdir, bool) else True
    kw = filter_keyword if isinstance(filter_keyword, str) else ""
    sm = sort if sort in (SORT_NAME, SORT_MTIME) else SORT_NAME
    return max(1, len(_scan_images(root, sub, kw, sm)))


class DirForLoopStart:
    """aiaiartist 批量循环-开始（自动递归计数）

    指定一个文件夹（可含多层子文件夹），自动统计图片总数并逐张循环，
    每轮直接输出当前图片及其文件名 / 相对子目录，无需单独的加载器或手填总量。
    把 image 接修复节点、filename / relative_dir 接保存节点、flow 接「批量循环-结束」。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "📁 文件夹路径": ("STRING", {"default": "", "tooltip": "根文件夹，支持 ~，会自动递归子文件夹"}),
                "📂 含子文件夹": ("BOOLEAN", {"default": True}),
                "↕️ 排序方式": ([SORT_NAME, SORT_MTIME], {"default": SORT_NAME}),
            },
            "optional": {
                "🔍 文件名筛选": ("STRING", {"default": "", "tooltip": "只加载文件名含该关键字的图片，可留空"}),
            },
            "hidden": {
                "initial_value0": (any_type,),
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "unique_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ("FLOW_CONTROL", "INT", "IMAGE", "STRING", "STRING", "STRING", "INT")
    RETURN_NAMES = ("🔁 循环流程", "🔢 当前序号", "🖼️ 图像", "📝 文件名", "📂 相对子目录", "📄 相对路径", "🔢 图片总数")
    FUNCTION = "start"
    CATEGORY = "🔁 SuperFor/批量"

    def start(self, **kwargs):
        directory = kwargs.get("📁 文件夹路径", "")
        include_subdir = kwargs.get("📂 含子文件夹", True)
        sort = kwargs.get("↕️ 排序方式", SORT_NAME)
        filter_keyword = kwargs.get("🔍 文件名筛选", "")
        root = _expand_dir(directory)
        files = _scan_images(root, include_subdir, filter_keyword, sort) if os.path.isdir(root) else []
        total = len(files)
        i = 0
        try:
            i = int(kwargs.get("initial_value0", 0) or 0)
        except (TypeError, ValueError):
            i = 0

        if total == 0:
            image = make_error_image(f"目录里没有图片：\n{root}")
            filename = relative_dir = relative_path = ""
        else:
            idx = max(0, min(i, total - 1))
            src = files[idx]
            image = pil_to_comfy_tensor(_load_pil(src))
            relative_path = os.path.relpath(src, root)
            relative_dir = os.path.dirname(relative_path)
            filename = os.path.splitext(os.path.basename(src))[0]
            log.info("[Aiaiartist_DirForLoopStart] %d/%d -> %s", idx + 1, total, relative_path)

        graph = GraphBuilder()
        graph.node("easy whileLoopStart", condition=max(1, total), initial_value0=i)
        return {
            "result": ("stub", i, image, filename, relative_dir, relative_path, total),
            "expand": graph.finalize(),
        }


class DirForLoopEnd:
    """aiaiartist 批量循环-结束

    与「批量循环-开始」配对。内部递归统计开始节点指定目录的图片数作为循环上限，
    把要进入循环体的结果（如保存节点的 saved_paths）接到本节点的 初始值1。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "🔁 循环流程": ("FLOW_CONTROL", {"rawLink": True}),
            },
            "optional": {
                "🔗 循环体回接": (
                    any_type,
                    {
                        "rawLink": True,
                        "tooltip": "把「按路径保存」节点的『💾 已保存路径』接到这里。\n"
                                   "这一根线决定了保存节点是否在循环体内——不接的话每张图不会保存，循环也只会跑一次。",
                    },
                ),
            },
            "hidden": {
                "dynprompt": "DYNPROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "unique_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = (any_type,)
    RETURN_NAMES = ("✅ 循环完成",)
    FUNCTION = "end"
    CATEGORY = "🔁 SuperFor/批量"
    # 关键：标记为输出节点，否则 GUI 点「运行」时本节点不会被执行，循环不会展开（只跑一次）
    OUTPUT_NODE = True

    def end(self, dynprompt=None, extra_pnginfo=None, unique_id=None, **kwargs):
        # 兼容旧工作流：新键名优先，回退到旧英文键名
        flow = kwargs.get("🔁 循环流程", kwargs.get("flow"))
        anchor = kwargs.get("🔗 循环体回接", kwargs.get("initial_value1", None))

        graph = GraphBuilder()
        while_open = flow[0]

        # 从开始节点读取目录参数（widget → 字面值），递归统计总数（字面整数，规避连线问题）
        total = 1
        try:
            forstart = dynprompt.get_node(while_open)
            inputs = forstart.get("inputs", {})
            total = _count_recursive(
                inputs.get("📁 文件夹路径", inputs.get("directory", "")),
                inputs.get("📂 含子文件夹", inputs.get("include_subdir", True)),
                inputs.get("🔍 文件名筛选", inputs.get("filter_keyword", "")),
                inputs.get("↕️ 排序方式", inputs.get("sort", SORT_NAME)),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("[Aiaiartist_DirForLoopEnd] 统计总数失败，回退为 1：%s", e)

        sub = graph.node("easy mathInt", operation="add", a=[while_open, 1], b=1)
        cond = graph.node("easy compare", a=sub.out(0), b=total, comparison="a < b")
        while_close = graph.node(
            "easy whileLoopEnd",
            flow=flow,
            condition=cond.out(0),
            initial_value0=sub.out(0),
            initial_value1=anchor,  # 唯一的「循环体锚点」，把保存节点拉进循环体
        )
        return {
            "result": (while_close.out(1),),
            "expand": graph.finalize(),
        }


if _HAS_GRAPH:
    NODE_CLASS_MAPPINGS = {
        "Aiaiartist_DirForLoopStart": DirForLoopStart,
        "Aiaiartist_DirForLoopEnd": DirForLoopEnd,
    }
    NODE_DISPLAY_NAME_MAPPINGS = {
        "Aiaiartist_DirForLoopStart": "🔄 批量循环-开始（自动计数）",
        "Aiaiartist_DirForLoopEnd": "🏁 批量循环-结束",
    }
else:  # pragma: no cover
    NODE_CLASS_MAPPINGS = {}
    NODE_DISPLAY_NAME_MAPPINGS = {}
    log.warning("[ComfyUI-CompanyAPI] 未找到 comfy_execution.graph_utils，循环节点不可用")
