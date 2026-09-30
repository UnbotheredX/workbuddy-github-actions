#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selfcheck.py — 离线自检（不联网、不需要真实 token）

用途：部署前/改动后确认脚本能正常导入、解析伪造 token、URL 拼接无误。
运行：python tests/selfcheck.py
"""

from __future__ import annotations

import base64
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

passed = 0
failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print("  [PASS] {}".format(name))
    else:
        failed += 1
        print("  [FAIL] {} {}".format(name, detail))


def make_fake_jwt(days: int = 60) -> str:
    """构造一个只含 exp 的假 JWT（签名段随便填，本流程不校验签名）。"""
    import time

    exp = int(time.time()) + days * 86400
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp, "sub": "fake"}).encode()
    ).decode().rstrip("=")
    return "eyJhbGciOiJIUzI1NiJ9.{}.fakesignature".format(payload)


def main() -> int:
    print("== 模块导入 ==")
    try:
        import checkin
        import credentials
        import http_client
        import travel
        check("导入 checkin/credentials/http_client/travel", True)
    except Exception as e:
        check("导入业务模块", False, str(e))
        return 1

    print("== 登录态解析 ==")
    os.environ.pop("WORKBUDDY_AUTH_JSON", None)
    os.environ.pop("WORKBUDDY_ACCESS_TOKEN", None)

    fake = make_fake_jwt(60)
    os.environ["WORKBUDDY_AUTH_JSON"] = json.dumps({
        "auth": {"accessToken": fake, "domain": "www.codebuddy.cn"},
        "account": {"uid": "u-12345"},
    })
    cred = credentials.load_credentials()
    check("从 WORKBUDDY_AUTH_JSON 读取成功", cred["access_token"] == fake)
    check("uid 解析正确", cred["uid"] == "u-12345")
    check("source 标记正确", cred["source"] == "env:WORKBUDDY_AUTH_JSON")

    # 简化格式
    os.environ["WORKBUDDY_AUTH_JSON"] = json.dumps(
        {"accessToken": fake, "uid": "u-9"}
    )
    cred = credentials.load_credentials()
    check("兼容简化格式 {accessToken,uid}", cred["access_token"] == fake and cred["uid"] == "u-9")

    # 加密态应给出明确错误
    os.environ["WORKBUDDY_AUTH_JSON"] = json.dumps({
        "auth": {"accessToken": {"$wbEncrypted": 1, "envelope": "xxx"}},
        "account": {"uid": "u-1"},
    })
    try:
        credentials.load_credentials()
        check("加密态应报错", False, "未抛异常")
    except credentials.CredentialError as e:
        check("加密态给出明确提示", "wbEncrypted" in str(e))

    # 坏 JSON 应给出明确错误
    os.environ["WORKBUDDY_AUTH_JSON"] = "{截断的json"
    try:
        credentials.load_credentials()
        check("坏 JSON 应报错", False, "未抛异常")
    except credentials.CredentialError as e:
        check("坏 JSON 提示截断", "截断" in str(e))

    print("== JWT 有效期解析 ==")
    ttl = credentials.token_ttl_days(make_fake_jwt(60))
    check("60 天 token 解析为 59-60 天", ttl in (59, 60), "得到 {}".format(ttl))
    check("非 JWT 返回 None", credentials.token_ttl_days("not-a-jwt") is None)
    check("已过期 token 返回 0", credentials.token_ttl_days(make_fake_jwt(-1)) == 0)

    print("== 脱敏 ==")
    d = credentials.describe({"access_token": "x" * 500, "uid": "u-1"})
    check("脱敏不泄露 token 本体", "x" * 10 not in json.dumps(d))
    check("脱敏显示长度", "500" in d["access_token"])

    print("== URL 契约 ==")
    check("签到 host 正确", checkin.CHECKIN_HOST == "https://copilot.tencent.com")
    check("签到路径正确", checkin.STATUS_PATH == "/billing/meter/checkin-status")
    check("签到幂等码 = 10001", checkin.CODE_ALREADY_CHECKED == 10001)
    check("旅行 host 正确", travel.TRAVEL_HOST == "https://www.workbuddy.cn")
    check("旅行 UA 正确", travel.TRAVEL_UA == "WorkBuddy/5.3.14")
    check("咖啡馆 location_id = 1", travel.LOCATION_ID == 1)

    print("== 无凭据时的失败路径 ==")
    os.environ.pop("WORKBUDDY_AUTH_JSON", None)
    os.environ.pop("WORKBUDDY_ACCESS_TOKEN", None)
    # 隔离本机登录态文件的影响：临时把候选路径指到不存在的目录
    os.environ["APPDATA"] = os.path.join(ROOT, "_nonexistent_")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(ROOT, "_nonexistent_")
    os.environ["HOME"] = os.path.join(ROOT, "_nonexistent_")
    r = checkin.run_checkin()
    check("无凭据时签到返回 failed 而非崩溃", r["status"] == "failed")
    check("failed 结果含 reason", bool(r.get("reason")))

    print("")
    print("通过 {} 项，失败 {} 项".format(passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
