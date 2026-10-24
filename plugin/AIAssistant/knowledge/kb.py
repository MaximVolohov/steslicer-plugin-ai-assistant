"""Expert knowledge: material families, print goals and 3D/5D rules.

The numbers are conservative FDM starting points for 0.4 mm nozzles; the
material profile's own temperatures always win (they are not overridden here).
Setting keys are the ones present in this STE Slicer build (fdmprinter.def.json).
"""
import collections

from ..core.errors import ToolError

# family -> description, warping risk, cooling, typical adhesion in 3D, notes
FAMILIES = collections.OrderedDict([
    ("pla", {
        "name": "PLA",
        "about": "жёсткий, простой в печати, низкая термостойкость (~55 °C), хрупкий при ударе",
        "warping": "низкий", "fan": 100, "adhesion_3d": "skirt",
        "notes": ["Лучший выбор для прототипов и макетов."],
    }),
    ("petg", {
        "name": "PETG / CPE",
        "about": "прочный и вязкий, хорошая межслойная адгезия, химстойкий, термостойкость ~70 °C",
        "warping": "низкий", "fan": 40, "adhesion_3d": "brim",
        "notes": ["Сильный обдув снижает межслойную прочность — для нагруженных деталей вентилятор 30–50 %.",
                  "Склонен к нитям: ретракт не отключать."],
    }),
    ("abs", {
        "name": "ABS / ABS-композиты",
        "about": "ударопрочный, термостойкий (~95 °C), заметная усадка",
        "warping": "высокий", "fan": 15, "adhesion_3d": "brim",
        "notes": ["Нужен закрытый/подогреваемый объём, минимальный обдув.",
                  "Для крупных деталей — кайма (brim) 8–10 мм."],
    }),
    ("pa", {
        "name": "Полиамид (PA6, PA12, нейлон)",
        "about": "очень прочный и износостойкий, гибкий, гигроскопичный",
        "warping": "высокий", "fan": 10, "adhesion_3d": "brim",
        "notes": ["Пруток обязательно сушить перед печатью.", "Минимальный обдув, кайма для удержания."],
    }),
    ("pc", {
        "name": "Поликарбонат",
        "about": "очень прочный и термостойкий (~115 °C), высокая усадка",
        "warping": "высокий", "fan": 5, "adhesion_3d": "brim",
        "notes": ["Высокие температуры сопла и стола, без обдува."],
    }),
    ("pp", {
        "name": "Полипропилен",
        "about": "лёгкий, химически стойкий, плохо липнет к столу",
        "warping": "высокий", "fan": 10, "adhesion_3d": "brim",
        "notes": ["Нужна широкая кайма; возможны проблемы адгезии к столу."],
    }),
    ("flex", {
        "name": "Эластомеры (TPU, TPE, SEBS, SBS, TPV)",
        "about": "гибкие, для уплотнений, демпферов, манжет",
        "warping": "низкий", "fan": 50, "adhesion_3d": "skirt",
        "notes": ["Печать медленная (15–25 мм/с), ретракт минимальный.", "Плотность заполнения задаёт жёсткость."],
    }),
    ("pva", {
        "name": "PVA (водорастворимый)",
        "about": "растворимый материал поддержек для второго экструдера",
        "warping": "низкий", "fan": 100, "adhesion_3d": "skirt",
        "notes": ["Используется как материал поддержек (support_extruder_nr на экструдер с PVA)."],
    }),
    ("hips", {
        "name": "HIPS", "about": "лёгкий ударопрочный, растворим в лимонене",
        "warping": "средний", "fan": 20, "adhesion_3d": "brim", "notes": [],
    }),
    ("metal", {
        "name": "Metalcast (металло-/керамонаполненный)",
        "about": "заготовки под отжиг/спекание: сталь 316L, оксидная керамика",
        "warping": "средний", "fan": 0, "adhesion_3d": "brim",
        "notes": ["Для последующего спекания нужно 100 % заполнение и учёт усадки (масштабирование).",
                  "Абразивный материал — закалённое сопло."],
    }),
    ("contifiber", {
        "name": "ContiFiber CPA (непрерывное углеволокно)",
        "about": "армирование непрерывным волокном, только через волоконный экструдер (hfe*-принтеры)",
        "warping": "—", "fan": 0, "adhesion_3d": "—",
        "notes": ["Включается настройкой reinforcement_enabled (доступна, если есть волоконный экструдер).",
                  "Армирование работает в режимах 3D (classic) и 5D (cylindrical_full)."],
    }),
])


