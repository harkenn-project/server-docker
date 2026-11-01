"use strict";

// helpers

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "style") Object.assign(node.style, v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(method, url, body) {
  const opts = { method, headers: { "X-Requested-With": "harkenn" } };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (res.status === 401) { location.href = "/admin/login?next=" + encodeURIComponent(location.pathname); }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) throw new Error(errorText(data, res.status));
  return data;
}

function errorText(data, status) {
  if (data && data.detail) {
    if (Array.isArray(data.detail)) {
      return data.detail.map(d => `${(d.loc || []).filter(p => p !== "body").join(".")}: ${d.msg}`).join("\n");
    }
    return String(data.detail);
  }
  return `Request failed (${status})`;
}

function toast(message, isError = false) {
  const t = $("#toast");
  t.textContent = message;
  t.className = isError ? "show error" : "show";
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.className = ""; }, isError ? 6000 : 2500);
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 10) return "just now";
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleString();
}

function bytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

function mac(id) { return id.match(/../g).join(":"); }

function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

function jsonScript(id) {
  const node = document.getElementById(id);
  return node ? JSON.parse(node.textContent) : null;
}

function subscribe(handler) {
  const source = new EventSource("/admin/events");
  source.onmessage = e => handler(JSON.parse(e.data));
  return source;
}

function getPath(obj, path) { return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj); }

function setPath(obj, path, value) {
  const keys = path.split(".");
  let o = obj;
  for (const k of keys.slice(0, -1)) o = o[k] ??= {};
  o[keys.at(-1)] = value;
}

// dashboard

function initDashboard() {
  const pendingCards = new Map();

  function pendingCard(req) {
    const code = el("input", { inputmode: "numeric", maxlength: 3, pattern: "\\d{3}", placeholder: "000",
                               class: "code", "aria-label": "code shown on the speaker", required: true });
    const name = el("input", { placeholder: req.known_as || "Name (e.g. Kitchen)", maxlength: 48 });
    const expires = el("span", { class: "hint" });
    const form = el("form", {
      class: "card adopt",
      onsubmit: async e => {
        e.preventDefault();
        try {
          const r = await api("POST", `/admin/api/adoptions/${req.id}/approve`, { code: code.value, name: name.value });
          toast(`Adopted ${r.name}`);
          refresh();
        } catch (err) { toast(err.message, true); code.select(); }
      },
    },
      el("h3", {}, req.known_as ? `${req.known_as} (re-adopting)` : "New speaker"),
      el("p", { class: "mono" }, mac(req.device_id)),
      el("p", { class: "hint" }, `${req.hardware || "unknown hardware"} · firmware ${req.firmware || "?"} · ${req.ip || ""}`),
      el("div", { class: "row" }, code, name),
      el("div", { class: "row" },
        el("button", { type: "submit" }, "Adopt"),
        el("button", { type: "button", class: "secondary", onclick: async () => {
          await api("POST", `/admin/api/adoptions/${req.id}/reject`); refresh();
        } }, "Reject"),
        expires));
    form.update = r => { expires.textContent = `expires in ${Math.max(0, Math.round(r.expires_in / 60))} min`; };
    return form;
  }

  function renderPending(pending) {
    const container = $("#pending");
    const ids = new Set(pending.map(r => r.id));
    for (const [id, card] of pendingCards) if (!ids.has(id)) { card.remove(); pendingCards.delete(id); }
    for (const req of pending) {
      if (!pendingCards.has(req.id)) {
        const card = pendingCard(req);
        pendingCards.set(req.id, card);
        container.append(card);
      }
      pendingCards.get(req.id).update(req);
    }
    $("#pending-section").hidden = pending.length === 0;
  }

  function renderDevices(devices) {
    const rows = devices.map(d => {
      const s = d.status || {};
      const wifi = s.rssi != null ? `${s.ssid || ""} ${s.rssi} dBm` : "";
      const battery = s.battery != null ? `${s.battery}%${s.charging ? " ⚡" : ""}` : "";
      return el("tr", { class: d.online ? "online" : "offline" },
        el("td", {}, el("span", { class: `dot ${d.online ? "on" : ""}`, title: d.online ? "online" : "offline" })),
        el("td", {}, el("a", { href: `/admin/devices/${d.id}` }, d.name)),
        el("td", {}, d.online ? (s.state || "connected") : "offline"),
        el("td", {}, wifi),
        el("td", {}, battery),
        el("td", {}, d.firmware),
        el("td", { class: "mono" }, d.last_ip || ""),
        el("td", {}, d.online ? "now" : ago(d.last_seen)));
    });
    $("#devices").replaceChildren(...rows);
    $("#no-devices").hidden = devices.length > 0;
  }

  async function refresh() {
    try {
      const o = await api("GET", "/admin/api/overview");
      renderPending(o.pending);
      renderDevices(o.devices);
    } catch (err) { toast(err.message, true); }
  }

  refresh();
  subscribe(debounce(refresh, 300));
  setInterval(refresh, 30000);
}

