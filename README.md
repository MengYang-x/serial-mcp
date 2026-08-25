# Serial MCP

基于 MCP 协议的本地串口通信服务，内置实时 Web 监控面板。通过自然语言让 AI 助手与单片机、嵌入式设备直接对话。

## 功能特性

- 🖥️ **MCP 支持** — 通过 Model Context Protocol 与 AI 客户端无缝集成
- 🔌 **串口控制** — 自动扫描、连接、读写串口设备
- 🌐 **Web 监控面板** — 内置 HTTP + WebSocket 双端口服务，浏览器实时旁路监控，支持手动干预

## 可用工具

| 工具 | 说明 |
|------|------|
| `list_ports` | 扫描本机所有可用串口 |
| `connect_port` | 连接指定串口（支持自定义波特率，默认 115200） |
| `close_port` | 显式断开当前串口连接 |
| `write_data` | 向串口写入数据（自动补全换行符） |
| `read_data` | 读取串口缓冲区数据（含用户干预历史） |
| `start_monitor_ui` | 启动 Web 监控面板（默认 HTTP 8080 / WebSocket 8081） |

## 安装运行

```bash
uv sync
uv run serial-mcp
```

## 客户端配置

在支持 MCP 的客户端（Claude Code、Cursor、Cline 等）的 `.mcp.json` 中添加：

```json
{
  "mcpServers": {
    "serial-mcp": {
      "command": "uv",
      "args": [
        "--directory",
        "d:/003.github/AI应用/serial-mcp",
        "run",
        "serial-mcp"
      ]
    }
  }
}
```

## 快速开始

```
请使用串口工具，连接到我的开发板并且测试通信
```

启动后，AI 会自动执行以下初始化检查：

```
1. start_monitor_ui    → 启动 Web 面板 http://localhost:8080
2. list_ports          → 发现 COM3 等串口
3. connect_port        → 连接设备（115200）
4. write_data("hello") → 测试通信
5. read_data()         → 读取串口输入
```

在浏览器中打开 `http://localhost:8080`，你可以：

- 🔘 手动控制串口连接/断开
- 💬 实时查看 LLM 与单片机的全部对话
- ✏️ 手动下发命令（旁路干预）

## Web 面板使用说明

**面板不会随 MCP 服务自动启动**。MCP 服务本身只通过 stdio 运行，不监听任何 HTTP 端口；只有显式调用 `start_monitor_ui` 工具后，才会监听 8080 端口（WebSocket 端口为 8081，页面自动连接）。

对 AI 说以下指令即可启动：

```
启动监控面板
```

也可以指定其他端口，例如：

```
启动监控面板，使用端口 9090
```

（此时 HTTP 端口为 9090，WebSocket 自动使用 9091）

启动成功后 AI 会返回链接，再用浏览器访问即可。常见问题：

- **浏览器提示「无法访问此页面」**：面板尚未启动，请先让 AI 执行 `start_monitor_ui`
- **8080 端口被占用**：换一个端口启动即可
- **面板何时关闭**：面板运行在 MCP 服务进程的后台线程中，AI 客户端（Claude Code 等）退出后随之关闭