def family_of(material_id, material_type=None):
    text = ("%s %s" % (material_id or "", material_type or "")).lower()
    if "contifiber" in text:
        return "contifiber"
    if "metalcast" in text:
        return "metal"
    rules = (("pva", "pva"), ("hips", "hips"), ("petg", "petg"), ("cpe", "petg"),
             ("abspa", "pa"), ("pa6", "pa"), ("pa12", "pa"), ("nylon", "pa"), ("cffpa", "pa"), ("gffpa", "pa"),
             ("tpu", "flex"), ("tpe", "flex"), ("sebs", "flex"), ("sbs", "flex"), ("tpv", "flex"),
             ("abs", "abs"), ("pc", "pc"), ("pp", "pp"), ("pla", "pla"))
    for token, family in rules:
        if token in text:
            return family
    return "pla"


GOALS = collections.OrderedDict([
    ("strength", {
        "label": "прочность / нагрузка",
        "quality": "draft",
        "settings": collections.OrderedDict([
            ("wall_line_count", (4, "4 периметра — основная несущая часть детали при изгибе и кручении")),
            ("top_bottom_thickness", (1.2, "толстые крышки/днища для жёсткости")),
            ("infill_sparse_density", (40, "40 % заполнения — баланс прочности и массы")),
            ("infill_pattern", ("gyroid", "гироид — изотропная прочность во всех направлениях")),
            ("speed_print", (45, "пониженная скорость улучшает спекание слоёв")),
        ]),
    }),
    ("balanced", {
        "label": "универсально",
        "quality": "fast",
        "settings": collections.OrderedDict([
            ("wall_line_count", (3, "3 периметра — запас прочности без лишнего времени")),
            ("infill_sparse_density", (20, "стандартное заполнение 20 %")),
        ]),
    }),
    ("fast", {
        "label": "быстро / черновик",
        "quality": "verydraft",
        "settings": collections.OrderedDict([
            ("wall_line_count", (2, "2 периметра — минимум для черновика")),
            ("infill_sparse_density", (12, "редкое заполнение экономит время и материал")),
            ("speed_print", (70, "повышенная скорость")),
        ]),
    }),
    ("quality", {
        "label": "качество поверхности / детализация",
        "quality": "normal",
        "settings": collections.OrderedDict([
            ("wall_line_count", (3, "3 периметра для ровной поверхности")),
            ("speed_print", (40, "медленнее — чище поверхность")),
            ("z_seam_type", ("sharpest_corner", "шов прячется в острых углах")),
            ("infill_sparse_density", (20, "заполнение не влияет на внешний вид")),
        ]),
    }),
    ("watertight", {
        "label": "герметичность",
        "quality": "fast",
        "settings": collections.OrderedDict([
            ("wall_line_count", (4, "4 периметра исключают сквозные поры")),
            ("top_bottom_thickness", (1.2, "плотные крышки")),
            ("infill_sparse_density", (30, "опора для крышек")),
            ("z_seam_type", ("back", "шов в одной линии сзади — меньше дефектов по периметру")),
        ]),
    }),
    ("lightweight", {
        "label": "лёгкая деталь",
        "quality": "fast",
        "settings": collections.OrderedDict([
            ("wall_line_count", (2, "2 периметра")),
            ("infill_sparse_density", (10, "минимум заполнения")),
            ("infill_pattern", ("gyroid", "гироид держит форму при малой плотности")),
        ]),
    }),
    ("flexible", {
        "label": "гибкая деталь (эластомер)",
        "quality": "fast",
        "settings": collections.OrderedDict([
            ("speed_print", (20, "эластомеры печатают медленно, иначе пруток сминается")),
            ("retraction_amount", (1.0, "минимальный ретракт для мягкого прутка")),
            ("wall_line_count", (3, "периметры задают упругость стенок")),
            ("infill_sparse_density", (25, "плотность регулирует жёсткость")),
        ]),
    }),
])

QUALITY_LAYERS = collections.OrderedDict([
    ("high", 0.06), ("normal", 0.1), ("fast", 0.15), ("draft", 0.2), ("verydraft", 0.3),
    ("coarse", 0.4), ("extra coarse", 0.6),
])