// config editor (device page and defaults page)

const FONT_PX = { sm: 14, md: 18, lg: 30, xl: 48 };
const SAMPLE_STATUS = {
  idle: "Say “OK Nabu”", listening: "Listening…", thinking: "Thinking…",
  speaking: "Turning on the kitchen lights.", sleeping: "", error: "Can't reach Hermes",
};

function ringColor(state, theme) {
  return { idle: theme.surface, listening: theme.primary, thinking: theme.muted, speaking: theme.accent,
           error: "#ff5d5d", sleeping: "transparent" }[state];
}

function initConfigForm() {
  const form = $(".config-form");
  if (!form) return;
  const catalog = jsonScript("catalog");
  const data = jsonScript("form-data");
  const wifiList = $(".wifi-list", form);

  function wifiRow(network = { ssid: "", password: "" }) {
    const row = el("div", { class: "row wifi" },
      el("input", { class: "ssid", placeholder: "SSID", maxlength: 32, value: network.ssid }),
      el("input", { class: "password", type: "password", placeholder: "password (empty = open)", maxlength: 64,
                    value: network.password, autocomplete: "new-password" }),
      el("button", { type: "button", class: "secondary", onclick: () => { row.remove(); preview(); } }, "Remove"));
    return row;
  }

  function fill() {
    for (const input of $$("[data-path]", form)) {
      const v = getPath(data, input.dataset.path);
      if (input.type === "checkbox") input.checked = !!v;
      else input.value = v ?? "";
    }
    const theme = catalog.themes[data.config.display.theme];
    for (const input of $$("[data-color]", form)) {
      const key = input.dataset.color;
      const custom = data.config.display.colors[key];
      input.value = custom || theme[key];
      $(`[data-color-enable="${key}"]`, form).checked = !!custom;
    }
    wifiList.replaceChildren(...data.config.wifi.map(wifiRow));
  }

  function read() {
    const out = {};
    for (const input of $$("[data-path]", form)) {
      let v = input.value;
      if (input.dataset.type === "int") v = parseInt(v, 10);
      if (input.dataset.type === "bool") v = input.checked;
      setPath(out, input.dataset.path, v);
    }
    out.config.display.colors = Object.fromEntries($$("[data-color]", form).map(input => [
      input.dataset.color, $(`[data-color-enable="${input.dataset.color}"]`, form).checked ? input.value : null]));
    out.config.wifi = $$(".wifi", wifiList)
      .map(row => ({ ssid: $(".ssid", row).value.trim(), password: $(".password", row).value }))
      .filter(n => n.ssid);
    return out;
  }

  function preview() {
    for (const range of $$("input[type=range]", form)) range.nextElementSibling.textContent = range.value;
    const { config } = read();
    const theme = { ...catalog.themes[config.display.theme] };
    for (const [k, v] of Object.entries(config.display.colors)) if (v) theme[k] = v;
    // untouched colour pickers follow the selected theme
    for (const input of $$("[data-color]", form)) {
      if (!$(`[data-color-enable="${input.dataset.color}"]`, form).checked) input.value = theme[input.dataset.color];
    }
    const layout = catalog.layouts[config.display.layout];
    const character = catalog.characters.find(c => c.id === config.character);
    const state = $(".preview-state").value;
    const screen = $(".screen");
    screen.style.background = theme.background;
    screen.style.filter = `brightness(${0.35 + 0.65 * config.display.brightness / 100})`;
    const now = new Date();
    const clock = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: !config.display.clock_24h });
    const widgets = layout.widgets.map(w => {
      const box = { left: `${w.x}px`, top: `${w.y}px`, width: `${w.w}px`, height: `${w.h}px` };
      const text = { color: theme.text, fontSize: `${FONT_PX[w.font] || 18}px` };
      switch (w.type) {
        case "ring":
          return el("div", { class: "w ring", style: { border: `${w.thickness}px solid ${ringColor(state, theme)}` } });
        case "character": {
          if (!character) return null;
          const src = character.states.includes(state) ? state : "idle";
          return el("img", { class: "w", style: box, alt: "",
                             src: `/assets/characters/${character.id}/${src}.gif?size=${w.w}&v=${character.version}` });
        }
        case "clock":
          return el("div", { class: "w text", style: { ...box, ...text, fontWeight: 600 } }, clock);
        case "date":
          return el("div", { class: "w text", style: { ...box, ...text, color: theme.muted } },
            now.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }));
        case "status":
          return el("div", { class: "w text", style: { ...box, ...text, WebkitLineClamp: w.lines } }, SAMPLE_STATUS[state]);
        default:
          return null;
      }
    });
    screen.replaceChildren(...widgets.filter(Boolean));
  }

  fill();
  preview();
  form.addEventListener("input", preview);
  form.addEventListener("change", preview);
  $(".preview-state").addEventListener("change", preview);
  $(".add-wifi", form).addEventListener("click", () => { wifiList.append(wifiRow()); });
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const message = $(".form-message", form);
    message.textContent = "";
    try {
      const body = read();
      const r = await api("PUT", form.dataset.endpoint, body);
      if (body.name) $("#device-name").textContent = body.name;
      message.textContent = r.version
        ? `Saved as version ${r.version}${r.pushed ? ", sent to speaker" : ", the speaker will get it when it reconnects"}`
        : "Saved";
      message.className = "form-message ok";
    } catch (err) {
      message.textContent = err.message;
      message.className = "form-message error";
    }
  });
}

