"""baseline 子进程适配协议的单元测试（不依赖任何真实 baseline）。"""

import sys

import pytest

from experiments.baselines.adapter import SubprocessAdapter


def test_adapter_parses_last_json_line():
    cmd = [sys.executable, "-c",
           "import json,sys; print('debug line'); print(json.dumps({'summary':'ok','steps':7,'elapsed':1.5}))"]
    result = SubprocessAdapter("fake", cmd).run("打开设置")
    assert result.summary == "ok"
    assert result.steps == 7
    assert result.elapsed == 1.5


def test_adapter_raises_on_non_zero_exit():
    cmd = [sys.executable, "-c", "import sys; print('boom'); sys.exit(2)"]
    with pytest.raises(RuntimeError) as exc:
        SubprocessAdapter("fake", cmd).run("任务")
    assert "退出码 2" in str(exc.value)