MODE_RULES = [
    "3D (classic) — универсальный режим: плоские, коробчатые, корпусные детали, кронштейны с плоским основанием, всё, что не является телом вращения.",
    "5D (cylindrical_full) — деталь поворачивается вокруг вертикальной оси в центре стола, слои ложатся цилиндрическими оболочками: тела вращения (втулки, ролики, трубы, валы, манжеты, крыльчатки), детали под внутреннее давление/радиальную нагрузку; меньше поддержек, выше прочность поперёк оси.",
    "В 5D модель ставится по центру стола (X=0, Y=0), ось вращения детали — вертикально; ядро вокруг оси (диаметр cylindrical_mode_base_diameter, по умолчанию 6 мм) печатается в 3D, остальное — цилиндрическими слоями.",
    "В 5D адгезия по умолчанию — raft (cylindrical_raft_type=solid): не переопределять без причины.",
    "5D доступен только на пятиосевых принтерах (machine_five_axis=true); на трёхосевых — только 3D.",
    "5D Spiral, Discrete, Spherical — экспериментальные режимы; использовать только по прямому запросу пользователя.",
]


def recommend(goal="balanced", material_id=None, material_type=None, printing_mode="classic",
              no_supports=False, temperatures=None):
    goal_key = (goal or "balanced").strip().lower()
    if goal_key not in GOALS:
        raise ToolError("Неизвестная цель '%s'." % goal, hint="Доступны: " + ", ".join(GOALS.keys()))
    preset = GOALS[goal_key]
    family_key = family_of(material_id, material_type)
    family = FAMILIES[family_key]
    five_d = printing_mode not in ("classic", "3D", "3d")

    settings = collections.OrderedDict()
    reasons = collections.OrderedDict()
    for key, (value, why) in preset["settings"].items():
        settings[key] = value
        reasons[key] = why

    fan = family["fan"]
    if family_key in ("petg",) and goal_key == "strength":
        fan = 30
    settings["cool_fan_speed"] = fan
    reasons["cool_fan_speed"] = "обдув для %s: %d %%" % (family["name"], fan)
    if fan == 0:
        settings["cool_fan_enabled"] = False
        reasons["cool_fan_enabled"] = "материал печатается без обдува"

    if family_key == "flex":
        for key in ("speed_print", "retraction_amount"):
            settings[key], reasons[key] = GOALS["flexible"]["settings"][key]
    if family_key == "metal":
        settings["infill_sparse_density"] = 100
        reasons["infill_sparse_density"] = "для спекания деталь должна быть сплошной"

    warnings = list(family["notes"])
    if not five_d:
        adhesion = family["adhesion_3d"]
        if adhesion in ("skirt", "brim"):
            settings["adhesion_type"] = adhesion
            reasons["adhesion_type"] = ("кайма удерживает углы от отрыва (склонность к короблению: %s)" % family["warping"]
                                        if adhesion == "brim" else "юбка достаточна — материал почти не коробится")
    else:
        warnings.append("Режим 5D: адгезия и цилиндрический raft задаются режимом автоматически.")

    if no_supports:
        settings["support_enable"] = False
        reasons["support_enable"] = "пользователь просит без поддержек — компенсируем ориентацией" + (" и 5D" if five_d else "")
    if family_key == "contifiber":
        settings["reinforcement_enabled"] = True
        reasons["reinforcement_enabled"] = "включить армирование непрерывным волокном"

    return {
        "goal": goal_key,
        "goal_label": preset["label"],
        "material_family": family["name"],
        "material_about": family["about"],
        "printing_mode": printing_mode,
        "recommended_quality": preset["quality"],
        "recommended_layer_height": QUALITY_LAYERS.get(preset["quality"]),
        "settings": settings,
        "reasons": reasons,
        "material_temperatures": temperatures or {},
        "notes": warnings,
        "how_to_apply": "Профиль: set_quality_profile(quality_type=recommended_quality); настройки: set_settings(settings). "
                        "Температуры берутся из профиля материала — меняйте их только при явной причине.",
    }


def prompt_digest():
    lines = ["Материалы (семейства):"]
    for family in FAMILIES.values():
        lines.append("- %s: %s; коробление: %s." % (family["name"], family["about"], family["warping"]))
    lines.append("Линейки Stereotech: Proto (PLA, PVA — прототипы), Enduse (ABS, PA6, PC, PETG, PP, SBS, TPU — "
                 "конечные изделия), Fiberpart (композиты со стекло-/углеволокном: G — стекло, C — углерод, число — %), "
                 "Sealant (эластомеры для уплотнений), Metalcast (под спекание), ContiFiber (непрерывное волокно).")
    lines.append("Профили качества (quality_type → слой): " + ", ".join(
        "%s → %.2f мм" % (k, v) for k, v in QUALITY_LAYERS.items()))
    lines.append("Цели для get_recommendations: " + ", ".join("%s (%s)" % (k, g["label"]) for k, g in GOALS.items()))
    lines.append("Правила выбора режима:")
    lines.extend("- " + rule for rule in MODE_RULES)
    return "\n".join(lines)
