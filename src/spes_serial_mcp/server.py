from mcp.server.mcpserver import MCPServer
import serial
import serial.tools.list_ports
import threading
import asyncio
import websockets
import json
import pathlib
import sys
import codecs
import time
import importlib.metadata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

mcp = MCPServer("SerialPortMCP")

active_serial = None
ws_clients = set()
ws_loop = None
ws_server_running = False

# --- LLM 上下文缓冲与串口自动读取线程 ---
llm_read_buffer = []       # [(source, msg), ...]
buffer_lock = threading.Lock()
serial_read_thread = None
serial_read_running = False

# --- 串口连接状态（供浏览器同步） ---
serial_status = {"connected": False, "port": None, "baudrate": 115200, "stopbits": 1,
                 "parity": "none", "bytesize": 8, "encoding": "utf-8"}

# --- 当前串口读取解码编码（读取线程使用） ---
serial_encoding = "utf-8"

# --- 串口参数映射 ---
PARITY_MAP = {"none": serial.PARITY_NONE, "even": serial.PARITY_EVEN, "odd": serial.PARITY_ODD}
STOPBITS_MAP = {1: serial.STOPBITS_ONE, 1.5: serial.STOPBITS_ONE_POINT_FIVE, 2: serial.STOPBITS_TWO}

# --- 包版本（未安装或版本冲突返回 None 时回退为 unknown，保证面板仍可用） ---
try:
    PACKAGE_VERSION = importlib.metadata.version("spes-serial-mcp") or "unknown"
except importlib.metadata.PackageNotFoundError:
    PACKAGE_VERSION = "unknown"


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        html_path = pathlib.Path(__file__).with_name("dashboard.html")
        html = html_path.read_text(encoding="utf-8").replace("{WS_PORT}", str(self.server.ws_port)).replace("{VERSION}", PACKAGE_VERSION)
        data = html.encode('utf-8')
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, format, *args): pass

def run_http_server(http_port, ws_port):
    # 多线程服务器:单个卡住的连接不会阻塞其他请求
    server = ThreadingHTTPServer(('0.0.0.0', http_port), DashboardHandler)
    server.ws_port = ws_port
    server.serve_forever()

def broadcast_to_ui(source: str, msg: str, hex_str: str = None):
    if ws_server_running and ws_clients and ws_loop:
        def _broadcast():
            websockets.broadcast(ws_clients, json.dumps({"source": source, "msg": msg, "hex": hex_str}))
        ws_loop.call_soon_threadsafe(_broadcast)

def broadcast_status():
    """广播当前串口状态给所有 WebSocket 客户端。"""
    if ws_server_running and ws_clients and ws_loop:
        def _broadcast():
            websockets.broadcast(ws_clients, json.dumps({
                "type": "status",
                "connected": serial_status["connected"],
                "port": serial_status["port"],
                "baudrate": serial_status["baudrate"],
                "stopbits": serial_status["stopbits"],
                "parity": serial_status["parity"],
                "bytesize": serial_status["bytesize"],
                "encoding": serial_status["encoding"]
            }))
        ws_loop.call_soon_threadsafe(_broadcast)


def serial_read_loop():
    """后台线程：持续读取串口数据，广播到 UI 并累积到 LLM 缓冲区。"""
    global active_serial, serial_read_running, llm_read_buffer, serial_encoding
    while serial_read_running:
        if active_serial and active_serial.is_open:
            try:
                # 非阻塞读取，不再依赖 in_waiting（某些平台不可靠）
                data = active_serial.read(4096)
                if data:
                    text_data = data.decode(serial_encoding, errors='ignore')
                    hex_data = data.hex(' ').upper()
                    # 纯二进制数据(无有效 UTF-8 文本)也广播 HEX,供前端十六进制接收
                    broadcast_to_ui("HARDWARE", text_data, hex_data)
                    if text_data:
                        with buffer_lock:
                            llm_read_buffer.append(("HARDWARE", text_data))
                else:
                    time.sleep(0.05)
            except Exception as e:
                print(f"[serial_read_loop] 异常: {e}", file=sys.stderr)
                time.sleep(0.1)
        else:
            time.sleep(0.2)


