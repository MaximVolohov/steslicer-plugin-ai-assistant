"""Local HTTP server: the chat SPA, its JSON API, server-sent events and the MCP endpoint.

Listens on 127.0.0.1 only.  Host/Origin checks keep web pages from other
origins (and DNS-rebinding) away from the slicer.
"""
import http.server
import io
import json
import mimetypes
import os
import re
import socketserver
import threading
from urllib.parse import parse_qs, urlparse

from UM.Logger import Logger

from ..agent.llm_client import LLMClient, LLMError
from ..core.qt_thread import run_on_main
from ..mcpserver.registry import ToolContext

JSON_LIMIT = 5 * 1024 * 1024
_LOCAL_ORIGIN = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")
_CORS_HEADERS = "Content-Type, Authorization, Mcp-Session-Id, Mcp-Protocol-Version, Last-Event-ID"
# The Windows registry can map .js/.css to odd MIME types; browsers reject stylesheets served wrongly.
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
}


class Response(object):
    def __init__(self, status=200, body=b"", content_type="application/json; charset=utf-8", headers=None):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or {}


def json_response(data, status=200):
    return Response(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def error_response(message, status=400):
    return json_response({"error": message}, status)


class _OverlayConfig(object):
    """Config view with unsaved values from the settings form (for 'test connection')."""

    def __init__(self, config, overrides):
        self._config = config
        self._overrides = overrides

    def get(self, key):
        if key in self._overrides and not (key == "api_key" and self._overrides[key] == self._config.maskedKey()):
            return self._overrides[key]
        return self._config.get(key)


class Router(object):
    def __init__(self, services, session, mcp, web_dir):
        self.services = services
        self.session = session
        self.mcp = mcp
        self.web_dir = os.path.abspath(web_dir)
        self.port = None
        self.stopping = False

    # -- helpers ---------------------------------------------------------------
    @staticmethod
    def _json(body):
        if not body:
            return {}
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("Некорректный JSON")
        if not isinstance(data, dict):
            raise ValueError("Ожидался JSON-объект")
        return data

    def _static(self, path):
        relative = path.lstrip("/") or "index.html"
        full = os.path.abspath(os.path.join(self.web_dir, relative))
        if not full.startswith(self.web_dir + os.sep) or not os.path.isfile(full):
            return error_response("Not found", 404)
        with open(full, "rb") as stream:
            data = stream.read()
        extension = os.path.splitext(full)[1].lower()
        content_type = _STATIC_TYPES.get(extension) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        return Response(200, data, content_type, {"Cache-Control": "no-cache"})

    def mcpUrl(self):
        return "http://127.0.0.1:%d/mcp" % self.port if self.port else None

    def state(self):
        data = self.session.state()
        data["last_event_id"] = self.services.bus.lastId()
        data["settings"] = self.services.config.publicDict()
        data["slicer"] = self.services.slicerState()
        data["mcp_url"] = self.mcpUrl()
        data["tools"] = [{"name": t.name, "title": t.title, "group": t.group} for t in self.services.registry.list()]
        return data

    # -- dispatch ----------------------------------------------------------------
    def handle(self, method, path, query, body_stream, length):
        """Route a request.  body_stream is read lazily (uploads stream to disk)."""
        try:
            if path == "/mcp":
                if method != "POST":
                    return Response(405, b"", headers={"Allow": "POST"})
                text = body_stream.read(length).decode("utf-8") if length else ""
                response = self.mcp.handleText(text)
                if response is None:
                    return Response(202, b"")
                return Response(200, response.encode("utf-8"))

            if not path.startswith("/api/"):
                if method != "GET":
                    return error_response("Method not allowed", 405)
                return self._static(path)

            if method == "POST" and path == "/api/upload":
                name = (query.get("name") or ["file"])[0]
                try:
                    item = self.services.uploads.save(name, body_stream, length)
                except ValueError as e:
                    return error_response(str(e), 413)
                return json_response({"id": item["id"], "name": item["name"], "kind": item["kind"],
                                      "size": item["size"], "url": "/api/upload/" + item["id"]})

            body = body_stream.read(length) if (method == "POST" and length) else b""
            if length > JSON_LIMIT:
                return error_response("Слишком большой запрос", 413)
            data = self._json(body) if method == "POST" else {}

            if method == "GET" and path == "/api/state":
                return json_response(self.state())
            if method == "GET" and path == "/api/settings":
                return json_response(self.services.config.publicDict())
            if method == "GET" and path.startswith("/api/upload/"):
                item = self.services.uploads.get(path.rsplit("/", 1)[-1])
                if item is None:
                    return error_response("Not found", 404)
                with open(item["path"], "rb") as stream:
                    return Response(200, stream.read(), item["mime"], {"Cache-Control": "max-age=3600"})
            if method == "GET" and path.startswith("/api/blob/"):
                blob = self.services.blobs.get(path.rsplit("/", 1)[-1])
                if blob is None:
                    return error_response("Not found", 404)
                return Response(200, blob[1], blob[0], {"Cache-Control": "max-age=3600"})

            if method != "POST":
                return error_response("Not found", 404)
            if path == "/api/chat":
                try:
                    self.session.submit(data.get("text", ""), data.get("attachments") or [])
                except (ValueError, RuntimeError) as e:
                    return error_response(str(e), 409)
                return json_response({"ok": True}, 202)
            if path == "/api/cancel":
                self.session.cancel()
                return json_response({"ok": True})
            if path == "/api/reset":
                self.session.reset()
                return json_response({"ok": True})
            if path == "/api/settings":
                changed = self.services.config.update(data)
                run_on_main(self.services.savePreferences, _timeout=10)
                self.services.bus.publish("settings", self.services.config.publicDict())
                return json_response({"changed": changed, "settings": self.services.config.publicDict()})
            if path == "/api/settings/test":
                client = LLMClient(_OverlayConfig(self.services.config, data), self.services.ca_bundle)
                try:
                    models = client.listModels()
                except LLMError as e:
                    return json_response({"ok": False, "error": e.message})
                return json_response({"ok": True, "models": models})
            if path == "/api/tool":
                name = data.get("name")
                if self.services.registry.get(name) is None:
                    return error_response("Неизвестный инструмент: %s" % name, 404)
                ctx = ToolContext(self.services, caller="ui")
                result = self.mcp.callTool(name, data.get("arguments") or {}, ctx)
                return json_response(result)
            return error_response("Not found", 404)
        except ValueError as e:
            return error_response(str(e), 400)
        except Exception as e:
            Logger.logException("e", "[AIAssistant] HTTP %s %s failed", method, path)
            return error_response("Внутренняя ошибка: %s" % e, 500)


def _make_handler(router):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "STE-AI-Assistant/1.0"

        def log_message(self, format, *args):
            pass

        def _hostAllowed(self):
            host = (self.headers.get("Host") or "").lower()
            hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
            return hostname in ("127.0.0.1", "localhost", "[::1]")

        def _originAllowed(self):
            origin = self.headers.get("Origin")
            if origin is None:
                return True  # non-browser clients (MCP clients, curl)
            return bool(_LOCAL_ORIGIN.match(origin))

        def _cors(self):
            origin = self.headers.get("Origin")
            if origin and _LOCAL_ORIGIN.match(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Headers", _CORS_HEADERS)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Vary", "Origin")

        def _send(self, response):
            try:
                self.send_response(response.status)
                if response.body or response.status not in (202, 204):
                    self.send_header("Content-Type", response.content_type)
                self.send_header("Content-Length", str(len(response.body)))
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self._cors()
                self.end_headers()
                if response.body and self.command != "HEAD":
                    self.wfile.write(response.body)
            except (ConnectionError, OSError):
                pass

        def _dispatch(self, method):
            if not self._hostAllowed():
                return self._send(error_response("Forbidden host", 403))
            if method == "POST" and not self._originAllowed():
                return self._send(error_response("Forbidden origin", 403))
            parsed = urlparse(self.path)
            if method == "GET" and parsed.path == "/api/events":
                return self._events(parse_qs(parsed.query))
            length = int(self.headers.get("Content-Length") or 0)
            response = router.handle(method, parsed.path, parse_qs(parsed.query), self.rfile, length)
            self._send(response)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._send(Response(405, b"", headers={"Allow": "GET, POST"}))

        def do_OPTIONS(self):
            self._send(Response(204, b""))

        def _events(self, query):
            bus = router.services.bus
            try:
                last_id = int(self.headers.get("Last-Event-ID") or (query.get("last") or ["0"])[0])
            except ValueError:
                last_id = 0
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.wfile.write(b"retry: 2000\n\n")
                self.wfile.flush()
                while not router.stopping:
                    events = bus.wait(last_id, 15.0)
                    if not events:
                        self.wfile.write(b": ping\n\n")
                    for event in events:
                        last_id = event["id"]
                        payload = json.dumps(event, ensure_ascii=False)
                        self.wfile.write(("id: %d\ndata: %s\n\n" % (event["id"], payload)).encode("utf-8"))
                    self.wfile.flush()
            except (ConnectionError, OSError):
                return

    return Handler


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let two servers share a port


class HttpServer(object):
    def __init__(self, router):
        self._router = router
        self._server = None
        self._thread = None
        self.port = None

    def start(self, preferred_port):
        last_error = None
        for port in range(int(preferred_port), int(preferred_port) + 20):
            try:
                self._server = _ThreadingServer(("127.0.0.1", port), _make_handler(self._router))
                break
            except OSError as e:
                last_error = e
                self._server = None
        if self._server is None:
            raise last_error or OSError("no free port")
        self.port = self._server.server_address[1]
        self._router.port = self.port
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.5},
                                        name="AIAssistantHttp")
        self._thread.daemon = True
        self._thread.start()
        Logger.log("i", "[AIAssistant] HTTP server listening on http://127.0.0.1:%d", self.port)
        return self.port

    def stop(self):
        self._router.stopping = True
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None

    def url(self, fragment=""):
        return "http://127.0.0.1:%d/%s" % (self.port, fragment)


def bridge_request(router, payload):
    """Serve a request coming over QWebChannel ({method, path, body}) -> JSON-able response dict."""
    import base64
    method = payload.get("method", "GET").upper()
    parsed = urlparse(payload.get("path", "/"))
    body = payload.get("body")
    if payload.get("bodyBase64"):
        raw = base64.b64decode(payload["bodyBase64"])
    elif body is None:
        raw = b""
    else:
        raw = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
    response = router.handle(method, parsed.path, parse_qs(parsed.query), io.BytesIO(raw), len(raw))
    text = response.body.decode("utf-8") if response.content_type.startswith("application/json") else ""
    return {"status": response.status, "body": text}
