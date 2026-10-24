"""AI Assistant extension: wires the services together and adds the menu.

    SPA (web/) --HTTP+SSE or QWebChannel--> Router --> ChatSession (LLM agent loop)
                                                   +-> McpServer --> tools --> STE Slicer API
"""
import json
import os
import threading
import webbrowser

from PyQt5.QtCore import QObject, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QGuiApplication

from UM.Extension import Extension
from UM.Logger import Logger
from UM.Message import Message

from steslicer.SteSlicerApplication import SteSlicerApplication

from .agent.agent import ChatSession
from .agent.llm_client import build_ca_bundle
from .core.config import Config, data_dir
from .core.events import BlobStore, EventBus
from .core.qt_thread import is_main_thread, run_on_main
from .core.uploads import UploadStore
from .mcpserver.server import InProcessMcpClient, McpServer
from .mcpserver.tools import build_registry
from .server.http_server import HttpServer, Router, bridge_request
from .slicer import api, scene, settings, slicing


class Services(object):
    """Everything a tool or request handler may need."""

    def __init__(self, app, config, bus, blobs, uploads, data_path, ca_bundle):
        self.app = app
        self.config = config
        self.bus = bus
        self.blobs = blobs
        self.uploads = uploads
        self.data_dir = data_path
        self.ca_bundle = ca_bundle
        self.monitor = None
        self.registry = None
        self.analysis_cache = {}

    def savePreferences(self):
        self.app.savePreferences()

    def collectState(self):
        """Header/side-panel summary for the SPA (main thread)."""
        stack = self.app.getGlobalContainerStack()
        if stack is None:
            return {"machine": None}
        mode = settings.printing_mode_info(stack)
        manager = self.app.getMachineManager()
        extruders = []
        for index, ext in enumerate(api.extruder_stacks(stack)):
            material = settings.material_summary(ext) or {}
            extruders.append({"index": index, "material": material.get("name"), "material_id": material.get("id"),
                              "enabled": bool(getattr(ext, "isEnabled", True))})
        objects = []
        for node in api.scene_objects():
            try:
                info = scene.object_info(node)
                objects.append({"id": info["id"], "name": info["name"], "size": info["size"],
                                "outside": info["outside_build_area"]})
            except Exception:
                continue
        state = {
            "machine": stack.getName(),
            "definition": stack.definition.getId(),
            "five_axis": mode["five_axis_machine"],
            "printing_mode": mode["value"],
            "printing_mode_label": mode["label"],
            "quality": manager.activeQualityOrQualityChangesName,
            "layer_height": settings._round(stack.getProperty("layer_height", "value")),
            "extruders": extruders,
            "objects": objects,
            "slice": self.monitor.status() if self.monitor else None,
        }
        if self.monitor is not None and self.monitor.state == "done":
            try:
                state["result"] = slicing.slice_result()
            except Exception:
                pass
        return state

    def slicerState(self):
        try:
            return run_on_main(self.collectState, _timeout=5)
        except Exception as e:
            return {"machine": None, "error": str(e)}


class Bridge(QObject):
    """QWebChannel transport for an embedded QWebEngineView (used only if QtWebEngine exists)."""

    response = pyqtSignal(str)
    event = pyqtSignal(str)

    def __init__(self, router, bus, parent=None):
        QObject.__init__(self, parent)
        self._router = router
        bus.addListener(self._onBusEvent)

    def _onBusEvent(self, event):
        SteSlicerApplication.getInstance().callLater(self.event.emit, json.dumps(event, ensure_ascii=False))

    @pyqtSlot(str)
    def request(self, payload_json):
        payload = json.loads(payload_json)

        def work():
            result = bridge_request(self._router, payload)
            result["id"] = payload.get("id")
            SteSlicerApplication.getInstance().callLater(self.response.emit, json.dumps(result, ensure_ascii=False))
        thread = threading.Thread(target=work, name="AIAssistantBridge")
        thread.daemon = True
        thread.start()