// device page

function initDevice() {
  const id = document.body.dataset.deviceId;
  const logs = $("#logs");

  function statusItem(label, value) {
    return value === undefined || value === null || value === "" ? [] : [el("dt", {}, label), el("dd", {}, value)];
  }

  async function refreshStatus() {
    const d = await api("GET", `/admin/api/devices/${id}`);
    const s = d.status || {};
    $("#online-dot").className = `dot ${d.online ? "on" : ""}`;
    const applied = s.config_version != null
      ? (s.config_version === d.config_version ? `v${s.config_version} (current)` : `v${s.config_version}, v${d.config_version} pending`)
      : undefined;
    $("#status").replaceChildren(
      ...statusItem("Connection", d.online ? "online" : `offline, last seen ${ago(d.last_seen)}`),
      ...statusItem("State", s.state),
      ...statusItem("Message", s.message),
      ...statusItem("Wi-Fi", s.rssi != null ? `${s.ssid || "?"} (${s.rssi} dBm)` : undefined),
      ...statusItem("IP", d.last_ip),
      ...statusItem("Battery", s.battery != null ? `${s.battery}%${s.charging ? ", charging" : ""}` : undefined),
      ...statusItem("Volume", s.volume != null ? `${s.volume}%${s.muted ? ", mic muted" : ""}` : undefined),
      ...statusItem("Uptime", s.uptime_s != null ? `${Math.floor(s.uptime_s / 3600)}h ${Math.floor(s.uptime_s / 60) % 60}m` : undefined),
      ...statusItem("Free memory", s.free_heap != null ? `${bytes(s.free_heap)} internal, ${bytes(s.free_psram || 0)} PSRAM` : undefined),
      ...statusItem("Firmware", d.firmware),
      ...statusItem("Hardware", d.hardware),
      ...statusItem("Config", applied),
      ...statusItem("MAC", mac(d.id)),
      ...statusItem("Status received", s.received_at ? ago(s.received_at) : undefined));
  }

  function appendLogs(lines) {
    const follow = $("#follow-logs").checked;
    logs.append(...lines.map(l => el("div", { class: `log lvl-${l.level}` },
      el("span", { class: "ts" }, new Date(l.ts * 1000).toLocaleTimeString()),
      el("span", { class: "lvl" }, l.level),
      el("span", { class: "tag" }, l.tag),
      el("span", { class: "msg" }, l.message))));
    while (logs.childElementCount > 2000) logs.firstElementChild.remove();
    if (follow) logs.scrollTop = logs.scrollHeight;
  }

  async function loadRecordings() {
    const recs = await api("GET", `/admin/api/devices/${id}/recordings`);
    $("#recordings").replaceChildren(...recs.map(r => el("tr", {},
      el("td", {}, new Date(r.created_at * 1000).toLocaleString()),
      el("td", {}, r.kind),
      el("td", {}, bytes(r.size)),
      el("td", {}, r.content_type.includes("wav") || r.content_type.includes("ogg")
        ? el("audio", { controls: true, preload: "none", src: `/admin/recordings/${r.id}` })
        : el("a", { href: `/admin/recordings/${r.id}` }, "download")))));
  }

  for (const button of $$("[data-command]")) {
    button.addEventListener("click", async () => {
      try {
        await api("POST", `/admin/api/devices/${id}/command`, { command: button.dataset.command });
        toast(`Sent ${button.dataset.command}`);
      } catch (err) { toast(err.message, true); }
    });
  }
  for (const button of $$("[data-install-device]")) {
    button.addEventListener("click", () => installFirmware($("#firmware-build").value, id));
  }
  $("#forget").addEventListener("click", async () => {
    if (!confirm("Forget this speaker? It will have to be adopted again.")) return;
    await api("DELETE", `/admin/api/devices/${id}`);
    location.href = "/admin";
  });

  const refreshSoon = debounce(() => refreshStatus().catch(err => toast(err.message, true)), 300);
  refreshSoon();
  api("GET", `/admin/api/devices/${id}/logs?limit=500`).then(appendLogs);
  loadRecordings();
  setInterval(refreshSoon, 15000);
  subscribe(ev => {
    if (ev.id !== id) return;
    if (ev.type === "log") appendLogs(ev.lines);
    else if (ev.type === "recording") loadRecordings();
    else refreshSoon();
  });
}