def start_serial_reader():
    """启动串口自动读取线程。"""
    global serial_read_thread, serial_read_running
    stop_serial_reader()
    serial_read_running = True
    serial_read_thread = threading.Thread(target=serial_read_loop, daemon=True)
    serial_read_thread.start()


def stop_serial_reader():
    """停止串口自动读取线程。"""
    global serial_read_running, serial_read_thread
    serial_read_running = False
    if serial_read_thread:
        serial_read_thread.join(timeout=1.0)
        serial_read_thread = None


async def ws_handler(websocket):
    ws_clients.add(websocket)
    # 新客户端连接时立即推送当前状态
    try:
        await websocket.send(json.dumps({
            "type": "status",
            "connected": serial_status["connected"],
            "port": serial_status["port"],
            "baudrate": serial_status["baudrate"],
            "stopbits": serial_status["stopbits"],
            "parity": serial_status["parity"],
            "bytesize": serial_status["bytesize"],
            "encoding": serial_status["encoding"]
        }))
    except Exception:
        pass

    try:
        async for message in websocket:
            data = json.loads(message)
            action = data.get("action")
            if action == "write" and active_serial and active_serial.is_open:
                mode = data.get("mode", "ascii")
                payload = data.get("payload", "")
                if mode == "hex":
                    try:
                        raw = bytes.fromhex(payload.replace(" ", ""))
                    except ValueError:
                        broadcast_to_ui("SYSTEM", "HEX 发送失败: 非法 HEX 数据")
                        continue
                    active_serial.write(raw)
                    display = ' '.join('{:02X}'.format(b) for b in raw)
                    broadcast_to_ui("USER_OVERRIDE", display, display)
                    with buffer_lock:
                        llm_read_buffer.append(("USER_OVERRIDE", display))
                else:
                    cmd = payload + "\r\n"
                    active_serial.write(cmd.encode('utf-8'))
                    cmd_stripped = cmd.strip()
                    broadcast_to_ui("USER_OVERRIDE", cmd_stripped)
                    with buffer_lock:
                        llm_read_buffer.append(("USER_OVERRIDE", cmd_stripped))
            elif action == "connect":
                payload = data.get("payload", {})
                port = payload.get("port", "")
                baud = payload.get("baudrate", 115200)
                stopbits = payload.get("stopbits", 1)
                parity = payload.get("parity", "none")
                bytesize = payload.get("bytesize", 8)
                encoding = payload.get("encoding", "utf-8")
                if port:
                    connect_port(port, baud, stopbits=stopbits, parity=parity,
                                 bytesize=bytesize, encoding=encoding)
            elif action == "list_ports":
                try:
                    ports = [{"device": p.device, "description": p.description}
                             for p in serial.tools.list_ports.comports()]
                    await websocket.send(json.dumps({"type": "ports", "ports": ports}))
                except Exception:
                    pass
            elif action == "close":
                close_port()
            elif action == "get_status":
                try:
                    await websocket.send(json.dumps({
                        "type": "status",
                        "connected": serial_status["connected"],
                        "port": serial_status["port"],
                        "baudrate": serial_status["baudrate"],
                        "stopbits": serial_status["stopbits"],
                        "parity": serial_status["parity"],
                        "bytesize": serial_status["bytesize"],
                        "encoding": serial_status["encoding"]
                    }))
                except Exception:
                    pass
    finally:
        ws_clients.remove(websocket)


def run_ws_server_thread(ws_port):
    global ws_loop, ws_server_running
    import traceback
    async def serve():
        global ws_loop, ws_server_running
        server = await websockets.serve(ws_handler, "0.0.0.0", ws_port)
        ws_loop = asyncio.get_running_loop()
        ws_server_running = True
        await asyncio.Future()  # run forever
    try:
        asyncio.run(serve())
    except Exception:
        ws_server_running = False
        traceback.print_exc()

