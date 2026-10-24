"""Tools: machine, settings, printing mode, quality profiles, materials."""
import time

from ..core.errors import ToolError
from ..core.qt_thread import run_on_main
from ..slicer import api, profiles, scene, settings
from .registry import prop, props

EXTRUDER_PROP = prop("string", "Номер экструдера (\"0\" — основной) или \"all\". По умолчанию — экструдеры, которыми печатаются модели.")


def register(registry):

    @registry.add("get_machine_info", "Принтер и текущая конфигурация",
                  "Сводка: активный принтер (определение, 5-осевой ли, область печати), режим печати 3D/5D, "
                  "профиль качества, экструдеры с материалами, число моделей и пользовательских изменений. "
                  "Вызывайте в начале работы.", group="settings")
    def get_machine_info(args, ctx):
        return run_on_main(settings.machine_info)

    @registry.add("list_machines", "Список принтеров", "Добавленные в слайсер принтеры (машины) и какой активен.",
                  group="settings")
    def list_machines(args, ctx):
        return run_on_main(settings.list_machines)

    @registry.add("set_active_machine", "Сменить принтер", "Сделать активным другой добавленный принтер (id или имя из list_machines).",
                  props(("machine_id", prop("string", "id или имя принтера"))), ["machine_id"], group="settings")
    def set_active_machine(args, ctx):
        result = run_on_main(settings.set_active_machine, args["machine_id"])
        time.sleep(1.0)
        result["machine"] = run_on_main(settings.machine_info)
        return result

    @registry.add("search_settings", "Поиск настроек",
                  "Найти ключи настроек по словам (рус./англ.: 'заполнение', 'retraction', 'поддержки'...). "
                  "Возвращает ключ, название, текущее значение, активна ли настройка.",
                  props(("query", prop("string", "Слова для поиска")),
                        ("limit", prop("integer", "Максимум результатов (по умолчанию 15)"))),
                  ["query"], group="settings")
    def search_settings(args, ctx):
        return run_on_main(settings.search_settings, args["query"], int(args.get("limit", 15)))

    @registry.add("get_settings", "Прочитать настройки",
                  "Значения настроек по ключам с описанием: единицы, допустимые варианты, пределы, "
                  "откуда взято значение (user/quality/material/definition), для каждого экструдера.",
                  props(("keys", prop("array", "Ключи настроек, например [\"layer_height\", \"infill_sparse_density\"]",
                                      items={"type": "string"})),
                        ("extruder", EXTRUDER_PROP)),
                  ["keys"], group="settings")
    def get_settings(args, ctx):
        return run_on_main(settings.get_settings, args["keys"], args.get("extruder"))

    @registry.add("set_settings", "Изменить настройки",
                  "Записать одну или несколько настроек {ключ: значение} в пользовательские изменения. "
                  "Глобальные настройки пишутся в общий стек, поэкструдерные — в выбранные экструдеры. "
                  "Проверяет тип, варианты enum и допустимые пределы. Машинные machine_* менять нельзя. "
                  "Режим печати лучше менять через set_printing_mode.",
                  props(("settings", prop("object", "Словарь {ключ: значение}, например {\"infill_sparse_density\": 40, \"support_enable\": false}")),
                        ("extruder", EXTRUDER_PROP)),
                  ["settings"], group="settings")
    def set_settings(args, ctx):
        return run_on_main(settings.set_settings, args["settings"], args.get("extruder"))

    @registry.add("reset_settings", "Сбросить настройки",
                  "Удалить пользовательские изменения: указанные ключи или все (режим печати по умолчанию сохраняется).",
                  props(("keys", prop("array", "Ключи для сброса; не указывать — сбросить всё", items={"type": "string"})),
                        ("keep_printing_mode", prop("boolean", "Не сбрасывать printing_mode (по умолчанию true)"))),
                  group="settings")
    def reset_settings(args, ctx):
        return run_on_main(settings.reset_settings, args.get("keys"), args.get("keep_printing_mode", True))

    @registry.add("list_user_changes", "Изменённые настройки",
                  "Все пользовательские изменения (что отличается от профиля) по глобальному стеку и экструдерам.",
                  group="settings")
    def list_user_changes(args, ctx):
        return run_on_main(settings.list_user_changes)

    @registry.add("set_printing_mode", "Режим печати 3D / 5D",
                  "Переключить режим печати: \"3D\" (classic) или \"5D\" (cylindrical_full); также доступны "
                  "cylindrical (5D Spiral), discrete, spherical, spherical_full (экспериментальные). "
                  "5D только для 5-осевых принтеров. При переходе в 5D модели по умолчанию ставятся в центр стола (на ось вращения).",
                  props(("mode", prop("string", "3D, 5D или ключ режима",
                                      enum=["3D", "5D", "classic", "cylindrical_full", "cylindrical", "discrete",
                                            "spherical", "spherical_full"])),
                        ("center_objects", prop("boolean", "Для 5D поставить модели в центр (X=0, Y=0). По умолчанию true"))),
                  ["mode"], group="settings")
    def set_printing_mode(args, ctx):
        result = run_on_main(settings.set_printing_mode, args["mode"])
        if result["new"] != "classic" and args.get("center_objects", True):
            def center_all():
                objects = api.scene_objects()
                for node in objects:
                    scene.center(node)
                return len(objects)
            count = run_on_main(center_all)
            if count:
                result["centered_objects"] = count
            if count > 1:
                result["warning"] = "В 5D печатается одна деталь на оси вращения; в сцене %d объектов." % count
        ctx.services.bus.publish("slicer_changed", {})
        return result

    @registry.add("list_quality_profiles", "Профили качества",
                  "Профили качества для текущего принтера (quality_type, название, высота слоя, доступность) "
                  "и пользовательские профили.", group="profiles")
    def list_quality_profiles(args, ctx):
        return run_on_main(profiles.list_quality_profiles)

    @registry.add("set_quality_profile", "Выбрать профиль",
                  "Активировать профиль качества по quality_type (например draft, fast, normal) "
                  "или пользовательский профиль по имени. Пользовательские изменения сохраняются.",
                  props(("quality_type", prop("string", "quality_type из list_quality_profiles")),
                        ("custom_profile", prop("string", "Имя пользовательского профиля"))),
                  group="profiles")
    def set_quality_profile(args, ctx):
        result = run_on_main(profiles.set_quality_profile, args.get("quality_type"), args.get("custom_profile"))
        ctx.services.bus.publish("slicer_changed", {})
        return result

    @registry.add("list_materials", "Список материалов",
                  "Материалы, доступные для экструдера (id, название, бренд, тип, диаметр, температуры). "
                  "Можно отфильтровать запросом, например \"petg\" или \"Stereotech\".",
                  props(("extruder", prop("integer", "Номер экструдера, по умолчанию 0")),
                        ("query", prop("string", "Фильтр по названию/бренду/типу"))),
                  group="profiles")
    def list_materials(args, ctx):
        return run_on_main(profiles.list_materials, int(args.get("extruder", 0)), args.get("query"))

    @registry.add("set_material", "Выбрать материал",
                  "Установить материал экструдеру по id из list_materials (например generic_petg_175, enduse_petg).",
                  props(("material_id", prop("string", "id материала")),
                        ("extruder", prop("integer", "Номер экструдера, по умолчанию 0"))),
                  ["material_id"], group="profiles")
    def set_material(args, ctx):
        result = run_on_main(profiles.set_material, args["material_id"], int(args.get("extruder", 0)))
        ctx.services.bus.publish("slicer_changed", {})
        return result

    @registry.add("create_profile", "Создать профиль",
                  "Сохранить текущие пользовательские изменения как новый пользовательский профиль и активировать его "
                  "(удобно для повторного использования настроек под задачу/материал).",
                  props(("name", prop("string", "Имя нового профиля"))), ["name"], group="profiles")
    def create_profile(args, ctx):
        name = run_on_main(profiles.begin_create_profile, args["name"])
        deadline = time.time() + 8.0
        while time.time() < deadline:
            time.sleep(0.3)
            activated = run_on_main(profiles.activate_custom_profile, name)
            if activated:
                ctx.services.bus.publish("slicer_changed", {})
                return activated
        raise ToolError("Профиль '%s' создан, но не появился в списке профилей." % name)

    @registry.add("save_profile", "Сохранить профиль",
                  "Записать текущие пользовательские изменения в активный пользовательский профиль.",
                  group="profiles")
    def save_profile(args, ctx):
        return run_on_main(profiles.save_profile)