// characters page

function initCharacters() {
  $("#create-character").addEventListener("submit", async e => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("POST", "/admin/api/characters", { id: f.get("id"), name: f.get("name") });
      location.reload();
    } catch (err) { toast(err.message, true); }
  });
  for (const input of $$("input[type=file][data-state]")) {
    input.addEventListener("change", async () => {
      if (!input.files.length) return;
      const body = new FormData();
      body.append("file", input.files[0]);
      toast("Converting…");
      try {
        await api("POST", `/admin/api/characters/${input.dataset.character}/states/${input.dataset.state}`, body);
        location.reload();
      } catch (err) { toast(err.message, true); }
    });
  }
  for (const button of $$("[data-delete-character]")) {
    button.addEventListener("click", async () => {
      if (!confirm(`Delete character ${button.dataset.deleteCharacter}?`)) return;
      try {
        await api("DELETE", `/admin/api/characters/${button.dataset.deleteCharacter}`);
        location.reload();
      } catch (err) { toast(err.message, true); }
    });
  }
}

// firmware

async function installFirmware(build, deviceId) {
  if (!build || !deviceId) return;
  try {
    const r = await api("POST", `/admin/api/devices/${deviceId}/update`, { firmware: build });
    toast(`Updating to ${r.version}: the speaker restarts when it's done`);
  } catch (err) { toast(err.message, true); }
}

