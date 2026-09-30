#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_token.py — 本机导出 WorkBuddy 明文登录态（供 GitHub Secret 使用）

必须在 Windows 本机、WorkBuddy 桌面端「正在运行且已登录」的状态下执行。

为什么必须在客户端运行时执行：
  WorkBuddy 5.6.0+ 把本地 accessToken 存成 $wbEncrypted 加密信封，
  解密密钥（atRestSecretKey）只存在于运行中的客户端进程内存里，不落盘。
  关掉客户端就只能拿到密文，解不开。

用法：
  python tools/export_token.py                    # 打印明文 JSON（复制粘贴到 GitHub Secret）
  python tools/export_token.py --out secrets.json # 同时写一份到文件（注意别提交！）

输出格式（单行 JSON，整份复制）：
  {"auth":{"accessToken":"eyJhbG...","domain":"www.codebuddy.cn"},"account":{"uid":"..."}}

安全提示：
  - 输出内容等同账号密码，不要截图、不要贴到聊天工具、不要 commit
  - 本脚本只读取登录态，不修改 WorkBuddy 任何文件，不发任何网络请求
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))


def _read_raw_desktop_info() -> tuple[str, dict] | tuple[None, None]:
    """读取本机 workbuddy-desktop.info 原文。"""
    rel = os.path.join(
        "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"
    )
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        path = os.path.join(home, "Library", "Application Support", rel)
    elif sys.platform == "win32":
        path = os.path.join(os.environ.get("APPDATA", ""), rel)
    else:
        path = os.path.join(
            os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config")), rel
        )

    if not os.path.isfile(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as f:
            return path, json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print("[FAIL] 无法读取登录态文件：{}：{}".format(path, e))
        return None, None


def _decrypt_via_runtime(envelope: dict) -> str | None:
    """尝试通过 WorkBuddy 客户端本地运行时解密 $wbEncrypted 信封。

    这是可选的增强路径：如果本机有 WorkBuddy 的 Node/Electron 运行时，
    可以直接调用它解密；否则提示用户用官方客户端能力或明文回退。
    """
    try:
        import wb_runtime
    except ImportError:
        return None
    try:
        return wb_runtime.decrypt_token(envelope)
    except Exception as e:
        print("[WARN] 本地运行时解密失败：{}".format(e))
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="导出 WorkBuddy 明文登录态，供 GitHub Actions Secret 使用"
    )
    parser.add_argument("--out", help="可选：同时把明文 JSON 写入该文件（勿提交到仓库！）")
    args = parser.parse_args()

    path, data = _read_raw_desktop_info()
    if not data:
        print("[FAIL] 未找到 WorkBuddy 登录态文件。")
        print("       请确认：① 已安装 WorkBuddy 桌面端；② 桌面端正在运行且已登录。")
        return 1

    auth = data.get("auth") or {}
    account = data.get("account") or {}
    token = auth.get("accessToken")

    if isinstance(token, str) and token:
        print("[OK] 检测到明文 accessToken（无需解密）")
    elif isinstance(token, dict) and token.get("$wbEncrypted"):
        print("[INFO] 检测到 $wbEncrypted 加密信封，尝试通过客户端本地运行时解密…")
        plain = _decrypt_via_runtime(token)
        if not plain:
            print("[FAIL] 无法解密。请确认 WorkBuddy 桌面端正在运行且已登录，然后重试。")
            print("       若持续失败，请升级 WorkBuddy 客户端后重启再试。")
            return 1
        token = plain
        print("[OK] 解密成功")
    else:
        print("[FAIL] 登录态中未找到 accessToken 字段。请先在桌面端完成登录。")
        return 1

    export = {
        "auth": {
            "accessToken": token,
            "domain": auth.get("domain") or "www.codebuddy.cn",
        },
        "account": {"uid": account.get("uid") or auth.get("uid") or ""},
    }
    payload = json.dumps(export, ensure_ascii=False, separators=(",", ":"))

    # 本地校验：确认导出的 JSON 能被读取模块正确解析
    os.environ["WORKBUDDY_AUTH_JSON"] = payload
    try:
        import credentials
        cred = credentials.load_credentials()
        ttl = credentials.token_ttl_days(cred["access_token"])
    except Exception as e:
        print("[FAIL] 导出结果自检失败：{}".format(e))
        return 1

    print("")
    print("=" * 68)
    print("以下内容是你的登录凭据，等同账号密码。请整份复制到 GitHub Secret：")
    print("  Settings -> Secrets and variables -> Actions -> New repository secret")
    print("  Name:  WORKBUDDY_AUTH_JSON")
    print("  Value: 粘贴下面这一整行")
    print("=" * 68)
    print(payload)
    print("=" * 68)
    if ttl is not None:
        print("剩余有效期：{} 天（低于 7 天 Actions 日志会给出警告）".format(ttl))
    print("请勿截图、勿贴聊天工具、勿提交到仓库。")

    if args.out:
        out_path = os.path.abspath(args.out)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(payload + "\n")
        print("[OK] 已写入：{}（请确保它不会被 git 提交）".format(out_path))

    return 0


if __name__ == "__main__":
    sys.exit(main())
