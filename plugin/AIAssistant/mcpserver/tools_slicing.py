"""Tools: slicing, results, export, variant comparison."""
import os
import time

from ..core.errors import ToolError
from ..core.qt_thread import is_main_thread, run_on_main
from ..slicer import api, profiles, settings, slicing
from .registry import prop, props

PATH_PROPS = (
    ("path", prop("string", "Полный путь к файлу (необязательно)")),
    ("directory", prop("string", "Папка (по умолчанию — папка вывода из настроек ассистента)")),
    ("filename", prop("string", "Имя файла без пути (по умолчанию — имя задания)")),
)


def _resolve_output(ctx, args, extension):
    path = args.get("path")
    if path:
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(path):
            directory, filename = path, None
        else:
            if not path.lower().endswith(extension):
                path += extension
            os.makedirs(os.path.dirname(path), exist_ok=True)
            return path
    else:
        directory = args.get("directory") or ctx.services.config.outputDir()
        filename = args.get("filename")
    directory = os.path.abspath(os.path.expanduser(directory))
    filename = filename or run_on_main(slicing.default_basename)
    if not filename.lower().endswith(extension):
        filename += extension
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, filename)


def _slice_and_wait(ctx, timeout):
    if is_main_thread():
        raise ToolError("Ожидание нарезки нельзя выполнять в главном потоке.")
    monitor = ctx.services.monitor
    run_on_main(monitor.start)
    state = monitor.wait(timeout=timeout, cancel_event=ctx.cancel_event)
    status = monitor.status()
    if state == "done":
        time.sleep(0.4)  # PrintInformation is updated right after the Done state
        status["result"] = run_on_main(slicing.slice_result)
    elif state == "timeout":
        status["error"] = "Нарезка не завершилась за %d с." % timeout
    elif state in ("error", "disabled"):
        status["error"] = monitor.error or "Слайсинг завершился с ошибкой."
    return state, status


def _capture_state():
    stack = api.global_stack()
    stacks = [stack] + api.extruder_stacks(stack)
    user = []
    for target in stacks:
        values = {}
        for key in target.userChanges.getAllKeys():
            values[key] = target.userChanges.getProperty(key, "value")
        user.append(values)
    mm = api.app().getMachineManager()
    custom = stack.qualityChanges.getName() if stack.qualityChanges.getId() != "empty_quality_changes" else None
    return {"user": user, "quality_type": mm.activeQualityType, "custom": custom}


def _restore_state(state):
    stack = api.global_stack()
    mm = api.app().getMachineManager()
    if state["custom"]:
        if stack.qualityChanges.getName() != state["custom"]:
            profiles.set_quality_profile(custom_profile=state["custom"])
    elif mm.activeQualityType != state["quality_type"] or stack.qualityChanges.getId() != "empty_quality_changes":
        profiles.set_quality_profile(quality_type=state["quality_type"])
    stacks = [stack] + api.extruder_stacks(stack)
    for target, values in zip(stacks, state["user"]):
        for key in list(target.userChanges.getAllKeys()):
            if key not in values:
                target.userChanges.removeInstance(key)
        for key, value in values.items():
            target.userChanges.setProperty(key, "value", value)


def _apply_variant(variant):
    applied = {}
    if variant.get("printing_mode"):
        applied["printing_mode"] = settings.set_printing_mode(variant["printing_mode"])["new"]
    if variant.get("quality_type"):
        applied["quality"] = profiles.set_quality_profile(quality_type=variant["quality_type"])["quality_type"]
    if variant.get("settings"):
        result = settings.set_settings(variant["settings"], variant.get("extruder"))
        if result.get("errors"):
            applied["errors"] = result["errors"]
    return applied


