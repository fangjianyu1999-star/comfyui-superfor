"""
生成「批量修复」示例工作流（litegraph UI 格式，可直接拖进 ComfyUI 导入）。

产出两个文件到桌面：
  - 批量修复-For循环.json    配合 easy-use For 循环，单次排队跑完整个文件夹
  - 批量修复-AutoQueue.json  不用循环，逐张 + Auto Queue，最简单

图生图节点（Aiaiartist_ImageToImage）在示例里仅作「修复节点占位」，
用户把它换成自己的高清修复 API 节点即可。
"""
from __future__ import annotations

import json
import os

LINK_TYPE_IMAGE = "IMAGE"
LINK_TYPE_INT = "INT"
LINK_TYPE_STRING = "STRING"
LINK_TYPE_FLOW = "FLOW_CONTROL"


class WF:
    """极简 litegraph 工作流构造器。"""

    def __init__(self) -> None:
        self.nodes: list[dict] = []
        self.links: list[list] = []
        self._nid = 0
        self._lid = 0

    def add(self, type_: str, pos, size, *, title=None, widgets=None,
            inputs=None, outputs=None, properties=None) -> dict:
        self._nid += 1
        node = {
            "id": self._nid,
            "type": type_,
            "pos": list(pos),
            "size": list(size),
            "flags": {},
            "order": self._nid,
            "mode": 0,
            "inputs": inputs or [],
            "outputs": outputs or [],
            "properties": properties or {"Node name for S&R": type_},
        }
        if title:
            node["title"] = title
        if widgets is not None:
            node["widgets_values"] = widgets
        self.nodes.append(node)
        return node

    def link(self, src_node: dict, src_slot: int, dst_node: dict, dst_slot: int, type_: str) -> None:
        self._lid += 1
        lid = self._lid
        self.links.append([lid, src_node["id"], src_slot, dst_node["id"], dst_slot, type_])
        # 登记到输出端
        src_node["outputs"][src_slot].setdefault("links", [])
        src_node["outputs"][src_slot]["links"].append(lid)
        # 登记到输入端
        dst_node["inputs"][dst_slot]["link"] = lid

    def dump(self) -> dict:
        return {
            "revision": 0,
            "last_node_id": self._nid,
            "last_link_id": self._lid,
            "nodes": self.nodes,
            "links": self.links,
            "groups": [],
            "config": {},
            "extra": {},
            "version": 0.4,
        }


def out(name: str, type_: str) -> dict:
    return {"name": name, "type": type_, "links": []}


def slot_in(name: str, type_: str) -> dict:
    """普通连接输入口。"""
    return {"name": name, "type": type_, "link": None}


def widget_in(name: str, type_: str) -> dict:
    """由 widget 转成的输入口（带 widget 引用）。"""
    return {"name": name, "type": type_, "widget": {"name": name}, "link": None}


# 用户可按需修改的默认路径
SRC_DIR = "/Users/ikun/Downloads/16岁"
OUT_DIR = "/Users/ikun/Downloads/xf"

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif")


def count_images(directory: str, include_subdir: bool = True) -> int:
    """统计目录图片数，用于写死 For 循环的 total（字面值）。"""
    root = os.path.expanduser(directory)
    n = 0
    if include_subdir:
        for _dp, _dn, names in os.walk(root):
            n += sum(1 for x in names if os.path.splitext(x)[1].lower() in IMAGE_EXTS)
    elif os.path.isdir(root):
        n += sum(
            1 for x in os.listdir(root)
            if os.path.isfile(os.path.join(root, x))
            and os.path.splitext(x)[1].lower() in IMAGE_EXTS
        )
    return max(1, n)

# 各节点 widget 顺序（必须与 src/batch.py、src/nodes.py 定义顺序一致）
LOADER_WIDGETS = [SRC_DIR, "single", 0, True, "name", ""]
LOADER_WIDGETS_AQ = [SRC_DIR, "incremental", 0, True, "name", ""]
COUNT_WIDGETS = [SRC_DIR, True, ""]
I2I_WIDGETS = ["", "", "", 1024, 1024, 1]  # prompt, image_url, negative_prompt, width, height, batch_size
# 顺序须与 object_info 一致：output_root, relative_dir, filename, image_format, quality, overwrite, [prefix, suffix]
SAVER_WIDGETS = [OUT_DIR, "", "", "png", 95, True, "", "_修复"]
LOADER_WIDGETS_LOOP = [SRC_DIR, "single", 0, True, "name", ""]  # 指定序号，index 由循环开始节点喂入
# ImageScale: upscale_method, width, height(0=按比例), crop
PREVIEW_SCALE_WIDGETS = ["lanczos", 1280, 0, "disabled"]
# 预览由「按路径保存」节点内置 PreviewImage UI 提供，勿再加 PreviewImage 节点（会与保存同为 OUTPUT，导致每张重复跑）


