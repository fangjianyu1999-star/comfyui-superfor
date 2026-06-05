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

- SuperFor_DirForLoopStart  批量循环-开始（自动递归计数 + 直接输出当前图）
- SuperFor_DirForLoopEnd    批量循环-结束

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

# 批量循环-开始节点在 prompt 里的 class_type（含旧名兼容）
_LOOP_START_TYPES = frozenset({
    "SuperFor_DirForLoopStart",
    "Aiaiartist_DirForLoopStart",
})


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


def _normalize_sort(value) -> str:
    """兼容旧工作流里的中文排序选项。"""
    if value in (SORT_NAME, SORT_MTIME):
        return value
    if value in ("按路径名", "name"):
        return SORT_NAME
    if value in ("按修改时间", "mtime"):
        return SORT_MTIME
    return SORT_NAME


def _pick(inputs: dict, *keys, default=None):
    """按优先级从 inputs 取值（兼容中文/英文/旧 emoji 键名）。"""
    for k in keys:
        if k in inputs:
            return inputs[k]
    return default


def _read_start_widget_inputs(inputs: dict) -> tuple[str, bool, str, str]:
    """从批量循环-开始节点的 inputs 读取目录参数。"""
    directory = _pick(inputs, "文件夹路径", "directory", "📁 文件夹路径", default="")
    include_subdir = _pick(inputs, "含子文件夹", "include_subdir", "📂 含子文件夹", default=True)
    filter_keyword = _pick(inputs, "文件名筛选", "filter_keyword", "🔍 文件名筛选", default="")
    sort = _normalize_sort(_pick(inputs, "排序方式", "sort", "↕️ 排序方式", default=SORT_NAME))
    return directory, include_subdir, filter_keyword, sort


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


def _find_loop_start_inputs(dynprompt, while_open_id) -> dict | None:
    """通过 dynprompt 找到配对的「批量循环-开始」节点 inputs。"""
    if dynprompt is None:
        return None
    try:
        real_id = dynprompt.get_real_node_id(str(while_open_id))
        node = dynprompt.get_node(real_id)
        if node.get("class_type") in _LOOP_START_TYPES:
            return node.get("inputs", {})
    except Exception:  # noqa: BLE001
        pass

    try:
        for nid, node in dynprompt.get_original_prompt().items():
            if node.get("class_type") in _LOOP_START_TYPES:
                return node.get("inputs", {})
    except Exception:  # noqa: BLE001
        pass
    return None


def _resolve_total(dynprompt, while_open_id) -> int:
    """解析循环总次数：始终从「批量循环-开始」的目录参数重新统计（与 easy loadImagesForLoop 一致）。"""
    start_inputs = _find_loop_start_inputs(dynprompt, while_open_id)
    if start_inputs:
        directory, include_subdir, filter_keyword, sort = _read_start_widget_inputs(start_inputs)
        return _count_recursive(directory, include_subdir, filter_keyword, sort)

    log.warning("[SuperFor_DirForLoopEnd] 无法解析循环总数，回退为 1")
    return 1


class DirForLoopStart:
    """SuperFor 批量循环-开始（自动递归计数）

    指定文件夹后自动统计图片总数并驱动 for 循环。
    ⚠ 循环体内请用「当前序号」→「批量遍历加载（指定序号）」取图，
    不要把本节点的「图像」直接接修复（ComfyUI 子图展开会缓存第一轮输出，导致重复保存）。
    """

    # 循环序号每轮都变，禁止跨轮缓存
    NOT_IDEMPOTENT = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "文件夹路径": ("STRING", {"default": "", "tooltip": "根文件夹，支持 ~，会自动递归子文件夹"}),
                "含子文件夹": ("BOOLEAN", {"default": True, "tooltip": "是否递归子文件夹"}),
                "排序方式": (["按路径名", "按修改时间"], {"default": "按路径名"}),
            },
            "optional": {
                "文件名筛选": ("STRING", {"default": "", "tooltip": "只加载文件名含该关键字的图片，可留空"}),
            },
            "hidden": {
                "initial_value0": (any_type,),
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "unique_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ("FLOW_CONTROL", "INT", "IMAGE", "STRING", "STRING", "STRING", "INT")
    RETURN_NAMES = ("循环流程", "当前序号", "图像", "文件名", "相对子目录", "相对路径", "图片总数")
    FUNCTION = "start"
    CATEGORY = "SuperFor/批量"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # 必须每轮失效；仅用 initial_value0 会在首轮被 is_changed 缓存冻结
        return float("nan")

    def start(self, **kwargs):
        directory = _pick(kwargs, "文件夹路径", "directory", "📁 文件夹路径", default="")
        include_subdir = _pick(kwargs, "含子文件夹", "include_subdir", "📂 含子文件夹", default=True)
        sort = _normalize_sort(_pick(kwargs, "排序方式", "sort", "↕️ 排序方式", default=SORT_NAME))
        filter_keyword = _pick(kwargs, "文件名筛选", "filter_keyword", "🔍 文件名筛选", default="")
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
            log.info("[SuperFor_DirForLoopStart] %d/%d -> %s", idx + 1, total, relative_path)

        graph = GraphBuilder()
        # 与 easy-use loadImagesForLoop 一致：condition 用布尔开关，总次数由结束节点的 compare 控制
        graph.node("easy whileLoopStart", condition=True, initial_value0=i)
        return {
            "result": ("stub", i, image, filename, relative_dir, relative_path, total),
            "expand": graph.finalize(),
        }


class DirForLoopEnd:
    """SuperFor 批量循环-结束

    与「批量循环-开始」配对。内部读取循环总数并控制展开，
    把要进入循环体的结果（如保存节点的 saved_paths）接到本节点的 🔗 循环体回接。
    """

    NOT_IDEMPOTENT = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "循环流程": ("FLOW_CONTROL", {"rawLink": True}),
            },
            "optional": {
                "循环体回接": (
                    any_type,
                    {
                        "rawLink": True,
                        "tooltip": "把「按路径保存」节点的「已保存路径」接到这里。\n"
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
    RETURN_NAMES = ("循环完成",)
    FUNCTION = "end"
    CATEGORY = "SuperFor/批量"
    # 关键：标记为输出节点，否则 GUI 点「运行」时本节点不会被执行，循环不会展开（只跑一次）
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def end(self, dynprompt=None, extra_pnginfo=None, unique_id=None, **kwargs):
        flow = _pick(kwargs, "循环流程", "flow", "🔁 循环流程")
        anchor = _pick(kwargs, "循环体回接", "loop_anchor", "🔗 循环体回接", "initial_value1", default=None)

        graph = GraphBuilder()
        while_open = flow[0]
        total = _resolve_total(dynprompt, while_open)

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
        "SuperFor_DirForLoopStart": DirForLoopStart,
        "SuperFor_DirForLoopEnd": DirForLoopEnd,
    }
    NODE_DISPLAY_NAME_MAPPINGS = {
        "SuperFor_DirForLoopStart": "批量循环-开始（自动计数）",
        "SuperFor_DirForLoopEnd": "批量循环-结束",
    }
else:  # pragma: no cover
    NODE_CLASS_MAPPINGS = {}
    NODE_DISPLAY_NAME_MAPPINGS = {}
    log.warning("[comfyui-superfor] 未找到 comfy_execution.graph_utils，循环节点不可用")
