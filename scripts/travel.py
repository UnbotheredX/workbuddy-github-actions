#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
travel.py — Buddy旅行（派猫猫）状态机（GitHub Actions 版）

接口（host：www.workbuddy.cn）：
  GET  /activity/growth/buddy/travel/status
  POST /activity/growth/buddy/travel/depart   body {"location_id": 1}
  POST /activity/growth/buddy/travel/claim    body {}
  认证：Authorization: Bearer <token> + X-User-Id: <uid> + UA: WorkBuddy/5.3.14

状态机（幂等，先领后派）：
  arrived   → claim（领取奖励）
  idle      → daily_limit_reached=false → depart；=true → 跳过
  traveling → 跳过（不重复派遣）

⚠️ 旅行接口属成长中心活动接口，活动改版/下线时会失效（返回 failed + code/msg），属预期行为。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import credentials  # noqa: E402
import http_client  # noqa: E402

TRAVEL_HOST = "https://www.workbuddy.cn"
STATUS_PATH = "/activity/growth/buddy/travel/status"
DEPART_PATH = "/activity/growth/buddy/travel/depart"
CLAIM_PATH = "/activity/growth/buddy/travel/claim"

LOCATION_ID = 1  # 咖啡馆
TRAVEL_UA = "WorkBuddy/5.3.14"

STATE_IDLE = "idle"
STATE_TRAVELING = "traveling"
STATE_ARRIVED = "arrived"


def _result(status: str, **fields) -> dict:
    out = {"task": "travel", "status": status}
    out.update(fields)
    return out


def run_travel(timeout: int = http_client.DEFAULT_TIMEOUT) -> dict:
    try:
        cred = credentials.load_credentials()
    except credentials.CredentialError as e:
        return _result("failed", reason="登录态读取失败：" + str(e))
    token = cred["access_token"]
    uid = cred.get("uid", "")
    headers = {"X-User-Id": uid}

    try:
        st_code, st_body = http_client.get_json(
            TRAVEL_HOST + STATUS_PATH, token=token,
            extra_headers=headers, timeout=timeout, user_agent=TRAVEL_UA,
        )
    except http_client.HttpTransportError as e:
        return _result("failed", reason="查询旅行状态失败（网络异常）：" + str(e))

    if st_code == 401:
        return _result("failed", reason="令牌已失效（401），请重新导出并更新 Secret")
    if st_body.get("code") != 0:
        return _result(
            "failed",
            reason="状态接口业务错误：code={} msg={}".format(
                st_body.get("code"), st_body.get("msg")
            ),
        )

    d = st_body.get("data") or {}
    state = d.get("state")
    daily_limit_reached = bool(d.get("daily_limit_reached"))
    arrive_at = d.get("arrive_at")

    if state == STATE_ARRIVED:
        try:
            c_code, c_body = http_client.post_json(
                TRAVEL_HOST + CLAIM_PATH, body={}, token=token,
                extra_headers=headers, timeout=timeout, user_agent=TRAVEL_UA,
            )
        except http_client.HttpTransportError as e:
            return _result("failed", reason="claim 失败（网络异常）：" + str(e))
        if c_code == 401:
            return _result("failed", reason="claim 返回 401（令牌失效），停止本轮")
        if c_body.get("code") != 0:
            return _result(
                "failed",
                reason="claim 业务错误：code={} msg={}".format(
                    c_body.get("code"), c_body.get("msg")
                ),
            )
        cd = c_body.get("data") or {}
        return _result(
            "claimed",
            reward_credit=cd.get("reward_credit"),
            record_id=cd.get("record_id"),
        )

    if state == STATE_IDLE:
        if daily_limit_reached:
            return _result("daily_limit_reached", message="今日旅行次数已完成")
        try:
            d_code, d_body = http_client.post_json(
                TRAVEL_HOST + DEPART_PATH, body={"location_id": LOCATION_ID},
                token=token, extra_headers=headers,
                timeout=timeout, user_agent=TRAVEL_UA,
            )
        except http_client.HttpTransportError as e:
            return _result("failed", reason="depart 失败（网络异常）：" + str(e))
        if d_code == 401:
            return _result("failed", reason="depart 返回 401（令牌失效），停止本轮")
        if d_body.get("code") != 0:
            return _result(
                "failed",
                reason="depart 业务错误：code={} msg={}".format(
                    d_body.get("code"), d_body.get("msg")
                ),
            )
        dd = d_body.get("data") or {}
        loc = dd.get("location") or {}
        return _result(
            "departed",
            record_id=dd.get("record_id"),
            location=loc.get("name"),
            arrive_at=dd.get("arrive_at"),
        )

    if state == STATE_TRAVELING:
        return _result("traveling", arrive_at=arrive_at)

    return _result("failed", reason="未知旅行状态：state={}".format(state))


if __name__ == "__main__":
    print(json.dumps(run_travel(), ensure_ascii=False))
