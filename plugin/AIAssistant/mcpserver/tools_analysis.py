"""Tools: geometry analysis, orientation, expert recommendations, scene snapshot."""
import numpy

from ..analysis import geometry
from ..core.errors import ToolError
from ..core.qt_thread import run_on_main
from ..knowledge import kb
from ..slicer import api, scene, settings, snapshot
from .registry import ToolResult, prop, props

OBJECT_PROP = prop("string", "id объекта (можно не указывать, если модель одна)")


def _transform_signature(node):
    return tuple(numpy.round(node.getWorldTransformation().getData(), 4).ravel().tolist())


def register(registry):

    @registry.add("analyze_model", "Анализ геометрии",
                  "Анализ модели для подготовки печати: габариты, объём, целостность сетки, кандидаты ориентации "
                  "(площадь свесов, опора, высота), проверка «тело вращения» и рекомендация режима 3D/5D с причинами. "
                  "Вызывайте после загрузки модели, до выбора режима и ориентации.",
                  props(("object_id", OBJECT_PROP)), group="analysis")
    def analyze_model(args, ctx):
        def collect():
            node = api.registry.get(args.get("object_id"))
            verts, faces = api.world_mesh(node)
            stack = api.global_stack()
            reference = api.extruder_stack(settings.used_extruders()[0], stack)
            support_angle = reference.getProperty("support_angle", "value") or 50.0
            five_axis = bool(stack.getProperty("machine_five_axis", "value"))
            base = stack.getProperty("cylindrical_mode_base_diameter", "value") or 6.0
            return (api.registry.idFor(node), node.getName(), verts, faces, float(support_angle),
                    five_axis, float(base), _transform_signature(node))
        object_id, name, verts, faces, support_angle, five_axis, base, signature = run_on_main(collect)
        ctx.progress("Анализ геометрии %s (%d треугольников)" % (name, len(faces)))
        try:
            result = geometry.analyze(verts, faces, support_angle, five_axis, base)
        except ValueError as e:
            raise ToolError("Не удалось проанализировать модель: %s" % e)
        ctx.services.analysis_cache[object_id] = {"result": result, "signature": signature}
        output = {"object_id": object_id, "name": name, "five_axis_machine": five_axis}
        output.update(result)
        output["how_to_apply"] = ("apply_orientation(object_id, candidate_index) кладёт модель выбранной стороной вниз; "
                                  "без candidate_index — рекомендованная ориентация. Для 5D затем set_printing_mode(\"5D\").")
        return output

    @registry.add("apply_orientation", "Применить ориентацию",
                  "Повернуть модель в ориентацию-кандидата из analyze_model (по умолчанию — рекомендованную) "
                  "и поставить на стол; в режиме 5D — ещё и в центр.",
                  props(("object_id", OBJECT_PROP),
                        ("candidate_index", prop("integer", "index кандидата из orientation_candidates"))),
                  group="analysis")
    def apply_orientation(args, ctx):
        def current():
            node = api.registry.get(args.get("object_id"))
            return api.registry.idFor(node), _transform_signature(node)
        object_id, signature = run_on_main(current)
        cached = ctx.services.analysis_cache.get(object_id)
        if cached is None:
            raise ToolError("Для объекта %s ещё нет анализа." % object_id, hint="Сначала вызовите analyze_model.")
        if cached["signature"] != signature:
            raise ToolError("Модель была перемещена или повёрнута после анализа.", hint="Повторите analyze_model.")
        result = cached["result"]
        index = args.get("candidate_index")
        if index is None:
            index = result["recommendation"]["orientation_index"]
        candidates = result["orientation_candidates"]
        matching = [c for c in candidates if c["index"] == int(index)]
        if not matching:
            raise ToolError("Нет кандидата с index=%s." % index)
        candidate = matching[0]

        def apply():
            node = api.registry.get(object_id)
            info = scene.face_down(node, candidate["down_direction"])
            mode = settings.printing_mode_info()["value"]
            if mode != "classic" or candidate.get("revolution_axis_vertical"):
                info = scene.center(node)
            return info
        info = run_on_main(apply)
        del ctx.services.analysis_cache[object_id]
        ctx.services.bus.publish("slicer_changed", {})
        return {"applied_candidate": candidate, "object": info}

    @registry.add("get_recommendations", "Рекомендации эксперта",
                  "Рекомендованные профиль и настройки для цели печати и материала (с обоснованием каждого значения) "
                  "из встроенной базы знаний. Цели: " + ", ".join("%s — %s" % (k, g["label"]) for k, g in kb.GOALS.items()) + ".",
                  props(("goal", prop("string", "Цель печати", enum=list(kb.GOALS.keys()))),
                        ("material_id", prop("string", "id материала (по умолчанию — материал экструдера)")),
                        ("extruder", prop("integer", "Экструдер, по умолчанию 0")),
                        ("no_supports", prop("boolean", "Пользователь просит без поддержек"))),
                  ["goal"], group="analysis")
    def get_recommendations(args, ctx):
        def collect():
            ext = api.extruder_stack(int(args.get("extruder", 0)))
            material = settings.material_summary(ext) or {}
            temperatures = {
                "print_temperature": ext.getProperty("material_print_temperature", "value"),
                "bed_temperature": ext.getProperty("material_bed_temperature", "value"),
            }
            return material, temperatures, settings.printing_mode_info()["value"]
        material, temperatures, mode = run_on_main(collect)
        material_id = args.get("material_id") or material.get("id")
        if args.get("material_id") and args["material_id"] != material.get("id"):
            temperatures = {}  # those of the active material would be misleading
        return kb.recommend(args["goal"], material_id, material.get("type"), mode,
                            bool(args.get("no_supports", False)), temperatures)

    @registry.add("get_scene_snapshot", "Снимок сцены",
                  "PNG-снимок: view=\"model\" — модели крупно, view=\"window\" — окно слайсера со столом. "
                  "Помогает визуально проверить ориентацию и расположение.",
                  props(("view", prop("string", "Что снимать", enum=["model", "window"])),
                        ("width", prop("integer", "Ширина, px (по умолчанию 640)")),
                        ("height", prop("integer", "Высота, px (по умолчанию 480)"))),
                  group="analysis")
    def get_scene_snapshot(args, ctx):
        view = args.get("view", "model")
        png, width, height = run_on_main(snapshot.take, view, args.get("width", 640), args.get("height", 480))
        return ToolResult({"view": view, "width": width, "height": height, "format": "png"},
                          images=[(png, "image/png")])