def register(registry):

    @registry.add("start_slice", "Нарезать",
                  "Запустить нарезку (слайсинг) текущей сцены активным бэкендом (3D или 5D) и по умолчанию дождаться "
                  "результата: время печати, расход материала. При ошибке вернёт её текст.",
                  props(("wait", prop("boolean", "Ждать завершения (по умолчанию true)")),
                        ("timeout_s", prop("integer", "Максимальное ожидание, с (по умолчанию 900)"))),
                  group="slicing")
    def start_slice(args, ctx):
        if not args.get("wait", True):
            run_on_main(ctx.services.monitor.start)
            return ctx.services.monitor.status()
        state, status = _slice_and_wait(ctx, int(args.get("timeout_s", 900)))
        if state not in ("done",):
            if state in ("error", "timeout", "disabled", "interrupted"):
                raise ToolError(status.get("error") or "Нарезка не выполнена (%s)." % state)
        return status

    @registry.add("get_slice_status", "Статус нарезки", "Состояние и прогресс нарезки.", group="slicing")
    def get_slice_status(args, ctx):
        return ctx.services.monitor.status()

    @registry.add("get_slice_result", "Результат нарезки",
                  "Время печати, длина и масса материала по экструдерам, время по типам линий (после нарезки).",
                  group="slicing")
    def get_slice_result(args, ctx):
        status = ctx.services.monitor.status()
        result = run_on_main(slicing.slice_result)
        result["slice_state"] = status["state"]
        if status["state"] != "done":
            result["warning"] = "Последняя нарезка не завершена или устарела — выполните start_slice."
        return result

    @registry.add("export_gcode", "Экспорт G-code",
                  "Сохранить G-code последней нарезки в файл. Без пути — в папку вывода ассистента под именем задания.",
                  props(*PATH_PROPS), group="slicing")
    def export_gcode(args, ctx):
        if ctx.services.monitor.state != "done":
            raise ToolError("Нет актуального результата нарезки.", hint="Сначала выполните start_slice.")
        path = _resolve_output(ctx, args, ".gcode")
        result = slicing.export_gcode(path)  # worker thread on purpose, see slicing._require_worker_thread
        ctx.services.bus.publish("file_saved", {"kind": "gcode", "path": result["path"]})
        return result

    @registry.add("save_project_3mf", "Сохранить проект 3MF",
                  "Сохранить проект (модели, их положение, принтер, профиль, материал и все настройки) в 3MF.",
                  props(*PATH_PROPS), group="slicing")
    def save_project_3mf(args, ctx):
        path = _resolve_output(ctx, args, ".3mf")
        result = slicing.save_project(path)  # worker thread on purpose, see slicing._require_worker_thread
        ctx.services.bus.publish("file_saved", {"kind": "3mf", "path": result["path"]})
        return result

    @registry.add("compare_variants", "Сравнить варианты",
                  "Нарезать 2–4 варианта настроек и сравнить время печати и расход материала. Каждый вариант: "
                  "{name, settings?, quality_type?, printing_mode?, extruder?}. После сравнения восстанавливаются "
                  "исходные настройки или применяется лучший вариант (apply: \"best_time\" | \"best_material\").",
                  props(("variants", prop("array", "Список вариантов", items={"type": "object"})),
                        ("apply", prop("string", "Что оставить после сравнения",
                                       enum=["original", "best_time", "best_material"]))),
                  ["variants"], group="slicing")
    def compare_variants(args, ctx):
        variants = args["variants"]
        if not isinstance(variants, list) or not 2 <= len(variants) <= 4:
            raise ToolError("Нужно от 2 до 4 вариантов.")
        original = run_on_main(_capture_state)
        rows = []
        try:
            for index, variant in enumerate(variants):
                if ctx.cancelled():
                    break
                if not isinstance(variant, dict):
                    raise ToolError("Каждый вариант должен быть объектом.")
                name = variant.get("name") or "Вариант %d" % (index + 1)
                ctx.progress("Вариант %d/%d: %s" % (index + 1, len(variants), name))
                run_on_main(_restore_state, original)
                applied = run_on_main(_apply_variant, variant)
                state, status = _slice_and_wait(ctx, 900)
                row = {"name": name, "applied": applied, "state": state}
                if state == "done":
                    result = status["result"]
                    row["print_time"] = result["print_time"]
                    row["print_time_seconds"] = result["print_time_seconds"]
                    row["total_weight_g"] = result["total_weight_g"]
                    row["material_length_m"] = result["material_length_m"]
                else:
                    row["error"] = status.get("error")
                rows.append(row)
        finally:
            run_on_main(_restore_state, original)
        done = [r for r in rows if r["state"] == "done"]
        summary = {"variants": rows}
        if done:
            summary["fastest"] = min(done, key=lambda r: r["print_time_seconds"])["name"]
            summary["least_material"] = min(done, key=lambda r: r["total_weight_g"])["name"]
        choice = args.get("apply", "original")
        if choice in ("best_time", "best_material") and done:
            best_name = summary["fastest"] if choice == "best_time" else summary["least_material"]
            best = [v for i, v in enumerate(variants) if (v.get("name") or "Вариант %d" % (i + 1)) == best_name][0]
            run_on_main(_apply_variant, best)
            summary["applied"] = best_name
        else:
            summary["applied"] = "исходные настройки"
        summary["note"] = "Результат нарезки в сцене относится к последнему варианту — перед экспортом выполните start_slice."
        ctx.services.monitor.state = "stale"
        ctx.services.bus.publish("slicer_changed", {})
        return summary
