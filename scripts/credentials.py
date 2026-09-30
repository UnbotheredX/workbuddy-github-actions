#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
credentials.py — WorkBuddy 签到（GitHub Actions 版）· 统一登录态读取

与本地版（workbuddy-reward-helper）的唯一区别：
  云端没有 WorkBuddy 桌面端进程，拿不到 atRestSecretKey，无法解密 $wbEncrypted，
  因此优先从环境变量读取本机已导出的明文 JWT。

读取优先级：
  1. 环境变量 WORKBUDDY_AUTH_JSON  —— 完整登录态 JSON（GitHub Secret 注入，推荐）
  2. 环境变量 WORKBUDDY_ACCESS_TOKEN + WORKBUDDY_UID —— 只拆开存 token/uid（备选）
  3. 本地明文登录态文件 workbuddy-desktop.info —— 本机调试时可用
  4. 旧版 state.vscdb（Electron safeStorage）—— 本机调试时可用

统一返回结构：
  { "access_token": "...", "uid": "...", "source": "..." }

安全规则：
  - access_token 等同账号密码：仅在内存中使用，禁止打印 / 写日志 / 落盘
  - GitHub Actions 会尽力遮蔽 Secret 输出，但仍不得主动打印 token
  - 本模块不做任何网络请求
"""

from __future__ import annotations

import json
import os
import sys

ENV_AUTH_JSON = "WORKBUDDY_AUTH_JSON"
ENV_ACCESS_TOKEN = "WORKBUDDY_ACCESS_TOKEN"
ENV_UID = "WORKBUDDY_UID"

SOURCE_ENV_JSON = "env:WORKBUDDY_AUTH_JSON"
SOURCE_ENV_TOKEN = "env:WORKBUDDY_ACCESS_TOKEN"
SOURCE_DESKTOP_INFO = "workbuddy-desktop.info"


class CredentialError(RuntimeError):
    """登录态读取失败的统一异常。"""


def _from_env_json() -> dict | None:
    """从 WORKBUDDY_AUTH_JSON 解析完整登录态。

    接受两种形态：
      A. {"auth":{"accessToken":"..."},"account":{"uid":"..."}}   ← make_min_auth.py 导出格式
      B. {"accessToken":"...","uid":"..."}                        ← 手写简化格式
    """
    raw = os.environ.get(ENV_AUTH_JSON, "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CredentialError(
            "环境变量 {} 不是合法 JSON：{}。"
            "请确认粘贴时未被截断（GitHub Secret 上限 48KB，正常 JWT 远小于此）。".format(
                ENV_AUTH_JSON, e
            )
        )
    if not isinstance(data, dict):
        raise CredentialError("环境变量 {} 解析结果不是对象。".format(ENV_AUTH_JSON))

    auth = data.get("auth") if isinstance(data.get("auth"), dict) else {}
    account = data.get("account") if isinstance(data.get("account"), dict) else {}

    token = auth.get("accessToken") or data.get("accessToken") or data.get("access_token")
    uid = (
        account.get("uid")
        or data.get("uid")
        or auth.get("uid")
        or ""
    )

    if isinstance(token, dict):
        raise CredentialError(
            "检测到加密态 accessToken（$wbEncrypted）。云端无法解密——请在本机 Windows 上"
            "运行 tools/export_token.py 重新导出明文 token 后再更新 Secret。"
        )
    if not (isinstance(token, str) and token):
        raise CredentialError(
            "环境变量 {} 中未找到 accessToken 字段。".format(ENV_AUTH_JSON)
        )
    return {"access_token": token, "uid": str(uid), "source": SOURCE_ENV_JSON}


def _from_env_token() -> dict | None:
    """从拆分的两个环境变量读取（备选方案）。"""
    token = os.environ.get(ENV_ACCESS_TOKEN, "").strip()
    if not token:
        return None
    return {
        "access_token": token,
        "uid": os.environ.get(ENV_UID, "").strip(),
        "source": SOURCE_ENV_TOKEN,
    }


def _desktop_info_candidates() -> list[str]:
    rel = os.path.join(
        "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"
    )
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return [os.path.join(home, "Library", "Application Support", rel)]
    if sys.platform == "win32":
        return [os.path.join(os.environ.get("APPDATA", ""), rel)]
    return [os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config")), rel)]


def _from_local_file() -> dict | None:
    """本机调试用：读取明文登录态文件（仅当环境变量未提供时）。"""
    for path in _desktop_info_candidates():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        auth = data.get("auth") or {}
        account = data.get("account") or {}
        token = auth.get("accessToken")
        if isinstance(token, str) and token:
            return {
                "access_token": token,
                "uid": account.get("uid") or auth.get("uid") or "",
                "source": SOURCE_DESKTOP_INFO,
            }
        if isinstance(token, dict) and token.get("$wbEncrypted"):
            raise CredentialError(
                "本机登录态为 $wbEncrypted 加密态，云端无法解密。"
                "请在本机运行 tools/export_token.py 导出明文后再配置 Secret。"
            )
    return None


def load_credentials() -> dict:
    """按优先级读取登录态，失败抛 CredentialError。"""
    cred = _from_env_json()
    if cred:
        return cred

    cred = _from_env_token()
    if cred:
        return cred

    cred = _from_local_file()
    if cred:
        return cred

    raise CredentialError(
        "未找到可用登录态。云端运行请配置 Repository Secret：{}。"
        "（本机运行请先安装并登录 WorkBuddy 桌面端，再用 tools/export_token.py 导出）".format(
            ENV_AUTH_JSON
        )
    )


def token_ttl_days(token: str) -> int | None:
    """解析 JWT 的 exp（不校验签名，仅读载荷），返回剩余整天数。

    JWT 结构：header.payload.signature，payload 是 base64url 编码的 JSON。
    解析失败返回 None（不抛异常，避免影响主流程）。
    """
    import base64
    import time

    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # 补齐 base64 padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return None
        remain = exp - int(time.time())
        if remain <= 0:
            return 0
        return remain // 86400
    except Exception:
        return None


def mask_secret(secret: str) -> str:
    """高敏感串：不展示任何字符，仅说明长度。"""
    if not secret:
        return "<空>"
    return "<已读取（不展示）, 长度 {}>".format(len(secret))


def describe(cred: dict) -> dict:
    return {
        "source": cred.get("source", ""),
        "access_token": mask_secret(cred.get("access_token", "")),
        "uid": "<已读取（不展示）>" if cred.get("uid") else "<空>",
    }


if __name__ == "__main__":
    try:
        c = load_credentials()
    except CredentialError as e:
        print("[FAIL] 读取登录态失败：{}".format(e))
        sys.exit(1)
    d = describe(c)
    print("[OK] 登录态来源：{}".format(d["source"]))
    print("[OK] access_token：{}".format(d["access_token"]))
    print("[OK] uid：{}".format(d["uid"]))
    ttl = token_ttl_days(c["access_token"])
    if ttl is not None:
        print("[OK] 剩余有效期：{} 天".format(ttl))
