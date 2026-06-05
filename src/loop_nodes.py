"""
自动计数的批量循环节点（递归遍历子目录）
=========================================

提供一对循环节点，复用 easy-use 已验证的 whileLoop/mathInt/compare 展开逻辑，
并在自定义 whileLoopEnd 中把递增后的序号写回「批量循环-开始」的 hidden initial_value0，
避免同一张图在循环体内被重复执行。

- SuperFor_DirForLoopStart  批量循环-开始（自动递归计数 + 直接输出当前图）
- SuperFor_DirForLoopEnd    批量循环-结束
- SuperFor_WhileLoopEnd     内部用，勿手动添加

⚠ 依赖：需要安装 comfyui-easy-use（提供 mathInt / compare / whileLoopStart）。
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

_LOOP_START_TYPES = frozenset({
    "SuperFor_DirForLoopStart",
    "Aiaiartist_DirForLoopStart",
})

_LOOP_END_TYPES = frozenset({
    "easy whileLoopEnd",
    "SuperFor_WhileLoopEnd",
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
    from comfy_execution.graph_utils import GraphBuilder, is_link

    _HAS_GRAPH = GraphBuilder is not None
except Exception:  # noqa: BLE001  # pragma: no cover
    GraphBuilder = None
    is_link = None
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
    """递归统计目录图片数（字面整数）。"""
    if not isinstance(directory, str):
        return 1
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
        for nid, node in dynprompt.get_original_prompt().items():
            if node.get("class_type") in _LOOP_START_TYPES:
                return node.get("inputs", {})
    except Exception:  # noqa: BLE001
        pass
    return None


def _resolve_total(dynprompt, while_open_id) -> int:
    """解析循环总次数：从「批量循环-开始」的目录参数递归统计。"""
    start_inputs = _find_loop_start_inputs(dynprompt, while_open_id)
    if start_inputs:
        directory, include_subdir, filter_keyword, sort = _read_start_widget_inputs(start_inputs)
        return _count_recursive(directory, include_subdir, filter_keyword, sort)

    log.warning("[SuperFor_DirForLoopEnd] 无法解析循环总数，回退为 1")
    return 1


def _loop_index_changed(**kwargs) -> int:
    """按循环序号失效缓存；同一序号内允许命中缓存，避免同一张重复跑。"""
    try:
        return int(kwargs.get("initial_value0", 0) or 0)
    except (TypeError, ValueError):
        return 0


class DirForLoopStart:
    """SuperFor 批量循环-开始（自动递归计数）

    指定文件夹后自动统计图片总数并驱动 for 循环。
    循环体内请直接把「图像 / 文件名 / 相对子目录」接到后续节点，不要用「批量遍历加载」接序号。
    """

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
        return _loop_index_changed(**kwargs)

    def start(self, **kwargs):
        directory = _pick(kwargs, "文件夹路径", "directory", "📁 文件夹路径", default="")
        include_subdir = _pick(kwargs, "含子文件夹", "include_subdir", "📂 含子文件夹", default=True)
        sort = _normalize_sort(_pick(kwargs, "排序方式", "sort", "↕️ 排序方式", default=SORT_NAME))
        filter_keyword = _pick(kwargs, "文件名筛选", "filter_keyword", "🔍 文件名筛选", default="")
        root = _expand_dir(directory)
        files = _scan_images(root, include_subdir, filter_keyword, sort) if os.path.isdir(root) else []
        total = len(files)
        i = _loop_index_changed(**kwargs)

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
            log.info(
                "[SuperFor_DirForLoopStart] %d/%d -> %s | shape=%s",
                idx + 1, total, relative_path, tuple(image.shape),
            )
            if image.shape[0] != 1:
                log.warning(
                    "[SuperFor_DirForLoopStart] 图像 batch=%d（应为 1）。"
                    "若反复保存，请检查上游是否返回了列表或 cat 增大了 batch。",
                    image.shape[0],
                )

        graph = GraphBuilder()
        graph.node("easy whileLoopStart", condition=True, initial_value0=i)
        return {
            "result": ("stub", i, image, filename, relative_dir, relative_path, total),
            "expand": graph.finalize(),
        }


class SuperForWhileLoopEnd:
    """fork easy whileLoopEnd：展开时把 next index 写回批量循环-开始节点的 initial_value0。"""

    @classmethod
    def INPUT_TYPES(cls):
        inputs = {
            "required": {
                "flow": ("FLOW_CONTROL", {"rawLink": True}),
                "condition": ("BOOLEAN", {}),
            },
            "optional": {},
            "hidden": {
                "dynprompt": "DYNPROMPT",
                "unique_id": "UNIQUE_ID",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }
        for i in range(MAX_FLOW_NUM):
            inputs["optional"]["initial_value%d" % i] = (any_type,)
        return inputs

    RETURN_TYPES = ByPassTypeTuple(tuple([any_type] * MAX_FLOW_NUM))
    RETURN_NAMES = ByPassTypeTuple(tuple(["value%d" % i for i in range(MAX_FLOW_NUM)]))
    FUNCTION = "while_loop_close"
    CATEGORY = "SuperFor/批量"

    def explore_dependencies(self, node_id, dynprompt, upstream, parent_ids):
        node_info = dynprompt.get_node(node_id)
        if "inputs" not in node_info:
            return

        for _k, v in node_info["inputs"].items():
            if is_link(v):
                parent_id = v[0]
                display_id = dynprompt.get_display_node_id(parent_id)
                display_node = dynprompt.get_node(display_id)
                class_type = display_node["class_type"]
                if class_type not in _LOOP_END_TYPES and class_type != "SuperFor_DirForLoopEnd":
                    parent_ids.append(display_id)
                if parent_id not in upstream:
                    upstream[parent_id] = []
                    self.explore_dependencies(parent_id, dynprompt, upstream, parent_ids)
                upstream[parent_id].append(node_id)

    def collect_contained(self, node_id, upstream, contained):
        if node_id not in upstream:
            return
        for child_id in upstream[node_id]:
            if child_id not in contained:
                contained[child_id] = True
                self.collect_contained(child_id, upstream, contained)

    def while_loop_close(self, flow, condition, dynprompt=None, unique_id=None, **kwargs):
        if not condition:
            return tuple(kwargs.get("initial_value%d" % i, None) for i in range(MAX_FLOW_NUM))

        upstream: dict = {}
        parent_ids: list = []
        self.explore_dependencies(unique_id, dynprompt, upstream, parent_ids)

        contained: dict = {}
        open_node = flow[0]
        self.collect_contained(open_node, upstream, contained)
        contained[unique_id] = True
        contained[open_node] = True

        graph = GraphBuilder()
        for node_id in contained:
            original_node = dynprompt.get_node(node_id)
            node = graph.node(
                original_node["class_type"],
                "Recurse" if node_id == unique_id else node_id,
            )
            node.set_override_display_id(node_id)

        for node_id in contained:
            original_node = dynprompt.get_node(node_id)
            node = graph.lookup_node("Recurse" if node_id == unique_id else node_id)
            for k, v in original_node["inputs"].items():
                if is_link(v) and v[0] in contained:
                    parent = graph.lookup_node(v[0])
                    node.set_input(k, parent.out(v[1]))
                else:
                    node.set_input(k, v)

        new_open = graph.lookup_node(open_node)
        next_index = kwargs.get("initial_value0", None)
        for i in range(MAX_FLOW_NUM):
            key = "initial_value%d" % i
            new_open.set_input(key, kwargs.get(key, None))

        # 关键补丁：下一轮必须把递增后的序号写回「批量循环-开始」
        for node_id in contained:
            original_node = dynprompt.get_node(node_id)
            if original_node.get("class_type") not in _LOOP_START_TYPES:
                continue
            start_node = graph.lookup_node(node_id)
            if start_node is not None:
                start_node.set_input("initial_value0", next_index)

        my_clone = graph.lookup_node("Recurse")
        result = map(lambda x: my_clone.out(x), range(MAX_FLOW_NUM))
        return {
            "result": tuple(result),
            "expand": graph.finalize(),
        }


class BatchLoopSink:
    """批量循环-完成出口

    必须有一个 OUTPUT 节点才能点「运行」，但不能把 OUTPUT 放在循环结束节点上——
    ComfyUI 每次循环展开都会把展开子图里的 OUTPUT 节点重新入队，导致同一张重复保存。
    本节点接在「批量循环-结束」之后，作为唯一队列出口。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "循环结果": (any_type, {"tooltip": "接「批量循环-结束」的「循环完成」输出"}),
            },
        }

    RETURN_TYPES = (any_type,)
    RETURN_NAMES = ("完成",)
    FUNCTION = "sink"
    CATEGORY = "SuperFor/批量"
    OUTPUT_NODE = True

    def sink(self, **kwargs):
        result = _pick(kwargs, "循环结果", "loop_result", default="批量处理完成")
        log.info("[SuperFor_BatchLoopSink] 全部完成")
        return (result,)


