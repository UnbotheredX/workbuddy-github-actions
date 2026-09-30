# WorkBuddy 每日签到 · GitHub Actions 版

把 WorkBuddy 的「Buddy 加油站签到」和「Buddy 旅行（派猫猫）」搬到 GitHub Actions 定时跑。
电脑关机、客户端不开也能签。

**为什么选 GitHub 而不是 Gitee**：Gitee Go 没有原生定时触发，必须外挂一个 cron 服务去打它的
OpenAPI；GitHub Actions 自带 `schedule:`，Secret 管理也更成熟。这类「定时跑脚本」的场景，
GitHub 少一个故障点。

---

## 一、原理

```
你的 Windows 电脑                         GitHub Actions（云端，Ubuntu）
─────────────────────                     ─────────────────────────────
WorkBuddy 桌面端（运行中且已登录）
   │
   │ ① 客户端进程内存里有 atRestSecretKey
   │    （不落盘，关掉客户端就没了）
   ▼
tools/export_token.py
   │ 解密 $wbEncrypted 信封 → 明文 JWT
   │
   │ ② 手动粘贴（一次性）
   ▼
GitHub Repository Secret
   WORKBUDDY_AUTH_JSON ──────────────────► scripts/credentials.py 读取
                                                │
                                                ▼
                                           scripts/main.py all
                                                │
                                    ┌───────────┴───────────┐
                                    ▼                       ▼
                            Buddy加油站签到          Buddy旅行状态机
                          copilot.tencent.com    www.workbuddy.cn
```

关键点：**解密只在你本机做**。云端拿到的是已经解好的明文 JWT，它不需要也没法解密——
因为解密密钥只存在于你运行中的客户端进程内存里。

---

## 二、准备工作

| 项目 | 说明 |
|---|---|
| WorkBuddy 客户端 | Windows 版，**导出密钥时必须正在运行且已登录** |
| Python 3.10+ | 本机执行导出命令时用；云端 Actions 自带 3.12，不用管 |
| GitHub 账号 | 免费，用于托管代码和跑 Actions |
| Git（可选） | 也可以全程在 GitHub 网页端建文件、传文件 |

---

## 三、部署：4 步

### 第 1 步：建仓库

新建一个**私有**仓库（Private），把 `workbuddy-github-actions/` 目录下的所有文件推上去：

```
你的仓库/
├── .github/workflows/checkin.yml
├── scripts/
│   ├── main.py
│   ├── checkin.py
│   ├── travel.py
│   ├── credentials.py
│   └── http_client.py
├── tools/export_token.py
├── tests/selfcheck.py
└── .gitignore
```

或者直接命令行：

