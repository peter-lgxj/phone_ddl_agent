"""通过 GitHub REST 创建/更新 PR，强制请求体按 UTF-8 发送。

背景：PowerShell 5 的 ``Invoke-RestMethod`` 会用本地 ANSI 代码页序列化
JSON 字符串，中文标题/正文偶尔变成 ``?``（``fix: ???? UI ...``）。本脚本在
Python 里构造 ``json.dumps(..., ensure_ascii=False).encode("utf-8")``，并显式
声明 ``Content-Type: application/json; charset=utf-8``，因此不依赖 PowerShell
的默认编码；PowerShell 5 与 PowerShell 7 下行为一致。

token 从 ``git credential fill`` 读取，只放在 Authorization 头里，不打印明文。

用法::

    # 创建 PR（正文建议放 UTF-8 文件，避免 shell 引号/换行问题）
    python scripts/create_pr.py --head fix/my-branch --title "fix: 中文标题" \
        --body-file pr_body.md

    # 更新已有 PR 的标题/正文
    python scripts/create_pr.py --update 19 --title "fix: 中文标题" \
        --body-file pr_body.md

创建/更新后会立即 GET 回读，逐字比较标题、正文（换行统一为 LF 后比较）；
不一致时非零退出。仓库默认从 ``git remote get-url origin`` 推断。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_ROOT = "https://api.github.com"
CONTENT_TYPE = "application/json; charset=utf-8"


def build_payload(fields: dict) -> bytes:
    """把字段序列化成 UTF-8 JSON 字节，中文保持原样（不转义成 \\uXXXX）。"""
    return json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def normalize_body(text: str | None) -> str:
    """GitHub 可能把 CRLF 归一成 LF，比较正文时先统一换行并去掉尾部空行。"""
    return (text or "").replace("\r\n", "\n").rstrip("\n")


def compare_fields(expected_title: str, expected_body: str,
                   actual: dict) -> list[str]:
    """返回读回字段与输入不一致的说明；空列表表示逐字一致。"""
    problems: list[str] = []
    if actual.get("title") != expected_title:
        problems.append(
            f"标题不一致：期望 {expected_title!r}，读回 {actual.get('title')!r}"
        )
    if normalize_body(actual.get("body")) != normalize_body(expected_body):
        problems.append(
            "正文不一致："
            f"期望 {normalize_body(expected_body)!r}，读回 {normalize_body(actual.get('body'))!r}"
        )
    return problems


def _run_git(args: list[str], *, input_text: str | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        input=input_text,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"git {' '.join(args)} 失败：{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout


def git_token(host: str = "github.com") -> str:
    """从 git credential fill 取 token；只返回，不打印。"""
    output = _run_git(
        ["credential", "fill"],
        input_text=f"protocol=https\nhost={host}\n\n",
    )
    values = dict(
        line.split("=", 1) for line in output.splitlines() if "=" in line
    )
    token = values.get("password", "")
    if not token:
        raise SystemExit(
            "未从 git credential fill 取到 password；请先配置 GitHub 凭据"
            "（Windows 可用 Git Credential Manager）。"
        )
    return token


def default_repo() -> str:
    """从 origin URL 推断 owner/repo。"""
    url = _run_git(["remote", "get-url", "origin"]).strip()
    match = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$", url)
    if not match:
        raise SystemExit(f"无法从 origin 推断仓库：{url}；请显式传 --repo owner/repo")
    return f"{match.group('owner')}/{match.group('repo')}"


def request_json(method: str, url: str, token: str, payload: bytes | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "phone-ddl-agent-create-pr",
    }
    if payload is not None:
        headers["Content-Type"] = CONTENT_TYPE
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"GitHub API {method} {url} 失败：{exc.code} {detail}") from exc


def read_text(path: str | None, inline: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    return inline or ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通过 REST 创建/更新 GitHub PR（UTF-8 安全）")
    parser.add_argument("--repo", default=None, help="owner/repo；默认从 origin 推断")
    parser.add_argument("--head", default=None, help="源分支（创建时必填）")
    parser.add_argument("--base", default="main", help="目标分支，默认 main")
    parser.add_argument("--title", required=True, help="PR 标题（中文按 UTF-8 发送）")
    body_group = parser.add_mutually_exclusive_group()
    body_group.add_argument("--body", default=None, help="PR 正文（短文本）")
    body_group.add_argument("--body-file", default=None, help="UTF-8 正文文件（推荐）")
    parser.add_argument("--update", type=int, default=None, help="更新已有 PR 编号，而不是新建")
    parser.add_argument("--draft", action="store_true", help="创建为 draft PR")
    args = parser.parse_args(argv)

    repo = args.repo or default_repo()
    body = read_text(args.body_file, args.body)
    token = git_token()

    if args.update is not None:
        url = f"{API_ROOT}/repos/{repo}/pulls/{args.update}"
        pr = request_json("PATCH", url, token, build_payload({"title": args.title, "body": body}))
    else:
        if not args.head:
            parser.error("创建 PR 需要 --head（更新已有 PR 用 --update）")
        url = f"{API_ROOT}/repos/{repo}/pulls"
        fields = {
            "title": args.title,
            "head": args.head,
            "base": args.base,
            "body": body,
        }
        if args.draft:
            fields["draft"] = True
        pr = request_json("POST", url, token, build_payload(fields))

    # 真闭环：立即回读，逐字比较标题/正文，而不是只相信 POST 的返回值。
    readback = request_json(
        "GET", f"{API_ROOT}/repos/{repo}/pulls/{pr['number']}", token
    )
    problems = compare_fields(args.title, body, readback)
    print(f"PR #{readback['number']} {readback['html_url']}")
    print(f"读回标题：{readback.get('title')}")
    if problems:
        for problem in problems:
            print(f"❌ {problem}", file=sys.stderr)
        return 1
    print("中文编码校验：通过（标题/正文与输入逐字一致）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