def loader_outputs() -> list[dict]:
    return [
        out("image", LINK_TYPE_IMAGE),
        out("filename", LINK_TYPE_STRING),
        out("relative_dir", LINK_TYPE_STRING),
        out("relative_path", LINK_TYPE_STRING),
        out("source_path", LINK_TYPE_STRING),
        out("current_index", LINK_TYPE_INT),
        out("total", LINK_TYPE_INT),
    ]


def build_for_loop() -> dict:
    wf = WF()
    total = count_images(SRC_DIR)  # 写死字面值：easy-use forLoopEnd 直接读 total 数值，不能用连线

    for_start = wf.add(
        "easy forLoopStart", (40, 40), (300, 130),
        title="① For循环-开始（total=图片数）",
        widgets=[total],  # total 字面值，关键！
        inputs=[],
        outputs=[out("flow", LINK_TYPE_FLOW), out("index", LINK_TYPE_INT)],
    )

    loader = wf.add(
        "SuperFor_LoadImageBatch", (40, 220), (320, 280),
        title="② 批量遍历加载（指定序号）",
        widgets=list(LOADER_WIDGETS),
        inputs=[widget_in("index", LINK_TYPE_INT)],
        outputs=loader_outputs(),
    )

    i2i = wf.add(
        "Aiaiartist_ImageToImage", (420, 220), (320, 260),
        title="③ 修复节点占位（换成你的修复节点）",
        widgets=list(I2I_WIDGETS),
        inputs=[slot_in("image", LINK_TYPE_IMAGE)],
        outputs=[out("images", LINK_TYPE_IMAGE)],
    )

    saver = wf.add(
        "SuperFor_SaveImageToDir", (800, 220), (320, 300),
        title="④ 按路径保存",
        widgets=list(SAVER_WIDGETS),
        inputs=[
            slot_in("images", LINK_TYPE_IMAGE),
            widget_in("relative_dir", LINK_TYPE_STRING),
            widget_in("filename", LINK_TYPE_STRING),
        ],
        outputs=[out("saved_paths", LINK_TYPE_STRING)],
    )

    for_end = wf.add(
        "easy forLoopEnd", (800, 40), (300, 130),
        title="⑤ For循环-结束",
        widgets=[],
        inputs=[
            slot_in("flow", LINK_TYPE_FLOW),
            slot_in("initial_value1", LINK_TYPE_STRING),
        ],
        outputs=[out("value1", LINK_TYPE_STRING)],
    )

    # 接线
    wf.link(for_start, 1, loader, 0, LINK_TYPE_INT)        # 索引 → 加载器.index
    wf.link(loader, 0, i2i, 0, LINK_TYPE_IMAGE)            # image → 修复
    wf.link(i2i, 0, saver, 0, LINK_TYPE_IMAGE)             # 修复 → 保存.images
    wf.link(loader, 2, saver, 1, LINK_TYPE_STRING)         # relative_dir
    wf.link(loader, 1, saver, 2, LINK_TYPE_STRING)         # filename
    wf.link(saver, 0, for_end, 1, LINK_TYPE_STRING)        # saved_paths → 循环结束 初始值1（关键）
    wf.link(for_start, 0, for_end, 0, LINK_TYPE_FLOW)      # flow

    return wf.dump()