function initFirmware() {
  for (const cell of $$("[data-bytes]")) cell.textContent = bytes(Number(cell.dataset.bytes));
  for (const cell of $$("[data-time]")) {
    if (cell.dataset.time) cell.textContent = new Date(cell.dataset.time * 1000).toLocaleString();
  }
  const check = $("#check-updates");
  if (check) check.addEventListener("click", async () => {
    check.disabled = true;
    try {
      const st = await api("POST", "/admin/api/firmware/check");
      toast(st.error ? `Check failed: ${st.error}` : `Latest release: ${st.release || "none"}`, !!st.error);
      setTimeout(() => location.reload(), 1200);
    } catch (err) { toast(err.message, true); check.disabled = false; }
  });
  const channel = $("#update-channel");
  if (channel) channel.addEventListener("change", async () => {
    toast("Checking the channel…");
    try {
      const st = await api("PUT", "/admin/api/firmware/channel", { channel: channel.value });
      toast(st.error ? `Check failed: ${st.error}` : `Latest release: ${st.release || "none"}`, !!st.error);
      setTimeout(() => location.reload(), 1200);
    } catch (err) { toast(err.message, true); }
  });
  $("#upload-firmware").addEventListener("submit", async e => {
    e.preventDefault();
    toast("Uploading…");
    try {
      const fw = await api("POST", "/admin/api/firmware", new FormData(e.target));
      toast(`Uploaded ${fw.version}`);
      setTimeout(() => location.reload(), 800);
    } catch (err) { toast(err.message, true); }
  });
  for (const button of $$("[data-install]")) {
    const select = $(`[data-install-target="${button.dataset.install}"]`);
    button.addEventListener("click", () => installFirmware(button.dataset.install, select.value));
  }
  for (const button of $$("[data-delete-firmware]")) {
    button.addEventListener("click", async () => {
      if (!confirm("Delete this firmware image?")) return;
      try {
        await api("DELETE", `/admin/api/firmware/${button.dataset.deleteFirmware}`);
        location.reload();
      } catch (err) { toast(err.message, true); }
    });
  }
}

// voice

function initVoice() {
  const form = $("#voice-form");
  const fill = data => {
    for (const input of form.elements) {
      if (!input.name || !(input.name in data)) continue;
      if (input.type === "checkbox") input.checked = !!data[input.name];
      else input.value = data[input.name] ?? "";
    }
    const env = data.from_environment || {};
    const parts = [];
    if (env.assistant_url) parts.push(`API ${env.assistant_api || "openai"} at ${env.assistant_url}` +
                                      (env.assistant_model ? `, model ${env.assistant_model}` : ""));
    if (env.assistant_key) parts.push("an API key");
    const note = $("#assistant-env");
    note.hidden = !parts.length;
    note.textContent = parts.length ? `The server's environment provides ${parts.join(" and ")}; ` +
      "it's used where the fields above are empty." : "";
    showFields();
  };
  const showFields = () => {
    for (const node of $$("[data-show]", form)) {
      const [name, value] = node.dataset.show.split("=");
      node.hidden = form.elements[name].value !== value;
    }
  };
  form.addEventListener("change", showFields);
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const body = {};
    for (const input of form.elements) {
      if (!input.name) continue;
      body[input.name] = input.type === "checkbox" ? input.checked : input.value;
    }
    try {
      fill(await api("PUT", "/admin/api/voice", body));
      toast("Saved");
    } catch (err) { toast(err.message, true); }
  });
  fill(jsonScript("voice-data"));

  // custom wake words
  const wwList = $("#wakeword-list");
  const showWakeWords = data => {
    const custom = data.wake_words.filter(w => w.custom);
    wwList.replaceChildren(...custom.map(w => el("p", {}, el("strong", {}, w.name),
      w.used_by.length ? ` · used by ${w.used_by.join(", ")} ` : " ",
      el("button", { type: "button", class: "link", onclick: async () => {
        if (!confirm(`Remove the ${w.name} wake word?`)) return;
        try { await api("DELETE", `/admin/api/wakewords/${w.id}`); showWakeWords(await api("GET", "/admin/api/wakewords")); }
        catch (err) { toast(err.message, true); }
      } }, "Remove"))));
    if (!custom.length) wwList.append(el("p", { class: "hint" }, "No custom wake words yet."));
  };
  if (wwList) {
    api("GET", "/admin/api/wakewords").then(showWakeWords).catch(() => {});
    $("#ww-add").addEventListener("click", async () => {
      const model = $("#ww-model").files[0];
      if (!model) { toast("Choose the model's .tflite file", true); return; }
      const fd = new FormData();
      fd.append("model", model);
      if ($("#ww-manifest").files[0]) fd.append("manifest", $("#ww-manifest").files[0]);
      fd.append("name", $("#ww-name").value);
      try {
        showWakeWords(await api("POST", "/admin/api/wakewords", fd));
        toast("Added: choose it for a speaker on its page");
        $("#ww-model").value = ""; $("#ww-manifest").value = ""; $("#ww-name").value = "";
      } catch (err) { toast(err.message, true); }
    });
  }

  const say = $("#say-form");
  if (say) say.addEventListener("submit", async e => {
    e.preventDefault();
    const f = new FormData(say);
    toast("Speaking… (the first time, the voice downloads first)");
    try {
      await api("POST", `/admin/api/devices/${f.get("device")}/say`, { text: f.get("text"), voice: f.get("voice") });
      toast("Sent");
    } catch (err) { toast(err.message, true); }
  });
}

