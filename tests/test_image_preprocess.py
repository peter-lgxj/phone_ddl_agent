"""截图预处理与坐标反向映射的单元测试。"""
from PIL import Image

import config
import image_preprocess


def make_png(path, size=(1080, 1920), color=(200, 30, 30)):
    Image.new("RGB", size, color).save(path)
    return path


def make_noisy_png(path, size=(1080, 1920)):
    """噪声图更接近真实截图：PNG 压不动，JPEG 才有优势。"""
    import random

    rng = random.Random(0)
    img = Image.new("RGB", size)
    img.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                 for _ in range(size[0] * size[1])])
    img.save(path)
    return path


# ── 坐标反算 ──────────────────────────────────────────────

def test_map_point_identity_has_no_offset():
    assert image_preprocess.map_point(100, 200, 1.0, 1.0, 1080, 1920) == (100, 200)


def test_map_point_halves_scale():
    # 发送图是原图的一半，模型坐标要乘 2
    assert image_preprocess.map_point(50, 100, 0.5, 0.5, 1080, 1920) == (100, 200)


def test_map_point_rounds_to_nearest():
    assert image_preprocess.map_point(51, 101, 0.5, 0.5, 1080, 1920) == (102, 202)


def test_map_point_clamps_negative_and_overflow():
    assert image_preprocess.map_point(-30, -8, 0.5, 0.5, 1080, 1920) == (0, 0)
    assert image_preprocess.map_point(9999, 9999, 0.5, 0.5, 1080, 1920) == (1079, 1919)


def test_map_point_rejects_non_positive_scale():
    try:
        image_preprocess.map_point(1, 1, 0, 1.0, 100, 100)
    except ValueError:
        return
    raise AssertionError("scale 为 0 时应报错")


def test_map_action_coords_is_identity_when_not_resized():
    meta = {"resized": False, "orig_size": [1080, 1920], "scale_x": 1.0, "scale_y": 1.0}
    action = {"action_type": "tap", "x": 100, "y": 200}
    mapped = image_preprocess.map_action_coords(action, meta)
    assert mapped is action, "未缩放时不应新建字典或引入偏移"
    assert mapped["x"] == 100 and mapped["y"] == 200


def test_map_action_coords_maps_tap_and_swipe():
    meta = {"resized": True, "orig_size": [1080, 1920], "scale_x": 0.5, "scale_y": 0.5}
    tap = image_preprocess.map_action_coords({"action_type": "tap", "x": 50, "y": 100}, meta)
    assert (tap["x"], tap["y"]) == (100, 200)

    swipe = image_preprocess.map_action_coords(
        {"action_type": "swipe", "x1": 0, "y1": 10, "x2": 100, "y2": 200}, meta)
    assert (swipe["x1"], swipe["y1"]) == (0, 20)
    assert (swipe["x2"], swipe["y2"]) == (200, 400)


def test_map_action_coords_crops_to_screen():
    meta = {"resized": True, "orig_size": [1080, 1920], "scale_x": 0.5, "scale_y": 0.5}
    mapped = image_preprocess.map_action_coords({"action_type": "tap", "x": 9999, "y": -5}, meta)
    assert (mapped["x"], mapped["y"]) == (1079, 0)


# ── 预处理 ────────────────────────────────────────────────

def test_jpeg_keeps_size_and_shrinks_bytes(tmp_path):
    src = make_noisy_png(tmp_path / "shot.png")
    png_bytes = src.stat().st_size
    data, meta = image_preprocess.preprocess_image(str(src), jpeg_quality=85)

    assert meta["format"] == "JPEG"
    assert meta["sent_size"] == [1080, 1920]
    assert meta["resized"] is False
    assert meta["scale_x"] == 1.0 and meta["scale_y"] == 1.0
    assert len(data) < png_bytes


def test_resize_keeps_aspect_ratio_and_records_scale(tmp_path):
    src = make_png(tmp_path / "shot.png")
    data, meta = image_preprocess.preprocess_image(str(src), jpeg_quality=85, max_side=1000)

    width, height = meta["sent_size"]
    assert max(width, height) == 1000
    assert abs(width / height - 1080 / 1920) < 0.01
    assert meta["resized"] is True
    assert abs(meta["scale_x"] - width / 1080) < 1e-9
    assert abs(meta["scale_y"] - height / 1920) < 1e-9
    assert meta["bytes"] == len(data)


def test_small_image_is_not_upscaled(tmp_path):
    src = make_png(tmp_path / "small.png", size=(400, 300))
    _, meta = image_preprocess.preprocess_image(str(src), jpeg_quality=85, max_side=1000)
    assert meta["sent_size"] == [400, 300]
    assert meta["resized"] is False


def test_quality_zero_keeps_png(tmp_path):
    src = make_png(tmp_path / "shot.png")
    data, meta = image_preprocess.preprocess_image(str(src), jpeg_quality=0)
    assert meta["format"] == "PNG"
    assert meta["mime"] == "image/png"
    assert meta["jpeg_quality"] is None


def test_enhance_does_not_change_geometry(tmp_path):
    src = make_png(tmp_path / "shot.png")
    _, meta = image_preprocess.preprocess_image(str(src), jpeg_quality=85, enhance=True)
    assert meta["sent_size"] == [1080, 1920]
    assert meta["enhanced"] is True


def test_prepare_respects_master_switch(tmp_path, monkeypatch):
    src = make_png(tmp_path / "shot.png")
    monkeypatch.setattr(config, "IMAGE_PREPROCESS", False)
    data, meta = image_preprocess.prepare_image_bytes(str(src))

    assert data == src.read_bytes()
    assert meta["format"] == "PNG"
    assert meta["resized"] is False and meta["scale_x"] == 1.0


def test_sent_filename_matches_format():
    assert image_preprocess.sent_filename("screen_current.png", {"format": "JPEG"}) == "screen_current_sent.jpg"
    assert image_preprocess.sent_filename("screen_current.png", {"format": "PNG"}) == "screen_current_sent.png"
