import json
import os
from UM.Logger import Logger
from steslicer.SteSlicerApplication import SteSlicerApplication
from PyQt5.QtCore import QUrl
from . import db

class MCPServer:
    def __init__(self):
        self.tools = {}
        self._register_default_tools()

    def _register_default_tools(self):
        self.register_tool(
            name="get_print_settings",
            description="Получить текущие настройки печати (температура, скорость, материал)",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=self._get_print_settings
        )
        self.register_tool(
            name="set_material_temperature",
            description="Установить температуру сопла и стола для текущего материала",
            parameters={
                "type": "object",
                "properties": {
                    "nozzle_temp": {"type": "integer", "description": "Температура сопла в °C"},
                    "bed_temp": {"type": "integer", "description": "Температура стола в °C"}
                },
                "required": ["nozzle_temp"]
            },
            handler=self._set_material_temperature
        )
        self.register_tool(
            name="start_slicing",
            description="Запустить процесс слайсинга (нарезки)",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=self._start_slicing
        )
        self.register_tool(
            name="get_print_info",
            description="Получить информацию о печати: время, расход материала",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=self._get_print_info
        )
        self.register_tool(
            name="load_model",
            description="Загрузить 3D-модель (STL/OBJ/3MF) в сцену слайсера",
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Путь к файлу модели"}
                },
                "required": ["file_path"]
            },
            handler=self._load_model
        )
        self.register_tool(
            name="set_setting_value",
            description="Изменить конкретную настройку слайсера (например, layer_height, speed_print, infill_sparse_density)",
            parameters={
                "type": "object",
                "properties": {
                    "setting_key": {"type": "string", "description": "Имя настройки, например layer_height"},
                    "setting_value": {"description": "Новое значение"}
                },
                "required": ["setting_key", "setting_value"]
            },
            handler=self._set_setting_value
        )
        self.register_tool(
            name="export_gcode",
            description="Сохранить G-code после слайсинга в файл",
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Куда сохранить .gcode"}
                },
                "required": ["file_path"]
            },
            handler=self._export_gcode
        )
        self.register_tool(
    name="get_slicer_params",
    description="Получить параметры печати из базы данных",
    parameters={
        "type": "object",
        "properties": {
            "case_type": {"type": "string", "description": "Тип детали: крючок, органайзер, ваза, шестерня, шуруп, прототип"},
            "subtype": {"type": "string", "description": "Подтип (необязательно): несущий, для мелочи, декор, герметичная и т.д."}
        },
        "required": ["case_type"]
    },
    handler=self._get_slicer_params
)

    def register_tool(self, name, description, parameters, handler):
        self.tools[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "handler": handler
        }

    def tools_list(self):
        return [
            {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"]
            }
            for tool in self.tools.values()
        ]

    def tools_call(self, tool_name, arguments):
        if tool_name not in self.tools:
            return {"error": "Инструмент '{}' не найден".format(tool_name)}
        try:
            result = self.tools[tool_name]["handler"](arguments)
            return {"status": "success", "result": result}
        except Exception as e:
            Logger.log("e", "Ошибка выполнения инструмента {}: {}".format(tool_name, str(e)))
            return {"status": "error", "message": str(e)}

    def _get_print_settings(self, args):
        app = SteSlicerApplication.getInstance()
        stack = app.getGlobalContainerStack()
        if not stack:
            return {"error": "Не выбран принтер"}
        settings = {
            "material_print_temperature": stack.getProperty("material_print_temperature", "value"),
            "material_bed_temperature": stack.getProperty("material_bed_temperature", "value"),
            "speed_print": stack.getProperty("speed_print", "value"),
            "layer_height": stack.getProperty("layer_height", "value")
        }
        return settings

    def _set_material_temperature(self, args):
        nozzle_temp = args.get("nozzle_temp")
        bed_temp = args.get("bed_temp")
        app = SteSlicerApplication.getInstance()
        stack = app.getGlobalContainerStack()
        if not stack:
            return {"error": "Не выбран принтер"}

        def apply_temps():
            if nozzle_temp is not None:
                stack.userChanges.setProperty("material_print_temperature", "value", nozzle_temp)
            if bed_temp is not None:
                stack.userChanges.setProperty("material_bed_temperature", "value", bed_temp)

        app.callLater(apply_temps)
        msg = "Температура сопла: {}°C".format(nozzle_temp)
        if bed_temp is not None:
            msg += ", стол: {}°C".format(bed_temp)
        return {"message": msg}

    def _set_setting_value(self, args):
        setting_key = args.get("setting_key")
        setting_value = args.get("setting_value")
        app = SteSlicerApplication.getInstance()
        stack = app.getGlobalContainerStack()
        if not stack:
            return {"error": "Не выбран принтер"}

        def apply():
            stack.userChanges.setProperty(setting_key, "value", setting_value)

        app.callLater(apply)
        return {"message": "Настройка {} изменена на {}".format(setting_key, setting_value)}

    def _start_slicing(self, args):
        app = SteSlicerApplication.getInstance()
        backend = app.getBackend()

        def do_slice():
            backend.slice()

        app.callLater(do_slice)
        return {"message": "Слайсинг запущен"}

    def _get_print_info(self, args):
        app = SteSlicerApplication.getInstance()
        print_info = app.getPrintInformation()
        if print_info:
            return {
                "print_time": print_info.currentPrintTime,
                "material_used": print_info.materialAmount
            }
        return {"message": "Сначала запустите слайсинг"}

    def _load_model(self, args):
        file_path = args.get("file_path")
        if not file_path or not os.path.exists(file_path):
            return {"error": "Файл не найден: {}".format(file_path)}
        app = SteSlicerApplication.getInstance()

        def do_load():
            try:
                normalized_path = os.path.normpath(file_path)
                url = QUrl.fromLocalFile(normalized_path)
                Logger.log("i", "Loading URL: {}".format(url.toString()))
                app.readLocalFile(url)
            except Exception as e:
                Logger.log("e", "Load failed: {}".format(str(e)))

        app.callLater(do_load)
        return {"message": "Модель загружается..."}

    def _export_gcode(self, args):
        file_path = args.get("file_path")
        if not file_path:
            return {"error": "Не указан путь для сохранения"}
        app = SteSlicerApplication.getInstance()

        def do_export():
            try:
                from UM.PluginRegistry import PluginRegistry
                writer = PluginRegistry.getInstance().getPluginObject("GCodeWriter")
                if not writer:
                    Logger.log("e", "GCodeWriter plugin not found")
                    return
                with open(file_path, "w") as f:
                    writer.write(f, app.getController().getScene().getRoot())
                Logger.log("i", "G-code exported to {}".format(file_path))
            except Exception as e:
                Logger.log("e", "Export G-code failed: {}".format(str(e)))

        app.callLater(do_export)
        return {"message": "G-code экспортируется в {}".format(file_path)}
    def _get_slicer_params(self, args):
        case_type = args.get("case_type")
        subtype = args.get("subtype")
        if not case_type:
            return {"error": "Не указан тип детали"}
            return db.find_params(case_type, subtype)