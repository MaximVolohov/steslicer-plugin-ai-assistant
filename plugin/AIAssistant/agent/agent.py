"""Chat session: the LLM dialogue loop with tool calling through the plugin's MCP server.

Runs one worker thread per user turn.  Every step is mirrored to the SPA as
transcript items (user / assistant / tool / error / info) over the event bus.
"""
import base64
import json
import os
import threading
import time
import uuid

from UM.Logger import Logger

from ..core.qt_thread import run_on_main
from ..mcpserver.registry import ToolContext
from ..slicer import settings as slicer_settings
from .llm_client import LLMClient, LLMError
from .prompts import system_prompt

TOOL_RESULT_LIMIT = 12000
HISTORY_TURNS = 12


def _new_id():
    return uuid.uuid4().hex[:10]


class ChatSession(object):
    def __init__(self, services, mcp_client, registry):
        self._services = services
        self._bus = services.bus
        self._mcp = mcp_client
        self._registry = registry
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._worker = None
        self._vision_disabled = False
        self._history_path = os.path.join(services.data_dir, "history.json")
        self.messages = []      # OpenAI messages without the system prompt
        self.transcript = []    # UI items
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self._llm = LLMClient(services.config, services.ca_bundle)
        self._load()

    # -- persistence ---------------------------------------------------------
    def _load(self):
        try:
            with open(self._history_path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
            self.messages = data.get("messages", [])
            self.transcript = data.get("transcript", [])
        except (OSError, ValueError):
            pass

    def _save(self):
        messages = [self._stripImages(m, keep=False) for m in self.messages]
        try:
            with open(self._history_path, "w", encoding="utf-8") as stream:
                json.dump({"messages": messages, "transcript": self.transcript[-400:]}, stream, ensure_ascii=False)
        except OSError as e:
            Logger.log("w", "[AIAssistant] cannot save history: %s", e)

    # -- transcript ----------------------------------------------------------
    def _addItem(self, kind, **fields):
        item = {"id": _new_id(), "kind": kind, "ts": time.time()}
        item.update(fields)
        with self._lock:
            self.transcript.append(item)
        self._bus.publish("item", item)
        return item

    def _updateItem(self, item, **fields):
        item.update(fields)
        patch = {"id": item["id"]}
        patch.update(fields)
        self._bus.publish("item_update", patch)

    @property
    def busy(self):
        return self._worker is not None and self._worker.is_alive()

    def state(self):
        with self._lock:
            transcript = list(self.transcript)
        return {"transcript": transcript, "busy": self.busy, "usage": self.usage}

    # -- public API ----------------------------------------------------------
    def submit(self, text, attachment_ids):
        text = (text or "").strip()
        if not text and not attachment_ids:
            raise ValueError("Пустое сообщение.")
        if self.busy:
            raise RuntimeError("Ассистент ещё обрабатывает предыдущий запрос.")
        attachments = []
        for upload_id in attachment_ids or []:
            item = self._services.uploads.get(upload_id)
            if item is not None:
                attachments.append(item)
        self._cancel.clear()
        self._worker = threading.Thread(target=self._runTurn, args=(text, attachments), name="AIAssistantTurn")
        self._worker.daemon = True
        self._worker.start()

    def cancel(self):
        self._cancel.set()

    def reset(self):
        if self.busy:
            self.cancel()
        with self._lock:
            self.messages = []
            self.transcript = []
            self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self._save()
        self._bus.publish("reset", {})

    # -- the turn ------------------------------------------------------------
    def _userContent(self, text, attachments):
        notes = []
        images = []
        for item in attachments:
            if item["kind"] == "model":
                notes.append("- 3D-модель «%s»: путь %s" % (item["name"], item["path"]))
            elif item["kind"] == "image":
                images.append(item)
                notes.append("- изображение «%s» (приложено ниже)" % item["name"])
            else:
                notes.append("- файл «%s»: путь %s" % (item["name"], item["path"]))
        full_text = text or "(без текста)"
        if notes:
            full_text += "\n\n[Вложения]\n" + "\n".join(notes)
        use_vision = images and self._services.config.get("vision") and not self._vision_disabled
        if not use_vision:
            if images:
                full_text += "\n(Изображения не переданы модели: распознавание изображений отключено.)"
            return full_text
        parts = [{"type": "text", "text": full_text}]
        for item in images:
            with open(item["path"], "rb") as stream:
                encoded = base64.b64encode(stream.read()).decode("ascii")
            parts.append({"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (item["mime"], encoded)}})
        return parts

    @staticmethod
    def _stripImages(message, keep):
        content = message.get("content")
        if keep or not isinstance(content, list):
            return message
        stripped = dict(message)
        parts = []
        for part in content:
            if part.get("type") == "image_url":
                parts.append({"type": "text", "text": "[изображение было приложено ранее]"})
            else:
                parts.append(part)
        stripped["content"] = parts
        return stripped

    def _requestMessages(self, slicer_state):
        messages = self.messages
        user_positions = [i for i, m in enumerate(messages) if m["role"] == "user" and not m.get("_synthetic")]
        if len(user_positions) > HISTORY_TURNS:
            messages = messages[user_positions[-HISTORY_TURNS]:]
            user_positions = [i for i, m in enumerate(messages) if m["role"] == "user" and not m.get("_synthetic")]
        last_user = user_positions[-1] if user_positions else 0
        result = [{"role": "system", "content": system_prompt(slicer_state)}]
        for index, message in enumerate(messages):
            keep = index >= last_user and not self._vision_disabled
            clean = dict((k, v) for k, v in self._stripImages(message, keep).items() if not k.startswith("_"))
            result.append(clean)
        return result

    def _repairHistory(self):
        """Every assistant tool_call must be answered by a tool message, or the API rejects the history."""
        repaired = []
        index = 0
        messages = self.messages
        while index < len(messages):
            message = messages[index]
            repaired.append(message)
            index += 1
            if message["role"] == "assistant" and message.get("tool_calls"):
                answered = set()
                while index < len(messages) and messages[index]["role"] == "tool":
                    repaired.append(messages[index])
                    answered.add(messages[index].get("tool_call_id"))
                    index += 1
                for call in message["tool_calls"]:
                    if call["id"] not in answered:
                        repaired.append({"role": "tool", "tool_call_id": call["id"],
                                         "content": "{\"error\": \"вызов прерван\"}"})
        self.messages = repaired

    def _slicerState(self):
        try:
            return run_on_main(slicer_settings.machine_info, _timeout=10)
        except Exception as e:
            return {"error": "состояние недоступно: %s" % e}

    def _toolsForLlm(self):
        return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                  "parameters": t.get("inputSchema", {"type": "object"})}}
                for t in self._mcp.listTools()]

    def _chat(self, slicer_state, tools):
        request = self._requestMessages(slicer_state)
        try:
            return self._llm.chat(request, tools, self._cancel)
        except LLMError as e:
            has_images = any(isinstance(m.get("content"), list) and
                             any(p.get("type") == "image_url" for p in m["content"]) for m in request)
            if has_images and e.status in (400, 413, 415, 422):
                self._vision_disabled = True
                self._addItem("info", text="Модель не принимает изображения — повторяю запрос без них.")
                return self._llm.chat(self._requestMessages(slicer_state), tools, self._cancel)
            raise

    def _runTurn(self, text, attachments):
        self._bus.publish("busy", {"busy": True})
        attachment_view = [{"id": a["id"], "name": a["name"], "kind": a["kind"], "url": "/api/upload/" + a["id"]}
                           for a in attachments]
        self._addItem("user", text=text, attachments=attachment_view)
        try:
            if not self._services.config.isLlmConfigured():
                self._addItem("error", text="LLM не настроен: укажите адрес API и модель в настройках (⚙).")
                return
            self._repairHistory()
            self.messages.append({"role": "user", "content": self._userContent(text, attachments)})
            tools = self._toolsForLlm()
            max_steps = max(1, int(self._services.config.get("max_steps")))
            for step in range(max_steps):
                if self._cancel.is_set():
                    self._addItem("info", text="Остановлено пользователем.")
                    break
                slicer_state = self._slicerState()
                thinking = self._addItem("thinking", text="Думаю…")
                try:
                    message, usage, finish = self._chat(slicer_state, tools)
                finally:
                    self._bus.publish("item_remove", {"id": thinking["id"]})
                    with self._lock:
                        self.transcript = [i for i in self.transcript if i["id"] != thinking["id"]]
                if self._cancel.is_set():
                    self._addItem("info", text="Остановлено пользователем.")
                    break
                for key in ("prompt_tokens", "completion_tokens"):
                    self.usage[key] += int(usage.get(key) or 0)
                self._bus.publish("usage", self.usage)
                content = message.get("content") or ""
                if isinstance(content, list):
                    content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
                tool_calls = self._normalizeToolCalls(message.get("tool_calls") or [])
                assistant = {"role": "assistant", "content": content or None}
                if tool_calls:
                    assistant["tool_calls"] = tool_calls
                self.messages.append(assistant)
                if content.strip():
                    self._addItem("assistant", text=content, final=not tool_calls)
                if not tool_calls:
                    if not content.strip():
                        self._addItem("info", text="Модель вернула пустой ответ.")
                    break
                pending_images = []
                for call in tool_calls:
                    pending_images.extend(self._runTool(call))
                if pending_images:
                    parts = [{"type": "text", "text": "[Снимок сцены из get_scene_snapshot для визуальной проверки]"}]
                    for data, mime in pending_images:
                        parts.append({"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, data)}})
                    self.messages.append({"role": "user", "content": parts, "_synthetic": True})
            else:
                self._addItem("info", text="Достигнут лимит шагов (%d). Напишите «продолжай», чтобы продолжить." % max_steps)
        except LLMError as e:
            self._addItem("error", text=e.message)
        except Exception as e:
            Logger.logException("e", "[AIAssistant] chat turn failed")
            self._addItem("error", text="Внутренняя ошибка ассистента: %s" % e)
        finally:
            self._save()
            self._bus.publish("busy", {"busy": False})

    @staticmethod
    def _normalizeToolCalls(raw_calls):
        calls = []
        for index, call in enumerate(raw_calls):
            function = call.get("function") or {}
            arguments = function.get("arguments")
            if isinstance(arguments, (dict, list)):
                arguments = json.dumps(arguments, ensure_ascii=False)
            calls.append({"id": call.get("id") or "call_%s_%d" % (_new_id(), index), "type": "function",
                          "function": {"name": function.get("name", ""), "arguments": arguments or "{}"}})
        return calls

    def _runTool(self, call):
        """Execute one tool call via MCP; append the tool message; return images for the LLM."""
        name = call["function"]["name"]
        tool = self._registry.get(name)
        try:
            arguments = json.loads(call["function"]["arguments"] or "{}")
            parse_error = None
        except ValueError as e:
            arguments, parse_error = {}, str(e)
        item = self._addItem("tool", call_id=call["id"], name=name, title=tool.title if tool else name,
                             group=tool.group if tool else "misc", args=arguments, status="running")
        started = time.time()
        images = []
        if parse_error:
            result_text = json.dumps({"error": "Аргументы не являются корректным JSON: %s" % parse_error}, ensure_ascii=False)
            is_error = True
        else:
            ctx = ToolContext(self._services, cancel_event=self._cancel, caller="agent",
                              progress=lambda message, fraction=None: self._updateItem(item, progress_text=message))
            try:
                result = self._mcp.callTool(name, arguments, ctx)
                is_error = bool(result.get("isError"))
                texts = []
                for part in result.get("content", []):
                    if part.get("type") == "text":
                        texts.append(part.get("text", ""))
                    elif part.get("type") == "image":
                        images.append((part["data"], part.get("mimeType", "image/png")))
                result_text = "\n".join(texts)
            except Exception as e:
                is_error = True
                result_text = json.dumps({"error": str(e)}, ensure_ascii=False)
        image_urls = []
        for data, mime in images:
            blob_id = self._services.blobs.put(base64.b64decode(data), mime)
            image_urls.append("/api/blob/" + blob_id)
        self._updateItem(item, status="error" if is_error else "ok", result=result_text[:20000],
                         images=image_urls, duration_ms=int((time.time() - started) * 1000))
        if len(result_text) > TOOL_RESULT_LIMIT:
            result_text = result_text[:TOOL_RESULT_LIMIT] + "\n…(обрезано)"
        self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result_text})
        if self._services.config.get("vision") and not self._vision_disabled:
            return images
        return []