// server

function initServer() {
  const form = $("#server-form");
  const fill = data => {
    form.elements.public_url.value = data.public_url;
    $("#effective-url").textContent = data.effective_public_url;
  };
  form.addEventListener("submit", async e => {
    e.preventDefault();
    try {
      fill(await api("PUT", "/admin/api/server", { public_url: form.elements.public_url.value }));
      toast("Saved; online speakers have been told");
    } catch (err) { toast(err.message, true); }
  });
  fill(jsonScript("server-data"));
}

// agents

function initAgents() {
  const rows = $("#token-rows");
  const show = data => {
    rows.replaceChildren(...data.tokens.map(t => el("tr", {},
      el("td", {}, t.name), el("td", {}, ago(t.created_at)), el("td", {}, ago(t.last_used)),
      el("td", {}, el("button", { class: "danger", onclick: async () => {
        if (!confirm(`Revoke the token for ${t.name}? Agents using it lose access.`)) return;
        try { await api("DELETE", `/admin/api/agents/tokens/${t.id}`); show(await api("GET", "/admin/api/agents")); }
        catch (err) { toast(err.message, true); }
      } }, "Revoke")))));
    if (!data.tokens.length) rows.append(el("tr", {}, el("td", { colspan: 4, class: "hint" }, "No tokens yet.")));
  };
  const form = $("#token-form");
  form.addEventListener("submit", async e => {
    e.preventDefault();
    try {
      const made = await api("POST", "/admin/api/agents/tokens", { name: form.elements.name.value });
      $("#new-token-value").textContent = made.token;
      $("#hermes-config").textContent = `mcp_servers:\n  harkenn:\n    url: "${$("#mcp-url").textContent}"\n` +
        `    headers:\n      Authorization: "Bearer ${made.token}"\n    timeout: 120\n`;
      $("#new-token").hidden = false;
      form.reset();
      show(await api("GET", "/admin/api/agents"));
    } catch (err) { toast(err.message, true); }
  });
  show(jsonScript("agents-data"));
}

// sounds