class AIAssistant(QObject, Extension):
    def __init__(self, parent=None):
        QObject.__init__(self, parent)
        Extension.__init__(self)
        self.setMenuName("AI Assistant")
        self.addMenuItem("Открыть чат", self.openChat)
        self.addMenuItem("Настройки LLM", self.openSettings)
        self.addMenuItem("Подключение внешнего MCP-клиента", self.showMcpInfo)

        self._app = SteSlicerApplication.getInstance()
        self._services = None
        self._session = None
        self._router = None
        self._http = None
        self._view = None
        self._bridge = None
        self._embedded = None
        self._last_state = None
        self._state_timer = None
        self._start_error = None

        self._app.initializationFinished.connect(self._onInitialized)
        self._app.applicationShuttingDown.connect(self._onShutdown)

    # -- lifecycle -------------------------------------------------------------
    def _onInitialized(self):
        try:
            self._start()
        except Exception as e:
            self._start_error = str(e)
            Logger.logException("e", "[AIAssistant] failed to start")
            Message("Не удалось запустить AI Assistant: %s" % e, lifetime=0, title="AI Assistant").show()

    def _start(self):
        config = Config(self._app.getPreferences())
        data_path = data_dir()
        bus = EventBus()
        uploads = UploadStore(os.path.join(data_path, "uploads"))
        ca_bundle = build_ca_bundle(os.path.join(data_path, "ca_bundle.pem"))
        services = Services(self._app, config, bus, BlobStore(), uploads, data_path, ca_bundle)
        services.monitor = slicing.SliceMonitor(bus)
        services.monitor.setup()
        services.registry = build_registry()
        mcp = McpServer(services.registry, services, bus)
        self._session = ChatSession(services, InProcessMcpClient(mcp), services.registry)
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
        self._router = Router(services, self._session, mcp, web_dir)
        self._http = HttpServer(self._router)
        port = self._http.start(config.get("port"))
        self._services = services

        self._state_timer = QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.setInterval(400)
        self._state_timer.timeout.connect(self._publishState)
        self._connectStateSignals()
        bus.addListener(self._onBusEvent)

        message = Message("Чат-ассистент запущен: http://127.0.0.1:%d\nMCP-сервер: http://127.0.0.1:%d/mcp" % (port, port),
                          lifetime=20, title="AI Assistant")
        message.addAction("open_chat", "Открыть чат", "", "")
        message.actionTriggered.connect(self._onMessageAction)
        message.show()
        self._startup_message = message
        if config.get("open_browser_on_start"):
            self.openChat()

    def _onShutdown(self):
        if self._session is not None:
            self._session.cancel()
        if self._http is not None:
            self._http.stop()

    # -- slicer state for the SPA header ------------------------------------
    def _connectStateSignals(self):
        manager = self._app.getMachineManager()
        for name in ("globalContainerChanged", "activeQualityGroupChanged", "activeQualityChangesGroupChanged",
                     "activeMaterialChanged", "rootMaterialChanged", "activeStackValueChanged"):
            signal = getattr(manager, name, None)
            if signal is not None:
                try:
                    signal.connect(self._scheduleState)
                except Exception:
                    pass
        self._app.globalContainerStackChanged.connect(self._scheduleState)
        self._app.getController().getScene().sceneChanged.connect(self._scheduleState)
        self._scheduleState()

    def _scheduleState(self, *args):
        if self._state_timer is None:
            return
        if is_main_thread():
            self._state_timer.start()
        else:
            self._app.callLater(self._state_timer.start)

    def _onBusEvent(self, event):
        if event["type"] in ("slicer_changed", "slice_state"):
            self._scheduleState()

    def _publishState(self):
        try:
            state = self._services.collectState()
        except Exception as e:
            Logger.log("w", "[AIAssistant] state collection failed: %s", e)
            return
        encoded = json.dumps(state, sort_keys=True, ensure_ascii=False)
        if encoded != self._last_state:
            self._last_state = encoded
            self._services.bus.publish("slicer_state", state)

    # -- UI entry points ---------------------------------------------------------
    def _url(self, fragment=""):
        if self._http is None:
            return None
        return self._http.url(fragment)

    def _tryEmbedded(self):
        if self._embedded is not None:
            return self._embedded
        try:
            from PyQt5.QtWebEngineWidgets import QWebEngineView  # optional: not shipped with STE Slicer 7.10
            from PyQt5.QtWebChannel import QWebChannel  # optional
        except Exception as e:
            Logger.log("i", "[AIAssistant] QtWebEngine is not available (%s): using the system browser", e)
            self._embedded = False
            return False
        self._view = QWebEngineView()
        self._view.setWindowTitle("AI Assistant — STE Slicer")
        channel = QWebChannel(self._view.page())
        self._bridge = Bridge(self._router, self._services.bus)
        channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(channel)
        self._view.load(QUrl(self._url("?transport=qt")))
        self._view.resize(520, 780)
        self._embedded = True
        return True

    def openChat(self, fragment=""):
        if self._http is None:
            Message("AI Assistant не запущен: %s" % (self._start_error or "сервер недоступен"),
                    lifetime=10, title="AI Assistant").show()
            return
        if self._tryEmbedded():
            if fragment:
                self._view.load(QUrl(self._url("?transport=qt" + fragment)))
            self._view.show()
            self._view.raise_()
            return
        webbrowser.open(self._url(fragment))

    def openSettings(self):
        self.openChat("#settings")

    def showMcpInfo(self):
        url = self._url("mcp")
        if url is None:
            return
        QGuiApplication.clipboard().setText(url)
        config = {"mcpServers": {"ste-slicer": {"type": "http", "url": url}}}
        Message("Адрес MCP-сервера скопирован в буфер обмена:\n%s\n\nКонфигурация для MCP-клиента:\n%s"
                % (url, json.dumps(config, indent=2)), lifetime=30, title="AI Assistant — MCP").show()

    def _onMessageAction(self, message, action_id):
        if action_id == "open_chat":
            message.hide()
            self.openChat()
