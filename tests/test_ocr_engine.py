"""OCR 引擎选择与按引擎分发。"""
import config
import ocr_locator


def test_default_engine_is_rapidocr(monkeypatch):
    monkeypatch.setattr(config, "OCR_ENGINE", "rapidocr")
    loc = ocr_locator.OCRLocator(device=None)
    assert loc._engine_name == "rapidocr"
    # 引擎延迟加载，构造时不应 import easyocr/rapidocr
    assert loc.reader is None and loc._rapid_reader is None


def test_engine_from_config(monkeypatch):
    monkeypatch.setattr(config, "OCR_ENGINE", "easyocr")
    assert ocr_locator.OCRLocator(device=None)._engine_name == "easyocr"


def test_explicit_engine_overrides_config(monkeypatch):
    monkeypatch.setattr(config, "OCR_ENGINE", "easyocr")
    assert ocr_locator.OCRLocator(engine="rapidocr", device=None)._engine_name == "rapidocr"


def test_device_none_skips_u2_connect():
    assert ocr_locator.OCRLocator(device=None).d is None


def test_ocr_detect_dispatches_by_engine(monkeypatch):
    loc = ocr_locator.OCRLocator(device=None)
    calls = []
    monkeypatch.setattr(loc, "_rapidocr_detect", lambda path, scale: (calls.append("rapid") or ["r"], {}))
    monkeypatch.setattr(loc, "_easyocr_detect", lambda path, scale: (calls.append("easy") or ["e"], {}))

    loc._engine_name = "rapidocr"
    assert loc.detect_ocr("p.png") == ["r"]
    loc._engine_name = "easyocr"
    assert loc.detect_ocr("p.png") == ["e"]
    assert calls == ["rapid", "easy"]


def test_detect_tree_does_not_run_ocr(monkeypatch):
    loc = ocr_locator.OCRLocator(device=None)
    monkeypatch.setattr(loc, "_xml_detect", lambda: [{"text": "设置"}])
    monkeypatch.setattr(loc, "_ocr_detect", lambda path: (_ for _ in ()).throw(AssertionError("不应跑 OCR")))
    assert loc.detect_tree() == [{"text": "设置"}]
