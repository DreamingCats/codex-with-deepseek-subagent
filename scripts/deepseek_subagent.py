#!/usr/bin/env python3
"""Independent DeepSeek web research. Standard library only; no automatic retries."""

import argparse
import json
from pathlib import Path
import socket
import sys
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


SKILL_DIR = Path(__file__).resolve().parent.parent
ENDPOINT = "https://api.deepseek.com/v1/responses"
MODEL = "deepseek-v4-pro"


class ResearchError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class NoRedirects(HTTPRedirectHandler):
    """Do not forward API authorization to a redirected destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_api_key(skill_dir=None):
    env_path = (skill_dir or SKILL_DIR) / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        raise ResearchError("missing_key", "请在 Skill 目录的 .env 中配置 DEEPSEEK_API_KEY。") from None
    except (OSError, UnicodeError):
        raise ResearchError("config_error", "无法读取 Skill 目录的 UTF-8 .env 文件。") from None
    key = ""
    for line in lines:
        name, separator, value = line.strip().partition("=")
        if separator and name.strip() == "DEEPSEEK_API_KEY":
            key = value.strip()
            if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
                key = key[1:-1].strip()
    if not key or key == "your_api_key":
        raise ResearchError("missing_key", "请在 Skill 目录的 .env 中配置 DEEPSEEK_API_KEY，勿在聊天中发送 Key。")
    if not key.isascii() or any(char.isspace() for char in key):
        raise ResearchError("config_error", "DEEPSEEK_API_KEY 格式错误，请检查本地 .env。")
    return key


def request_research(question, api_key, timeout=180):
    payload = {
        "model": MODEL,
        "reasoning": {"effort": "low"},
        "tools": [{"type": "web_search"}],
        "input": question + (
            "\n\n请在回答末尾逐条列出本次实际参考的全部来源完整 URL（去重），"
            "区分已打开阅读与仅看搜索摘要；无法取得 URL 时注明，禁止编造。"
            "请保持正文简洁，为完整来源列表预留篇幅。"
        ),
        "max_output_tokens": 2048,
        "stream": False,
    }
    request = Request(
        ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with build_opener(NoRedirects()).open(request, timeout=timeout) as response:
            data = json.load(response)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        messages = {
            401: ("authentication_error", "DeepSeek 认证失败（HTTP 401），请检查本地 API Key。"),
            403: ("authentication_error", "DeepSeek 拒绝访问（HTTP 403），请检查 Key 和账户权限。"),
            402: ("insufficient_balance", "DeepSeek 账户余额不足（HTTP 402）。"),
            429: ("rate_limit", "DeepSeek 请求被限流（HTTP 429），本次未重试。"),
        }
        code, message = messages.get(status, ("http_error", f"DeepSeek HTTP 错误 {status}，本次未重试。"))
        raise ResearchError(code, message) from None
    except (TimeoutError, socket.timeout):
        raise ResearchError("timeout", "DeepSeek 请求超时；结果未知，本次未重试。") from None
    except (URLError, OSError):
        raise ResearchError("network_error", "无法连接 DeepSeek，或连接中断；本次未重试。") from None
    except (ValueError, UnicodeError):
        raise ResearchError("invalid_response", "DeepSeek 未返回有效 JSON。") from None
    if not isinstance(data, dict):
        raise ResearchError("invalid_response", "DeepSeek 返回的 JSON 不是响应对象。")
    return data


def parse_response(data):
    if data.get("error"):
        raise ResearchError("api_error", "DeepSeek 返回 API 错误，未获得完整研究结果。")
    output = data.get("output", [])
    if not isinstance(output, list):
        raise ResearchError("invalid_response", "DeepSeek 响应缺少有效 output 列表。")
    text_parts = []
    preserved = []
    search_completed = False
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            preserved.append(item)
            search_completed |= item.get("status") == "completed"
        elif item.get("type") == "message":
            preserved.append(item)
            for part in item.get("content", []):
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
                elif part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
                    text_parts.append(part["refusal"])
    usage = data.get("usage") or {}
    tool_use = usage.get("server_tool_use") or {}
    searched = search_completed or tool_use.get("tool_search", 0) > 0
    answer = "\n\n".join(text_parts)
    if not answer and data.get("status") == "completed":
        raise ResearchError("empty_response", "DeepSeek 已结束但未返回回答正文。")
    warnings = []
    if not searched:
        warnings.append("未获得服务端完成联网搜索的证据；不得将该回答描述为已联网核实。")
    if data.get("status") != "completed":
        warnings.append("DeepSeek 响应未完成；answer 可能只是部分结果。")
    return {
        "provider": "deepseek",
        "model": data.get("model", MODEL),
        "response_id": data.get("id"),
        "status": data.get("status", "unknown"),
        "answer": answer,
        "output": preserved,
        "web_search_used": searched,
        "usage": usage,
        "incomplete_details": data.get("incomplete_details"),
        "warnings": warnings,
    }


def emit(value, stream, api_key=""):
    text = json.dumps(value, ensure_ascii=False, indent=2)
    if api_key:
        text = text.replace(api_key, "[REDACTED]")
    print(text, file=stream)


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--question", help="原始问题；也可以通过 UTF-8 stdin 传入")
    group.add_argument("--question-file", type=Path, help="UTF-8 原始问题文件")
    group.add_argument("--check-config", action="store_true", help="只检查 .env，不联网")
    args = parser.parse_args(argv)
    api_key = ""
    try:
        if args.check_config:
            load_api_key()
            emit({"configured": True, "network_called": False}, sys.stdout)
            return 0
        if args.question_file:
            try:
                question = args.question_file.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                raise ResearchError("input_error", "无法读取 UTF-8 问题文件。") from None
        elif args.question is not None:
            question = args.question
        else:
            question = "" if sys.stdin.isatty() else sys.stdin.read()
        if not question.strip():
            raise ResearchError("input_error", "请通过 stdin、--question 或 --question-file 提供原始问题。")
        api_key = load_api_key()
        result = parse_response(request_research(question, api_key))
        emit(result, sys.stdout, api_key)
        return 0 if result["status"] == "completed" else 1
    except ResearchError as exc:
        emit({"error": {"code": exc.code, "message": str(exc)}}, sys.stderr, api_key)
        return 1


if __name__ == "__main__":
    sys.exit(main())
