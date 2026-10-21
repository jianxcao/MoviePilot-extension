# PT站点CDP签到插件 (MoviePilot V3)

通过 **Chrome DevTools Protocol (CDP)** 协议直连外部 Chrome / Edge 浏览器，利用真实日常浏览器的环境指纹与登录态，自动化完成 PT 站点打卡与签到。

## 核心优势

1. **天然抗风控**：连接的是你日常使用的真实 Chrome 浏览器，拥有真实指纹、真实日常家宽网络与完整 Profile，无需与 Cloudflare 五秒盾、Turnstile 硬碰硬。
2. **轻量零依赖**：只基于 CDP 远程协议与原生 WebSocket 通信，MoviePilot 容器无需安装 Playwright、Chromium、X11 等臃肿依赖包。
3. **Cookie 自动同步**：每次签到前可自动从 Chrome 提取对应 PT 站点的最新 Cookie；即使站点没有提供 attendance.php，也能在浏览器后台 Tab 中完成模拟点击。

## Chrome 启动配置

在运行 Chrome 的电脑/容器上，以远程调试模式启动 Chrome：

### macOS 示例：
```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --remote-allow-origins=*
```

### Windows 示例：
快捷方式目标后追加参数：
```cmd
chrome.exe --remote-debugging-port=9222 --remote-allow-origins=*
```

### Linux / Docker 示例：
```bash
google-chrome-stable \
  --remote-debugging-port=9222 \
  --remote-debugging-address=0.0.0.0 \
  --remote-allow-origins=*
```

> **注意**：`--remote-allow-origins=*` 为现代 Chrome 跨主机/跨容器连接所必需。

## 插件配置项

- **Chrome CDP 调试地址**：例如 `http://192.168.1.100:9222`（跨容器/跨主机请填写局域网 IP）。
- **执行周期**：Cron 表达式，默认每天 `20 8 * * *`。
- **签到站点**：支持「全部站点（含后续新增）」或从已配置的站点中自由勾选。