function initSounds() {
  const root = $("#packs");
  const voices = jsonScript("voices-data");
  const deviceSelect = $("#sound-device");
  let data = jsonScript("sounds-data");
  let player = null;

  async function play(pack, slot) {
    const device = deviceSelect ? deviceSelect.value : "";
    if (device) {
      try { await api("POST", `/admin/api/devices/${device}/sound`, { pack: pack.id, slot }); }
      catch (err) { toast(err.message, true); }
      return;
    }
    if (player) player.pause();
    player = new Audio(`/assets/sounds/${pack.id}/${slot}.wav?v=${pack.version}`);
    player.play().catch(() => toast("Couldn't play that", true));
  }

  const refresh = async () => { data = await api("GET", "/admin/api/sounds"); render(); };

  function slotRow(pack, slot, label) {
    const has = pack.slots[slot];
    const cells = [el("td", {}, label),
      el("td", {}, has ? el("button", { type: "button", class: "secondary", onclick: () => play(pack, slot) }, "▶ Play")
                       : el("span", { class: "hint" }, "silent"))];
    if (pack.kind === "voiced") {
      const input = el("input", { value: pack.lines[slot] || "", maxlength: 120, placeholder: "empty: silent" });
      input.dataset.slot = slot;
      cells.push(el("td", { class: "grow" }, input));
    } else if (pack.kind === "custom") {
      const file = el("input", { type: "file", accept: "audio/*", onchange: async () => {
        if (!file.files.length) return;
        const fd = new FormData();
        fd.append("file", file.files[0]);
        toast("Uploading…");
        try { await api("PUT", `/admin/api/sounds/${pack.id}/${slot}`, fd); toast("Saved"); await refresh(); }
        catch (err) { toast(err.message, true); }
      } });
      cells.push(el("td", {}, file), el("td", {}, has ? el("button", { type: "button", class: "link", onclick: async () => {
        try { await api("DELETE", `/admin/api/sounds/${pack.id}/${slot}`); await refresh(); }
        catch (err) { toast(err.message, true); }
      } }, "Make silent") : ""));
    }
    return el("tr", {}, ...cells);
  }

  function packCard(pack) {
    const rows = Object.entries(data.slots).map(([slot, label]) => slotRow(pack, slot, label));
    const head = [el("h2", {}, pack.name)];
    const kind = { builtin: "Built in", voiced: `Voiced · ${voices[pack.voice] || pack.voice}`, custom: "Custom" }[pack.kind];
    head.push(el("p", { class: "hint" }, [pack.description, kind,
      pack.used_by.length ? `Used by ${pack.used_by.join(", ")}` : ""].filter(Boolean).join(" · ")));
    const card = el("section", { class: "card" }, ...head, el("table", {}, el("tbody", {}, ...rows)));
    const buttons = [];
    if (pack.kind === "voiced") {
      const voice = el("select", {}, ...Object.entries(voices).map(([v, l]) => el("option", { value: v }, l)));
      voice.value = pack.voice;
      card.append(el("label", {}, "Voice ", voice));
      buttons.push(el("button", { type: "button", onclick: async () => {
        const lines = Object.fromEntries($$("input[data-slot]", card).map(i => [i.dataset.slot, i.value]));
        toast("Speaking the lines…");
        try { await api("PUT", `/admin/api/sounds/${pack.id}`, { voice: voice.value, lines }); toast("Saved"); await refresh(); }
        catch (err) { toast(err.message, true); }
      } }, "Save"));
    }
    if (pack.kind !== "builtin") {
      buttons.push(el("button", { type: "button", class: "danger", onclick: async () => {
        if (!confirm(`Delete the ${pack.name} sounds?`)) return;
        try { await api("DELETE", `/admin/api/sounds/${pack.id}`); await refresh(); }
        catch (err) { toast(err.message, true); }
      } }, "Delete"));
    }
    if (buttons.length) card.append(el("div", { class: "buttons" }, ...buttons));
    return card;
  }

  function render() {
    root.replaceChildren(...data.packs.map(packCard));
    const copy = $("select[name=copy_from]");
    const keep = copy.value;
    copy.replaceChildren(...data.packs.map(p => el("option", { value: p.id }, p.name)));
    copy.value = keep || "classic";
  }

  const form = $("#new-pack");
  const showKind = () => { for (const node of $$("[data-kind]", form)) node.hidden = node.dataset.kind !== form.elements.kind.value; };
  form.elements.kind.addEventListener("change", showKind);
  form.elements.voice.value = "en_US-hfc_female-medium";
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const f = form.elements;
    const body = { name: f.name.value, kind: f.kind.value };
    if (body.kind === "voiced") {
      body.voice = f.voice.value;
      body.lines = Object.fromEntries(Object.keys(data.slots).map(s => [s, f[`line_${s}`].value]));
      toast("Speaking the lines… (a new voice downloads first)");
    } else {
      body.copy_from = f.copy_from.value;
    }
    try { await api("POST", "/admin/api/sounds", body); form.reset(); showKind(); toast("Made"); await refresh(); }
    catch (err) { toast(err.message, true); }
  });
  showKind();
  render();
}

// people (voiceprints)

// Records from the browser's microphone until the button is pressed again (or 10 s), then resolves to a Blob.
function recordClip(button) {
  return new Promise(async (resolve, reject) => {
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch { reject(new Error("The browser didn't allow the microphone")); return; }
    const rec = new MediaRecorder(stream);
    const chunks = [];
    const label = button.textContent;
    rec.ondataavailable = e => chunks.push(e.data);
    rec.onstop = () => {
      stream.getTracks().forEach(t => t.stop());
      button.textContent = label;
      button.onclick = null;
      resolve(new Blob(chunks, { type: rec.mimeType }));
    };
    button.textContent = "⏹ Stop";
    button.onclick = () => rec.state === "recording" && rec.stop();
    rec.start();
    setTimeout(() => rec.state === "recording" && rec.stop(), 10000);
  });
}

