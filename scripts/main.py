#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py — WorkBuddy 签到（GitHub Actions 版）· 统一入口

子命令：
  checkin  只执行 Buddy加油站签到
  travel   只执行 Buddy旅行（派猫猫）状态机
  all      先签到、后旅行（默认）
  status   只读查询，不做任何领取/派遣

用法：
  python3 scripts/main.py all
  python3 scripts/main.py checkin

输出：统一 JSON（绝不含 token / uid / 请求头）
退出码：0 = 完成（含 already_checked 等正常幂等态）；1 = 存在 failed
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import checkin  # noqa: E402
import credentials  # noqa: E402
import travel  # noqa: E402

USAGE = """WorkBuddy 签到 · 统一入口
用法：python3 scripts/main.py [checkin|travel|all|status]
  checkin  只执行 Buddy加油站签到
  travel   只执行 Buddy旅行（派猫猫）状态机
  all      先签到、后旅行（默认）
  status   只读查询签到状态 + 旅行状态"""

# 剩余有效期低于此天数时输出告警（GitHub Actions 会渲染为 warning）
TTL_WARN_DAYS = 7


def _checkin_status(cred: dict) -> dict:
    import http_client

    try:
        code, body = http_client.post_json(
            checkin.CHECKIN_HOST + checkin.STATUS_PATH, body={},
            token=cred["access_token"],
        )
    except http_client.HttpTransportError as e:
        return {"query": "checkin_status", "status": "failed", "reason": "网络异常：" + str(e)}
    if code == 401:
        return {"query": "checkin_status", "status": "failed", "reason": "令牌已过期（401）"}
    if body.get("code") != 0:
        return {"query": "checkin_status", "status": "failed",
                "reason": "业务错误：code={} msg={}".format(body.get("code"), body.get("msg"))}
    data = body.get("data") or {}
    return {
        "query": "checkin_status",
        "status": "ok",
        "today_checked_in": data.get("today_checked_in"),
        "streak_days": data.get("streak_days"),
    }


def _travel_status(cred: dict) -> dict:
    import http_client

    try:
        code, body = http_client.get_json(
            travel.TRAVEL_HOST + travel.STATUS_PATH, token=cred["access_token"],
            extra_headers={"X-User-Id": cred.get("uid", "")},
            user_agent=travel.TRAVEL_UA,
        )
    except http_client.HttpTransportError as e:
        return {"query": "travel_status", "status": "failed", "reason": "网络异常：" + str(e)}
    if code == 401:
        return {"query": "travel_status", "status": "failed", "reason": "令牌已过期（401）"}
    if body.get("code") != 0:
        return {"query": "travel_status", "status": "failed",
                "reason": "业务错误：code={} msg={}".format(body.get("code"), body.get("msg"))}
    data = body.get("data") or {}
    return {
        "query": "travel_status",
        "status": "ok",
        "state": data.get("state"),
        "daily_limit_reached": data.get("daily_limit_reached"),
        "arrive_at": data.get("arrive_at"),
    }


def run_status() -> dict:
    try:
        cred = credentials.load_credentials()
    except credentials.CredentialError as e:
        return {"status": "failed", "reason": "登录态读取失败：" + str(e)}
    return {
        "checkin_status": _checkin_status(cred),
        "travel_status": _travel_status(cred),
    }


def _emit_ttl_notice(cred: dict) -> None:
    """打印登录态来源与剩余有效期（不含 token）。

    GitHub Actions 识别 ::warning:: 与 ::notice:: 前缀并渲染成注释。
    """
    print("登录态来源：{}".format(cred.get("source", "unknown")))
    ttl = credentials.token_ttl_days(cred.get("access_token", ""))
    if ttl is None:
        print("登录态剩余有效期：无法解析（token 非标准 JWT，不影响签到）")
        return
    if ttl <= TTL_WARN_DAYS:
        print(
            "::warning::登录态剩余有效期仅 {} 天，请尽快在本机重新导出并更新 GitHub Secret "
            "WORKBUDDY_AUTH_JSON".format(ttl)
        )
    print("登录态剩余有效期：{} 天".format(ttl))


def run_task(task: str) -> tuple[dict, bool]:
    """执行任务，返回 (结果, 是否存在 failed)。"""
    if task == "status":
        result = run_status()
        has_failure = result.get("status") == "failed" or any(
            v.get("status") == "failed" for v in result.values() if isinstance(v, dict)
        )
        return result, has_failure

    try:
        cred = credentials.load_credentials()
    except credentials.CredentialError as e:
        return {"status": "failed", "reason": "登录态读取失败：" + str(e)}, True

    _emit_ttl_notice(cred)

    if task == "checkin":
        r = checkin.run_checkin()
        return r, r.get("status") == "failed"
    if task == "travel":
        r = travel.run_travel()
        return r, r.get("status") == "failed"

    # all
    c = checkin.run_checkin()
    print("--- 签到 ---")
    print(json.dumps(c, ensure_ascii=False))
    t = travel.run_travel()
    print("--- 旅行 ---")
    print(json.dumps(t, ensure_ascii=False))
    has_failure = c.get("status") == "failed" or t.get("status") == "failed"
    return {"checkin": c, "travel": t}, has_failure


def main(argv: list[str]) -> int:
    task = argv[1] if len(argv) > 1 else "all"
    if task in ("help", "-h", "--help"):
        print(USAGE)
        return 0
    if task not in ("checkin", "travel", "all", "status"):
        print(json.dumps(
            {"task": task, "status": "failed",
             "reason": "未知子命令（可选：checkin/travel/all/status）"}, ensure_ascii=False))
        return 1

    started = time.time()
    result, has_failure = run_task(task)
    print("--- 结果 JSON ---")
    print(json.dumps(result, ensure_ascii=False))
    print("耗时 {:.1f}s".format(time.time() - started))
    return 1 if has_failure else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
