#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb_runtime.py — WorkBuddy 积分助手（workbuddy-reward-helper）v1.0.2 · $wbEncrypted 运行时解密

迁移来源（仅最小必要实现，保留署名与协议）：
  - 开源项目：https://github.com/88lin/workbuddy-auto-signin
  - 提交：cceadda3fc98172a8d2fb3c26aee116c67f26ea2
  - 协议：MIT（Copyright (c) 2026 88lin）
  本模块只复用其「通过 WorkBuddy 客户端本地运行时（Electron 原生绑定）内存解密
  $wbEncrypted 信封」的最小实现，未改动原项目。

原理（与 88lin/workbuddy-auto-signin 一致，已在 WorkBuddy 5.6.2 / Electron 37.10.3
本机实测通过）：
  - 通过 ELECTRON_RUN_AS_NODE=1 以子进程方式启动 WorkBuddy 桌面端的 Electron 二进制；
  - 在子进程内调用 process._linkedBinding('electron_browser_workbuddy_storage').loggerGet()
    取得 atRestSecretKey（仅存在于客户端运行时内存，不做任何硬编码、不落盘、不上传）；
  - 用 sha256(key) 派生 AES-256-GCM 密钥，按固定 AAD（WB-AAD\\0 + WBEV1 + sym-v1 + keyId）
    解密 accessToken 信封；解密出的明文 token 仅经 stdout 单行回传，仅在父进程内存中使用。

安全红线的硬约束（继承自来源与本 Skill）：
  - atRestSecretKey 不硬编码、不打印、不落盘、不上传；派生密钥与明文用完即清零；
  - 仅支持 sym-v1 / suite 1；密钥不匹配 / 信封不合法 / 运行时缺失均按固定原因失败，
    绝不输出 envelope 内容、token、refreshToken、Authorization 或解密密钥；
  - 本模块不做任何业务网络请求（仅启动本地子进程 + 读取其 stdout）；
  - 任何失败都只抛出固定、非敏感的 AuthError(reason)，reason 取白名单。

跨平台说明：
  - macOS / Windows 运行时自动发现已实现；本机（macOS）已实测通过；
  - Windows 路径走与来源一致的逻辑，但本 Skill 未在 Windows 环境实测，
    对外必须如实标注为「NOT_VERIFIED」，不得声称已验证。
