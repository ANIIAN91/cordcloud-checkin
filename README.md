# CordCloud 自动签到

通过 GitHub Actions 每天为 CordCloud 账号自动签到，支持验证器 TOTP 二步验证及可选的 Telegram 结果通知。适合希望保留云端无人值守签到的用户。

首次使用请先完成下方的 GitHub 配置，再手动运行一次。本仓库当前默认使用 **Patchright + Google Chrome + Xvfb** 完成 Cap 验证。

2026-10-29 的[完整云端验证](https://github.com/ANIIAN91/cordcloud-checkin/actions/runs/40796343093)已完成登录、签到并获得 171 MB 流量，Telegram 通知发送成功。这是一次实测结果，不代表后续每次都能通过站点验证。

## GitHub Actions 快速开始

### 1. 准备账号和仓库

Fork 本仓库，并在自己的仓库中启用 Actions。当前定时工作流使用 `cordcloud.biz` 和验证器二步验证，需要账号邮箱、密码及验证器的 TOTP 长期密钥。

`secret` 是绑定验证器时取得的长期密钥，不是每隔一段时间变化的六位验证码。若拿到的是 `otpauth://` 链接，应填写其中 `secret` 参数的值。仅有一次性邮件验证码的账号不能直接使用当前无人值守配置。

### 2. 添加仓库 Secrets

进入仓库的 **Settings → Secrets and variables → Actions → New repository secret**，添加：

| Secret | 内容 | 用途 |
| --- | --- | --- |
| `CC_EMAIL` | CordCloud 登录邮箱 | 必填 |
| `CC_PASSWD` | CordCloud 登录密码 | 必填 |
| `CC_SECRET` | 验证器 TOTP 长期密钥 | 当前工作流必填 |
| `TG_BOT_TOKEN` | Telegram Bot Token | 可选，通知需与下一项一起配置 |
| `TG_CHAT_ID` | Telegram 目标会话 ID | 可选 |

Telegram Bot Token 可通过 BotFather 创建机器人取得。先向机器人发送消息，再通过 Telegram 的 `getUpdates` 接口读取目标 `chat.id`；群组还需先加入机器人。不要把这些值写进工作流或提交到仓库。

### 3. 手动验证一次

打开 **Actions → CordCloud → Run workflow**，选择 `main` 并运行。

成功日志应包含“尝试帐号登录，结果：验证成功”或其他站点登录成功消息，以及“尝试帐号签到，结果：获得了…流量”或“您似乎已经签到过了”。最终应显示 `CordCloud Action 成功结束运行！`。

Telegram 推送失败会产生警告，但不会把已经完成的签到改判为失败。请以签到步骤和工作流最终状态为准。

### 4. 每日自动运行

唯一的定时配置入口是 [.github/workflows/cordcloud.yml](.github/workflows/cordcloud.yml)：

- 计划时间：每天 UTC `02:20`，即北京时间 `10:20`。
- 定时触发后额外随机等待 0～599 秒；手动运行跳过该等待。
- 使用 Ubuntu 24.04、Python 3.12，以及托管 runner 已安装的 Chrome 和 Xvfb。
- 先执行回归测试，再运行 `xvfb-run -a python -u main.py`。
- 当前仅尝试 `cordcloud.biz`，使用 `verify_method=ga`，不启用信任设备。

计划时间不等于实际开始时间。若工作流被停用，需先重新启用；更改时间或站点请修改上述文件，避免另外创建重复的每日任务。

## 本地运行

### Linux

使用 Python 3.12、Google Chrome，以及独立 Python 虚拟环境。以下从源码运行，不需要另外部署服务：

```bash
git clone https://github.com/ANIIAN91/cordcloud-checkin.git
cd cordcloud-checkin
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp config.default.json config.local.json
```

编辑 `config.local.json`，填写 `email`、`passwd`、`secret`，将 `verify_method` 设为 `ga`、`host` 设为 `cordcloud.biz`。已有配置时直接编辑，勿重新复制模板覆盖。

若系统尚未安装 Google Chrome，可使用 Patchright 安装入口；可能需要管理员权限：

```bash
.venv/bin/python -m patchright install chrome
```

无桌面的 Ubuntu/Debian 服务器还需虚拟显示：

```bash
sudo apt-get update
sudo apt-get install -y xvfb xauth
xvfb-run -a .venv/bin/python main.py --host cordcloud.biz
```

已有桌面显示时可直接运行：

```bash
.venv/bin/python main.py --host cordcloud.biz
```

升级已有项目时，应重新安装 `requirements.txt` 中的依赖；旧版仅安装 Playwright 的环境不能直接运行新版本。

### Windows

安装 Python 3.12 和 Google Chrome 后，可在 PowerShell 中使用项目虚拟环境：

```powershell
git clone https://github.com/ANIIAN91/cordcloud-checkin.git
cd cordcloud-checkin
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.default.json config.local.json
```

按 Linux 一节说明填写配置后运行：

```powershell
.\.venv\Scripts\python.exe main.py --host cordcloud.biz
```

本轮完整验证在 Linux/GitHub Actions 上完成，Windows 命令未重新实测。旧 [local_test.ps1](local_test.ps1) 内含特定机器的绝对路径，不能作为通用启动入口；旧 `.conda/cordcloud-test` 也不能用于 Linux。

### 可选：复用本机浏览器登录状态

如果已配置 `agent-browser-cli` 并保持浏览器扩展连接，先在浏览器中登录并打开 `https://cordcloud.biz/user`，再运行：

```bash
.venv/bin/python main.py --browser --host cordcloud.biz
```

该模式使用浏览器当前账号，在内存中导入本站 Cookie，不使用配置中的邮箱密码重新登录。会话过期后需要手动登录。GitHub 定时任务使用自动登录流程，不依赖此模式。

## 配置说明

[config.default.json](config.default.json) 是空值模板，本地账号信息放在已被 Git 忽略的 `config.local.json` 中。

一般参数的优先级为：非空 Action 输入或 `INPUT_*` 环境变量 → 本地配置 → 默认配置。`--host` 高于其他站点配置；`--browser` 切换为本机浏览器会话模式。

| 参数 | 含义 | 默认或约束 |
| --- | --- | --- |
| `email` | 登录邮箱 | 自动登录必填 |
| `passwd` | 登录密码 | 自动登录必填 |
| `secret` | 验证器 TOTP 长期密钥 | 云端验证器二步验证使用 |
| `code` | 一次性邮箱或设备验证码 | 仅适合临时手动运行 |
| `verify_method` | 二步验证方式 | `ga`、`email`、`auto` 或留空；当前工作流用 `ga` |
| `host` | 站点域名，可用逗号分隔 | 不带协议头；当前工作流覆盖为 `cordcloud.biz` |
| `trust_device` | 是否信任当前设备 | 默认 `false` |
| `insecure_skip_verify` | 是否跳过 TLS 验证 | 默认 `false`，仅供明确的临时调试 |
| `telegram_bot_token` | Telegram Bot Token | 与 `telegram_chat_id` 同时配置才启用通知 |
| `telegram_chat_id` | Telegram 会话 ID | 可选 |
| `device_fingerprint` | 本地配置中的设备指纹 | 由程序维护；Cap 登录采用当次浏览器生成的值 |

默认站点列表为 `cordcloud.us,cordcloud.one,cordcloud.biz,c-cloud.xyz`，不代表这些域名都已在本轮验证。建议使用已实测的 `cordcloud.biz`。

多站点模式在可重试错误后尝试下一个域名；账号认证错误会停止。当前单站点工作流没有自动重试。

## 登录流程与诊断

Cap 登录按以下顺序执行：

1. 创建全新的临时 Chrome 配置，由网站组件完成验证。
2. 在页面主执行环境读取 Cap 令牌、CSRF、设备指纹和 UA，并取得本站 Cookie。
3. 关闭浏览器并删除临时配置，将会话数据在内存中交给登录请求。
4. 完成账号登录及所需的二步验证，再调用签到接口。

浏览器启动使用 [Patchright Python](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python)，版本由 [requirements.txt](requirements.txt) 固定。旧 ALTCHA 页面仍保留原有处理路径。

| 日志 | 说明 |
| --- | --- |
| `Cap diagnostic` | 验证阶段、耗时、Chrome 版本、登录页状态、必要字段是否就绪 |
| `event=cap_http` | 页面实际验证 API 的 `challenge` / `redeem` HTTP 状态 |
| `Cap session` | 浏览器会话导入状态、提交登录请求的 HTTP 状态 |
| `redacted_unknown` | 错误内容未匹配允许输出的公开原因码，已隐藏原文 |

Patchright 不提供控制台事件，诊断使用网络响应及字段状态。上述诊断不输出令牌、Cookie、账号、设备指纹值、响应正文或浏览器异常原文。HTTP 200 或取得 Cap 令牌本身不等于登录或签到成功。

### 常见问题

- **`automated_browser` 或验证接口拒绝：**站点检测未通过。检查实际运行提交是否已采用新流程，再结合验证阶段和 HTTP 状态定位；不要将失败当作已签到。
- **浏览器启动失败：**检查 Google Chrome 是否安装。无桌面 Linux 应通过 `xvfb-run -a` 启动，并安装 `xvfb`、`xauth`。
- **二步验证失败：**核对 `CC_SECRET` 是否为长期 TOTP 密钥，而非六位动态码；当前工作流强制使用 `ga`。
- **缺少账号参数：**核对 Secrets 名称，尤其是 `CC_EMAIL`、`CC_PASSWD`；本地则检查 `config.local.json`。
- **显示已经签到：**程序将站点的“您似乎已经签到过”视为当日已完成，不会再次增加流量。
- **只有 Telegram 警告：**检查机器人和会话配置；签到与通知分别判断结果。

## Docker Action 与维护

仓库仍提供 [action.yml](action.yml) 和 [Dockerfile](Dockerfile) 形式的 Docker Action。镜像安装 Chrome、Xvfb、xauth，并用 tini 管理启动进程；当前每日工作流直接运行 Python，不需要构建镜像。

修改登录流程后，可先执行不访问真实账号的回归测试：

```bash
.venv/bin/python -m unittest -v test.py
```

当前验证证据：

- [独立 Cap 探针](https://github.com/ANIIAN91/cordcloud-checkin/actions/runs/40795157310)：本机取得 1 次、云端取得 3 次令牌，未执行账号登录。
- [完整云端签到](https://github.com/ANIIAN91/cordcloud-checkin/actions/runs/40796343093)：登录成功、获得 171 MB、通知成功，运行提交 `3642150`。
- 本轮 21 项回归测试通过；Docker 构建及隔离容器中的模拟页面启动检查通过。Docker 模拟检查不等于再次真实签到。

站点规则、浏览器及托管 runner 环境可能变化，现有小样本不构成长期成功率保证。报告问题时可附工作流链接和脱敏后的错误阶段，不要附账号配置或完整浏览器会话。

## 来源与许可证

本项目基于 [yanglbme/cordcloud-action](https://github.com/yanglbme/cordcloud-action) 改造，补充了当前站点的 CSRF、设备二步验证、Cap 登录及通知流程。

本仓库使用 [MIT License](LICENSE)。Patchright、Chrome 等依赖按各自许可证分发。
