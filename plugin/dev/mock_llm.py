"""Scripted OpenAI-compatible LLM for testing the agent loop without a real model.

    python mock_llm.py [--port 8001]
    -> in the plugin settings: API URL http://127.0.0.1:8001/v1, model "mock-agent", any key.

It validates the request shape (system prompt, tools, tool_call/tool pairing,
image parts) and plays a fixed engineer workflow through the real tools:
machine info -> load -> analyze -> mode -> material -> recommendations ->
profile+settings -> orientation -> snapshot -> slice -> export -> summary.
"""
import argparse
import http.server
import json
import re
import time
import uuid


def tool_call(name, **arguments):
    return {"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def validate(request):
    problems = []
    messages = request.get("messages") or []
    if not messages or messages[0].get("role") != "system":
        problems.append("first message must be the system prompt")
    if not request.get("tools"):
        problems.append("no tools in request")
    for tool in request.get("tools") or []:
        if tool.get("type") != "function" or "parameters" not in tool.get("function", {}):
            problems.append("bad tool schema: %s" % tool)
            break
    pending = set()
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            if pending:
                problems.append("assistant message while tool calls %s unanswered" % pending)
            pending = set(c["id"] for c in message.get("tool_calls") or [])
        elif role == "tool":
            if message.get("tool_call_id") not in pending:
                problems.append("tool message for unknown id %s" % message.get("tool_call_id"))
            pending.discard(message.get("tool_call_id"))
        elif role == "user" and pending:
            problems.append("user message while tool calls %s unanswered" % pending)
    return problems


def current_turn(messages):
    """Messages after the last real user message, the user text and whether it carried images."""
    start = 0
    for index, message in enumerate(messages):
        if message.get("role") == "user":
            content = message.get("content")
            text = content if isinstance(content, str) else " ".join(
                p.get("text", "") for p in content if p.get("type") == "text")
            if not text.startswith("[Снимок"):
                start = index
    user = messages[start]
    content = user.get("content")
    if isinstance(content, str):
        text, images = content, 0
    else:
        text = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
        images = sum(1 for p in content if p.get("type") == "image_url")
    return messages[start + 1:], text, images


def results_by_name(turn):
    names = {}
    for message in turn:
        for call in message.get("tool_calls") or []:
            names[call["id"]] = call["function"]["name"]
    results = {}
    for message in turn:
        if message.get("role") == "tool":
            try:
                data = json.loads(message.get("content") or "{}")
            except ValueError:
                data = {"raw": message.get("content")}
            results[names.get(message["tool_call_id"], "?")] = data
    return results


def policy(request):
    turn, text, images = current_turn(request["messages"])
    done = results_by_name(turn)
    steps = sum(1 for m in turn if m.get("role") == "assistant" and m.get("tool_calls"))
    lowered = text.lower()
    for name, data in done.items():
        if isinstance(data, dict) and data.get("error"):
            return "Инструмент **%s** вернул ошибку: %s. (mock-агент останавливается)" % (name, data["error"]), None

    path_match = re.search(r"путь (.+?\.(?:stl|obj|3mf))", text, re.IGNORECASE)
    script = ["get_machine_info", "load_model", "analyze_model", "set_printing_mode", "list_materials",
              "set_material", "get_recommendations", "profile_and_settings", "apply_orientation",
              "get_scene_snapshot", "start_slice", "export"]
    if steps >= len(script):
        return summary(done, images), None
    step = script[steps]
    if step == "get_machine_info":
        return None, [tool_call("get_machine_info")]
    if step == "load_model":
        if not path_match:
            return "Приложите, пожалуйста, 3D-модель (STL/OBJ/3MF) — без неё я могу только рассказать о настройках.", None
        return None, [tool_call("load_model", path=path_match.group(1))]
    object_id = (done.get("load_model") or {}).get("loaded", [{}])[0].get("id")
    if step == "analyze_model":
        return None, [tool_call("analyze_model", object_id=object_id)]
    if step == "set_printing_mode":
        mode = done["analyze_model"]["recommendation"]["printing_mode"]
        if not done["get_machine_info"]["machine"]["five_axis"]:
            mode = "3D"
        return None, [tool_call("set_printing_mode", mode=mode)]
    if step == "list_materials":
        query = "petg" if "petg" in lowered else "abs" if "abs" in lowered else "pla"
        return None, [tool_call("list_materials", extruder=0, query=query)]
    if step == "set_material":
        materials = done["list_materials"]["materials"]
        if not materials:
            return "Подходящий материал не найден.", None
        return None, [tool_call("set_material", material_id=materials[0]["id"], extruder=0)]
    if step == "get_recommendations":
        goal = "strength" if "нагруз" in lowered or "прочн" in lowered else "balanced"
        return None, [tool_call("get_recommendations", goal=goal, no_supports="без поддерж" in lowered)]
    if step == "profile_and_settings":
        advice = done["get_recommendations"]
        return None, [tool_call("set_quality_profile", quality_type=advice["recommended_quality"]),
                      tool_call("set_settings", settings=advice["settings"])]
    if step == "apply_orientation":
        return None, [tool_call("apply_orientation", object_id=object_id)]
    if step == "get_scene_snapshot":
        return None, [tool_call("get_scene_snapshot", view="window")]
    if step == "start_slice":
        return None, [tool_call("start_slice")]
    if step == "export":
        return None, [tool_call("export_gcode"), tool_call("save_project_3mf")]
    return summary(done, images), None


def summary(done, images):
    machine = done.get("get_machine_info", {}).get("machine", {}).get("name", "?")
    mode = done.get("set_printing_mode", {}).get("label", "?")
    material = done.get("set_material", {}).get("name", "?")
    advice = done.get("get_recommendations", {})
    slice_result = (done.get("start_slice") or {}).get("result", {})
    rows = ["| Параметр | Значение | Почему |", "|---|---|---|",
            "| Режим | %s | %s |" % (mode, "; ".join(done.get("analyze_model", {}).get("recommendation", {}).get("reasons", [])[:1])),
            "| Материал | %s | выбран по запросу |" % material,
            "| Профиль | %s | %s |" % (advice.get("recommended_quality", "?"), advice.get("goal_label", ""))]
    for key, value in list(advice.get("settings", {}).items())[:6]:
        rows.append("| `%s` | %s | %s |" % (key, value, advice.get("reasons", {}).get(key, "")))
    text = ["Готово: модель подготовлена на принтере **%s** и нарезана (mock-агент)." % machine, "", "\n".join(rows), "",
            "Время печати: **%s**, материал: **%s г**." % (slice_result.get("print_time", "?"), slice_result.get("total_weight_g", "?")),
            "Файлы: `%s`, `%s`." % (done.get("export_gcode", {}).get("path", "?"), done.get("save_project_3mf", {}).get("path", "?"))]
    if images:
        text.append("(Получено изображений во входном сообщении: %d.)" % images)
    return "\n".join(text)


class Handler(http.server.BaseHTTPRequestHandler):
    def _json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"object": "list", "data": [{"id": "mock-agent", "object": "model"}]})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": {"message": "not found"}})
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8"))
        problems = validate(request)
        if problems:
            print("PROTOCOL PROBLEMS:", problems, flush=True)
            return self._json(400, {"error": {"message": "; ".join(problems)}})
        content, calls = policy(request)
        message = {"role": "assistant", "content": content}
        if calls:
            message["tool_calls"] = calls
        print("step -> %s" % (", ".join(c["function"]["name"] for c in calls) if calls else "final answer"), flush=True)
        self._json(200, {
            "id": "chatcmpl-" + uuid.uuid4().hex[:10], "object": "chat.completion", "created": int(time.time()),
            "model": request.get("model"),
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": len(json.dumps(request)) // 4, "completion_tokens": 50},
        })

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("mock LLM on http://127.0.0.1:%d/v1 (model: mock-agent)" % args.port, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
