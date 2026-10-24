"""Tools: models on the build plate."""
import time

from ..core.errors import ToolError
from ..core.qt_thread import is_main_thread, run_on_main
from ..slicer import api, scene, settings
from .registry import prop, props

OBJECT_PROP = prop("string", "id объекта из list_objects (obj1, obj2...). Можно не указывать, если в сцене одна модель.")


def _object_call(func, args, *extra):
    def call():
        node = api.registry.get(args.get("object_id"))
        return func(node, *extra)
    return run_on_main(call)


def _wait_until_stable(probe, timeout, quiet=1.0, ctx=None):
    """Poll probe() (run on the main thread) until its value stops changing."""
    deadline = time.time() + timeout
    last = run_on_main(probe)
    last_change = time.time()
    while time.time() < deadline:
        if ctx is not None and ctx.cancelled():
            break
        time.sleep(0.25)
        value = run_on_main(probe)
        if value != last:
            last = value
            last_change = time.time()
        elif time.time() - last_change >= quiet:
            break
    return last


def register(registry):

    @registry.add("load_model", "Загрузить модель",
                  "Загрузить файл модели (STL, OBJ, 3MF...) в сцену и дождаться загрузки. "
                  "Пути вложений пользователя указаны в его сообщении. Возвращает id, размеры, объём новых объектов.",
                  props(("path", prop("string", "Полный путь к файлу модели"))), ["path"], group="scene")
    def load_model(args, ctx):
        if is_main_thread():
            raise ToolError("load_model нельзя вызывать из главного потока.")
        before = run_on_main(scene.start_load, args["path"])
        deadline = time.time() + 180.0
        found = []
        while time.time() < deadline and not ctx.cancelled():
            time.sleep(0.3)
            found = run_on_main(lambda: [id(n) for n in scene.new_objects(before)])
            if found:
                break
        if not found:
            raise ToolError("Модель не появилась в сцене за отведённое время (ошибка чтения файла?).")
        _wait_until_stable(lambda: (len(scene.new_objects(before)), scene.positions_signature()), 10.0, ctx=ctx)
        objects = run_on_main(scene.describe_new, before)
        mode = run_on_main(settings.printing_mode_info)
        ctx.services.bus.publish("slicer_changed", {})
        result = {"loaded": objects, "printing_mode": mode["value"]}
        for item in objects:
            if any(abs(item["scale"][axis] - 1.0) > 1e-3 for axis in ("x", "y", "z")):
                result["note"] = "Слайсер автоматически масштабировал модель при загрузке — проверьте размеры."
        return result

    @registry.add("list_objects", "Список объектов",
                  "Модели в сцене: id, имя, положение (мм от центра стола, Z — низ модели), габариты, масштаб, "
                  "выход за область печати.", group="scene")
    def list_objects(args, ctx):
        return run_on_main(scene.list_objects)

    @registry.add("get_object_info", "Информация об объекте",
                  "Подробно об объекте: габариты, объём (см³), площадь поверхности, число треугольников, файл.",
                  props(("object_id", OBJECT_PROP)), group="scene")
    def get_object_info(args, ctx):
        return _object_call(lambda node: scene.object_info(node, detailed=True), args)

    @registry.add("move_object", "Переместить",
                  "Переместить объект. Абсолютно: x, y — центр габарита относительно центра стола, z — высота низа "
                  "(0 = на столе). relative=true — сдвиг на величины.",
                  props(("object_id", OBJECT_PROP), ("x", prop("number", "X, мм")), ("y", prop("number", "Y, мм")),
                        ("z", prop("number", "Z низа модели, мм (обычно 0)")),
                        ("relative", prop("boolean", "Сдвиг относительно текущего положения"))),
                  group="scene")
    def move_object(args, ctx):
        return _object_call(scene.move, args, args.get("x"), args.get("y"), args.get("z"), args.get("relative", False))

    @registry.add("rotate_object", "Повернуть",
                  "Повернуть объект: вокруг оси принтера (axis x|y|z, angle в градусах, против часовой при взгляде с конца оси), "
                  "или положить направлением face_down=[x,y,z] вниз на стол, или reset=true — исходная ориентация. "
                  "После поворота модель опускается на стол.",
                  props(("object_id", OBJECT_PROP), ("axis", prop("string", "Ось поворота", enum=["x", "y", "z"])),
                        ("angle", prop("number", "Угол, градусы")),
                        ("face_down", prop("array", "Направление (в текущей ориентации), которое должно смотреть вниз",
                                           items={"type": "number"})),
                        ("reset", prop("boolean", "Сбросить поворот к исходному"))),
                  group="scene")
    def rotate_object(args, ctx):
        if args.get("reset"):
            return _object_call(scene.reset_orientation, args)
        if args.get("face_down") is not None:
            return _object_call(scene.face_down, args, args["face_down"])
        if args.get("axis") and args.get("angle") is not None:
            return _object_call(scene.rotate, args, args["axis"], args["angle"])
        raise ToolError("Укажите axis+angle, face_down или reset=true.")

    @registry.add("scale_object", "Масштабировать",
                  "Масштабировать объект: factor (1.0 — без изменений) или size {x, y, z} в мм "
                  "(при uniform=true — пропорционально по первой указанной оси).",
                  props(("object_id", OBJECT_PROP), ("factor", prop("number", "Коэффициент масштаба")),
                        ("size", prop("object", "Целевой размер, мм: {\"z\": 50}")),
                        ("uniform", prop("boolean", "Пропорционально (по умолчанию true)"))),
                  group="scene")
    def scale_object(args, ctx):
        return _object_call(scene.scale, args, args.get("factor"), args.get("size"), args.get("uniform", True))

    @registry.add("center_object", "В центр стола",
                  "Поставить объект в центр стола (X=0, Y=0) на стол. Обязательно для 5D: ось вращения в центре.",
                  props(("object_id", OBJECT_PROP)), group="scene")
    def center_object(args, ctx):
        return _object_call(scene.center, args)

    @registry.add("arrange_all", "Авто-расстановка",
                  "Автоматически расставить все модели на столе без пересечений (для 3D и нескольких деталей).",
                  group="scene")
    def arrange_all(args, ctx):
        run_on_main(scene.arrange_all)
        time.sleep(0.5)
        _wait_until_stable(scene.positions_signature, 30.0, quiet=1.2, ctx=ctx)
        return run_on_main(scene.list_objects)

    @registry.add("delete_object", "Удалить объект", "Удалить объект из сцены.",
                  props(("object_id", prop("string", "id объекта"))), ["object_id"], group="scene")
    def delete_object(args, ctx):
        result = _object_call(scene.delete, args)
        ctx.services.bus.publish("slicer_changed", {})
        return result

    @registry.add("clear_scene", "Очистить сцену", "Удалить все модели со стола.", group="scene")
    def clear_scene(args, ctx):
        result = run_on_main(scene.clear)
        ctx.services.bus.publish("slicer_changed", {})
        return result
