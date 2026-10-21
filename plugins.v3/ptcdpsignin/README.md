# PT站点CDP签到（MoviePilot V3）

通过 CDP 连接外部 Chrome/Chromium，复用专用持久化 Profile 中的登录态。MoviePilot 端只需已有的 Python HTTP 和 WebSocket 依赖，不启动本地浏览器。

## 安装与连接

本插件仅发布到 `package.v3.json` 和 `plugins.v3/ptcdpsignin/`，V2 不会扫描或安装。已存在的其他插件继续使用各自的原版本目录。

Chrome 136 及以后，远程调试必须使用**非默认用户目录**。先启动专用浏览器，在该窗口中手动登录需要签到的站点；它不会自动继承日常默认 Profile 的 Cookie。

macOS：

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-data-dir="$HOME/.moviepilot-chrome" \
  --remote-debugging-port=9222
```

Windows（调整 Chrome 安装路径）：

```cmd
"C:\Program Files\Google\Chrome\Application\chrome.exe" --user-data-dir="C:\MoviePilotChrome" --remote-debugging-port=9222
```

Linux：

```bash
google-chrome --user-data-dir="$HOME/.moviepilot-chrome" --remote-debugging-port=9222
```

远端需支持 CDP `Target.createTarget(hidden=true)` 和 flat session。插件使用会话绑定的隐藏页面，即使连接中断也由浏览器回收；不支持这些能力的旧内核会明确拒绝，不能退回可能遗留标签的创建方式。

CDP 地址支持：

- `http://127.0.0.1:9222`：自动发现浏览器 WebSocket。
- `https://browser.example/cdp?token=...`：保留 HTTPS、代理前缀和认证查询参数。
- 完整 `ws://.../devtools/browser/...` 或 `wss://...`：直接连接，不附加 `/json/version`。

浏览器进程重启后，完整 WebSocket 中的浏览器 ID 可能变化；普通 Chrome 优先配置 HTTP 发现地址。浏览器服务必须支持标准 CDP；Browserless 的 BrowserQL 地址不是 CDP 地址。

## 网络与安全

CDP 具有浏览器控制和读取登录凭证的权限，**不要直接暴露到公网**。优先用 SSH 隧道或有认证的 TLS 反向代理，限制访问来源。本插件不发送 WebSocket Origin，不要求 `--remote-allow-origins=*`。

Docker 内的 `127.0.0.1` 指向容器自身。让 MP 连接可达的隧道/代理入口；不能假定开启 Chrome 调试端口就能跨主机访问。HTTP 发现地址返回的浏览器内部主机名会替换为所配置的入口，页面通信统一复用该浏览器 WebSocket，不另猜 `/devtools/page` 地址。

远端浏览器使用远端网络和代理；MP 的站点代理只应用于 HTTP 分支，不能改变已运行 Chrome 的代理。CDP/真实浏览器**不保证通过风控、验证码或二次验证**，更不能保证 Cookie 永久有效。

## 配置与执行

浏览器基础设施只填一个 **CDP 地址**，不需要 Browserless Token。其余为签到任务设置：

- 启用插件、通知、Cron、保存后立即执行一次。
- 签到站点及独立登录站点；支持全部站点，双选站点一轮只处理一次。
- 站点附加信息可提供账号、密码与登录字段；优先验证浏览器会话，未认证时导入 MP Cookie 重新验证，再尝试账号密码登录。
- 最近动向、自定义站点规则、并发、超时、失败重试。
- 当天成功记录自动跳过；需要重跑时使用一次性的“清理本日缓存”。记录按天保留最近 30 天。

账号示例（站点管理的附加信息，不是插件配置）：

```json
{"username":"账号","password":"密码","login_path":"/login.php","activity_path":"/userdetails.php?id=123"}
```

自定义站点规则示例：

```json
{"pt.example.com":{"engine":"cdp","path":"/attendance.php","submit_selector":"button[type=submit]","submit_text":"立即签到","success":"签到成功","already":"今日已签到"}}
```

保留站点预设、HTTP 表单和入口发现，以及特殊站点模块。浏览器分支在同一页面完成入口导航、表单提交和结果核验；只关闭插件创建的标签，不接管用户现有标签或关闭整个浏览器。

验证码和 Turnstile 只等待浏览器正常完成；原站点模块具备的 OCR/API 能力沿用。需要外部解题服务或人工二次验证时明确失败，不伪造 token、不把按钮点击本身当成功。成功判定排除脚本/模板文案，未签到和待提交状态优先，不以“连续签到”说明判成功。

认证已经确认且获得非空 Cookie 时，写回对应 MP 站点。写前重新检查原 Cookie 和 URL，避免覆盖已观察到的用户修改；宿主站点更新接口不提供原子 CAS，极窄的并发修改窗口无法完全消除。Cookie 不写入插件历史、日志或通知。

停止/重载会取消一次性任务和失败重试，并等待执行中的站点释放资源。已发送的网络请求无法撤回；取消后不继续下一站，也不发布旧生命周期的结果。

## 本地验证

在具有 MoviePilot 运行依赖和 pytest 的 Python 环境中：

```bash
python -m pytest tests -q
```

测试仅启动独立临时 Chrome Profile 和回环 HTTP 服务。Linux 可以设置 `CHROME_BIN`；缺少 Chrome 时真实浏览器用例会明确跳过。没有使用真实 PT 账号验证，具体站点 DOM/规则变化仍需实际站点反馈。

## 来源与许可

站点规则、HTTP/登录/最近动向解析和特殊站点模块改编自 [qingyukb/MoviePilot-Plugins](https://github.com/qingyukb/MoviePilot-Plugins/tree/c9a298fd898ea4803b7455a633372bc1d2f631af/plugins.v3/ptsitesignin)，参考提交 `c9a298fd898ea4803b7455a633372bc1d2f631af`；原作者及其引用的 tmzg0000 等来源保留在源码说明中。该上游仓库使用 GPL-3.0，与本仓库根 LICENSE 一同分发；本插件改为 V3 CDP 执行，不依赖原 BrowserQL 引擎。

## 变更记录

- **1.1**：修复 CDP Target、TLS/代理连接、Cookie、成功误判和多步提交；恢复原版规则、登录、动向、历史与任务设置；修复 V3 清单及任务停止行为。
- **1.0**：首次 CDP 版本；存在已确认的 Cookie、签到判定、生命周期和功能覆盖问题，不建议继续使用。
