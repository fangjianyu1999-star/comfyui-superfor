# comfyui-superfor

ComfyUI **批处理 / 自动循环**节点包：把一个文件夹（可含多层子目录）里的图片逐张送进工作流，结果按**原目录结构 + 原文件名**保存；**点一次「运行」**即可跑完全部（无需手填张数、无需 Auto Queue 逐张排队）。

> 本包从 `ComfyUI-CompanyAPI` 中独立出来。节点内部 `class_type` 统一为 `SuperFor_*`。

## 节点一览（菜单：`SuperFor/批量`）

| 节点 | 作用 |
| --- | --- |
| **批量循环-开始**（`SuperFor_DirForLoopStart`） | 指定根目录，递归统计张数并驱动循环；输出当前图及路径信息 |
| **批量循环-结束**（`SuperFor_DirForLoopEnd`） | 与开始配对；`已保存路径` 必须接 `循环体回接` |
| **批量遍历加载**（`SuperFor_LoadImageBatch`） | 递归扫描，逐张或按序号取图（配合 Auto Queue 的另一种方案） |
| **按路径保存**（`SuperFor_SaveImageToDir`） | 保存到 `根目录/相对子目录/文件名` |
| **目录图片计数**（`SuperFor_CountImagesInDir`） | 递归统计图片数，可接 easy-use For 循环的 `total` 字面值 |
| **文件夹批量导出**（`SuperFor_BatchFolderExport`） | 单节点内 Python 循环：缩放/复制整夹，不经图展开 |
| **批量循环-内部结束**（`SuperFor_WhileLoopEnd`） | 内部用，勿手动添加 |

依赖：[comfyui-easy-use](https://github.com/yolain/ComfyUI-Easy-Use)（提供 `easy whileLoopStart`、`easy mathInt`、`easy compare` 等）。

## 推荐接法：自动循环（示例：`批量修复-自动循环.json`）

```
① 批量循环-开始
   ├─ 图像 ──────────→ ② 处理 / 修复
   ├─ 相对子目录 ─────→ ③ 按路径保存（与保存节点「相对子目录」上下对齐，线不交叉）
   ├─ 文件名 ─────────→ ③ 按路径保存
   └─ 循环流程 ───────→ ④ 批量循环-结束
③ 已保存路径 ─────────→ ④ 循环体回接   ← 必须接，否则只跑一张
④ 循环完成 ───────────→ ⑤ PreviewAny（完成出口，便于点「运行」）
```

要点：

- **开始**的「文件夹路径」填源根目录（支持 `~`，递归子文件夹由「含子文件夹」控制）。
- **字符串输出口顺序**为：`相对子目录` → `文件名` → `相对路径`，与 **按路径保存** 的 `相对子目录`、`文件名` 输入顺序一致，直连不交叉。
- **④ 结束**不是输出节点；**⑤ PreviewAny** 接在循环外，作为队列出口（避免循环展开时 OUTPUT 被反复入队）。
- 换源文件夹或源目录内增删图后，**开始**节点会根据目录指纹自动失效缓存，一般无需 `--cache-none`。

## 单节点整夹导出

`批量修复-批量导出.json`：仅 **文件夹批量导出**，适合「只要缩放/复制到另一目录」、中间不接其它图节点。

## 生成桌面示例工作流

```bash
cd /path/to/ComfyUI
source venv/bin/activate
python custom_nodes/comfyui-superfor/tools/build_batch_workflow.py
```

会在桌面生成：

- `批量修复-自动循环.json`
- `批量修复-AutoQueue.json`
- `批量修复-批量导出.json`

## 旧工作流 / 旧节点名

若 JSON 里仍是 `Aiaiartist_*` 循环节点，请用上面脚本重新生成或对照本 README 改 `class_type`。

若旧图里「开始 → 保存」两根线按**旧端口顺序**（先文件名后目录）连接，更新节点后请 **删线重连** 或重新 Load 新版 JSON。

## Windows 乱码

若标题/参数出现 `åæ'è` 等，多为 UTF-8 被误解码。请 `git pull` 后重启 ComfyUI，并重新导入 JSON；必要时设置 `PYTHONUTF8=1` 再启动。
