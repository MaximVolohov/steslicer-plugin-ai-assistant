/* AI Assistant SPA: chat transcript, tool-call cards, slicer status panel, settings. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = window.Markdown.escape;
  const GROUP_ICONS = { settings: "⚙️", profiles: "🎛️", scene: "📦", slicing: "🔪", analysis: "🔍", misc: "🛠️" };
  const SLICE_STATES = {
    idle: "Не выполнялась", slicing: "Нарезка…", done: "Готово", error: "Ошибка",
    stale: "Результат устарел — настройки или сцена изменились", interrupted: "Прервана",
    cancelled: "Отменена", timeout: "Превышено время ожидания", disabled: "Нарезка недоступна", not_started: "Ожидание"
  };
  const MAX_IMAGE_SIDE = 1568;

  const app = {
    transport: null,
    busy: false,
    items: new Map(),
    tools: {},
    settings: {},
    slice: null,
    pending: [],
    mcpUrl: null,
    reconnectTimer: null,
    localSeq: 0
  };

  // ------------------------------------------------------------------ utils
  function el(tag, className, html) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (html !== undefined) node.innerHTML = html;
    return node;
  }
  function basename(path) { return String(path || "").split(/[\\/]/).pop(); }
  function fmtValue(v) {
    if (typeof v === "number") return String(Math.round(v * 1000) / 1000);
    if (typeof v === "boolean") return v ? "да" : "нет";
    if (v === null || v === undefined) return "—";
    return typeof v === "string" ? v : JSON.stringify(v);
  }
  function truncate(text, n) { text = String(text || ""); return text.length > n ? text.slice(0, n - 1) + "…" : text; }
  function toast(text, isError) {
    const node = el("div", "toast" + (isError ? " err" : ""));
    node.textContent = text;
    $("toasts").appendChild(node);
    setTimeout(() => node.remove(), isError ? 7000 : 4500);
  }
  function nearBottom() {
    const box = $("messages");
    return box.scrollHeight - box.scrollTop - box.clientHeight < 160;
  }
  function scrollBottom(force) {
    const box = $("messages");
    if (force || nearBottom()) box.scrollTop = box.scrollHeight;
  }

  // ------------------------------------------------------------------ boot / sync
  async function boot() {
    app.transport = await window.Transport.create();
    bindUi();
    await resync();
    if (location.hash === "#settings") openSettings();
  }

  async function resync() {
    clearTimeout(app.reconnectTimer);
    try {
      const data = await app.transport.request("GET", "/api/state");
      applyState(data);
      app.transport.connect(data.last_event_id, onEvent, onConnStatus);
    } catch (e) {
      onConnStatus(false);
    }
  }

  function onConnStatus(ok) {
    const conn = $("conn");
    conn.className = "conn " + (ok ? "ok" : "bad");
    conn.querySelector("span").textContent = ok ? "слайсер подключён" : "нет связи со слайсером";
    if (!ok) {
      clearTimeout(app.reconnectTimer);
      app.reconnectTimer = setTimeout(resync, 2500);
    }
  }

  function applyState(data) {
    app.tools = {};
    (data.tools || []).forEach((t) => { app.tools[t.name] = t; });
    app.settings = data.settings || {};
    app.mcpUrl = data.mcp_url;
    $("mcpUrl").textContent = data.mcp_url || "—";
    clearMessages();
    (data.transcript || []).forEach((item) => addItem(item));
    setBusy(!!data.busy);
    updateUsage(data.usage);
    renderSlicer(data.slicer);
    updateSetupHint();
    scrollBottom(true);
  }

  function onEvent(event) {
    const data = event.data || {};
    switch (event.type) {
      case "item": addItem(data); break;
      case "item_update": updateItem(data); break;
      case "item_remove": removeItem(data.id); break;
      case "busy": setBusy(!!data.busy); break;
      case "reset": clearMessages(); break;
      case "slicer_state": renderSlicer(data); break;
      case "slice_state":
      case "slice_progress": renderSliceStatus(data); break;
      case "settings": app.settings = data; updateSetupHint(); break;
      case "usage": updateUsage(data); break;
      case "file_saved": toast("Сохранено: " + data.path); break;
      default: break;
    }
  }

  // ------------------------------------------------------------------ transcript
  function clearMessages() {
    app.items.forEach((entry) => entry.el.remove());
    app.items.clear();
    $("welcome").hidden = false;
  }

  function addItem(item) {
    if (app.items.has(item.id)) return;
    const stick = nearBottom();
    const node = renderItem(item);
    node.dataset.id = item.id;
    app.items.set(item.id, { item: item, el: node });
    $("messages").appendChild(node);
    if (item.kind !== "thinking") $("welcome").hidden = true;
    if (stick || item.kind === "user") scrollBottom(true);
  }

  function updateItem(patch) {
    const entry = app.items.get(patch.id);
    if (!entry) return;
    Object.assign(entry.item, patch);
    const wasOpen = entry.el.classList.contains("open");
    const fresh = renderItem(entry.item);
    fresh.dataset.id = entry.item.id;
    if (wasOpen) fresh.classList.add("open");
    entry.el.replaceWith(fresh);
    entry.el = fresh;
    scrollBottom();
  }

  function removeItem(id) {
    const entry = app.items.get(id);
    if (!entry) return;
    entry.el.remove();
    app.items.delete(id);
  }

  function renderItem(item) {
    switch (item.kind) {
      case "user": return renderUser(item);
      case "assistant": return renderAssistant(item);
      case "tool": return renderTool(item);
      case "thinking": return renderThinking();
      case "error": { const n = el("div", "error-card"); n.textContent = "⚠ " + item.text; return n; }
      default: { const n = el("div", "note"); n.textContent = item.text; return n; }
    }
  }

  function renderUser(item) {
    const wrap = el("div", "msg user");
    const atts = item.attachments || [];
    if (atts.length) {
      const row = el("div", "att-row");
      atts.forEach((a) => {
        if (a.kind === "image") {
          const img = el("img");
          img.src = a.url;
          img.alt = a.name;
          row.appendChild(img);
        } else {
          const chip = el("span", "att-chip");
          chip.textContent = "🧊 " + a.name;
          row.appendChild(chip);
        }
      });
      wrap.appendChild(row);
    }
    if (item.text) {
      const bubble = el("div", "bubble");
      bubble.textContent = item.text;
      wrap.appendChild(bubble);
    }
    return wrap;
  }

  function renderAssistant(item) {
    const wrap = el("div", "msg assistant");
    wrap.appendChild(el("div", "bubble md", window.Markdown.render(item.text)));
    return wrap;
  }

  function renderThinking() {
    return el("div", "thinking", '<span class="dots"><span></span><span></span><span></span></span> ассистент думает');
  }

  function argsSummary(name, a) {
    a = a || {};
    switch (name) {
      case "set_settings":
        return Object.keys(a.settings || {}).map((k) => k + "=" + fmtValue(a.settings[k])).join(", ") +
          (a.extruder !== undefined ? " · экструдер " + a.extruder : "");
      case "get_settings": return (a.keys || []).join(", ");
      case "search_settings": return "«" + (a.query || "") + "»";
      case "load_model": return basename(a.path);
      case "set_printing_mode": return a.mode || "";
      case "set_material": return (a.material_id || "") + (a.extruder ? " · экструдер " + a.extruder : "");
      case "list_materials": return a.query ? "«" + a.query + "»" : "";
      case "set_quality_profile": return a.quality_type || a.custom_profile || "";
      case "get_recommendations": return [a.goal, a.material_id, a.no_supports ? "без поддержек" : ""].filter(Boolean).join(", ");
      case "rotate_object":
        if (a.reset) return "сброс поворота";
        if (a.face_down) return "вниз направлением [" + a.face_down.map((v) => fmtValue(v)).join(", ") + "]";
        return (a.axis || "") + " " + fmtValue(a.angle) + "°";
      case "move_object": return ["x", "y", "z"].filter((k) => a[k] !== undefined).map((k) => k + "=" + fmtValue(a[k])).join(" ");
      case "apply_orientation": return a.candidate_index !== undefined ? "вариант " + a.candidate_index : "рекомендованная";
      case "compare_variants": return (a.variants || []).map((v) => v.name || "?").join(" vs ");
      case "export_gcode":
      case "save_project_3mf": return basename(a.path || a.filename || "");
      default: {
        const text = JSON.stringify(a);
        return text === "{}" ? "" : truncate(text.replace(/^\{|\}$/g, ""), 90);
      }
    }
  }

  function parseResult(item) {
    try { return JSON.parse(item.result); } catch (e) { return null; }
  }

  function resultSummary(item) {
    if (item.status === "running") return item.progress_text || "";
    const d = parseResult(item);
    if (!d) return "";
    if (item.status === "error") return truncate(d.error || "ошибка", 120);
    switch (item.name) {
      case "start_slice": return d.result ? d.result.print_time + " · " + fmtValue(d.result.total_weight_g) + " г" : (d.state || "");
      case "get_slice_result": return (d.print_time || "") + " · " + fmtValue(d.total_weight_g) + " г";
      case "export_gcode":
      case "save_project_3mf": return basename(d.path);
      case "analyze_model": return d.recommendation ? "рекомендовано: " + d.recommendation.printing_mode + ", " + d.recommendation.orientation : "";
      case "load_model": return (d.loaded || []).map((o) => o.id + " " + fmtValue(o.size.x) + "×" + fmtValue(o.size.y) + "×" + fmtValue(o.size.z) + " мм").join(", ");
      case "set_printing_mode": return d.label || "";
      case "set_material": return d.name || "";
      case "set_quality_profile": return (d.name || "") + (d.layer_height ? " · слой " + fmtValue(d.layer_height) + " мм" : "");
      case "set_settings": {
        const ok = (d.applied || []).length, bad = (d.errors || []).length;
        return ok + " применено" + (bad ? ", " + bad + " с ошибкой" : "");
      }
      case "compare_variants": return d.fastest ? "быстрее всего: " + d.fastest : "";
      case "get_machine_info": return d.machine ? d.machine.name + " · " + d.printing_mode.label : "";
      default: return "";
    }
  }

  function renderTool(item) {
    const meta = app.tools[item.name] || {};
    const node = el("div", "tool " + (item.status || "running"));
    const head = el("div", "tool-head");
    head.appendChild(el("span", "tool-icon", GROUP_ICONS[item.group || meta.group] || GROUP_ICONS.misc));
    const title = el("span", "tool-title");
    title.textContent = item.title || meta.title || item.name;
    head.appendChild(title);
    const args = el("span", "tool-args");
    args.textContent = argsSummary(item.name, item.args);
    head.appendChild(args);
    const summary = el("span", "tool-sum");
    summary.textContent = resultSummary(item);
    head.appendChild(summary);
    const status = el("span", "tool-status");
    if (item.status === "running") status.appendChild(el("span", "spinner"));
    else status.textContent = item.status === "error" ? "✕" : "✓";
    head.appendChild(status);
    head.addEventListener("click", () => node.classList.toggle("open"));
    node.appendChild(head);

    if (item.status === "running" && (item.name === "start_slice" || item.name === "compare_variants") && app.slice) {
      const bar = el("div", "progress");
      const fill = el("div");
      fill.style.width = Math.round((app.slice.progress || 0) * 100) + "%";
      bar.appendChild(fill);
      node.appendChild(bar);
    }

    const body = el("div", "tool-body");
    body.appendChild(el("h5", "", "Инструмент"));
    body.appendChild(el("pre", "", esc(item.name)));
    body.appendChild(el("h5", "", "Аргументы"));
    body.appendChild(el("pre", "", esc(JSON.stringify(item.args || {}, null, 2))));
    if (item.result) {
      body.appendChild(el("h5", "", "Результат" + (item.duration_ms ? " · " + (item.duration_ms / 1000).toFixed(1) + " с" : "")));
      const parsed = parseResult(item);
      body.appendChild(el("pre", "", esc(parsed ? JSON.stringify(parsed, null, 2) : item.result)));
    }
    if (item.images && item.images.length) {
      const images = el("div", "tool-images");
      item.images.forEach((url) => { const img = el("img"); img.src = url; images.appendChild(img); });
      body.appendChild(images);
      node.classList.add("open");
    }
    node.appendChild(body);
    return node;
  }

  // ------------------------------------------------------------------ status panel
  function chip(text, className, title) {
    const node = el("span", "chip " + (className || ""));
    node.innerHTML = text;
    if (title) node.title = title;
    return node;
  }

  function renderSlicer(s) {
    const chips = $("chips");
    chips.innerHTML = "";
    if (!s || !s.machine) {
      $("machineInfo").textContent = (s && s.error) ? "Нет данных: " + s.error : "Принтер не выбран";
      return;
    }
    const is3d = s.printing_mode === "classic";
    chips.appendChild(chip("🖨️ " + esc(s.machine), "", s.definition));
    chips.appendChild(chip("<b>" + esc(s.printing_mode_label || s.printing_mode) + "</b>", is3d ? "mode-3d" : "mode-5d", "Режим печати"));
    (s.extruders || []).forEach((e) => {
      if (e.enabled && e.material) chips.appendChild(chip("🧵 " + esc(e.material), "", "Экструдер " + (e.index + 1) + ": " + e.material_id));
    });
    if (s.quality) chips.appendChild(chip("📏 " + esc(s.quality) + (s.layer_height ? " · " + s.layer_height + " мм" : ""), "", "Профиль и высота слоя"));

    const rows = [
      ["Принтер", esc(s.machine)],
      ["Режим", esc(s.printing_mode_label || s.printing_mode) + (s.five_axis ? "" : " (3-осевой)")],
      ["Профиль", esc(s.quality || "—")],
      ["Слой", s.layer_height ? s.layer_height + " мм" : "—"]
    ];
    (s.extruders || []).forEach((e) => {
      rows.push(["Экструдер " + (e.index + 1), esc(e.material || "—") + (e.enabled ? "" : " (выкл.)")]);
    });
    $("machineInfo").className = "kv";
    $("machineInfo").innerHTML = rows.map((r) => "<span>" + r[0] + "</span><span>" + r[1] + "</span>").join("");

    const objects = s.objects || [];
    if (!objects.length) {
      $("objects").className = "muted";
      $("objects").textContent = "Моделей нет";
    } else {
      $("objects").className = "";
      $("objects").innerHTML = objects.map((o) =>
        '<div class="obj"><span>' + esc(o.id + " · " + o.name) + (o.outside ? ' <span class="badge">вне области</span>' : "") +
        '</span><span class="size">' + [o.size.x, o.size.y, o.size.z].map(fmtValue).join("×") + " мм</span></div>").join("");
    }
    if (s.slice) renderSliceStatus(s.slice, s.result);
  }

  function renderSliceStatus(status, result) {
    app.slice = status;
    const label = SLICE_STATES[status.state] || status.state;
    let text = label;
    if (status.state === "slicing") text += " " + Math.round((status.progress || 0) * 100) + "%";
    if (status.state === "error" && status.error) text += ": " + status.error;
    if (status.elapsed_s && status.state !== "idle") text += " · " + status.elapsed_s + " с";
    $("sliceState").textContent = text;
    const bar = $("sliceProgress");
    bar.hidden = status.state !== "slicing";
    bar.firstElementChild.style.width = Math.round((status.progress || 0) * 100) + "%";
    if (result) {
      $("sliceResult").innerHTML = '<div class="big">' + esc(result.print_time) + "</div>" +
        "<div>" + fmtValue(result.total_weight_g) + " г · " +
        (result.material_length_m || []).map((m) => fmtValue(m) + " м").join(" + ") + "</div>";
    } else if (status.state !== "done") {
      $("sliceResult").innerHTML = "";
    }
    app.items.forEach((entry) => {
      const it = entry.item;
      if (it.kind === "tool" && it.status === "running" && (it.name === "start_slice" || it.name === "compare_variants")) {
        const fill = entry.el.querySelector(".progress > div");
        if (fill) fill.style.width = Math.round((status.progress || 0) * 100) + "%";
      }
    });
  }

  function updateUsage(usage) {
    if (!usage) return;
    const total = (usage.prompt_tokens || 0) + (usage.completion_tokens || 0);
    $("usage").textContent = total ? "токены: " + total.toLocaleString("ru-RU") : "";
  }

  function updateSetupHint() {
    let hint = $("setupHint");
    const configured = app.settings.api_base_url && app.settings.model;
    if (configured) { if (hint) hint.remove(); return; }
    if (!hint) {
      hint = el("p", "", "");
      hint.id = "setupHint";
      hint.innerHTML = '⚠ Подключение к LLM не настроено. <button class="link" id="btnSetup">Открыть настройки</button>';
      $("welcome").appendChild(hint);
      $("btnSetup").addEventListener("click", openSettings);
    }
  }

  // ------------------------------------------------------------------ busy / composer
  function setBusy(busy) {
    app.busy = busy;
    $("btnSend").hidden = busy;
    $("btnStop").hidden = !busy;
    refreshSend();
  }

  function refreshSend() {
    const uploading = app.pending.some((p) => p.status === "uploading");
    const empty = !$("input").value.trim() && !app.pending.some((p) => p.status === "done");
    $("btnSend").disabled = app.busy || uploading || empty;
  }

  function autosize() {
    const input = $("input");
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
    refreshSend();
  }

  function kindOf(name) {
    const ext = (name.split(".").pop() || "").toLowerCase();
    if (["png", "jpg", "jpeg", "webp", "gif", "bmp"].indexOf(ext) !== -1) return "image";
    if (["stl", "obj", "3mf", "amf", "ply", "x3d"].indexOf(ext) !== -1) return "model";
    return "file";
  }

  async function prepareImage(file) {
    try {
      const bitmap = await createImageBitmap(file);
      const scale = Math.min(1, MAX_IMAGE_SIDE / Math.max(bitmap.width, bitmap.height));
      if (scale === 1 && file.size < 1.5 * 1024 * 1024) return file;
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(bitmap.width * scale);
      canvas.height = Math.round(bitmap.height * scale);
      canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.9));
      return new File([blob], file.name.replace(/\.\w+$/, "") + ".jpg", { type: "image/jpeg" });
    } catch (e) {
      return file;
    }
  }

  function renderPending() {
    const box = $("attachments");
    box.innerHTML = "";
    app.pending.forEach((p) => {
      const node = el("div", "pending " + p.status);
      if (p.preview) { const img = el("img"); img.src = p.preview; node.appendChild(img); }
      else node.appendChild(el("span", "", "🧊"));
      const name = el("span");
      name.textContent = truncate(p.name, 36) + (p.status === "uploading" ? " · " + Math.round(p.progress * 100) + "%" : "") +
        (p.status === "failed" ? " · ошибка" : "");
      node.appendChild(name);
      const remove = el("button", "", "✕");
      remove.title = "Убрать";
      remove.addEventListener("click", () => {
        app.pending = app.pending.filter((x) => x !== p);
        renderPending();
      });
      node.appendChild(remove);
      box.appendChild(node);
    });
    refreshSend();
  }

  async function addFiles(fileList) {
    const files = Array.prototype.slice.call(fileList || []);
    for (const original of files) {
      const kind = kindOf(original.name);
      if (kind === "file") { toast("Неподдерживаемый файл: " + original.name, true); continue; }
      const file = kind === "image" ? await prepareImage(original) : original;
      const entry = { local: ++app.localSeq, name: file.name, kind: kind, status: "uploading", progress: 0,
                      preview: kind === "image" ? URL.createObjectURL(file) : null, id: null };
      app.pending.push(entry);
      renderPending();
      app.transport.upload(file, file.name, (fraction) => { entry.progress = fraction; renderPending(); })
        .then((data) => { entry.status = "done"; entry.id = data.id; renderPending(); })
        .catch((e) => { entry.status = "failed"; renderPending(); toast("Не удалось загрузить " + file.name + ": " + e.message, true); });
    }
  }

  async function send() {
    if ($("btnSend").disabled) return;
    const text = $("input").value.trim();
    const ready = app.pending.filter((p) => p.status === "done");
    try {
      await app.transport.request("POST", "/api/chat", { text: text, attachments: ready.map((p) => p.id) });
      $("input").value = "";
      app.pending = app.pending.filter((p) => p.status !== "done");
      renderPending();
      autosize();
    } catch (e) {
      toast(e.message, true);
    }
  }

  async function callTool(name, args, okText) {
    try {
      const result = await app.transport.request("POST", "/api/tool", { name: name, arguments: args || {} });
      const data = result.structuredContent || {};
      if (result.isError) toast(data.error || "Ошибка", true);
      else if (okText) toast(okText(data));
    } catch (e) {
      toast(e.message, true);
    }
  }

  // ------------------------------------------------------------------ settings
  function openSettings() {
    const form = $("settingsForm");
    const s = app.settings;
    ["api_base_url", "api_key", "model", "temperature", "max_steps", "request_timeout", "output_dir"].forEach((k) => {
      form.elements[k].value = s[k] !== undefined && s[k] !== null ? s[k] : "";
    });
    ["vision", "verify_ssl", "open_browser_on_start"].forEach((k) => { form.elements[k].checked = !!s[k]; });
    $("testResult").textContent = "";
    $("testResult").className = "test-result";
    $("settingsDialog").showModal();
  }

  function formValues() {
    const form = $("settingsForm");
    return {
      api_base_url: form.elements.api_base_url.value.trim(),
      api_key: form.elements.api_key.value,
      model: form.elements.model.value.trim(),
      temperature: parseFloat(form.elements.temperature.value || "0.2"),
      max_steps: parseInt(form.elements.max_steps.value || "30", 10),
      request_timeout: parseInt(form.elements.request_timeout.value || "180", 10),
      output_dir: form.elements.output_dir.value.trim(),
      vision: form.elements.vision.checked,
      verify_ssl: form.elements.verify_ssl.checked,
      open_browser_on_start: form.elements.open_browser_on_start.checked
    };
  }

  async function testConnection() {
    const box = $("testResult");
    box.className = "test-result";
    box.textContent = "Проверка…";
    try {
      const result = await app.transport.request("POST", "/api/settings/test", formValues());
      if (result.ok) {
        box.className = "test-result ok";
        box.textContent = "✓ Подключение работает. Доступно моделей: " + result.models.length +
          (result.models.length ? " (" + truncate(result.models.slice(0, 6).join(", "), 120) + ")" : "");
        $("modelList").innerHTML = result.models.map((m) => '<option value="' + esc(m) + '">').join("");
      } else {
        box.className = "test-result bad";
        box.textContent = "✕ " + result.error;
      }
    } catch (e) {
      box.className = "test-result bad";
      box.textContent = "✕ " + e.message;
    }
  }

  async function saveSettings() {
    try {
      const result = await app.transport.request("POST", "/api/settings", formValues());
      app.settings = result.settings;
      updateSetupHint();
      $("settingsDialog").close();
      toast("Настройки сохранены");
    } catch (e) {
      toast(e.message, true);
    }
  }

  // ------------------------------------------------------------------ export
  function exportChat() {
    const lines = ["# Диалог с AI Assistant (STE Slicer)", "", "_" + new Date().toLocaleString("ru-RU") + "_", ""];
    app.items.forEach((entry) => {
      const it = entry.item;
      if (it.kind === "user") {
        lines.push("**Пользователь:** " + (it.text || ""));
        (it.attachments || []).forEach((a) => lines.push("> вложение: " + a.name));
      } else if (it.kind === "assistant") {
        lines.push("**Ассистент:**", "", it.text);
      } else if (it.kind === "tool") {
        lines.push("- 🔧 " + (it.title || it.name) + ": " + argsSummary(it.name, it.args) +
          (resultSummary(it) ? " → " + resultSummary(it) : "") + (it.status === "error" ? " (ошибка)" : ""));
      } else if (it.kind === "error") {
        lines.push("> ⚠ " + it.text);
      }
      lines.push("");
    });
    const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
    const link = document.createElement("a");
    const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "");
    link.href = URL.createObjectURL(blob);
    link.download = "chat_" + stamp + ".md";
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 2000);
  }

  // ------------------------------------------------------------------ UI bindings
  function bindUi() {
    const input = $("input");
    input.addEventListener("input", autosize);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        send();
      }
    });
    input.addEventListener("paste", (e) => {
      const files = e.clipboardData && e.clipboardData.files;
      if (files && files.length) { e.preventDefault(); addFiles(files); }
    });
    $("btnSend").addEventListener("click", send);
    $("btnStop").addEventListener("click", () => app.transport.request("POST", "/api/cancel", {}).catch(() => {}));
    $("btnAttach").addEventListener("click", () => $("fileInput").click());
    $("fileInput").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });

    const chat = $("chat");
    let dragDepth = 0;
    chat.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; chat.classList.add("dragging"); });
    chat.addEventListener("dragover", (e) => e.preventDefault());
    chat.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) chat.classList.remove("dragging"); });
    chat.addEventListener("drop", (e) => {
      e.preventDefault();
      dragDepth = 0;
      chat.classList.remove("dragging");
      addFiles(e.dataTransfer.files);
    });

    Array.prototype.forEach.call($("examples").querySelectorAll("button"), (b) => {
      b.addEventListener("click", () => { input.value = b.textContent; autosize(); input.focus(); });
    });

    $("btnSettings").addEventListener("click", openSettings);
    $("btnTest").addEventListener("click", testConnection);
    $("btnSaveSettings").addEventListener("click", saveSettings);
    $("btnCancelSettings").addEventListener("click", () => $("settingsDialog").close());
    $("btnReset").addEventListener("click", () => {
      if (confirm("Начать новый диалог? История переписки будет очищена (настройки слайсера не изменятся).")) {
        app.transport.request("POST", "/api/reset", {}).catch((e) => toast(e.message, true));
      }
    });
    $("btnExport").addEventListener("click", exportChat);
    $("btnPanel").addEventListener("click", () => $("panel").classList.toggle("open"));
    $("btnCopyMcp").addEventListener("click", () => {
      if (!app.mcpUrl) return;
      navigator.clipboard.writeText(app.mcpUrl).then(() => toast("Адрес MCP скопирован"), () => toast(app.mcpUrl));
    });
    $("btnSlice").addEventListener("click", () => callTool("start_slice", { wait: false }));
    $("btnGcode").addEventListener("click", () => callTool("export_gcode", {}, (d) => "G-code: " + d.path));
    $("btnProject").addEventListener("click", () => callTool("save_project_3mf", {}, (d) => "Проект: " + d.path));
    window.addEventListener("hashchange", () => { if (location.hash === "#settings") openSettings(); });
    autosize();
  }

  boot();
})();
