"""
工具函数：图像编解码、错误占位图、日志
"""
from __future__ import annotations

import base64
import io
import logging
from typing import Any

import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("comfyui-superfor")


def pil_to_comfy_tensor(img: Image.Image) -> torch.Tensor:
    """
    PIL.Image  →  ComfyUI 标准张量

    ComfyUI 约定：图像 tensor shape `[B, H, W, C]`，值域 `[0, 1]`，dtype `float32`。
    """
    if img.mode == "RGBA":
        img = img.convert("RGB")
    elif img.mode != "RGB":
        img = img.convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    tensor = torch.from_numpy(arr).unsqueeze(0)
    return tensor


def comfy_tensor_to_pil(tensor: torch.Tensor) -> Image.Image:
    """ComfyUI 张量 → PIL.Image（取 batch 第一张）"""
    if tensor.ndim == 4:
        tensor = tensor[0]
    arr = (tensor.detach().cpu().clamp(0, 1).numpy() * 255).astype(np.uint8)
    return Image.fromarray(arr)


def comfy_tensor_to_pil_list(tensor: torch.Tensor) -> list[Image.Image]:
    """ComfyUI 图像张量 → PIL.Image 列表（保留 batch 中每一张）"""
    if tensor.ndim == 3:
        tensor = tensor.unsqueeze(0)
    images: list[Image.Image] = []
    for single in tensor:
        arr = (single.detach().cpu().clamp(0, 1).numpy() * 255).astype(np.uint8)
        images.append(Image.fromarray(arr))
    return images


def comfy_tensor_to_base64(tensor: torch.Tensor, format: str = "PNG") -> str:
    """ComfyUI 张量 → base64 字符串（用于上传到 API）"""
    img = comfy_tensor_to_pil(tensor)
    buf = io.BytesIO()
    img.save(buf, format=format)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def pil_to_png_bytes(img: Image.Image) -> bytes:
    """PIL.Image → PNG 字节流（用于 multipart 上传）"""
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def base64_or_url_to_pil(data: str) -> Image.Image:
    """
    自适应解析图像数据：
    - 如果是 `data:image/...;base64,xxx` 或纯 base64 → 解码
    - 如果是 `http(s)://...` → 下载
    """
    if data.startswith(("http://", "https://")):
        import requests

        resp = requests.get(data, timeout=60)
        resp.raise_for_status()
        return Image.open(io.BytesIO(resp.content))

    if data.startswith("data:"):
        data = data.split(",", 1)[1]

    raw = base64.b64decode(data)
    return Image.open(io.BytesIO(raw))


def make_error_image(message: str, width: int = 512, height: int = 512) -> torch.Tensor:
    """
    生成一张带错误信息的占位图（替代抛出异常导致整个工作流红框崩溃）。

    用户在 ComfyUI 画布上看到这张图就知道哪里出错了。
    """
    img = Image.new("RGB", (width, height), color=(40, 40, 40))
    draw = ImageDraw.Draw(img)

    font, title_font = _load_cjk_font(20), _load_cjk_font(28)

    draw.text((20, 20), "⚠ 公司 API 调用失败", fill=(255, 100, 100), font=title_font)
    y = 80
    for line in _wrap_text(message, max_chars=42)[:18]:
        draw.text((20, y), line, fill=(220, 220, 220), font=font)
        y += 28

    return pil_to_comfy_tensor(img)


# 跨平台中文字体候选（macOS / Windows / Linux）
_CJK_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",            # macOS
    "/System/Library/Fonts/STHeiti Medium.ttc",      # macOS
    "C:/Windows/Fonts/msyh.ttc",                     # Windows 微软雅黑
    "C:/Windows/Fonts/msyhbd.ttc",                   # Windows 微软雅黑粗体
    "C:/Windows/Fonts/simhei.ttf",                   # Windows 黑体
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",  # Linux 文泉驿
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # Linux Noto
]


def _load_cjk_font(size: int):
    """按平台依次尝试中文字体，全部失败再退回默认字体。"""
    for path in _CJK_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _wrap_text(text: str, max_chars: int) -> list[str]:
    """简单中英文换行（按字符数）"""
    lines: list[str] = []
    for raw_line in text.splitlines() or [""]:
        if not raw_line:
            lines.append("")
            continue
        while len(raw_line) > max_chars:
            lines.append(raw_line[:max_chars])
            raw_line = raw_line[max_chars:]
        lines.append(raw_line)
    return lines


def pretty_error(prefix: str, error: BaseException) -> str:
    """构造对用户友好的中文错误描述"""
    err_type = type(error).__name__
    msg = str(error) or "（无详细信息）"
    return f"{prefix}\n类型: {err_type}\n详情: {msg}"


def truncate(s: Any, n: int = 80) -> str:
    """日志辅助：截断长字符串"""
    s = str(s)
    return s if len(s) <= n else s[:n] + "..."
