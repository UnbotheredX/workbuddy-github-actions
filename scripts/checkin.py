#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
checkin.py — Buddy加油站每日签到（GitHub Actions 版）

接口（host：copilot.tencent.com）：
  POST /billing/meter/checkin-status   查签到状态（body {}）
  POST /billing/meter/daily-checkin    执行签到（body {}）
  认证：Authorization: Bearer <accessToken>

幂等规则：
  - today_checked_in 实测不可靠（签到成功后仍可能为 false），仅作快速短路
  - code=10001 表示「今日已签到」，视为 already_checked，不是失败

返回结构：
  成功：  {"task":"checkin","status":"success","credit":100,"streak_days":5}
  已签到：{"task":"checkin","status":"already_checked","message":"今日已签到"}
  失败：  {"task":"checkin","status":"failed","reason":"..."}
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import credentials  # noqa: E402
import http_client  # noqa: E402

CHECKIN_HOST = "https://copilot.tencent.com"
STATUS_PATH = "/billing/meter/checkin-status"
CHECKIN_PATH = "/billing/meter/daily-checkin"

CODE_SUCCESS = 0
CODE_ALREADY_CHECKED = 10001


def _result(status: str, **fields) -> dict:
    out = {"task": "checkin", "status": status}
    out.update(fields)
    return out


def run_checkin(timeout: int = http_client.DEFAULT_TIMEOUT) -> dict:
    try:
        cred = credentials.load_credentials()
    except credentials.CredentialError as e:
        return _result("failed", reason="登录态读取失败：" + str(e))
    token = cred["access_token"]

    # 1. 查状态（不可靠，仅快速短路 + 401 探测）
    try:
        st_code, st_body = http_client.post_json(
            CHECKIN_HOST + STATUS_PATH, body={}, token=token, timeout=timeout
        )
    except http_client.HttpTransportError as e:
        return _result("failed", reason="查询签到状态失败（网络异常）：" + str(e))

    if st_code == 401:
        return _result("failed", reason="令牌已过期（401），请重新导出并更新 Secret")

    if (st_body.get("data") or {}).get("today_checked_in") is True:
        return _result("already_checked", message="今日已签到")

    # 2. 执行签到
    try:
        c_code, c_body = http_client.post_json(
            CHECKIN_HOST + CHECKIN_PATH, body={}, token=token, timeout=timeout
        )
    except http_client.HttpTransportError as e:
        return _result("failed", reason="签到请求失败（网络异常）：" + str(e))

    if c_code == 401:
        return _result("failed", reason="令牌已过期（401），请重新导出并更新 Secret")

    code = c_body.get("code")
    data = c_body.get("data") or {}

    if code == CODE_SUCCESS:
        return _result(
            "success", credit=data.get("credit"), streak_days=data.get("streak_days")
        )
    if code == CODE_ALREADY_CHECKED:
        return _result("already_checked", message="今日已签到")

    return _result(
        "failed", reason="签到未成功：code={} msg={}".format(code, c_body.get("msg"))
    )


if __name__ == "__main__":
    print(json.dumps(run_checkin(), ensure_ascii=False))