"""

from __future__ import annotations

import base64
import json
import os
import plistlib
import re
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------------------
# 常量（与 88lin/workbuddy-auto-signin 一致）
# ---------------------------------------------------------------------------
DEFAULT_ENDPOINT = "https://copilot.tencent.com"
AUTH_HELPER_TIMEOUT = 10.0
AUTH_INPUT_LIMIT = 65536
AUTH_OUTPUT_LIMIT = 65536
TOKEN_LIMIT = 32768

# 固定、非敏感的失败原因白名单（绝不夹杂任何凭据/token/envelope 内容）
AUTH_REASONS = {
    "INVALID_FORMAT": "登录凭据格式无效，请检查凭据来源和客户端版本",
    "UNSUPPORTED_ENVELOPE": "尚不支持此加密凭据格式，请更新脚本；重新登录不会改变加密格式",
    "RUNTIME_NOT_FOUND": "未找到 WorkBuddy 客户端，请用 WORKBUDDY_EXE 指定其可执行文件",
    "INVALID_RUNTIME_PATH": "WORKBUDDY_EXE 不是可用的可执行文件，请核对路径",
    "RUNTIME_UNAVAILABLE": "客户端运行时不支持所需的原生存储接口，请检查客户端和脚本版本",
    "KEY_MISMATCH": "凭据与所选客户端的密钥不匹配，请用 WORKBUDDY_EXE 指定对应客户端",
    "DECRYPT_FAILED": "加密凭据认证失败，请检查客户端版本及凭据是否完整",
    "HELPER_TIMEOUT": "凭据处理超时，已停止子进程，请稍后重试",
    "HELPER_PROTOCOL": "客户端凭据助手返回无效结果，请检查客户端和脚本版本",
}


class AuthError(Exception):
    """Only fixed, non-sensitive messages may cross the credential boundary."""

    def __init__(self, reason, result="AUTH_ERROR"):
        self.reason = reason
        self.result = result
        super().__init__(AUTH_REASONS.get(reason, "本地未找到有效登录会话，请先登录客户端"))


# ---------------------------------------------------------------------------
# 内嵌 JS 助手（verbatim，来自 88lin/workbuddy-auto-signin；仅 sym-v1/suite 1）
# 通过 stdin 接收请求 JSON；解密材料（envelope）仅在进程内流转，绝不落盘/上传。
# ---------------------------------------------------------------------------
AUTH_HELPER_JS = r"""
'use strict';
const crypto = require('crypto');
const inputLimit = 65536;
const failure = reason => { throw {reason}; };
const object = x => x !== null && typeof x === 'object' && !Array.isArray(x);
function base64(value, length) {
  if (typeof value !== 'string' || value.length > inputLimit) failure('INVALID_FORMAT');
  const bytes = Buffer.from(value, 'base64');
  if (bytes.toString('base64') !== value || (length !== undefined && bytes.length !== length))
    failure('INVALID_FORMAT');
  return bytes;
}
function utf8(bytes) {
  const text = bytes.toString('utf8');
  if (!Buffer.from(text, 'utf8').equals(bytes)) failure('INVALID_FORMAT');
  return text;
}
function decode(value) {
  if (!object(value) || Object.keys(value).sort().join(',') !== '$wbEncrypted,envelope' || value.$wbEncrypted !== 1)
    failure('UNSUPPORTED_ENVELOPE');
  let envelope;
  try { envelope = JSON.parse(utf8(base64(value.envelope))); }
  catch (e) { failure(e.reason || 'INVALID_FORMAT'); }
  if (!object(envelope) || !Number.isInteger(envelope.suite)) failure('INVALID_FORMAT');
  if (envelope.suite !== 1) failure('UNSUPPORTED_ENVELOPE');
  if (Object.keys(envelope).sort().join(',') !== 'authTag,ciphertext,keyId,nonce,suite' ||
      typeof envelope.keyId !== 'string' || !/^[0-9a-f]{16}$/.test(envelope.keyId)) failure('INVALID_FORMAT');
  return {keyId: envelope.keyId, nonce: base64(envelope.nonce, 12),
    tag: base64(envelope.authTag, 16), ciphertext: base64(envelope.ciphertext)};
}
function nativeStorage() {
  try {
    const storage = process._linkedBinding('electron_browser_workbuddy_storage');
    if (typeof storage.loggerGet !== 'function') failure('RUNTIME_UNAVAILABLE');
    return storage;
  } catch (_) { failure('RUNTIME_UNAVAILABLE'); }
}
function decrypt(envelope) {
  let payload;
  try { payload = JSON.parse(nativeStorage().loggerGet()); }
  catch (_) { failure('RUNTIME_UNAVAILABLE'); }
  let key;
  let plaintext;
  try {
    if (!object(payload) || payload.version !== 1) failure('RUNTIME_UNAVAILABLE');
    let secret;
    try { secret = base64(payload.atRestSecretKey, 32); }
    catch (_) { failure('RUNTIME_UNAVAILABLE'); }
    const empty = secret.every(b => b === 0);
    secret.fill(0);
    if (empty) failure('RUNTIME_UNAVAILABLE');
    key = crypto.createHash('sha256').update(payload.atRestSecretKey, 'utf8').digest();
    payload = null;
    if (crypto.createHash('sha256').update(key).digest('hex').slice(0, 16) !== envelope.keyId)
      failure('KEY_MISMATCH');
    const lp = s => {
      const bytes = Buffer.from(s, 'utf8');
      const length = Buffer.alloc(4); length.writeUInt32BE(bytes.length);
      return Buffer.concat([length, bytes]);
    };
    const aad = Buffer.concat([Buffer.from('WB-AAD\0', 'ascii'), Buffer.from([1]),
      lp('WBEV1'), lp('sym-v1'), Buffer.from([0, 0, 0, 1]), lp(envelope.keyId), Buffer.from([2, 0, 0])]);
    try {
      const cipher = crypto.createDecipheriv('aes-256-gcm', key, envelope.nonce, {authTagLength: 16});
      cipher.setAAD(aad); cipher.setAuthTag(envelope.tag);
      plaintext = Buffer.concat([cipher.update(envelope.ciphertext), cipher.final()]);
    } catch (_) { failure('DECRYPT_FAILED'); }
    const token = utf8(plaintext);
    if (!token.length || token.length > 32768 || !/^[A-Za-z0-9._~+\/-]+=*$/.test(token))
      failure('INVALID_FORMAT');
    return token;
  } finally {
    if (key) key.fill(0);
    if (plaintext) plaintext.fill(0);
  }
}
let chunks = [], size = 0;
function reply(value) {
  process.stdout.write(JSON.stringify({version: 1, ...value}), () => process.exit(value.ok ? 0 : 1));
}
process.stdin.on('data', chunk => {
  size += chunk.length;
  if (size > inputLimit) reply({ok: false, reason: 'INVALID_FORMAT'});
  else chunks.push(chunk);
});
process.stdin.on('error', () => reply({ok: false, reason: 'HELPER_PROTOCOL'}));
process.stdin.on('end', () => {
  try {
    const request = JSON.parse(utf8(Buffer.concat(chunks))); chunks = [];
    if (!object(request) || request.version !== 1) failure('HELPER_PROTOCOL');
    if (request.operation === 'probe') {
      nativeStorage();
      if (!crypto.getCiphers().includes('aes-256-gcm')) failure('RUNTIME_UNAVAILABLE');
      reply({ok: true, electron: process.versions.electron || 'unknown'});
    } else if (request.operation === 'decrypt') {
      reply({ok: true, accessToken: decrypt(decode(request.value))});
    } else failure('HELPER_PROTOCOL');
  } catch (e) {
    const reasons = ['INVALID_FORMAT','UNSUPPORTED_ENVELOPE','RUNTIME_UNAVAILABLE',
      'KEY_MISMATCH','DECRYPT_FAILED','HELPER_PROTOCOL'];
    reply({ok: false, reason: reasons.includes(e.reason) ? e.reason : 'HELPER_PROTOCOL'});
  }
});
"""


# ---------------------------------------------------------------------------
# 信封格式校验（纯 Python，不解密）
# ---------------------------------------------------------------------------
def _valid_token(token: str) -> bool:
    return (isinstance(token, str) and 0 < len(token) <= TOKEN_LIMIT
            and re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token) is not None)


def token_format(value) -> str:
    """返回 'plaintext' / 'sym-v1' / 否则抛 AuthError。不输出任何敏感内容。"""
    if isinstance(value, str):
        if not _valid_token(value):
            raise AuthError("INVALID_FORMAT")
        return "plaintext"
    if not isinstance(value, dict):
        raise AuthError("INVALID_FORMAT")
    if (set(value) != {"$wbEncrypted", "envelope"} or
            type(value.get("$wbEncrypted")) is not int or value["$wbEncrypted"] != 1):
        raise AuthError("UNSUPPORTED_ENVELOPE")
    try:
        encoded = value["envelope"]
        if not isinstance(encoded, str) or not 0 < len(encoded) <= AUTH_INPUT_LIMIT - 1024:
            raise ValueError()
        raw = base64.b64decode(encoded, validate=True)
        if base64.b64encode(raw).decode("ascii") != encoded:
            raise ValueError()
        envelope = json.loads(raw.decode("utf-8"))
        if not isinstance(envelope, dict) or type(envelope.get("suite")) is not int:
            raise ValueError()
        if envelope["suite"] != 1:
            raise AuthError("UNSUPPORTED_ENVELOPE")
        if set(envelope) != {"suite", "keyId", "nonce", "authTag", "ciphertext"}:
            raise ValueError()
        if not isinstance(envelope["keyId"], str) or not re.fullmatch(r"[0-9a-f]{16}", envelope["keyId"]):
            raise ValueError()
        for field, length in (("nonce", 12), ("authTag", 16), ("ciphertext", None)):
            data = envelope[field]
            if not isinstance(data, str):
                raise ValueError()
            decoded = base64.b64decode(data, validate=True)
            if base64.b64encode(decoded).decode("ascii") != data or (length is not None and len(decoded) != length):
                raise ValueError()
        return "sym-v1"
    except (ValueError, TypeError, KeyError):
        raise AuthError("INVALID_FORMAT") from None


# ---------------------------------------------------------------------------
# 运行时发现（macOS / Windows）；Windows 未实测，对外标注 NOT_VERIFIED
# ---------------------------------------------------------------------------
def _mac_runtime(bundle: str):
    try:
        with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as f:
            name = plistlib.load(f).get("CFBundleExecutable")
        if not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name:
            return None
        return os.path.join(bundle, "Contents", "MacOS", name)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def find_workbuddy_runtime() -> str:
    """定位 WorkBuddy 桌面端 Electron 可执行文件；可用 WORKBUDDY_EXE 显式指定。"""
    override = os.environ.get("WORKBUDDY_EXE")
    if override:
        path = os.path.abspath(os.path.expanduser(override))
        if not os.path.isfile(path) or (os.name != "nt" and not os.access(path, os.X_OK)):
            raise AuthError("INVALID_RUNTIME_PATH")
        return path
    home = os.path.expanduser("~")
    candidates = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        candidates.append(os.path.join(local, "Programs", "WorkBuddy", "WorkBuddy.exe"))
        for name in ("ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(name):
                candidates.append(os.path.join(os.environ[name], "WorkBuddy", "WorkBuddy.exe"))
    elif sys.platform == "darwin":
        candidates = [_mac_runtime(os.path.join(root, "WorkBuddy.app"))
                      for root in ("/Applications", os.path.join(home, "Applications"))]
    else:
        # Linux：暂未覆盖（桌面端不在 Linux 分发），如实报错
        raise AuthError("RUNTIME_NOT_FOUND")
    for path in candidates:
        if path and os.path.isfile(path) and (os.name == "nt" or os.access(path, os.X_OK)):
            return os.path.abspath(path)
    raise AuthError("RUNTIME_NOT_FOUND")


# ---------------------------------------------------------------------------
# 助手子进程：固定管道 + 截止时间；不在异常中夹带任何捕获数据
# ---------------------------------------------------------------------------
def _run_auth_helper(exe: str, request: dict) -> dict:
    data = json.dumps(dict(request, version=1), ensure_ascii=True).encode("ascii")
    if len(data) > AUTH_INPUT_LIMIT:
        raise AuthError("INVALID_FORMAT")
    timeout = AUTH_HELPER_TIMEOUT
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("NODE_", "ELECTRON_", "WORKBUDDY_"))}
    env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        process = subprocess.Popen([exe, "-e", AUTH_HELPER_JS], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, shell=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        raise AuthError("RUNTIME_UNAVAILABLE") from None
    output: dict = {}
    failed = threading.Event()

    def read_pipe(name, pipe, limit):
        try:
            captured = pipe.read(limit + 1)
            if len(captured) > limit:
                failed.set()
                process.kill()
            else:
                output[name] = captured
        except OSError:
            failed.set()

    def write_pipe():
        try:
            process.stdin.write(data)
            process.stdin.close()
        except OSError:
            failed.set()

    workers = [threading.Thread(target=read_pipe, args=("stdout", process.stdout, AUTH_OUTPUT_LIMIT), daemon=True),
               threading.Thread(target=read_pipe, args=("stderr", process.stderr, 8192), daemon=True),
               threading.Thread(target=write_pipe, daemon=True)]
    deadline = time.monotonic() + timeout
    try:
        for worker in workers:
            worker.start()
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise AuthError("HELPER_TIMEOUT") from None
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in workers):
            raise AuthError("HELPER_TIMEOUT")
        if failed.is_set():
            raise AuthError("HELPER_PROTOCOL")
        try:
            reply = json.loads(output.get("stdout", b"").decode("utf-8"))
        except (ValueError, UnicodeError):
            raise AuthError("HELPER_PROTOCOL") from None
        if not isinstance(reply, dict) or type(reply.get("version")) is not int or reply["version"] != 1:
            raise AuthError("HELPER_PROTOCOL")
        if reply.get("ok") is False and process.returncode == 1:
            reason = reply.get("reason")
            raise AuthError(reason if isinstance(reason, str) and reason in AUTH_REASONS else "HELPER_PROTOCOL")
        if reply.get("ok") is not True or process.returncode != 0:
            raise AuthError("HELPER_PROTOCOL")
        if request["operation"] == "decrypt":
            if set(reply) != {"version", "ok", "accessToken"} or not _valid_token(reply.get("accessToken")):
                raise AuthError("HELPER_PROTOCOL")
        elif (set(reply) != {"version", "ok", "electron"} or not isinstance(reply.get("electron"), str)
              or re.fullmatch(r"[0-9A-Za-z.+-]{1,64}", reply["electron"]) is None):
            raise AuthError("HELPER_PROTOCOL")
        return reply
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for worker in workers:
            if worker.ident is not None:
                worker.join(1)
        if not any(worker.is_alive() for worker in workers):
            for pipe in (process.stdin, process.stdout, process.stderr):
                try:
                    pipe.close()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# 对外高层接口
# ---------------------------------------------------------------------------
def _sensitive_values() -> set:
    """返回进程内已解出的敏感 token 集合（仅用于调用方自检/避免回显，不对外输出）。"""
    return _RUN_DECRYPTED


_RUN_DECRYPTED: set = set()


# 冷启动瞬时失败的一次性重试等待（落在 300–800ms 区间）
_COLD_RETRY_WAIT = 0.5

# 仅这一类原因属于“本地 Runtime / native storage 尚未 ready”的瞬时初始化问题，
# 允许极窄范围一次性重试；其余（含确定性 KEY_MISMATCH / DECRYPT_FAILED /
# UNSUPPORTED_ENVELOPE，以及超时 HELPER_TIMEOUT、协议 HELPER_PROTOCOL 等）一律不重试。
_TRANSIENT_INIT_REASONS = frozenset({"RUNTIME_UNAVAILABLE"})


def decrypt_token(value) -> str:
    """解密 $wbEncrypted 信封，返回明文 access_token（仅内存使用）。失败抛 AuthError。

    冷启动可靠性（COLD_START_RELIABILITY_GATE）：仅当首次本地 Runtime 解密抛出
    瞬时初始化错误（RUNTIME_UNAVAILABLE：客户端原生存储接口尚未 ready）时，等待
    300–800ms 后重新启动/调用一次 Runtime 解密；最多 1 次。第二次仍失败则原样返回
    真实错误，绝不静默吞掉。其余错误类型（确定性 / 超时 / 协议）一律不重试。
    """
    kind = token_format(value)  # 先校验格式；非 sym-v1 会抛 AuthError（含 plaintext 直接失败提示）
    if kind == "plaintext":
        raise AuthError("INVALID_FORMAT")
    exe = find_workbuddy_runtime()
    try:
        reply = _run_auth_helper(exe, {"operation": "decrypt", "value": value})
    except AuthError as _first:
        if _first.reason in _TRANSIENT_INIT_REASONS:
            time.sleep(_COLD_RETRY_WAIT)  # 给本地 Runtime / native storage 初始化一个窗口
            reply = _run_auth_helper(exe, {"operation": "decrypt", "value": value})  # 最多 1 次；失败原样抛
        else:
            raise
    token = reply["accessToken"]
    _RUN_DECRYPTED.add(token)
    return token


def probe_runtime() -> dict:
    """离线探测客户端运行时能力（不解密、不联网）。返回 {ok, electron, exe} 或抛 AuthError。"""
    exe = find_workbuddy_runtime()
    reply = _run_auth_helper(exe, {"operation": "probe"})
    return {"ok": True, "electron": reply.get("electron", "unknown"), "exe": exe}


if __name__ == "__main__":
    # 直接运行仅做离线 probe（不解密、不联网、不打印任何凭据）
    try:
        info = probe_runtime()
        print("✅ 客户端运行时可用：electron={} exe={}".format(info["electron"], info["exe"]))
    except AuthError as e:
        print("❌ 运行时探测失败：{}".format(e))
        sys.exit(1)