```bash
cd workbuddy-github-actions
git init
git add .
git commit -m "init: WorkBuddy 每日签到"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

### 第 2 步：本机导出登录态

**保持 WorkBuddy 客户端正在运行且已登录**，在仓库目录打开 PowerShell：

```powershell
python tools/export_token.py
```

它会读取本机登录态 → 解密（`$wbEncrypted` 加密态自动处理）→ 打印一行可以直接整份复制的 JSON：

```json
{"auth":{"accessToken":"eyJhbG...","domain":"www.codebuddy.cn"},"account":{"uid":"..."}}
```

> 如果提示「无法解密」，先确认客户端确实在运行且已登录。
> **这一步不能省**——密钥只在运行中的客户端进程内存里，关掉客户端就只能拿到密文。

### 第 3 步：配置 Secret

在 GitHub 仓库页面：

**Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `WORKBUDDY_AUTH_JSON` | 粘贴上一步输出的**整行** JSON |

> 注意：粘贴时别换行、别截断。GitHub Secret 上限 48KB，正常 JWT 只有几 KB，不会超。
> 如果日志报「不是合法 JSON」，八成是粘贴被截断了——删掉旧 Secret 重新生成覆盖。

### 第 4 步：首次运行验证

**Actions 标签页 → 左侧选「WorkBuddy 每日签到」→ Run workflow → 选 main 分支 → 运行**

展开日志，核对这几行：

| 日志位置 | 期望看到 |
|---|---|
| 验证登录态是否已配置 | `Secret 已配置（长度 N 字符，不展示内容）` |
| 执行签到 + 旅行 | `登录态来源：env:WORKBUDDY_AUTH_JSON` |
| | `登录态剩余有效期：xx 天` |
| | `--- 签到 ---` 后接 `"status":"success"` 或 `"already_checked"` |
| | `--- 旅行 ---` 后接 `departed` / `claimed` / `traveling` |
| Job Summary | 页面顶部有「WorkBuddy 签到结果」表格 |

看到 `success` / `already_checked` / `departed` / `claimed` 就代表跑通了。

---

## 四、定时策略（重要）

GitHub Actions 的 cron **一律按 UTC 解释**，北京时间 = UTC+8。本项目配了两条：

| cron (UTC) | 北京时间 | 干什么 |
|---|---|---|
| `35 23 * * *` | 07:35 | 签到 + 派遣猫猫去旅行 |
| `5 5 * * *` | 13:05 | 签到（幂等，返回 already_checked）+ 领取旅行奖励 |

**为什么要跑两次**：派猫猫出去后要等 1–4 小时才会「到达」，一次执行只能覆盖
「派遣」或「领取」中的一个，所以用两次错开的执行补齐闭环。签到本身幂等，重复跑无副作用。

想改时间，编辑 `.github/workflows/checkin.yml` 里的 cron，推送到 main 即可。
**换算公式：北京时间 − 8 小时 = UTC**（负数就往前退一天）。

改完记得手动 Run 一次验证。

> ⚠️ 已知限制：GitHub 的 `schedule` 在高峰期可能延迟几分钟到几十分钟，不对准分钟。
> 签到按自然日结算，这点延迟不影响。

---

## 五、日常维护

### 换 Token（约 55 天一次）

登录态是 JWT，实测有效期约 55 天。流水线每天打印剩余天数，**不足 7 天会输出 warning**。

到期前做三件事：

```powershell
# 1. 打开并登录 WorkBuddy 客户端（保持运行）
# 2. 在仓库目录重新导出
python tools/export_token.py
# 3. 整份复制 → 更新 GitHub Secret WORKBUDDY_AUTH_JSON → 保存
```

第 1 步不能省。另外，GitHub 可能把运行失败/即将到期的通知发到你的邮箱，
不想收可以在 Actions 页面关掉。

### 改签到时间

1. 改 `.github/workflows/checkin.yml` 里的 cron
2. 推到 main
3. 手动 Run 一次验证

不需要重新导出 token（这一点比原 CNB 方案省事——CNB 的 `allow_events` 校验要求改完时间
必须重新生成密钥文件）。

---

## 六、常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `::error::未配置 Secret WORKBUDDY_AUTH_JSON` | 没配 Secret 或名字写错。注意大小写完全一致 |
| 日志报「不是合法 JSON」/「疑似被截断」 | 粘贴不完整。删掉旧 Secret，重新导出整份覆盖 |
| 报「检测到加密态 accessToken（$wbEncrypted）」 | Secret 里存的是加密信封。本机重新运行 `export_token.py` 导出**明文** |
| 报「无法解密」 | 客户端没运行/没登录。**必须先打开并登录 WorkBuddy 桌面端**，再执行导出 |
| 报 401 | token 过期。重新导出并更新 Secret |
| 旅行返回 `failed` + code/msg | Buddy 旅行属成长中心活动接口，**活动改版/下线时会失效**，属预期行为；签到不受影响 |
| 签到返回 `already_checked` | 正常，今日已领过（幂等保护） |
| 到点没跑 | GitHub schedule 在高峰期会延迟；确认 workflow 在 default 分支上；确认仓库 60 天内有活动（长期不活跃的仓库 schedule 会被自动暂停） |
| 手动能跑、定时不跑 | 检查仓库是否被 GitHub 标记为 inactive。去 Actions 页面点一次 Enable workflow |

---

## 七、安全说明

- **凭据即账号密码**：`accessToken` 等同你的 WorkBuddy 账号密码。
- **token 只在内存中使用**：不打印、不写文件、不落盘、不上传任何第三方。
- 网络访问仅限 WorkBuddy 服务端接口：`copilot.tencent.com`（签到）、`www.workbuddy.cn`（旅行）。
- **仓库务必设为 Private**。GitHub 私有仓库的 Actions Secret 对该仓库有写权限的协作者可见。
- `.gitignore` 已排除 `secrets.json` / `logs/` / `.env`，别把这些文件提交上去。
- 只操作你自己账号，别用于他人账户或批量刷分。

---

## 八、离线自检

不想联网验证、只想确认脚本没坏：

```bash
python tests/selfcheck.py
```

用伪造的 JWT 走一遍解析链路，20 项断言，不需要真实 token，也不发任何网络请求。

---

## 九、迁移来源

- 业务逻辑（签到幂等规则、旅行状态机、接口契约）迁移自 `workbuddy-reward-helper` skill
- 签到部分最早来自 `workbuddy-checkin`（连续运行 3 周+）
- 旅行部分最早来自 `workbuddy-travel-auto`（完成 depart→claim 同单闭环验收）
- `$wbEncrypted` 运行时解密参考 `88lin/workbuddy-auto-signin`（MIT）

本版改动：把 `credentials.py` 的凭据来源从「本机运行时解密」换成「环境变量读取」，
其余业务逻辑保持不变；新增 GitHub Actions workflow 与导出工具。
