"""截图预处理与坐标反向映射。

顺序：截图 → 编码优化（可转 JPEG）→ 等比缩放 → 轻量增强 → 发送。
缩放会改变模型看到的坐标系，因此模型返回的坐标必须先用 map_action_coords
反向映射回设备坐标，再交给 tap/swipe。
"""
import io
import os

from PIL import Image, ImageEnhance

import config

MIME_BY_FORMAT = {"JPEG": "image/jpeg", "PNG": "image/png"}


def preprocess_image(path: str, jpeg_quality: int = 85, max_side: int = 0,
                     enhance: bool = False) -> tuple[bytes, dict]:
    """按配置处理一张截图，返回 (编码后的字节, 元数据)。

    jpeg_quality=0 时保留 PNG；max_side=0 时不缩放。
    元数据里的 scale_x/scale_y 是「发送尺寸 / 原始尺寸」，用于坐标反算。
    """
    with Image.open(path) as raw:
        img = raw.convert("RGB")
    orig_w, orig_h = img.size

    sent_w, sent_h = orig_w, orig_h
    if max_side and max(orig_w, orig_h) > max_side:
        ratio = max_side / max(orig_w, orig_h)
        sent_w = max(1, round(orig_w * ratio))
        sent_h = max(1, round(orig_h * ratio))
        img = img.resize((sent_w, sent_h), Image.LANCZOS)

    if enhance:
        img = ImageEnhance.Contrast(img).enhance(1.15)
        img = ImageEnhance.Sharpness(img).enhance(1.2)

    buf = io.BytesIO()
    if jpeg_quality:
        img.save(buf, format="JPEG", quality=int(jpeg_quality), optimize=True)
        fmt = "JPEG"
    else:
        img.save(buf, format="PNG", optimize=True)
        fmt = "PNG"

    data = buf.getvalue()
    meta = {
        "format": fmt,
        "mime": MIME_BY_FORMAT[fmt],
        "jpeg_quality": int(jpeg_quality) if jpeg_quality else None,
        "orig_size": [orig_w, orig_h],
        "sent_size": [sent_w, sent_h],
        "scale_x": sent_w / orig_w,
        "scale_y": sent_h / orig_h,
        "resized": [sent_w, sent_h] != [orig_w, orig_h],
        "enhanced": bool(enhance),
        "bytes": len(data),
    }
    return data, meta


def prepare_image_bytes(path: str) -> tuple[bytes, dict]:
    """按 config 的开关处理截图；整体关闭时原样返回文件字节。"""
    if not getattr(config, "IMAGE_PREPROCESS", True):
        with open(path, "rb") as f:
            data = f.read()
        with Image.open(path) as img:
            width, height = img.size
        return data, {
            "format": "PNG",
            "mime": "image/png",
            "jpeg_quality": None,
            "orig_size": [width, height],
            "sent_size": [width, height],
            "scale_x": 1.0,
            "scale_y": 1.0,
            "resized": False,
            "enhanced": False,
            "bytes": len(data),
        }

    return preprocess_image(
        path,
        jpeg_quality=int(getattr(config, "IMAGE_JPEG_QUALITY", 85)),
        max_side=int(getattr(config, "IMAGE_MAX_SIDE", 0)),
        enhance=bool(getattr(config, "IMAGE_ENHANCE", False)),
    )


def map_point(x, y, scale_x: float, scale_y: float, width: int, height: int) -> tuple[int, int]:
    """模型坐标 → 设备坐标：除以比例、四舍五入、裁到 [0, width-1] / [0, height-1]。"""
    if scale_x <= 0 or scale_y <= 0:
        raise ValueError(f"缩放比例必须为正：scale_x={scale_x}, scale_y={scale_y}")
    mapped_x = round(float(x) / scale_x)
    mapped_y = round(float(y) / scale_y)
    mapped_x = max(0, min(width - 1, mapped_x))
    mapped_y = max(0, min(height - 1, mapped_y))
    return mapped_x, mapped_y


def map_action_coords(action: dict, image_meta: dict) -> dict:
    """把动作里的 x/y、x1/y1、x2/y2 反向映射到设备坐标；未缩放时原样返回，不引入偏移。"""
    if not image_meta.get("resized"):
        return action

    width, height = image_meta["orig_size"]
    scale_x = image_meta["scale_x"]
    scale_y = image_meta["scale_y"]
    mapped = dict(action)
    # tap 用 x/y，swipe 用 x1/y1 -> x2/y2
    for key_x, key_y in (("x", "y"), ("x1", "y1"), ("x2", "y2")):
        if mapped.get(key_x) is None or mapped.get(key_y) is None:
            continue
        try:
            mapped[key_x], mapped[key_y] = map_point(
                mapped[key_x], mapped[key_y], scale_x, scale_y, width, height
            )
        except (TypeError, ValueError):
            # 坐标不是数字时不猜测，交给执行层按原值处理
            continue
    return mapped


def describe(meta: dict) -> str:
    """一行摘要，写进日志便于核对本次发送的图片规格。"""
    orig = "x".join(str(v) for v in meta["orig_size"])
    sent = "x".join(str(v) for v in meta["sent_size"])
    return (f"{meta['format'].lower()} q={meta['jpeg_quality']} "
            f"{orig} -> {sent} scale=({meta['scale_x']:.4f},{meta['scale_y']:.4f}) "
            f"{meta['bytes']}B")


def sent_filename(path: str, meta: dict) -> str:
    """发送图另存一份便于复查（不覆盖原始截图），扩展名随编码格式。"""
    ext = ".jpg" if meta.get("format") == "JPEG" else ".png"
    return os.path.splitext(path)[0] + "_sent" + ext
