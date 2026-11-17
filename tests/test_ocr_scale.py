"""OCR 缩放与坐标映射：几何正确性、边界裁切、scale=1.0 不改变行为。"""
from PIL import Image

import config
import ocr_locator


def test_map_point_scales_back():
    assert ocr_locator.map_point(270, 593.5, 2.0, 1080, 2374) == (540, 1187)


def test_map_point_clamps_to_image_bounds():
    assert ocr_locator.map_point(-10, -10, 2.0, 1080, 2374) == (0, 0)
    assert ocr_locator.map_point(1000, 2000, 2.0, 1080, 2374) == (1079, 2373)


def test_map_bbox_scales_and_keeps_order():
    x1, y1, x2, y2 = ocr_locator.map_bbox(100, 200, 300, 400, 2.0, 1080, 2374)
    assert (x1, y1, x2, y2) == (200, 400, 600, 800)


def test_map_bbox_clamps_and_normalizes():
    x1, y1, x2, y2 = ocr_locator.map_bbox(-50, -50, 1000, 2000, 2.0, 1080, 2374)
    assert (x1, y1) == (0, 0)
    assert (x2, y2) == (1079, 2373)
    assert x1 <= x2 and y1 <= y2


def test_load_scaled_halves_size(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OCR_SCALE", 0.5)
    path = tmp_path / "shot.png"
    Image.new("RGB", (1080, 2374), (255, 255, 255)).save(path)

    loc = ocr_locator.OCRLocator(device=None)
    img, orig, scaled = loc._load_scaled(str(path), 0.5)

    assert orig == (1080, 2374)
    assert scaled == (540, 1187)
    assert img.shape[1] == 540 and img.shape[0] == 1187


def test_load_scaled_noop_when_scale_is_one(tmp_path):
    path = tmp_path / "shot.png"
    Image.new("RGB", (1080, 2374), (255, 255, 255)).save(path)

    loc = ocr_locator.OCRLocator(device=None)
    img, orig, scaled = loc._load_scaled(str(path), 1.0)

    assert orig == scaled == (1080, 2374)
    assert img.shape[1] == 1080


def test_scale_is_clamped_in_detect(monkeypatch):
    loc = ocr_locator.OCRLocator(device=None)
    seen = {}

    def fake_rapid(image_path, scale):
        seen["scale"] = scale
        return [], {"scale": scale}

    monkeypatch.setattr(loc, "_rapidocr_detect", fake_rapid)
    monkeypatch.setattr(config, "OCR_SCALE", 5.0)
    loc._ocr_detect("p.png")
    assert seen["scale"] == 1.0

    monkeypatch.setattr(config, "OCR_SCALE", 0.01)
    loc._ocr_detect("p.png")
    assert seen["scale"] == 0.1
