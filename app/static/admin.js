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

// A secret (a speaker password): stays up until closed.
function showSecret(title, secret, hint) {
  const d = $("#secret");
  $("h3", d).textContent = title;
  $(".secret", d).textContent = secret;
  $(".hint", d).textContent = hint;
  d.showModal();
}

const PASSWORD_HINT = "For the speaker's own page and its hotspot, when it can't reach Harkenn (the Harkenn admin " +
  "password works on the page too). You can see it again on the speaker's page here.";

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

// layout: the menu drawer on narrow screens, tabs, the unsaved-changes bar

function initNav() {
  const button = $(".menu-button");
  if (!button) return;
  const backdrop = $(".nav-backdrop");
  const open = on => {
    document.body.classList.toggle("nav-open", on);
    button.setAttribute("aria-expanded", on);
    backdrop.hidden = !on;
  };
  button.addEventListener("click", () => open(!document.body.classList.contains("nav-open")));
  backdrop.addEventListener("click", () => open(false));
  document.addEventListener("keydown", e => { if (e.key === "Escape") open(false); });
}

function initTabs() {
  const tabs = $$(".tabs a");
  if (!tabs.length) return;
  const panels = tabs.map(a => document.getElementById(a.hash.slice(1)));
  function show() {
    const target = location.hash && document.getElementById(location.hash.slice(1));
    const panel = (target && target.closest(".tab-panel")) || panels[0];
    for (const p of panels) p.hidden = p !== panel;
    for (const a of tabs) a.classList.toggle("active", a.hash === `#${panel.id}`);
    if (target && target !== panel) target.scrollIntoView();
  }
  window.addEventListener("hashchange", show);
  show();
}

const savebar = {
  dirty: false,
  init(save) {
    const bar = $("#savebar");
    $("[data-savebar=save]", bar).onclick = save;
    $("[data-savebar=discard]", bar).onclick = () => { this.dirty = false; location.reload(); };
    window.addEventListener("beforeunload", e => { if (this.dirty) e.preventDefault(); });
  },
  set(on) { this.dirty = on; $("#savebar").hidden = !on; },
};

// speakers: grouped cards

function wifiBars(rssi) {
  const level = rssi == null ? 0 : rssi > -55 ? 4 : rssi > -65 ? 3 : rssi > -75 ? 2 : 1;
  return el("span", { class: "bars", title: rssi == null ? "" : `${rssi} dBm` },
    [4, 7, 10, 13].map((h, i) => el("i", { class: i < level ? "on" : "", style: { height: `${h}px` } })));
}

function stateText(d) {
  const s = d.status || {};
  return d.online ? (s.state || "connected") : `offline · seen ${ago(d.last_seen)}`;
}

