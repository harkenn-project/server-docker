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
  const opts = { method, headers: { "X-Requested-With": "kotodama" } };
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
      ...statusItem("Volume", s.volume),
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

const page = document.body.dataset.page;
if (page === "dashboard") initDashboard();
if (page === "device") { initConfigForm(); initDevice(); }
if (page === "defaults") initConfigForm();
if (page === "characters") initCharacters();
