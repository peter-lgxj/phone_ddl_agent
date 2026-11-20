"""检查 ``tests/`` 的第三方 import 是否都出现在 ``requirements-lock.txt``。

CI 在跑 pytest 前执行本脚本。新增测试 import 了新包、却只在本机装过而没写进
lock 时，这里会直接失败，避免再次出现“本地绿、CI ModuleNotFoundError”的循环。

只做静态 import 扫描，不运行测试、不联网；模块名到发行包名的映射优先用
``importlib.metadata.packages_distributions()``，并为常见别名保留静态兜底
（如 ``cv2 -> opencv-python-headless``、``PIL -> pillow``）。
"""

from __future__ import annotations

import argparse
import ast
import importlib.metadata
import re
import sys
from pathlib import Path

# 模块名（小写）到发行包名的兜底映射；包名统一按 PEP 503 归一化后比较
STATIC_ALIASES = {
    "pil": "pillow",
    "cv2": "opencv-python-headless",
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "flask": "flask",
    "openpyxl": "openpyxl",
    "pytest": "pytest",
    "uiautomator2": "uiautomator2",
    "openai": "openai",
    "numpy": "numpy",
    "torch": "torch",
    "requests": "requests",
}

_REQUIREMENT_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_SKIP_PARTS = {".git", ".venv", "venv", "venv312", "runs", "output", "state", "__pycache__"}


def normalize_distribution(name: str) -> str:
    """PEP 503 归一化：大小写、``-``/``_``/``.`` 不参与比较。"""
    return re.sub(r"[-_.]+", "-", str(name or "").strip()).lower()


def parse_requirements(path: Path, seen: set[Path] | None = None) -> set[str]:
    """读取 requirements 文件（支持 ``-r other.txt``），返回归一化包名集合。"""
    path = Path(path)
    seen = seen or set()
    if path in seen or not path.exists():
        return set()
    seen.add(path)

    names: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r ", "--requirement ")):
            included = line.split(maxsplit=1)[1].strip()
            names |= parse_requirements(path.parent / included, seen)
            continue
        if line.startswith("-"):
            continue
        match = _REQUIREMENT_RE.match(line)
        if match:
            names.add(normalize_distribution(match.group(1)))
    return names


def _local_module_names(repo_root: Path) -> set[str]:
    """仓库内自有模块（含 tests 辅助文件与 experiments 等目录）不算第三方依赖。"""
    names: set[str] = set()
    for path in repo_root.rglob("*.py"):
        if any(part in _SKIP_PARTS or part.startswith(".") for part in path.parts):
            continue
        names.add(path.stem)
    # 顶层目录只要包含 .py 就视为本地命名空间/包（experiments 没有 __init__.py）
    for child in repo_root.iterdir():
        if not child.is_dir() or child.name in _SKIP_PARTS or child.name.startswith("."):
            continue
        if next(child.rglob("*.py"), None) is not None:
            names.add(child.name)
    return names


def collect_imports(test_dir: Path) -> dict[str, set[str]]:
    """返回 ``{顶层模块名: {引用它的测试文件}}``，忽略相对 import。"""
    imports: dict[str, set[str]] = {}
    for path in sorted(Path(test_dir).rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module.split(".")[0]]
            for module in modules:
                imports.setdefault(module, set()).add(str(path))
    return imports


def _distribution_candidates(module: str) -> list[str]:
    alias = STATIC_ALIASES.get(module.lower())
    if alias:
        return [alias]
    try:
        mapping = importlib.metadata.packages_distributions()
    except Exception:
        mapping = {}
    candidates = mapping.get(module) or []
    return list(candidates) or [module]


def check(repo_root: Path | str, test_dir: Path | str | None = None,
          lock_file: Path | str | None = None) -> tuple[list[str], set[str], dict[str, set[str]]]:
    """返回 ``(missing, locked_names, imports)``；missing 为空表示一致。"""
    repo_root = Path(repo_root)
    test_dir = Path(test_dir) if test_dir else repo_root / "tests"
    lock_file = Path(lock_file) if lock_file else repo_root / "requirements-lock.txt"

    locked = parse_requirements(lock_file)
    imports = collect_imports(test_dir)
    local = _local_module_names(repo_root)
    stdlib = set(sys.stdlib_module_names) | {"__future__"}

    missing: list[str] = []
    for module in sorted(imports):
        if module in local or module in stdlib:
            continue
        candidates = _distribution_candidates(module)
        if any(normalize_distribution(name) in locked for name in candidates):
            continue
        files = ", ".join(sorted(imports[module]))
        missing.append(f"module '{module}' -> distribution {candidates} (引用：{files})")
    return missing, locked, imports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="仓库根目录（默认当前目录）")
    parser.add_argument("--tests", default=None, help="测试目录（默认 <repo>/tests）")
    parser.add_argument("--lock", default=None, help="锁文件（默认 <repo>/requirements-lock.txt）")
    args = parser.parse_args(argv)

    missing, _locked, imports = check(args.repo, args.tests, args.lock)
    if missing:
        print("❌ tests/ 引用了 requirements-lock.txt 里没有的第三方包：")
        for item in missing:
            print(f"   - {item}")
        print("   新增测试依赖请只改 requirements-lock.txt（测试/运行同一份），"
              "再执行 pip install -r requirements-lock.txt。")
        return 1

    print(f"✅ 测试第三方 import 与 requirements-lock.txt 一致（扫描 {len(imports)} 个顶层模块）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