function initPeople() {
  const root = $("#people");
  const names = jsonScript("devices-data"), ids = jsonScript("device-ids");
  let data = jsonScript("people-data");
  const refresh = async () => { data = await api("GET", "/admin/api/people"); render(); };
  const upload = (url, blob) => { const fd = new FormData(); fd.append("file", blob, "voice.webm"); return api("POST", url, fd); };

  function card(p) {
    const sentence = data.sentences[p.samples % data.sentences.length];
    const status = p.samples ? `${p.samples} sample${p.samples === 1 ? "" : "s"}` +
      (p.samples < 3 ? " · add a few more for better recognition" : " · ready") : "no voice samples yet";
    const browser = el("button", { type: "button", class: "secondary" }, "🎙 Record in this browser");
    browser.addEventListener("click", async () => {
      if (browser.onclick) return;  // recording: the stop handler takes this click
      try {
        const blob = await recordClip(browser);
        toast("Learning the voice…");
        await upload(`/admin/api/people/${p.id}/samples`, blob);
        toast("Added"); await refresh();
      } catch (err) { toast(err.message, true); }
    });
    const speakers = el("select", {}, ...ids.map((id, k) => el("option", { value: id }, names[k])));
    const onSpeaker = el("button", { type: "button", class: "secondary", onclick: async () => {
      toast(`Listen to the speaker, then read the sentence after the tone`);
      try { await api("POST", `/admin/api/people/${p.id}/record/${speakers.value}`); toast("Added"); await refresh(); }
      catch (err) { toast(err.message, true); }
    } }, "Record on");
    const buttons = [browser];
    if (ids.length) buttons.push(el("span", { class: "inline-form" }, onSpeaker, speakers));
    return el("section", { class: "card" },
      el("h2", {}, p.name), el("p", { class: "hint" }, status),
      el("p", {}, "Read aloud: ", el("strong", {}, sentence)),
      el("div", { class: "buttons" }, ...buttons),
      el("p", { class: "hint" }, "One voiceprint works on every speaker: choosing a speaker only picks which " +
        "microphone records you. Recording on the speakers you use most helps."),
      el("div", { class: "buttons" },
        p.samples ? el("button", { type: "button", class: "link", onclick: async () => {
          if (!confirm(`Forget ${p.name}'s voice samples and start again?`)) return;
          try { await api("DELETE", `/admin/api/people/${p.id}/samples`); await refresh(); }
          catch (err) { toast(err.message, true); }
        } }, "Start over") : "",
        el("button", { type: "button", class: "danger", onclick: async () => {
          if (!confirm(`Delete ${p.name} and their voiceprint?`)) return;
          try { await api("DELETE", `/admin/api/people/${p.id}`); await refresh(); }
          catch (err) { toast(err.message, true); }
        } }, "Delete")));
  }

  function render() {
    root.replaceChildren(...data.people.map(card));
    if (!data.people.length) root.append(el("p", { class: "hint" }, "No one yet."));
  }

  const form = $("#new-person");
  form.addEventListener("submit", async e => {
    e.preventDefault();
    try {
      await api("POST", "/admin/api/people", { name: form.elements.name.value, consent: form.elements.consent.checked });
      form.reset(); await refresh();
    } catch (err) { toast(err.message, true); }
  });

  const check = $("#identify");
  check.addEventListener("click", async () => {
    if (check.onclick) return;
    try {
      const blob = await recordClip(check);
      const { who } = await upload("/admin/api/people/identify", blob);
      $("#identify-result").textContent = !who ? "Too short to tell, or no one has voice samples yet." :
        who.id ? `Probably ${who.name} (${who.sure ? "fairly sure" : "not certain"}, ${who.score.toFixed(2)})` :
        `Not recognized (closest match ${who.score.toFixed(2)})`;
    } catch (err) { toast(err.message, true); }
  });
  render();
}
const page = document.body.dataset.page;
if (page === "dashboard") initDashboard();
if (page === "device") { initConfigForm(); initDevice(); }
if (page === "defaults") initConfigForm();
if (page === "characters") initCharacters();
if (page === "firmware") initFirmware();
if (page === "voice") initVoice();
if (page === "server") initServer();
if (page === "agents") initAgents();
if (page === "sounds") initSounds();
if (page === "people") initPeople();