function initDashboard() {
  const catalog = jsonScript("catalog");
  const pendingCards = new Map();
  const characters = Object.fromEntries(catalog.characters.map(c => [c.id, c]));

  function pendingCard(req) {
    const code = el("input", { inputmode: "numeric", maxlength: 3, pattern: "\\d{3}", placeholder: "000",
                               class: "code", "aria-label": "code shown on the speaker", required: true });
    const name = el("input", { placeholder: req.known_as || "Name (e.g. Kitchen)", maxlength: 48, "aria-label": "Name" });
    const group = el("select", { "aria-label": "Group" }, req.known_as ? el("option", { value: "" }, "Its group now") : null,
      catalog.groups.map(g => el("option", { value: g.id, selected: !req.known_as && g.id === catalog.default_group },
        `Group: ${g.name}`)));
    const expires = el("span", { class: "hint" });
    const form = el("form", {
      class: "card adopt",
      onsubmit: async e => {
        e.preventDefault();
        try {
          const r = await api("POST", `/admin/api/adoptions/${req.id}/approve`,
            { code: code.value, name: name.value, group: group.value || null });
          toast(`Adopted ${r.name}`);
          if (r.speaker_password) showSecret(`${r.name}'s speaker password`, r.speaker_password, PASSWORD_HINT);
          refresh();
        } catch (err) { toast(err.message, true); code.select(); }
      },
    },
      el("h3", {}, req.known_as ? `${req.known_as} (adopting again)` : "New speaker"),
      el("p", { class: "hint" }, `${mac(req.device_id)} · ${req.hardware || "unknown hardware"} · firmware ${req.firmware || "?"} · ${req.ip || ""}`),
      el("div", { class: "row" }, code, name),
      el("div", { class: "row" }, group),
      el("div", { class: "row" },
        el("button", { type: "submit" }, "Adopt"),
        el("button", { type: "button", class: "secondary", onclick: async () => {
          await api("POST", `/admin/api/adoptions/${req.id}/reject`); refresh();
        } }, "Reject"),
        expires));
    form.update = r => { expires.textContent = `code expires in ${Math.max(0, Math.round(r.expires_in / 60))} min`; };
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

  function speakerCard(d) {
    const s = d.status || {};
    const c = characters[d.character] || characters.default;
    return el("a", { class: `card speaker-card ${d.online ? "" : "offline"}`, href: `/admin/devices/${d.id}` },
      c ? el("img", { class: "avatar", alt: "", loading: "lazy",
                      src: `/assets/characters/${c.id}/${d.online && s.state === "sleeping" ? "sleeping" : "idle"}.gif?size=144&v=${c.version}` })
        : el("span", { class: "avatar" }),
      el("div", {},
        el("div", { class: "name" },
          el("span", { class: `dot ${d.online ? (d.via === "relay" ? "remote" : "on") : ""}` }), d.name,
          d.via === "relay" ? el("span", { class: "badge warn", title: "connected through the relay" }, "remote") : null),
        el("div", { class: "meta" },
          el("span", {}, stateText(d)),
          s.rssi != null ? el("span", {}, wifiBars(s.rssi), " ", s.ssid || "") : null,
          s.battery != null ? el("span", {}, `${s.battery}%${s.charging ? " ⚡" : ""}`) : null,
          d.timers && d.timers.length ? el("span", {}, `⏱ ${d.timers.length}`) : null,
          d.notifications && d.notifications.length ? el("span", {}, `✉ ${d.notifications.length}`) : null),
        el("div", { class: "meta" },
          d.last_ip ? el("span", { class: "mono" }, d.last_ip) : null,
          d.firmware ? el("span", {}, `firmware ${d.firmware}`) : null,
          d.overrides.length ? el("span", {}, `${d.overrides.length} own setting${d.overrides.length > 1 ? "s" : ""}`) : null)));
  }

  function renderDevices(devices) {
    const blocks = catalog.groups.map(g => {
      const members = devices.filter(d => d.group.id === g.id);
      if (!members.length) return null;
      return el("section", { class: "group-block" },
        el("h2", { class: "group-title" }, el("a", { href: `/admin/groups/${g.id}` }, g.name),
          el("small", {}, `${members.filter(d => d.online).length} of ${members.length} online`)),
        el("div", { class: "cards" }, members.map(speakerCard)));
    });
    $("#speaker-groups").replaceChildren(...blocks.filter(Boolean));
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

// the settings editor (a speaker's page and a group's page)

const FONT_PX = { sm: 14, md: 18, lg: 30, xl: 48 };
const SAMPLE_STATUS = {
  idle: "Say “OK Nabu”", listening: "Listening…", thinking: "Thinking…",
  speaking: "Turning on the kitchen lights.", sleeping: "", error: "Can't reach Hermes",
};

function ringColor(state, theme) {
  return { idle: theme.surface, listening: theme.primary, thinking: theme.muted, speaking: theme.accent,
           error: "#ff5d5d", sleeping: "transparent" }[state];
}

const STATIC_FIELDS = [["ip", "IP address", "192.168.1.50"], ["subnet", "Subnet mask", "255.255.255.0"],
                       ["gateway", "Gateway", "192.168.1.1"], ["dns", "DNS (optional)", "the gateway"]];
let wifiCount = 0;

function initConfigForm() {
  const form = $(".config-form");
  if (!form) return null;
  const editor = form.closest(".config-editor");
  const deviceMode = editor.dataset.mode === "device";
  const catalog = jsonScript("catalog");
  const data = jsonScript("form-data");
  const own = new Set(data.overrides || []);
  const ownWifi = $(".own-wifi", form);
  const groupWifi = $(".group-wifi", form);
  const groupSelect = $("[data-path=group]", form);

  const keysOf = setting => setting.dataset.key.split(",");
  const isOwn = setting => keysOf(setting).some(k => own.has(k));
  const group = () => data.groups[groupSelect.value];

  // Wi-Fi networks
  function wifiCard(n = { ssid: "", password: "" }) {
    const id = `ipmode-${wifiCount++}`;
    const fixed = !!n.ip;
    const statics = el("div", { class: "static", hidden: !fixed }, STATIC_FIELDS.map(([key, label, placeholder]) =>
      el("label", {}, label, el("input", { class: key, placeholder, maxlength: 15, inputmode: "decimal", value: n[key] || "" }))));
    const card = el("div", { class: "wifi-net" },
      el("div", { class: "top" },
        el("label", {}, "Network name (SSID)", el("input", { class: "ssid", maxlength: 32, value: n.ssid, required: true })),
        el("label", {}, "Password", el("input", { class: "password", type: "password", placeholder: "empty: an open network",
                                                    maxlength: 64, value: n.password, autocomplete: "new-password" })),
        el("button", { type: "button", class: "secondary", onclick: () => { card.remove(); changed(); } }, "Remove")),
      el("div", { class: "ipmode" }, el("span", {}, "IP address:"),
        el("label", {}, el("input", { type: "radio", name: id, value: "auto", checked: !fixed }), "Automatic (DHCP)"),
        el("label", {}, el("input", { type: "radio", name: id, value: "fixed", checked: fixed }), "Fixed"),
        n.ssid && n.ssid === data.connected_ssid ? el("span", { class: "badge ok" }, "connected now") : null),
      statics);
    for (const radio of $$("input[type=radio]", card)) {
      radio.addEventListener("change", () => {
        statics.hidden = radio.value !== "fixed" || !radio.checked;
        if (!statics.hidden && !$(".subnet", card).value) $(".subnet", card).value = "255.255.255.0";
      });
    }
    return card;
  }

  function readWifi(card) {
    const fixed = $("input[value=fixed]", card).checked;
    return { ssid: $(".ssid", card).value.trim(), password: $(".password", card).value,
             ...Object.fromEntries(STATIC_FIELDS.map(([key]) => [key, fixed ? $(`.${key}`, card).value.trim() : ""])) };
  }

  function renderGroupWifi() {
    if (!groupWifi) return;
    const mine = new Set($$(".wifi-net", ownWifi).map(c => $(".ssid", c).value.trim()));
    groupWifi.replaceChildren(...group().config.wifi.map(n => el("div", { class: "wifi-net readonly" },
      el("div", {},
        el("div", { class: "title" }, n.ssid, n.ssid === data.connected_ssid ? el("span", { class: "badge ok" }, "connected now") : null,
          mine.has(n.ssid) ? el("span", { class: "badge own" }, "changed on this speaker") : null),
        el("div", { class: "sub" }, `${n.password ? "Password" : "Open"} · ${n.ip ? `fixed IP ${n.ip}` : "automatic IP"} · from ${group().name}`)),
      mine.has(n.ssid) ? null : el("button", { type: "button", class: "secondary small", onclick: () => {
        ownWifi.append(wifiCard(n)); changed(); renderGroupWifi();
      } }, "Change on this speaker"))));
  }

  // filling and reading the fields
  function setInput(input, v) {
    if (input.type === "checkbox") input.checked = !!v;
    else if (input.classList.contains("sleep-after") && v != null && !$$("option", input).some(o => o.value === String(v))) {
      input.append(el("option", { value: v }, `${v} seconds`));
      input.value = v;
    } else input.value = v ?? "";
  }

  function fillColors(config) {
    const theme = catalog.themes[config.display.theme];
    for (const input of $$("[data-color]", form)) {
      const key = input.dataset.color;
      const custom = config.display.colors[key];
      input.value = custom || theme[key];
      $(`[data-color-enable="${key}"]`, form).checked = !!custom;
    }
  }

  function tzState() {
    const setting = $(".tz-name", form).closest(".setting");
    setting.classList.toggle("tz-custom", !$(".tz-name", form).value);
  }

  // Fill fields from `source` ({name, group, config}); with `only`, just the fields in those settings.
  function fill(source, only = null) {
    const scope = only ? only.flatMap(s => $$("[data-path]", s)) : $$("[data-path]", form);
    for (const input of scope) setInput(input, getPath(source, input.dataset.path));
    if (!only || only.some(s => s.dataset.key === "display.colors")) fillColors(source.config);
    tzState();
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
    out.config.wifi = $$(".wifi-net", ownWifi).map(readWifi).filter(n => n.ssid);
    out.config.timezone ||= "UTC0";
    if (deviceMode) {
      out.overrides = [...own].filter(k => !k.startsWith("wifi:"));
      // what a speaker doesn't set itself comes from its group; send the group's, so nothing is lost
      out.config.assistant = data.config.assistant;
    } else delete out.group;
    return out;
  }

  // where each setting comes from (a speaker's page)
  function origin(setting) {
    if (!deviceMode) return;
    const mine = isOwn(setting);
    setting.classList.toggle("own", mine);
    $(".origin", setting).replaceChildren(...[mine
      ? el("span", { class: "badge own" }, "This speaker")
      : el("span", { class: "badge", title: "Change it to give this speaker its own" }, `From ${group().name}`),
      mine ? el("button", { type: "button", class: "link", onclick: () => {
        for (const k of keysOf(setting)) own.delete(k);
        fill({ config: group().config }, [setting]);
        origin(setting);
        changed();
      } }, "Use group's") : null].filter(Boolean));
  }

  function changed() {
    savebar.set(true);
    preview();
  }

  // the screen preview
  function preview() {
    for (const range of $$("input[type=range]", form)) {
      const out = range.nextElementSibling;
      out.textContent = range.value + (out.dataset.unit || "");
    }
    const { config } = read();
    const theme = { ...catalog.themes[config.display.theme] };
    for (const [k, v] of Object.entries(config.display.colors)) if (v) theme[k] = v;
    for (const input of $$("[data-color]", form)) {  // untouched colour pickers follow the scheme
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

  async function save() {
    if (!form.reportValidity()) return;
    try {
      const body = read();
      const r = await api("PUT", form.dataset.endpoint, body);
      $("#device-name").textContent = body.name;
      savebar.set(false);
      if (deviceMode) {
        toast(r.pushed ? "Saved and sent to the speaker" : "Saved: the speaker gets it when it connects");
        $("#group-link").textContent = group().name;
        $("#group-link").href = `/admin/groups/${groupSelect.value}`;
      } else {
        toast(r.speakers ? `Saved: sent to ${r.pushed} of ${r.speakers} speakers that changed` : "Saved");
      }
    } catch (err) { toast(err.message, true); }
  }

  // set up
  fill(data);
  ownWifi.replaceChildren(...data.config.wifi.filter(n => !deviceMode || own.has(`wifi:${n.ssid}`)).map(wifiCard));
  renderGroupWifi();
  const settings = $$(".setting[data-key]", form);
  settings.forEach(origin);
  preview();
  savebar.init(save);

  form.addEventListener("input", e => {
    const setting = e.target.closest(".setting[data-key]");
    if (deviceMode && setting && !isOwn(setting)) {  // changing a setting makes it this speaker's own
      for (const k of keysOf(setting)) own.add(k);
      origin(setting);
    }
    if (e.target.classList.contains("tz-name")) tzState();
    if (e.target.classList.contains("ssid")) renderGroupWifi();
    changed();
  });
  form.addEventListener("change", e => {
    if (e.target === groupSelect) {  // another group: everything this speaker doesn't set itself follows it
      fill({ config: group().config }, settings.filter(s => !isOwn(s)));
      settings.forEach(origin);
      renderGroupWifi();
    }
    changed();
  });
  form.addEventListener("submit", e => { e.preventDefault(); save(); });
  $(".preview-state").addEventListener("change", preview);
  $(".add-wifi", form).addEventListener("click", () => {
    const card = wifiCard();
    ownWifi.append(card);
    $(".ssid", card).focus();
    changed();
  });
  $(".tz-browser", form).addEventListener("click", () => {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const select = $(".tz-name", form);
    if (!$$("option", select).some(o => o.value === zone)) { toast(`${zone} isn't in Harkenn's list`, true); return; }
    select.value = zone;
    select.dispatchEvent(new Event("input", { bubbles: true }));
  });
  return { save };
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
    $("#online-dot").className = `dot ${d.online ? (d.via === "relay" ? "remote" : "on") : ""}`;
    $("#state-chip").textContent = stateText(d);
    $("#firmware-now").textContent = d.firmware || "unknown";
    const tile = (k, v, sub) => v == null || v === "" ? null
      : el("div", { class: "tile" }, el("div", { class: "k" }, k), el("div", { class: "v" }, v), sub ? el("div", { class: "s" }, sub) : null);
    $("#tiles").replaceChildren(...[
      tile("State", d.online ? (s.state || "connected") : "offline", d.online ? (d.via === "relay" ? "through the relay" : "")
        : `seen ${ago(d.last_seen)}`),
      tile("Wi-Fi", s.rssi != null ? el("span", {}, wifiBars(s.rssi), " ", s.ssid || "?") : null, s.rssi != null ? `${s.rssi} dBm` : ""),
      tile("IP address", d.last_ip, s.hotspot ? "hotspot on" : ""),
      tile("Volume", s.volume != null ? `${s.volume}%` : null, s.muted ? "mic muted" : ""),
      tile("Battery", s.battery != null ? `${s.battery}%` : null, s.charging ? "charging" : ""),
      tile("Firmware", d.firmware, d.hardware),
      tile("Up for", s.uptime_s != null ? `${Math.floor(s.uptime_s / 3600)} h ${Math.floor(s.uptime_s / 60) % 60} min` : null),
      tile("Own settings", String(d.overrides.length), `the rest from ${d.group.name}`),
    ].filter(Boolean));
    $("#remote-warning").hidden = d.via !== "relay";
    const applied = s.config_version != null
      ? (s.config_version === d.config_version ? `v${s.config_version} (current)` : `v${s.config_version}, v${d.config_version} pending`)
      : undefined;
    $("#status").replaceChildren(
      ...statusItem("Connection", d.online ? (d.via === "relay" ? "online, REMOTELY through the relay" : "online")
        : `offline, last seen ${ago(d.last_seen)}` + (d.relay_ready ? " (waiting for it at the relay)" : "")),
      ...statusItem("Message", s.message),
      ...statusItem("Free memory", s.free_heap != null ? `${bytes(s.free_heap)} internal` +
        (s.largest_free != null ? ` (largest block ${bytes(s.largest_free)}, lowest ${bytes(s.min_free_heap)})` : "") +
        `, ${bytes(s.free_psram || 0)} PSRAM` : undefined),
      ...statusItem("Stack never used", s.stack_free
        ? Object.entries(s.stack_free).map(([task, n]) => `${task} ${bytes(n)}`).join(", ") : undefined),
      ...statusItem("Settings", applied),
      ...statusItem("MAC", mac(d.id)),
      ...statusItem("Status received", s.received_at ? ago(s.received_at) : undefined));
    const la = d.local_access || {};
    $("#hotspot-name").textContent = `Harkenn-${d.id.slice(8).toUpperCase()}`;
    const locked = s.local_locked ? "LOCKED after too many wrong passwords: unlock it here"
      : s.local_failures ? `${s.local_failures} wrong password${s.local_failures > 1 ? "s" : ""} since the last right one`
      : s.local_failures === 0 ? "no wrong passwords" : undefined;
    $("#local-access").replaceChildren(
      ...statusItem("Speaker password", !la.speaker_password ? "not set yet"
        : la.viewable ? "set" : "set, but made before Harkenn kept them: make a new one to see it"),
      ...statusItem("Admin password", la.admin_password ? "sent to speakers"
        : "not sent yet: log out and in again to send it"),
      ...statusItem("Hotspot", s.hotspot ? "ON: the speaker can't join its Wi-Fi" : undefined),
      ...statusItem("Lockout", locked));
    $("#unlock").hidden = !(s.local_locked || s.local_failures);
    renderTimers(d);
    $("#show-speaker-password").hidden = !la.viewable;
  }

  const filter = $("#log-filter");
  const matches = row => !filter.value || row.textContent.toLowerCase().includes(filter.value.toLowerCase());
  filter.addEventListener("input", () => { for (const row of logs.children) row.hidden = !matches(row); });

  function appendLogs(lines) {
    const follow = $("#follow-logs").checked;
    logs.append(...lines.map(l => el("div", { class: `log lvl-${l.level}`, hidden: !matches({ textContent: `${l.tag} ${l.message}` }) },
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
  function renderTimers(d) {
    const days = { 127: "daily", 62: "weekdays", 65: "weekends" };
    const names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    const items = d.timers || [];
    $("#timers").replaceChildren(...(d.timers == null ? [el("li", { class: "hint" }, "Not reported yet (firmware 0.13 or later).")]
      : !items.length ? [el("li", { class: "hint" }, "None")]
      : items.map(t => {
        const left = Math.max(0, Math.round(t.due - Date.now() / 1000));
        const leftText = left >= 3600 ? `${Math.floor(left / 3600)} h ${Math.floor(left / 60) % 60} min`
          : `${Math.floor(left / 60)} min ${left % 60} s`;
        const when = t.kind === "alarm"
          ? `${t.time}, ${days[t.days] || (t.days ? names.filter((_, i) => t.days & (1 << i)).join(" ") : "once")}`
          : `${leftText} left`;
        return el("li", {}, el("span", {}, `${t.kind === "alarm" ? "⏰" : "⏱"} ${t.label || (t.kind === "alarm" ? "Alarm" : "Timer")}`),
          el("span", { class: "when" }, when),
          el("button", { class: "secondary", onclick: () => cancelTimer(String(t.id)) }, "Cancel"));
      })));
    $("#cancel-all-timers").hidden = !items.length;
    const notes = d.notifications || [];
    $("#notes-box").hidden = !notes.length;
    $("#notes").replaceChildren(...notes.map(n => el("li", {}, el("span", {}, n.title ? `${n.title}: ${n.text}` : n.text),
      el("span", { class: "when" }, ago(n.at)))));
  }
  async function cancelTimer(what) {
    try { await api("POST", `/admin/api/devices/${id}/timers/cancel`, { what }); toast("Cancelled"); }
    catch (err) { toast(err.message, true); }
  }
  $("#cancel-all-timers").addEventListener("click", () => {
    if (confirm("Cancel every timer and alarm on this speaker?")) cancelTimer("all");
  });
  $("#clear-notes").addEventListener("click", async () => {
    try { await api("POST", `/admin/api/devices/${id}/notifications/clear`); refreshSoon(); }
    catch (err) { toast(err.message, true); }
  });

  async function setSpeakerPassword(password) {
    try {
      const r = await api("POST", `/admin/api/devices/${id}/speaker-password`, { password });
      if (r.password) showSecret("New speaker password", r.password, PASSWORD_HINT);
      else toast(r.pushed ? "Speaker password set" : "Saved: the speaker gets it when it next connects");
      $("#speaker-password").value = "";
      refreshSoon();
    } catch (err) { toast(err.message, true); }
  }
  $("#show-speaker-password").addEventListener("click", async () => {
    try {
      const r = await api("GET", `/admin/api/devices/${id}/speaker-password`);
      showSecret(`${$("#device-name").textContent}'s speaker password`, r.password, PASSWORD_HINT);
    } catch (err) { toast(err.message, true); }
  });
  $("#new-speaker-password").addEventListener("click", () => {
    if (confirm("Make a new speaker password? The old one stops working.")) setSpeakerPassword("");
  });
  $("#set-speaker-password").addEventListener("click", () => {
    const pw = $("#speaker-password").value;
    if (pw.length < 8) { toast("Use at least 8 characters", true); return; }
    setSpeakerPassword(pw);
  });
  $("#unlock").addEventListener("click", async () => {
    try {
      const r = await api("POST", `/admin/api/devices/${id}/unlock`);
      toast(r.pushed ? "Unlocked" : "The speaker is unlocked when it next connects");
    } catch (err) { toast(err.message, true); }
  });
  $("#factory-reset").addEventListener("click", async () => {
    if (!confirm("Factory reset this speaker? It forgets its Wi-Fi, adoption and settings, and Harkenn forgets it. " +
                 "It starts over in Wi-Fi setup.")) return;
    try {
      await api("POST", `/admin/api/devices/${id}/factory-reset`);
      location.href = "/admin";
    } catch (err) { toast(err.message, true); }
  });
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

// groups

function initGroups() {
  async function render() {
    const groups = await api("GET", "/admin/api/groups");
    $("#groups").replaceChildren(...groups.map(g => el("section", { class: "card group-card" },
      el("h2", {}, el("a", { href: `/admin/groups/${g.id}` }, g.name), g.default ? el("span", { class: "badge" }, "new speakers") : null),
      el("p", { class: "hint" }, `${g.members.length} speaker${g.members.length === 1 ? "" : "s"}` +
        ` · ${g.config.wifi.length} Wi-Fi network${g.config.wifi.length === 1 ? "" : "s"}` +
        (g.config.timezone_name ? ` · ${g.config.timezone_name.replace(/_/g, " ")}` : "")),
      g.members.length ? el("ul", { class: "members" }, g.members.map(m => el("li", {},
        el("span", { class: `dot ${m.online ? "on" : ""}` }), el("a", { href: `/admin/devices/${m.id}` }, m.name),
        m.overrides ? el("small", { title: "settings of its own" }, `${m.overrides} own`) : null))) : null,
      el("a", { class: "button secondary", href: `/admin/groups/${g.id}` }, "Settings"))));
  }
  $("#new-group").addEventListener("submit", async e => {
    e.preventDefault();
    const f = e.target;
    try {
      const g = await api("POST", "/admin/api/groups", { name: f.name.value, copy_from: f.copy_from.value || null });
      location.href = `/admin/groups/${g.id}`;
    } catch (err) { toast(err.message, true); }
  });
  render().catch(err => toast(err.message, true));
}

function initGroup() {
  const id = document.body.dataset.groupId;
  initConfigForm();
  async function members() {
    const [g, o] = await Promise.all([api("GET", `/admin/api/groups/${id}`), api("GET", "/admin/api/overview")]);
    $("#members").replaceChildren(...(g.members.length ? g.members.map(m => el("li", {},
      el("span", { class: `dot ${m.online ? "on" : ""}` }), el("a", { href: `/admin/devices/${m.id}` }, m.name),
      m.overrides ? el("small", {}, `${m.overrides} setting${m.overrides > 1 ? "s" : ""} of its own`) : null))
      : [el("li", { class: "hint" }, "No speakers yet")]));
    const others = o.devices.filter(d => d.group.id !== id);
    $("#add-member").replaceChildren(...others.map(d => el("option", { value: d.id }, `${d.name} (now in ${d.group.name})`)));
    $("#add-member").closest(".inline-form").hidden = !others.length;
  }
  $("#add-member-button").addEventListener("click", async () => {
    const device = $("#add-member").value;
    if (!device) return;
    try {
      await api("POST", `/admin/api/groups/${id}/members`, { devices: [device] });
      toast("Moved: it keeps the settings it has of its own");
      members();
    } catch (err) { toast(err.message, true); }
  });
  $("#make-default")?.addEventListener("click", async () => {
    try { await api("POST", `/admin/api/groups/${id}/default`); location.reload(); }
    catch (err) { toast(err.message, true); }
  });
  $("#delete-group")?.addEventListener("click", async () => {
    if (!confirm("Delete this group? Its speakers move to the group new speakers join, and keep their own settings.")) return;
    try { await api("DELETE", `/admin/api/groups/${id}`); savebar.dirty = false; location.href = "/admin/groups"; }
    catch (err) { toast(err.message, true); }
  });
  members().catch(err => toast(err.message, true));
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

  const wx = $("#weather-form");
  const showWeather = data => {
    wx.elements.place.value = data.place;
    wx.elements.units.value = data.units;
    $("#weather-now").textContent = data.place ? (data.now || "can't reach the weather service") : "not set";
  };
  wx.addEventListener("submit", async e => {
    e.preventDefault();
    try {
      showWeather(await api("PUT", "/admin/api/weather", { place: wx.elements.place.value, units: wx.elements.units.value }));
      toast("Saved");
    } catch (err) { toast(err.message, true); }
  });
  api("GET", "/admin/api/weather").then(showWeather).catch(err => toast(err.message, true));

  const ha = $("#ha-form");
  const showHa = data => {
    ha.elements.enabled.checked = data.enabled;
    ha.elements.broker.value = data.broker;
    ha.elements.discovery_prefix.value = data.discovery_prefix;
    $("#ha-status").textContent = data.status;
    $("#ha-env").textContent = data.from_environment ? "(from HARKENN_MQTT_URL)" : "";
  };
  ha.addEventListener("submit", async e => {
    e.preventDefault();
    const f = ha.elements;
    try {
      showHa(await api("PUT", "/admin/api/homeassistant",
        { enabled: f.enabled.checked, broker: f.broker.value, discovery_prefix: f.discovery_prefix.value }));
      toast("Saved");
    } catch (err) { toast(err.message, true); }
  });
  api("GET", "/admin/api/homeassistant").then(showHa).catch(err => toast(err.message, true));
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
function recordClip(button, maxMs = 10000) {
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
    setTimeout(() => rec.state === "recording" && rec.stop(), maxMs);
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
    const field = (key, label, attrs = {}) => el("label", {}, label, " ",
      el("input", { value: p[key] || "", maxlength: key === "phonetic" ? 60 : 40, "data-key": key, ...attrs }));
    const details = el("form", { class: "inline-form", onsubmit: async e => {
      e.preventDefault();
      const body = Object.fromEntries($$("input", details).map(i => [i.dataset.key, i.value]));
      try { data = await api("PATCH", `/admin/api/people/${p.id}`, body); toast("Saved"); render(); }
      catch (err) { toast(err.message, true); }
    } },
      field("name", "Name", { required: true }), field("phonetic", "Phonetic", { placeholder: "how to say it" }),
      field("gender", "Gender identity", { list: "genders", placeholder: "optional" }),
      el("button", { type: "submit", class: "secondary" }, "Save"));
    return el("section", { class: "card" },
      el("h2", {}, p.name, p.phonetic ? el("small", { class: "hint" }, ` (say "${p.phonetic}")`) : ""),
      details, el("p", { class: "hint" }, status),
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
      await api("POST", "/admin/api/people", { name: form.elements.name.value, phonetic: form.elements.phonetic.value,
        gender: form.elements.gender.value, consent: form.elements.consent.checked });
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
// screen

function initScreen() {
  const spec = $("#screen-spec"), device = $("#screen-device"), info = $("#screen-info");
  const parse = () => {
    try { return JSON.parse(spec.value); } catch (err) { toast(`That isn't valid JSON: ${err.message}`, true); return null; }
  };
  async function preview() {
    const s = parse();
    if (!s) return;
    try {
      const out = await api("POST", `/admin/api/screen/preview?device=${encodeURIComponent(device.value)}`, { spec: s });
      $("#screen-png").src = `data:image/png;base64,${out.png}`;
      $("#screen-regions").replaceChildren(...out.regions.map(r => el("div", { class: "region", title: r.label,
        style: { left: `${r.x}px`, top: `${r.y}px`, width: `${r.w}px`, height: `${r.h}px` } }, r.label)));
      info.textContent = `${bytes(out.bytes)} for the speaker. Tappable: ${out.regions.map(r => r.label).join(", ") || "nothing"}.` +
        (out.warnings.length ? `
Problems: ${out.warnings.join("; ")}` : "");
    } catch (err) { toast(err.message, true); }
  }
  $("#screen-preview-btn").addEventListener("click", preview);
  $("#screen-show").addEventListener("click", async () => {
    const s = parse();
    if (!s) return;
    if (!device.value) { toast("Pick a speaker first", true); return; }
    try {
      const out = await api("POST", `/admin/api/devices/${device.value}/screen`, { spec: s });
      toast(out.warnings.length ? `Sent, with problems: ${out.warnings.join("; ")}` : "Sent", !!out.warnings.length);
    } catch (err) { toast(err.message, true); }
  });

  const form = $("#overlay-form");
  const list = $("#overlays");
  const fill = (name, o) => {
    form.elements.name.value = name;
    for (const k of ["description", "svg", "x", "y", "w", "h"]) form.elements[k].value = o[k] ?? "";
    $("#overlay-title").textContent = `Edit ${name}`;
    form.scrollIntoView({ behavior: "smooth" });
  };
  const show = overlays => {
    const names = Object.keys(overlays);
    list.replaceChildren(...names.map(name => {
      const o = overlays[name];
      return el("div", { class: "row" },
        el("code", {}, name), " ", el("span", { class: "hint" }, o.description || ""), " ",
        el("small", { class: "hint" }, o.params.length ? `(${o.params.join(", ")})` : ""), " ",
        el("button", { type: "button", class: "secondary", onclick: () => fill(name, o) }, "Edit"), " ",
        el("button", { type: "button", class: "secondary", onclick: () => {
          const params = Object.fromEntries(o.params.map(p => [p, p]));
          spec.value = JSON.stringify({ overlays: [{ type: name, ...params }] }, null, 2);
          preview();
        } }, "Try"), " ",
        el("button", { type: "button", class: "danger", onclick: async () => {
          if (!confirm(`Delete the overlay ${name}?`)) return;
          try { await api("DELETE", `/admin/api/screen/overlays/${name}`); await refresh(); }
          catch (err) { toast(err.message, true); }
        } }, "Delete"));
    }));
    if (!names.length) list.append(el("p", { class: "hint" }, "None yet."));
  };
  async function refresh() {
    const data = await api("GET", "/admin/api/screen");
    show(data.overlays);
    $("#screen-prompt").textContent = data.prompt;
  }
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const f = form.elements;
    const body = { description: f.description.value, svg: f.svg.value, x: +f.x.value, y: +f.y.value, w: +f.w.value, h: +f.h.value };
    try { await api("PUT", `/admin/api/screen/overlays/${f.name.value}`, body); toast("Saved"); await refresh(); }
    catch (err) { toast(err.message, true); }
  });
  $("#overlay-new").addEventListener("click", () => { form.reset(); $("#overlay-title").textContent = "New overlay"; });
  show(jsonScript("overlays-data"));
  preview();
}

// household modules: the Modules page, and a page per module

function initModules() {
  let data = jsonScript("household-data");
  let settings = structuredClone(data.settings);
  const SECTIONS = Object.fromEntries(data.sections);

  async function save(quiet = false) {
    try {
      data = await api("PUT", "/admin/api/household", { settings });
      settings = structuredClone(data.settings);
      if (!quiet) toast("Saved");
      return true;
    } catch (err) { toast(err.message, true); return false; }
  }

  function modules() {
    $("#modules").replaceChildren(...data.modules.map(m => [m.name, m]).map(([name, m]) => el("div", { class: `card module${m.enabled ? " on" : ""}` },
      el("div", { class: "module-head" },
        el("h3", {}, m.title),
        el("label", { class: "switch", title: m.enabled ? "On" : "Off" },
          el("input", { type: "checkbox", checked: m.enabled, onchange: async e => {
            settings.enabled[name] = e.target.checked;
            if (await save(true)) {
              toast(`${m.title} ${e.target.checked ? "on" : "off"}`);
              location.reload();  // the menu changes
            } else e.target.checked = !e.target.checked;
          } }), el("span", { class: "slider" }))),
      el("p", { class: "hint" }, m.about),
      m.enabled && m.page ? el("a", { href: `/admin/household/${name}` }, `Open ${m.title.toLowerCase()} →`) : null)));
  }

  function links(kind) {
    const root = $(`[data-links="${kind}"]`);
    const list = settings[kind];
    root.replaceChildren(...list.map((link, i) => el("div", { class: "link-row" },
      el("input", { value: link.name, placeholder: "Name", maxlength: 60, "aria-label": "Name",
                    onchange: e => { link.name = e.target.value; save(); } }),
      el("input", { class: "grow", value: link.url, placeholder: "https://…", "aria-label": "Address",
                    onchange: e => { link.url = e.target.value.trim(); save(); } }),
      data.errors[link.url] ? el("span", { class: "badge danger", title: data.errors[link.url] }, "can't read it") : null,
      el("button", { type: "button", class: "secondary small", onclick: async () => {
        list.splice(i, 1); if (await save()) links(kind);
      } }, "Remove"))));
    if (!list.length) root.append(el("p", { class: "hint" }, "None yet."));
  }
  for (const button of $$("[data-add]")) {
    button.addEventListener("click", () => {
      const kind = button.dataset.add;
      const name = prompt("Name (e.g. Family, CBC)");
      if (!name) return;
      const url = prompt("Address (https:// or webcal://)");
      if (!url) return;
      settings[kind].push({ name, url: url.trim() });
      save().then(ok => { if (ok) links(kind); else settings[kind].pop(); });
    });
  }
  $("#news-test").addEventListener("click", async () => {
    const out = $("#news-preview");
    out.replaceChildren(el("p", { class: "hint" }, "Reading…"));
    try {
      const got = await api("GET", "/admin/api/household/news");
      out.replaceChildren(...got.feeds.map(f => el("div", {},
        el("h4", {}, f.name), f.error ? el("p", { class: "hint error" }, f.error) : null,
        el("ul", { class: "headlines" }, ...f.headlines.map(h => el("li", {}, h.title))))));
      if (!got.feeds.length) out.replaceChildren(el("p", { class: "hint" }, "Add a feed first."));
    } catch (err) { out.replaceChildren(); toast(err.message, true); }
  });
  $("#calendars-refresh").addEventListener("click", async () => {
    try {
      const got = await api("POST", "/admin/api/household/calendars/refresh");
      const bad = got.calendars.filter(c => !c.ok);
      toast(bad.length ? `Couldn't read ${bad.map(c => `${c.name} (${c.error})`).join(", ")}`
        : got.calendars.length ? "Read them all" : "Add a calendar first", bad.length > 0);
      data = await api("GET", "/admin/api/household");
      links("calendars");
    } catch (err) { toast(err.message, true); }
  });

  function briefing() {
    $("#briefing-head").replaceChildren(el("tr", {}, el("th", {}, ""), ...Object.values(SECTIONS).map(s => el("th", {}, s))));
    const row = (label, sections, own, onchange) => el("tr", {},
      el("td", {}, label, own === null ? null : el("label", { class: "inline small" },
        el("input", { type: "checkbox", checked: own, onchange: e => onchange(null, e.target.checked) }), " its own")),
      ...Object.keys(SECTIONS).map(k => el("td", {}, el("input", { type: "checkbox", "aria-label": `${label}: ${SECTIONS[k]}`,
        checked: sections.includes(k), disabled: own === false,
        onchange: e => onchange(k, e.target.checked) }))));
    const rows = [row("Everyone", settings.briefing_sections, null, (k, on) => {
      settings.briefing_sections = on ? [...settings.briefing_sections, k] : settings.briefing_sections.filter(x => x !== k);
      save().then(briefing);
    })];
    for (const g of data.groups) {
      const own = g.id in settings.briefing_groups;
      rows.push(row(g.name, own ? settings.briefing_groups[g.id] : settings.briefing_sections, own, (k, on) => {
        if (k === null) {
          if (on) settings.briefing_groups[g.id] = [...settings.briefing_sections];
          else delete settings.briefing_groups[g.id];
        } else {
          const list = settings.briefing_groups[g.id];
          settings.briefing_groups[g.id] = on ? [...list, k] : list.filter(x => x !== k);
        }
        save().then(briefing);
      }));
    }
    $("#briefing-rows").replaceChildren(...rows);
  }
  const check = (id, key) => {
    const box = $(id);
    box.checked = settings[key];
    box.addEventListener("change", () => { settings[key] = box.checked; save(); });
  };
  check("#briefing-after-alarm", "briefing_after_alarm");
  check("#reminders-speak", "reminders_speak");
  modules();
  links("calendars");
  links("news_feeds");
  briefing();
}

function initHousehold() {
  const module = document.body.dataset.module;
  let data = jsonScript("household-items");
  let editing = null;  // the item being edited, or null to add
  const form = $("#item-form");
  const fields = data.fields;
  const shown = fields.filter(f => !f.form_only);
  const editable = fields.filter(f => f.type !== "readonly" && !f.table_only);

  function input(f) {
    const common = { name: f.key, placeholder: f.placeholder || "", required: f.required || false };
    switch (f.type) {
      case "textarea": return el("textarea", { ...common, rows: 2, maxlength: 500 });
      case "bool": return el("input", { type: "checkbox", name: f.key });
      case "date": return el("input", { ...common, type: "date" });
      case "time": return el("input", { ...common, type: "time" });
      case "datetime": return el("input", { ...common, type: "datetime-local" });
      case "select": return el("select", { name: f.key }, ...f.options.map(([v, label]) => el("option", { value: v }, label)));
      case "owner": return el("select", { name: f.key }, el("option", { value: "" }, "Household"),
        ...data.people.map(p => el("option", { value: p.id }, p.name)));
      case "speaker": return el("select", { name: f.key }, el("option", { value: "all" }, "All speakers"),
        ...data.speakers.map(s => el("option", { value: s.id }, s.name)));
      case "person": return el("input", { ...common, list: "people-list", maxlength: 40 });
      default: return el("input", { ...common, maxlength: 300 });
    }
  }

  function buildForm() {
    form.replaceChildren(
      el("datalist", { id: "people-list" }, ...data.people.map(p => el("option", { value: p.name }))),
      ...editable.map(f => f.type === "bool"
        ? el("label", { class: "inline" }, input(f), el("span", {}, f.label))
        : el("label", { class: f.type === "textarea" ? "wide" : "" }, f.label, input(f))),
      el("div", { class: "buttons wide" },
        el("button", { type: "submit" }, "Add"),
        el("button", { type: "button", class: "secondary", hidden: true, onclick: () => edit(null) }, "Cancel")));
  }

  function edit(item) {
    editing = item;
    form.reset();
    $("#item-form-title").textContent = item ? "Change" : "Add";
    $("button[type=submit]", form).textContent = item ? "Save" : "Add";
    $("button.secondary", form).hidden = !item;
    if (!item) return;
    for (const f of editable) {
      const node = form.elements[f.key];
      const value = f.key === "owner" ? item.owner : item.data[f.key];
      if (f.type === "bool") node.checked = !!value;
      else if (f.type === "list") node.value = (value || []).join(", ");
      else if (f.type === "datetime") node.value = (value || "").slice(0, 16);
      else if (f.type === "speaker") node.value = value || "all";
      else node.value = value ?? "";
    }
    form.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function read() {
    const out = { data: {} };
    for (const f of editable) {
      const node = form.elements[f.key];
      const value = f.type === "bool" ? node.checked : node.value;
      if (f.key === "owner") out.owner = value;
      else out.data[f.key] = value;
    }
    return out;
  }

  form.addEventListener("submit", async e => {
    e.preventDefault();
    try {
      data = editing ? await api("PATCH", `/admin/api/household/${module}/${editing.id}`, read())
        : await api("POST", `/admin/api/household/${module}`, read());
      toast(editing ? "Saved" : "Added");
      edit(null);
      render();
    } catch (err) { toast(err.message, true); }
  });

  function cell(item, f) {
    if (f.type === "bool") {
      return el("td", { class: "center" }, el("input", { type: "checkbox", checked: !!item.data[f.key], "aria-label": f.label,
        onchange: async e => {
          try { data = await api("PATCH", `/admin/api/household/${module}/${item.id}`, { data: { [f.key]: e.target.checked } }); render(); }
          catch (err) { e.target.checked = !e.target.checked; toast(err.message, true); }
        } }));
    }
    return el("td", { class: f.type === "textarea" ? "text" : "" }, item.view[f.key] || "");
  }

  function render() {
    $("#item-head").replaceChildren(el("tr", {}, ...shown.map(f => el("th", {}, f.label)), el("th", {}, "")));
    const finished = i => i.data.done || i.data.got || i.data.delivered_at;
    $("#item-rows").replaceChildren(...data.items.map(item => el("tr", { class: finished(item) ? "finished" : "" },
      ...shown.map(f => cell(item, f)),
      el("td", { class: "row-actions" },
        el("button", { type: "button", class: "secondary small", onclick: () => edit(item) }, "Change"),
        el("button", { type: "button", class: "danger small", onclick: async () => {
          if (!confirm("Delete this?")) return;
          try { data = await api("DELETE", `/admin/api/household/${module}/${item.id}`); render(); }
          catch (err) { toast(err.message, true); }
        } }, "Delete")))));
    if (!data.items.length) {
      $("#item-rows").append(el("tr", {}, el("td", { colspan: shown.length + 1, class: "hint" }, "Nothing yet.")));
    }
    if (data.upcoming) {
      let day = "";
      const rows = [];
      for (const e of data.upcoming) {
        if (e.date !== day) { day = e.date; rows.push(el("h4", { class: "day" }, e.when.split(" ")[0] === "today" ? "Today" : new Date(e.date + "T12:00").toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" }))); }
        rows.push(el("div", { class: "event" },
          el("span", { class: "event-time" }, e.when.includes(":") ? e.when.split(" ").at(-1) : "all day"),
          el("span", { class: "event-title" }, e.title, e.where ? el("small", {}, ` · ${e.where}`) : null),
          el("span", { class: "badge" }, e.calendar), e.for !== "Household" ? el("span", { class: "badge own" }, e.for) : null));
      }
      $("#upcoming").replaceChildren(...(rows.length ? rows : [el("p", { class: "hint" }, "Nothing in the next 31 days.")]));
    }
  }

  subscribe(msg => {
    if (msg.type === "household" && msg.module === module && !editing) {
      api("GET", `/admin/api/household/${module}`).then(d => { data = d; render(); }).catch(() => {});
    }
  });
  buildForm();
  edit(null);
  render();
}

// broadcast

function initBroadcast() {
  let data = jsonScript("broadcast-data");
  const form = $("#broadcast-form");
  $("#broadcast-to").replaceChildren(...data.destinations.map(d => el("option", { value: d.id },
    d.kind === "group" ? `${d.name} (group)` : d.name)));
  const show = () => {
    $("#broadcast-rows").replaceChildren(...data.history.map(b => el("tr", {},
      el("td", {}, new Date(b.sent_at * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })),
      el("td", {}, b.from || "—"),
      el("td", { class: "text" }, b.voice ? "🎙 " : "", b.text || "(a voice message)"),
      el("td", {}, b.to_name), el("td", {}, b.heard_on.join(", ") || "No speaker was on"))));
    if (!data.history.length) $("#broadcast-rows").append(el("tr", {}, el("td", { colspan: 5, class: "hint" }, "None yet.")));
  };
  const sent = out => {
    data = out;
    show();
    toast(out.sent.heard_on.length ? `Sent to ${out.sent.heard_on.join(", ")}` : "No speaker is on to hear it", !out.sent.heard_on.length);
  };
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const f = form.elements;
    if (!f.text.value.trim()) { toast("Type a message, or record one", true); return; }
    try { sent(await api("POST", "/admin/api/broadcasts", { text: f.text.value, to: f.to.value, sender: f.sender.value })); form.text.value = ""; }
    catch (err) { toast(err.message, true); }
  });
  $("#broadcast-record").addEventListener("click", async e => {
    const button = e.currentTarget;
    if (button.dataset.busy) return;
    button.dataset.busy = "1";
    try {
      const blob = await recordClip(button, 30000);
      const body = new FormData();
      body.append("file", blob, "broadcast.webm");
      body.append("to", form.elements.to.value);
      body.append("sender", form.elements.sender.value);
      button.textContent = "Sending…";
      sent(await api("POST", "/admin/api/broadcasts/recording", body));
    } catch (err) { toast(err.message, true); }
    finally { delete button.dataset.busy; button.textContent = "🎙 Record and send"; }
  });
  subscribe(msg => { if (msg.type === "broadcast") api("GET", "/admin/api/broadcasts").then(d => { data = d; show(); }).catch(() => {}); });
  show();
}

const page = document.body.dataset.page;
initNav();
initTabs();
if (page === "dashboard") initDashboard();
if (page === "device") { initConfigForm(); initDevice(); }
if (page === "groups") initGroups();
if (page === "group") initGroup();
if (page === "characters") initCharacters();
if (page === "firmware") initFirmware();
if (page === "voice") initVoice();
if (page === "server") initServer();
if (page === "agents") initAgents();
if (page === "sounds") initSounds();
if (page === "people") initPeople();
if (page === "screen") initScreen();
if (page === "modules") initModules();
if (page === "broadcast") initBroadcast();
if (page.startsWith("household-")) initHousehold();
