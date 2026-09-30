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
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))


def _candidate_paths() -> list[str]:
    """登录态文件候选路径。

    Windows 注意：实测该文件位于 %LOCALAPPDATA%，而非 %APPDATA%（Roaming）。
    两者都试，Local 优先。
    """
    rel = os.path.join(
        "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"
    )
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return [os.path.join(home, "Library", "Application Support", rel)]
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        roaming = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        return [os.path.join(local, rel), os.path.join(roaming, rel)]
    return [os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config")), rel
    )]


def _read_raw_desktop_info() -> tuple[str, dict] | tuple[None, None]:
    """读取本机 workbuddy-desktop.info 原文，返回 (路径, 解析结果)。"""
    tried = []
    for path in _candidate_paths():
        tried.append(path)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                return path, json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print("[WARN] 读取失败，尝试下一个候选：{}：{}".format(path, e))
            continue
    print("[INFO] 已检查以下路径，均未命中：")
    for p in tried:
        print("       " + p)
    return None, None


def _find_workbuddy_exe() -> str | None:
    """定位 WorkBuddy.exe。

    优先级：环境变量 WORKBUDDY_EXE > 标准安装路径 > 查询运行中的进程。

    实测有机器装在非标准路径（如 E:\\work\\Workbuddy\\install\\WorkBuddy.exe），
    标准路径发现会失败，因此增加「查询运行中进程」作为兜底。
    """
    env_exe = os.environ.get("WORKBUDDY_EXE", "")
    if env_exe and os.path.isfile(env_exe):
        return env_exe

    home = os.path.expanduser("~")
    roots = [
        os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local"),
        os.environ.get("ProgramFiles", ""),
        os.environ.get("ProgramFiles(x86)", ""),
    ]
    candidates = []
    for root in roots:
        if root:
            candidates.append(os.path.join(root, "Programs", "WorkBuddy", "WorkBuddy.exe"))
            candidates.append(os.path.join(root, "WorkBuddy", "WorkBuddy.exe"))
    for c in candidates:
        if os.path.isfile(c):
            return c

    # 兜底：查运行中的进程（可覆盖自定义安装目录）
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                [
                    "powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "(Get-Process WorkBuddy -ErrorAction SilentlyContinue | "
                    "Where-Object { $_.Path } | Select-Object -First 1 "
                    "-ExpandProperty Path)",
                ],
                capture_output=True, text=True, timeout=25,
            )
            for line in (out.stdout or "").splitlines():
                p = line.strip()
                if p.lower().endswith(".exe") and os.path.isfile(p):
                    return p
        except Exception:
            pass
    return None


def _decrypt_via_runtime(envelope: dict) -> str | None:
    """通过 WorkBuddy 客户端本地运行时解密 $wbEncrypted 信封。

    解密密钥（atRestSecretKey）只存在于运行中的客户端进程内存里，所以这一步
    必须在本机、且客户端正在运行时执行。
    """
    try:
        import wb_runtime
    except ImportError:
        print("[WARN] 未找到 wb_runtime 模块，跳过运行时解密。")
        return None

    exe = _find_workbuddy_exe()
    if not exe:
        print("[WARN] 未定位到 WorkBuddy.exe。")
        print("       请用环境变量显式指定后重试，例如：")
        print('         set WORKBUDDY_EXE=E:\\path\\to\\WorkBuddy.exe')
        return None
    os.environ["WORKBUDDY_EXE"] = exe
    print("[INFO] 使用客户端运行时：{}".format(exe))

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
