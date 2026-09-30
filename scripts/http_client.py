#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
http_client.py — WorkBuddy 签到（GitHub Actions 版）· 统一 HTTP 客户端

基于标准库 urllib，零第三方依赖。
返回 (http_status, parsed_json)；传输层失败抛 HttpTransportError。
token 仅在内存中作为请求头传递，绝不打印/写日志/落盘。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 15
DEFAULT_UA = "WorkBuddy/5.3.14"


class HttpTransportError(RuntimeError):
    """网络传输层失败（未收到 HTTP 响应：DNS/连接/超时/重置等）。"""


def _parse_json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except Exception:
        return {"__non_json__": True}


def request_json(
    method: str,
    url: str,
    body: dict | None = None,
    token: str | None = None,
    extra_headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_UA,
) -> tuple[int, dict]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": user_agent,
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    if extra_headers:
        headers.update(extra_headers)

    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, _parse_json(raw)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
            return e.code, _parse_json(raw)
        except Exception:
            return e.code, {}
    except Exception as e:
        raise HttpTransportError(str(e))


def post_json(url: str, body: dict | None = None, **kwargs) -> tuple[int, dict]:
    return request_json("POST", url, body=body, **kwargs)


def get_json(url: str, **kwargs) -> tuple[int, dict]:
    return request_json("GET", url, body=None, **kwargs)
