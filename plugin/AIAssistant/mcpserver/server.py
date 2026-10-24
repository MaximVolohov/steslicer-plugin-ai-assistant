"""Minimal MCP server: JSON-RPC 2.0 with initialize, ping, tools/list and tools/call.

Transport-agnostic: the HTTP server feeds it request bodies (Streamable HTTP,
JSON responses only), and the agent talks to it in-process through
InProcessMcpClient so both paths run exactly the same code.
"""
import base64
import json
import threading
import traceback

from UM.Logger import Logger

from ..core.errors import ToolError
from .registry import ArgumentError, ToolContext

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "ste-slicer-ai-assistant", "title": "STE Slicer AI Assistant", "version": "1.0.0"}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = ("Инструменты управляют слайсером STE Slicer (Stereotech, форк Cura 3.6): машина, режим печати 3D/5D, "
                "профили, материалы, настройки, модели на столе, нарезка и экспорт. Координаты в мм, Z вверх, "
                "0 — центр стола. Начните с get_machine_info.")


class McpServer(object):
    def __init__(self, registry, services, bus=None):
        self._registry = registry
        self._services = services
        self._bus = bus
        self._lock = threading.Lock()

    # -- entry points --------------------------------------------------------
    def handleText(self, text):
        """Process a raw JSON-RPC body; returns the response text or None (notification only)."""
        try:
            message = json.loads(text)
        except ValueError:
            return json.dumps(self._error(None, PARSE_ERROR, "Parse error"))
        if isinstance(message, list):
            if not message:
                return json.dumps(self._error(None, INVALID_REQUEST, "Empty batch"))
            responses = [r for r in (self.handle(m) for m in message) if r is not None]
            return json.dumps(responses, ensure_ascii=False) if responses else None
        response = self.handle(message)
        return json.dumps(response, ensure_ascii=False) if response is not None else None

    def handle(self, message, ctx=None):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            return self._error(message.get("id") if isinstance(message, dict) else None,
                               INVALID_REQUEST, "Invalid request")
        method = message["method"]
        request_id = message.get("id")
        is_notification = "id" not in message
        params = message.get("params") or {}
        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [tool.toMcp() for tool in self._registry.list()]}
            elif method == "tools/call":
                result = self.callTool(params.get("name"), params.get("arguments") or {}, ctx)
            elif method.startswith("notifications/"):
                return None
            else:
                return None if is_notification else self._error(request_id, METHOD_NOT_FOUND, "Method not found: %s" % method)
        except ArgumentError as e:
            return None if is_notification else self._error(request_id, INVALID_PARAMS, str(e))
        except Exception as e:
            Logger.logException("e", "[AIAssistant] MCP %s failed", method)
            return None if is_notification else self._error(request_id, INTERNAL_ERROR, str(e))
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # -- methods -------------------------------------------------------------
    def _initialize(self, params):
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }

    def callTool(self, name, arguments, ctx=None):
        """Run a tool and wrap the outcome as an MCP CallToolResult."""
        if ctx is None:
            ctx = ToolContext(self._services, caller="mcp")
        if self._registry.get(name) is None:
            raise ArgumentError("Unknown tool: %s" % name)
        try:
            result = self._registry.call(name, arguments, ctx)
        except ArgumentError as e:
            return self._toolError("Ошибка аргументов: %s" % e)
        except ToolError as e:
            payload = {"error": e.message}
            if e.hint:
                payload["hint"] = e.hint
            return self._toolError(payload)
        except Exception as e:
            Logger.logException("e", "[AIAssistant] tool %s crashed", name)
            return self._toolError({"error": "Внутренняя ошибка инструмента: %s" % e,
                                    "trace": traceback.format_exc().splitlines()[-3:]})
        data = result.data
        content = [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=1)}]
        for image_bytes, mime in result.images:
            content.append({"type": "image", "data": base64.b64encode(image_bytes).decode("ascii"), "mimeType": mime})
        response = {"content": content, "isError": False}
        if isinstance(data, dict):
            response["structuredContent"] = data
        return response

    def _toolError(self, payload):
        if not isinstance(payload, dict):
            payload = {"error": str(payload)}
        return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "isError": True, "structuredContent": payload}

    @staticmethod
    def _error(request_id, code, message):
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class InProcessMcpClient(object):
    """The agent's MCP client: JSON-RPC messages go through McpServer.handle() in-process."""

    def __init__(self, server):
        self._server = server
        self._next_id = 0
        self._lock = threading.Lock()

    def _request(self, method, params=None, ctx=None):
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        # Round-trip through JSON so the agent sees exactly what an external client would.
        message = json.loads(json.dumps(message))
        response = self._server.handle(message, ctx)
        if response is None:
            return None
        if "error" in response:
            raise ArgumentError(response["error"]["message"])
        return response["result"]

    def initialize(self):
        return self._request("initialize", {"protocolVersion": SUPPORTED_VERSIONS[0],
                                            "clientInfo": {"name": "ai-assistant-agent", "version": "1.0.0"},
                                            "capabilities": {}})

    def listTools(self):
        return self._request("tools/list")["tools"]

    def callTool(self, name, arguments, ctx=None):
        return self._request("tools/call", {"name": name, "arguments": arguments}, ctx)