def build_dir_loop() -> dict:
    """自动循环：开始节点直接出图/路径，循环体内不再经「批量遍历加载」接序号（会落后一轮）。"""
    wf = WF()

    start = wf.add(
        "SuperFor_DirForLoopStart", (40, 60), (320, 200),
        title="① 批量循环-开始（自动计数）",
        widgets=[SRC_DIR, True, "按路径名", ""],
        inputs=[],
        outputs=[
            out("循环流程", LINK_TYPE_FLOW),
            out("当前序号", LINK_TYPE_INT),
            out("图像", LINK_TYPE_IMAGE),
            out("文件名", LINK_TYPE_STRING),
            out("相对子目录", LINK_TYPE_STRING),
            out("相对路径", LINK_TYPE_STRING),
            out("图片总数", LINK_TYPE_INT),
        ],
    )

    i2i = wf.add(
        "Aiaiartist_ImageToImage", (400, 220), (320, 260),
        title="② 修复节点占位（换成你的修复节点）",
        widgets=list(I2I_WIDGETS),
        inputs=[slot_in("image", LINK_TYPE_IMAGE)],
        outputs=[out("images", LINK_TYPE_IMAGE)],
    )

    saver = wf.add(
        "SuperFor_SaveImageToDir", (760, 220), (320, 300),
        title="③ 按路径保存（右侧自动预览）",
        widgets=list(SAVER_WIDGETS),
        inputs=[
            slot_in("images", LINK_TYPE_IMAGE),
            widget_in("relative_dir", LINK_TYPE_STRING),
            widget_in("filename", LINK_TYPE_STRING),
        ],
        outputs=[out("saved_paths", LINK_TYPE_STRING)],
    )

    end = wf.add(
        "SuperFor_DirForLoopEnd", (760, 60), (300, 110),
        title="④ 批量循环-结束",
        widgets=[],
        inputs=[
            slot_in("循环流程", LINK_TYPE_FLOW),
            slot_in("循环体回接", LINK_TYPE_STRING),
        ],
        outputs=[out("循环完成", LINK_TYPE_STRING)],
    )

    # 用 ComfyUI 自带的 PreviewAny 作队列出口（无需自定义节点，避免未重启时报 class_type 缺失）
    sink = wf.add(
        "PreviewAny", (1100, 60), (280, 100),
        title="⑤ 完成出口 PreviewAny（必须保留）",
        widgets=[],
        inputs=[slot_in("source", LINK_TYPE_STRING)],
        outputs=[out("STRING", LINK_TYPE_STRING)],
        properties={"Node name for S&R": "PreviewAny"},
    )

    wf.link(start, 0, end, 0, LINK_TYPE_FLOW)          # 循环流程 → 结束
    wf.link(start, 2, i2i, 0, LINK_TYPE_IMAGE)         # 图像 → 修复（直接接开始，勿经加载器）
    wf.link(i2i, 0, saver, 0, LINK_TYPE_IMAGE)          # 修复 → 保存
    wf.link(start, 4, saver, 1, LINK_TYPE_STRING)       # 相对子目录
    wf.link(start, 3, saver, 2, LINK_TYPE_STRING)       # 文件名
    wf.link(saver, 0, end, 1, LINK_TYPE_STRING)          # 已保存路径 → 循环体回接（关键）
    wf.link(end, 0, sink, 0, LINK_TYPE_STRING)           # 循环完成 → 完成出口（关键）

    return wf.dump()


def build_auto_queue() -> dict:
    wf = WF()

    loader = wf.add(
        "SuperFor_LoadImageBatch", (40, 60), (320, 280),
        title="① 批量遍历加载（逐张）",
        widgets=list(LOADER_WIDGETS_AQ),
        inputs=[],
        outputs=loader_outputs(),
    )

    i2i = wf.add(
        "Aiaiartist_ImageToImage", (400, 60), (320, 260),
        title="② 修复节点占位（换成你的修复节点）",
        widgets=list(I2I_WIDGETS),
        inputs=[slot_in("image", LINK_TYPE_IMAGE)],
        outputs=[out("images", LINK_TYPE_IMAGE)],
    )

    saver = wf.add(
        "SuperFor_SaveImageToDir", (760, 60), (320, 300),
        title="③ 按路径保存",
        widgets=list(SAVER_WIDGETS),
        inputs=[
            slot_in("images", LINK_TYPE_IMAGE),
            widget_in("relative_dir", LINK_TYPE_STRING),
            widget_in("filename", LINK_TYPE_STRING),
        ],
        outputs=[out("saved_paths", LINK_TYPE_STRING)],
    )

    wf.link(loader, 0, i2i, 0, LINK_TYPE_IMAGE)
    wf.link(i2i, 0, saver, 0, LINK_TYPE_IMAGE)
    wf.link(loader, 2, saver, 1, LINK_TYPE_STRING)
    wf.link(loader, 1, saver, 2, LINK_TYPE_STRING)

    return wf.dump()


def build_batch_export() -> dict:
    """最稳：单节点一次跑完（适合缩放/复制，不经图展开循环）。"""
    wf = WF()
    wf.add(
        "SuperFor_BatchFolderExport", (40, 60), (360, 340),
        title="文件夹批量导出（推荐：缩放/复制用这个）",
        widgets=[SRC_DIR, OUT_DIR, True, "name", "", 0, "_修复", "png", 95, True],
        inputs=[],
        outputs=[
            out("summary", LINK_TYPE_STRING),
            out("count", LINK_TYPE_INT),
        ],
    )
    return wf.dump()


def main() -> None:
    desktop = os.path.expanduser("~/Desktop")
    targets = {
        os.path.join(desktop, "批量修复-自动循环.json"): build_dir_loop(),
        os.path.join(desktop, "批量修复-批量导出.json"): build_batch_export(),
        os.path.join(desktop, "批量修复-AutoQueue.json"): build_auto_queue(),
    }
    for path, data in targets.items():
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"已生成：{path}")


if __name__ == "__main__":
    main()
