"""配置加载与 provider 声明的一致性校验。"""

import config
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_providers_have_required_fields():
    for name, p in config.PROVIDERS.items():
        assert p["base_url"].startswith("http"), name
        assert p["model"], name
        assert "api_key" in p, name
        assert isinstance(p.get("support_multi_image"), bool), f"{name} 缺少 support_multi_image"


def test_api_key_env_covers_every_provider():
    missing = set(config.PROVIDERS) - set(config.API_KEY_ENV)
    assert not missing, f"缺少环境变量映射：{missing}"


def test_text_only_providers_are_real_providers():
    assert config.TEXT_ONLY_PROVIDERS <= set(config.PROVIDERS)


def test_active_provider_is_vision_capable():
    assert config.ACTIVE_PROVIDER not in config.TEXT_ONLY_PROVIDERS


def test_numeric_settings_are_sane():
    assert config.MAX_STEPS >= 30, "实验规程要求上限 30 步"
    assert config.STEP_RETRIES >= 0
    assert 0 <= config.LLM_TEMPERATURE <= 1


def test_max_steps_env_override():
    proc = subprocess.run(
        [sys.executable, "-c", "import config; print(config.MAX_STEPS)"],
        cwd=str(ROOT),
        env={**os.environ, "MAX_STEPS": "12"},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "12"
