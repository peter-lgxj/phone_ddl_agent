"""导出脱敏证据：不包含截图与原始聊天文本。"""

import json

from export_evidence import export_evidence


def test_export_removes_images_and_raw_text(tmp_path):
    run = tmp_path / "runs" / "20261117-120000"
    run.mkdir(parents=True)
    secret = "2026 计算机班级群：作业截止 11 月 29 日"
    (run / "step_001.json").write_text(json.dumps({
        "step": 1,
        "action": {"action_type": "tap", "target": "发送", "thought": "点发送"},
        "observation": secret,
        "image": {"orig_size": [1080, 2374], "sent_size": [1080, 2374]},
        "timings": {"decision": 3.2, "observe": 0.0,
                    "ocr_snap": {"moves": [{"text": secret}]}},
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }, ensure_ascii=False), encoding="utf-8")
    (run / "step_001_current.png").write_bytes(b"image")
    (run / "step_001_observe.png").write_bytes(b"image")

    out = tmp_path / "export"
    result = export_evidence(run, out)

    assert result["steps"] == 1
    assert list(out.glob("*.png")) == []
    assert "截图" in (out / "README.md").read_text(encoding="utf-8")
    step = json.loads((out / "steps.jsonl").read_text(encoding="utf-8").strip())
    assert step["action_type"] == "tap"
    assert step["driver"] == "uiautomator2"
    assert step["target_present"] is True
    assert step["observation_chars"] == len(secret)
    assert step["observation_sha256"]
    assert step["timings"]["ocr_snap"] == {"moves": 1}
    assert step["usage"]["total_tokens"] == 12
    text = (out / "steps.jsonl").read_text(encoding="utf-8")
    assert secret not in text
    assert "2026 计算机班级群" not in text

    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["steps"] == 1
    assert summary["source_run"] == "20261117-120000"
    assert summary["drivers"] == ["uiautomator2"]
    assert summary["full_pipeline"] is True
