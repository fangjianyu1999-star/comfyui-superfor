"""
comfyui-superfor 节点包入口
===========================

把「批量遍历 / 按路径保存 / 目录计数 / 自动递归计数 for 循环」这一套
批处理节点从 ComfyUI-CompanyAPI 中独立出来，单独管理与发布。

注册方式：
1. **V3（推荐）**：通过 `comfy_entrypoint` 返回 `ComfyExtension`，
   暴露批量相关 V3 节点（加载 / 保存 / 计数）。
2. **批量循环节点（V1）**：依赖 hidden 输入 + 图展开（GraphBuilder/dynprompt），
   无法用 V3 Schema 表达，因此用 V1 类直接写入 ComfyUI 全局节点注册表。

⚠ 依赖 comfyui-easy-use（提供底层 whileLoop / mathInt / compare 节点）。
"""
from __future__ import annotations

import logging

log = logging.getLogger("comfyui-superfor")

_V3_REGISTERED = False

try:
    from comfy_api.latest import ComfyExtension

    from .src.batch import get_batch_v3_nodes

    class SuperForExtension(ComfyExtension):
        async def get_node_list(self):
            return get_batch_v3_nodes()

    async def comfy_entrypoint() -> "SuperForExtension":
        return SuperForExtension()

    _V3_REGISTERED = True
    log.info("[comfyui-superfor] V3 extension 入口已注册（%d 个批量节点）", len(get_batch_v3_nodes()))
except ImportError:
    log.info("[comfyui-superfor] 当前 ComfyUI 不支持 V3 API，批量节点不可用（循环节点仍可用）")
except Exception as e:  # noqa: BLE001
    log.exception("[comfyui-superfor] V3 extension 注册失败：%s", e)

if _V3_REGISTERED:
    __all__ = ["comfy_entrypoint"]

# 「批量循环」节点（V1）：直接写入 ComfyUI 全局节点注册表。
# 不暴露模块级 NODE_CLASS_MAPPINGS，以免抢占 V1 分支导致上面的 V3 入口被跳过。
try:
    import nodes as _comfy_nodes

    from .src.loop_nodes import (
        NODE_CLASS_MAPPINGS as _LOOP_NCM,
        NODE_DISPLAY_NAME_MAPPINGS as _LOOP_NDM,
    )

    if _LOOP_NCM:
        _comfy_nodes.NODE_CLASS_MAPPINGS.update(_LOOP_NCM)
        _comfy_nodes.NODE_DISPLAY_NAME_MAPPINGS.update(_LOOP_NDM)
        log.info("[comfyui-superfor] 已注册 %d 个批量循环节点（V1）", len(_LOOP_NCM))
except Exception as e:  # noqa: BLE001
    log.exception("[comfyui-superfor] 批量循环节点注册失败：%s", e)