@mcp.tool()
def start_monitor_ui(http_port: int = 8080) -> str:
    """启动监控UI。成功后返回链接给用户。"""
    global ws_server_running
    ws_port = http_port + 1
    if ws_server_running: return f"已启动: http://localhost:{http_port}"
    try:
        threading.Thread(target=run_ws_server_thread, args=(ws_port,), daemon=True).start()
        threading.Thread(target=run_http_server, args=(http_port, ws_port), daemon=True).start()
        return f"启动成功！请点击: http://localhost:{http_port}"
    except Exception as e: return f"失败: {str(e)}"

@mcp.tool()
def list_ports() -> str:
    ports = serial.tools.list_ports.comports()
    return "\n".join([f"{p.device} - {p.description}" for p in ports]) if ports else "未找到串口"

@mcp.tool()
def close_port() -> str:
    global active_serial, serial_status
    try:
        if active_serial and active_serial.is_open:
            stop_serial_reader()
            active_serial.close()
            port_name = serial_status.get("port", "未知")
            serial_status["connected"] = False
            serial_status["port"] = None
            broadcast_to_ui("SYSTEM", f"已断开串口 {port_name}")
            broadcast_status()
            return f"已断开串口 {port_name}"
        serial_status["connected"] = False
        broadcast_status()
        return "串口未打开"
    except Exception as e:
        return f"断开失败: {str(e)}"

@mcp.tool()
def connect_port(port: str, baudrate: int = 115200, stopbits: float = 1, parity: str = 'none',
                 bytesize: int = 8, encoding: str = 'utf-8') -> str:
    global active_serial, serial_status, serial_encoding
    try:
        if active_serial and active_serial.is_open:
            stop_serial_reader()
            active_serial.close()
        parity_value = PARITY_MAP.get(str(parity).lower(), serial.PARITY_NONE)
        try:
            stopbits_value = STOPBITS_MAP.get(float(stopbits), serial.STOPBITS_ONE)
        except (TypeError, ValueError):
            stopbits_value = serial.STOPBITS_ONE
        # 非法编码会拖垮读取线程,连接前兜底为 UTF-8
        try:
            codecs.lookup(encoding)
        except LookupError:
            encoding = 'utf-8'
        active_serial = serial.Serial(port, baudrate, bytesize=bytesize, parity=parity_value,
                                      stopbits=stopbits_value, timeout=0)
        start_serial_reader()
        serial_encoding = encoding
        serial_status["connected"] = True
        serial_status["port"] = port
        serial_status["baudrate"] = baudrate
        serial_status["stopbits"] = stopbits
        serial_status["parity"] = str(parity).lower()
        serial_status["bytesize"] = bytesize
        serial_status["encoding"] = encoding
        broadcast_to_ui("SYSTEM", f"已连接到 {port} ({bytesize},{parity},{stopbits})")
        broadcast_status()
        return f"已连接到 {port} (波特率: {baudrate}, 数据位: {bytesize}, 校验: {parity}, 停止位: {stopbits}, 编码: {encoding})"
    except Exception as e:
        serial_status["connected"] = False
        broadcast_to_ui("SYSTEM", f"连接失败: {str(e)}")
        broadcast_status()
        return f"连接失败: {str(e)}"

@mcp.tool()
def write_data(data: str) -> str:
    global active_serial
    if not active_serial or not active_serial.is_open: return "串口未打开"
    try:
        payload = data + "\r\n" if not data.endswith("\n") else data
        active_serial.write(payload.encode('utf-8'))
        broadcast_to_ui("LLM", data.strip())
        return "发送成功"
    except Exception as e: return str(e)

@mcp.tool()
def read_data() -> str:
    """读取串口数据。优先返回用户干预与下位机回应的历史记录，清空后返回实时串口数据或无数据。"""
    global active_serial, llm_read_buffer
    if not active_serial or not active_serial.is_open: return "串口未打开"
    try:
        # 优先返回 buffer 中累积的用户干预 + 下位机回应
        with buffer_lock:
            if llm_read_buffer:
                result = []
                for source, msg in llm_read_buffer:
                    label = "[用户]" if source == "USER_OVERRIDE" else "[下位机]"
                    result.append(f"{label} {msg}")
                llm_read_buffer.clear()
                return "\n".join(result)
        return "无数据"
    except Exception as e: return str(e)

# --- 必须包装在 main 函数中供 uvx 调用 ---
def main():
    mcp.run(transport='stdio')
