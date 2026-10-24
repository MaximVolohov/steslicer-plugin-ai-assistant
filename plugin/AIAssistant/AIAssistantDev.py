import json
import os
import threading
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn

from UM.Extension import Extension
from UM.Logger import Logger

from .agent.mcp_server import MCPServer
from .agent.main import get_response

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_uploads")

mcp = MCPServer()


class ChatServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        path = path.split('?', 1)[0].split('#', 1)[0]
        rel = path.lstrip('/')
        return os.path.join(WEB_DIR, rel)

    def do_POST(self):
        Logger.log("i", "POST request to: {}".format(self.path))
        try:
            if self.path == "/api/upload":
                content_type = self.headers.get('Content-Type', '')
                if 'multipart/form-data' not in content_type:
                    self.send_error(400, "Expected multipart/form-data")
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length)
                boundary = None
                for part in content_type.split(';'):
                    if 'boundary=' in part:
                        boundary = part.split('=')[1].strip().strip('"')
                        break
                if not boundary:
                    self.send_error(400, "No boundary found")
                    return
                upload_dir = UPLOAD_DIR
                if not os.path.exists(upload_dir):
                    os.makedirs(upload_dir)
                saved_paths = []
                parts = body.split(b"--" + boundary.encode())
                for part in parts:
                    if b'filename="' in part:
                        filename_start = part.find(b'filename="') + len(b'filename="')
                        filename_end = part.find(b'"', filename_start)
                        try:
                            filename = part[filename_start:filename_end].decode('utf-8')
                        except:
                            filename = "upload_{}.stl".format(len(saved_paths))
                        filename = filename.replace('\\', '_').replace('/', '_')
                        data_start = part.find(b'\r\n\r\n') + 4
                        file_data = part[data_start:]
                        if file_data.endswith(b'\r\n'):
                            file_data = file_data[:-2]
                        filepath = os.path.join(upload_dir, filename)
                        with open(filepath, 'wb') as f:
                            f.write(file_data)
                        saved_paths.append(filepath)
                        Logger.log("i", "File uploaded: {} -> {}".format(filename, filepath))
                response = {"paths": saved_paths}
                body_out = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body_out)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body_out)
            elif self.path == "/mcp":
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length).decode("utf-8"))
                method = req.get("method")
                req_id = req.get("id")
                if method == "initialize":
                    resp = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "ste-slicer-mcp", "version": "1.0"}
                        }
                    }
                elif method == "tools/list":
                    resp = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"tools": mcp.tools_list()}
                    }
                elif method == "tools/call":
                    params = req.get("params", {})
                    result = mcp.tools_call(params.get("name"), params.get("arguments", {}))
                    resp = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": result
                    }
                else:
                    resp = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": "Method not found"}
                    }
                body_out = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body_out)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body_out)
            elif self.path == "/api/message":
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                user_text = data.get("text", "")
                file_names = data.get("files", [])
                reply = ""
                file_loaded = False
                if file_names:
                    filename = file_names[0]
                    filepath = os.path.join(UPLOAD_DIR, filename)
                    if os.path.exists(filepath):
                        result = mcp.tools_call("load_model", {"file_path": filepath})
                        if isinstance(result, dict) and "error" in result:
                            reply += "Ошибка загрузки: {}\n\n".format(result["error"])
                        else:
                            reply += "Модель '{}' отправлена в сцену.\n\n".format(filename)
                        file_loaded = True
                        
                    else:
                        reply += "Файл '{}' не найден в папке загрузок ({}).\n\n".format(filename, UPLOAD_DIR)
                if not file_loaded:
                    ai_reply = get_response(user_text)
                    reply += ai_reply
                body = json.dumps({"reply": reply}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
        except Exception as e:
            Logger.log("e", "POST handler error: {}".format(str(e)))
            error_body = json.dumps({"error": str(e)}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(error_body)


class AIAssistant(Extension):
    def __init__(self):
        super().__init__()
        self.setMenuName("AI Assistant")
        self.addMenuItem("Open chat", self.showChat)
        self._server = None
        if os.path.exists(UPLOAD_DIR):
            for f in os.listdir(UPLOAD_DIR):
                fp = os.path.join(UPLOAD_DIR, f)
                try:
                    if os.path.isfile(fp):
                        os.remove(fp)
                except Exception as e:
                    Logger.log("w", "Не удалось удалить {}: {}".format(fp, str(e)))
        else:
            os.makedirs(UPLOAD_DIR)

    def showChat(self):
        if self._server is None:
            self._server = ChatServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=self._server.serve_forever, daemon=True).start()
        webbrowser.open("http://127.0.0.1:%d/" % self._server.server_port)