class DirForLoopEnd:
    """SuperFor 批量循环-结束（不可设 OUTPUT_NODE，见 BatchLoopSink）"""

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

    def end(self, dynprompt=None, extra_pnginfo=None, unique_id=None, **kwargs):
        flow = _pick(kwargs, "循环流程", "flow", "🔁 循环流程")
        anchor = _pick(kwargs, "循环体回接", "loop_anchor", "🔗 循环体回接", "initial_value1", default=None)

        graph = GraphBuilder()
        while_open = flow[0]
        total = _resolve_total(dynprompt, while_open)

        sub = graph.node("easy mathInt", operation="add", a=[while_open, 1], b=1)
        cond = graph.node("easy compare", a=sub.out(0), b=total, comparison="a < b")
        while_close = graph.node(
            "SuperFor_WhileLoopEnd",
            flow=flow,
            condition=cond.out(0),
            initial_value0=sub.out(0),
            initial_value1=anchor,
        )
        return {
            "result": (while_close.out(1),),
            "expand": graph.finalize(),
        }


if _HAS_GRAPH:
    NODE_CLASS_MAPPINGS = {
        "SuperFor_DirForLoopStart": DirForLoopStart,
        "SuperFor_DirForLoopEnd": DirForLoopEnd,
        "SuperFor_BatchLoopSink": BatchLoopSink,
        "SuperFor_WhileLoopEnd": SuperForWhileLoopEnd,
    }
    NODE_DISPLAY_NAME_MAPPINGS = {
        "SuperFor_DirForLoopStart": "批量循环-开始（自动计数）",
        "SuperFor_DirForLoopEnd": "批量循环-结束",
        "SuperFor_BatchLoopSink": "批量循环-完成出口",
        "SuperFor_WhileLoopEnd": "批量循环-内部结束",
    }
else:  # pragma: no cover
    NODE_CLASS_MAPPINGS = {}
    NODE_DISPLAY_NAME_MAPPINGS = {}
    log.warning("[comfyui-superfor] 未找到 comfy_execution.graph_utils，循环节点不可用")
