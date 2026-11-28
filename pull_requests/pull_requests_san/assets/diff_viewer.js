const DIFF_COLOR_STORAGE_KEY = "diff_san_theme_colors_v1";
const DIFF_SANDHI_STORAGE_KEY = "diff_san_highlight_sandhi_v2";
let sandhiHighlightEnabled = true;

const DIFF_COLOR_FIELDS = [
  { id: "colorRedChar", cssVar: "--red-char", label: "Deleted word / token", group: "Deleted (Old)", defaultVal: "#ffeded" },
  { id: "colorDelCharSub", cssVar: "--del-char-sub", label: "Deleted intra-word char", group: "Deleted (Old)", defaultVal: "#ffd6db" },
  { id: "colorDelWordBase", cssVar: "--del-word-base", label: "Deleted word wrapper", group: "Deleted (Old)", defaultVal: "#fffdfd" },
  { id: "colorRedRow", cssVar: "--red-row", label: "Deleted row tint", group: "Deleted (Old)", defaultVal: "#fffafa" },
  { id: "colorDelBorder", cssVar: "--del-border", label: "Deleted underline", group: "Deleted (Old)", defaultVal: "#e57373" },
  { id: "colorDelText", cssVar: "--del-text", label: "Deleted text", group: "Deleted (Old)", defaultVal: "#7f1d1d" },
  { id: "colorInsChar", cssVar: "--ins-char", label: "Added word / token", group: "Added (New)", defaultVal: "#fef3c7" },
  { id: "colorInsCharSub", cssVar: "--ins-char-sub", label: "Added intra-word char", group: "Added (New)", defaultVal: "#fde047" },
  { id: "colorInsWordBase", cssVar: "--ins-word-base", label: "Added word wrapper", group: "Added (New)", defaultVal: "#fffdf5" },
  { id: "colorInsRow", cssVar: "--ins-row", label: "Added row tint", group: "Added (New)", defaultVal: "#fefce8" },
  { id: "colorInsBorder", cssVar: "--ins-border", label: "Added underline", group: "Added (New)", defaultVal: "#ca8a04" },
  { id: "colorInsText", cssVar: "--ins-text", label: "Added text", group: "Added (New)", defaultVal: "#713f12" },
  { id: "colorSandhiChar", cssVar: "--sandhi-char", label: "Sandhi span highlight", group: "Interword Sandhi", defaultVal: "#ede9fe" },
  { id: "colorSandhiCharSub", cssVar: "--sandhi-char-sub", label: "Sandhi intra-word char", group: "Interword Sandhi", defaultVal: "#ddd6fe" },
  { id: "colorSandhiBorder", cssVar: "--sandhi-border", label: "Sandhi underline", group: "Interword Sandhi", defaultVal: "#7c3aed" },
  { id: "colorSandhiText", cssVar: "--sandhi-text", label: "Sandhi text", group: "Interword Sandhi", defaultVal: "#4c1d95" },
];

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function setStatus(msg, autoClearMs = 0) {
  const el = document.getElementById("statusText");
  if (!el) return;
  el.textContent = msg || "";
  el.title = msg || "";
  if (autoClearMs > 0 && msg) {
    setTimeout(() => {
      if (el.textContent === msg) {
        el.textContent = "";
        el.title = "";
      }
    }, autoClearMs);
  }
}

function showError(msg) {
  const el = document.getElementById("errorBanner");
  if (!el) return;
  if (!msg) {
    el.style.display = "none";
    el.textContent = "";
    return;
  }
  el.textContent = msg;
  el.style.display = "block";
}

async function apiJson(url, opts = null, fallbackErr = "Request failed") {
  if (typeof window !== "undefined" && typeof window.staticApiHandler === "function") {
    return window.staticApiHandler(url, opts, fallbackErr);
  }
  let resp;
  try {
    resp = await fetch(url, opts || undefined);
  } catch (err) {
    throw new Error(`${fallbackErr}: ${err?.message || err}`);
  }
  let data = null;
  try {
    data = await resp.json();
  } catch (_) {
    data = null;
  }
  if (!resp.ok) {
    const detail = data?.detail || data?.message || `HTTP ${resp.status}`;
    throw new Error(detail);
  }
  return data;
}

function applyEditorFont(pxVal, storageKey = "github_pr_review_font_px") {
  const px = Math.max(14, Math.min(42, Number(pxVal) || 26));
  document.documentElement.style.setProperty("--dev-font-size", `${px}px`);
  const inp = document.getElementById("fontPxInput");
  if (inp) inp.value = px;
  try { localStorage.setItem(storageKey, String(px)); } catch (_) {}
}

function getCurrentDiffColorMap() {
  const out = {};
  for (const f of DIFF_COLOR_FIELDS) out[f.cssVar] = f.defaultVal;
  try {
    const raw = localStorage.getItem(DIFF_COLOR_STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object") {
        for (const f of DIFF_COLOR_FIELDS) {
          const v = parsed[f.cssVar];
          if (typeof v === "string" && /^#[0-9a-fA-F]{6}$/.test(v)) {
            out[f.cssVar] = v.toLowerCase();
          }
        }
      }
    }
  } catch (_) {}
  return out;
}

function syncColorControlValues(colorMap) {
  for (const f of DIFF_COLOR_FIELDS) {
    const val = (colorMap[f.cssVar] || f.defaultVal).toLowerCase();
    const inp = document.getElementById(f.id);
    if (inp) inp.value = val;
    const hexEl = document.getElementById(`${f.id}_hex`);
    if (hexEl) hexEl.textContent = val;
  }
}

function applyAllDiffColors(colorMap, persist = false) {
  for (const f of DIFF_COLOR_FIELDS) {
    const val = colorMap[f.cssVar] || f.defaultVal;
    document.documentElement.style.setProperty(f.cssVar, val);
  }
  syncColorControlValues(colorMap);
  if (persist) {
    try { localStorage.setItem(DIFF_COLOR_STORAGE_KEY, JSON.stringify(colorMap)); } catch (_) {}
  }
}

function setDiffColor(cssVar, hexVal) {
  if (!/^#[0-9a-fA-F]{6}$/.test(String(hexVal || ""))) return;
  const val = String(hexVal).toLowerCase();
  const cur = getCurrentDiffColorMap();
  cur[cssVar] = val;
  applyAllDiffColors(cur, true);
}

function resetDiffColors() {
  const defaults = {};
  for (const f of DIFF_COLOR_FIELDS) defaults[f.cssVar] = f.defaultVal;
  applyAllDiffColors(defaults, true);
  setStatus("Diff colors reset to defaults", 2000);
}

function isSandhiHighlightEnabled() {
  return !!sandhiHighlightEnabled;
}

function syncSandhiButtonUi() {
  const btn = document.getElementById("sandhiHighlightBtn");
  if (btn && btn.classList) btn.classList.toggle("active", !!sandhiHighlightEnabled);
}

function setSandhiHighlightEnabled(nextVal, persist = true) {
  sandhiHighlightEnabled = !!nextVal;
  syncSandhiButtonUi();
  if (persist) {
    try { localStorage.setItem(DIFF_SANDHI_STORAGE_KEY, sandhiHighlightEnabled ? "true" : "false"); } catch (_) {}
  }
}

function renderSettingsPaneHtml() {
  const cur = getCurrentDiffColorMap();
  const groups = ["Deleted (Old)", "Added (New)", "Interword Sandhi"];
  const groupsHtml = groups.map(grp => {
    const rows = DIFF_COLOR_FIELDS.filter(f => f.group === grp).map(f => {
      const val = (cur[f.cssVar] || f.defaultVal).toLowerCase();
      return `<label class="color-picker-row">
        <span>${esc(f.label)}</span>
        <span class="color-picker-ctrl">
          <span id="${f.id}_hex" class="mono color-hex">${esc(val)}</span>
          <input id="${f.id}" type="color" value="${esc(val)}" data-css-var="${esc(f.cssVar)}"
            oninput="setDiffColor('${esc(f.cssVar)}', this.value)"
            onchange="setDiffColor('${esc(f.cssVar)}', this.value)" />
        </span>
      </label>`;
    }).join("");
    return `<div class="settings-group">
      <div class="settings-group-title">${esc(grp)}</div>
      ${rows}
    </div>`;
  }).join("");

  return `
    <div class="row" style="justify-content:space-between;">
      <strong style="font-size:19px;">⚙ Diff Color Settings</strong>
      <div class="row" style="gap:6px;">
        <button id="resetColorsBtn" class="small" type="button" onclick="resetDiffColors()">↺ Reset Defaults</button>
        <button id="closeSettingsBtn" class="small" type="button" onclick="closeSettingsPane()">✕ Close</button>
      </div>
    </div>
    <div class="settings-preview">
      <div class="settings-preview-line del-sample">
        <span class="ch-del ch-has-sub">पा<span class="ch-sub">०</span></span>
        <span class="ch-del">[यान्त्रिकम्]</span> (deleted line / token preview)
      </div>
      <div class="settings-preview-line ins-sample">
        <span class="ch-ins ch-has-sub">पा<span class="ch-sub">॰</span></span>
        <span class="ch-ins">नवीनम्</span> (added line / token preview)
      </div>
      <div class="settings-preview-line sandhi-sample">
        <span class="ch-pair ch-sandhi"><span class="ch-del ch-has-sub ch-sandhi">अस्<span class="ch-sub">ति इ</span>ति</span><span class="ch-sep">➤➤</span><span class="ch-ins ch-has-sub ch-sandhi">अस्<span class="ch-sub">ती</span>ति</span></span> (interword sandhi preview)
      </div>
    </div>
    ${groupsHtml}
  `;
}

function isSettingsPaneOpen() {
  const pane = typeof document !== "undefined" && typeof document.getElementById === "function"
    ? document.getElementById("settingsPane")
    : null;
  return Boolean(pane && pane.style && pane.style.display && pane.style.display !== "none");
}

function toggleSettingsPane(forceOpen = null) {
  const pane = document.getElementById("settingsPane");
  if (!pane) return;
  if (!pane.innerHTML) pane.innerHTML = renderSettingsPaneHtml();
  const nextOpen = forceOpen === null ? !isSettingsPaneOpen() : !!forceOpen;
  if (nextOpen) {
    if (typeof closeHelpPane === "function" && isHelpPaneOpen()) closeHelpPane();
    if (typeof document.querySelector === "function") {
      const hdr = document.querySelector("header");
      if (hdr && typeof hdr.getBoundingClientRect === "function") {
        const topPx = Math.max(48, Math.round(hdr.getBoundingClientRect().bottom + 8));
        pane.style.top = `${topPx}px`;
        pane.style.maxHeight = `calc(100vh - ${topPx + 14}px)`;
      }
    }
  }
  pane.style.display = nextOpen ? "flex" : "none";
  if (nextOpen) syncColorControlValues(getCurrentDiffColorMap());
  const btn = document.getElementById("settingsBtn");
  if (btn && btn.classList) btn.classList.toggle("active", nextOpen);
}

function closeSettingsPane() {
  toggleSettingsPane(false);
}

function isHelpPaneOpen() {
  const pane = typeof document !== "undefined" && typeof document.getElementById === "function"
    ? document.getElementById("helpPane")
    : null;
  return Boolean(pane && pane.style && pane.style.display && pane.style.display !== "none");
}

function toggleHelpPane(forceOpen = null) {
  const pane = typeof document !== "undefined" && typeof document.getElementById === "function"
    ? document.getElementById("helpPane")
    : null;
  if (!pane) return;
  const nextOpen = forceOpen === null ? !isHelpPaneOpen() : !!forceOpen;
  if (nextOpen) {
    if (typeof closeSettingsPane === "function" && isSettingsPaneOpen()) closeSettingsPane();
    if (typeof document.querySelector === "function") {
      const hdr = document.querySelector("header");
      if (hdr && typeof hdr.getBoundingClientRect === "function") {
        const topPx = Math.max(48, Math.round(hdr.getBoundingClientRect().bottom + 8));
        pane.style.top = `${topPx}px`;
        pane.style.maxHeight = `calc(100vh - ${topPx + 14}px)`;
      }
    }
  }
  if (pane.style) pane.style.display = nextOpen ? "flex" : "none";
  const btn = typeof document.getElementById === "function" ? document.getElementById("helpBtn") : null;
  if (btn && btn.classList) btn.classList.toggle("active", nextOpen);
}

function closeHelpPane() {
  toggleHelpPane(false);
}

let _diffRowMouseBound = false;
let _diffMouseDownPos = null;

function _extractDiffInlineNodeText(node, isNlStandalone = false) {
  if (!node) return "";
  if (node.nodeType === 3) return String(node.nodeValue || "");
  if (node.classList && node.classList.contains("ch-sep")) return "";
  if (typeof node.getAttribute === "function") {
    const dataCh = node.getAttribute("data-ch");
    if (dataCh != null) return dataCh;
  }
  if (node.classList && (node.classList.contains("nl-del") || node.classList.contains("nl-ins"))) {
    return isNlStandalone ? "↵" : "\n";
  }
  const children = node.childNodes;
  if (children && children.length) {
    let acc = "";
    for (let i = 0; i < children.length; i++) {
      acc += _extractDiffInlineNodeText(children[i], isNlStandalone);
    }
    return acc;
  }
  return String(node.textContent ?? node.innerText ?? "");
}

function findClickedDiffBox(targetEl) {
  if (!targetEl || typeof targetEl.closest !== "function") return null;
  if (targetEl.closest("button, a, input, textarea, select, summary, .thread-box, .inline-composer, #diffRowContextMenu, #diffFindBar, #settingsPane")) {
    return null;
  }
  const rowEl = targetEl.closest("tr.diff-row");
  if (!rowEl) return null;
  const pair = targetEl.closest(".ch-pair");
  if (pair && (!rowEl.contains || rowEl.contains(pair))) return pair;
  const single = targetEl.closest(".ch-del, .ch-ins, .nl-del, .nl-ins");
  if (single && (!rowEl.contains || rowEl.contains(single)) && (!single.closest || !single.closest(".ch-pair"))) {
    return single;
  }
  return null;
}

function extractDiffBoxOldNewText(boxEl) {
  if (!boxEl || !boxEl.classList) return "";
  let oldText = "";
  let newText = "";
  if (boxEl.classList.contains("ch-pair")) {
    const rawOld = typeof boxEl.getAttribute === "function" ? boxEl.getAttribute("data-old") : null;
    const rawNew = typeof boxEl.getAttribute === "function" ? boxEl.getAttribute("data-new") : null;
    if (rawOld != null && rawNew != null) {
      oldText = rawOld;
      newText = rawNew;
    } else {
      const oldParts = [];
      const newParts = [];
      const children = boxEl.childNodes || [];
      for (let i = 0; i < children.length; i++) {
        const child = children[i];
        if (child && child.classList) {
          if (child.classList.contains("ch-sep")) continue;
          if (child.classList.contains("ch-del")) oldParts.push(_extractDiffInlineNodeText(child, false));
          else if (child.classList.contains("ch-ins")) newParts.push(_extractDiffInlineNodeText(child, false));
          else if (child.classList.contains("nl-del")) oldParts.push("\n");
          else if (child.classList.contains("nl-ins")) newParts.push("\n");
          else {
            const neutral = _extractDiffInlineNodeText(child, false);
            oldParts.push(neutral);
            newParts.push(neutral);
          }
        } else {
          const neutral = _extractDiffInlineNodeText(child, false);
          oldParts.push(neutral);
          newParts.push(neutral);
        }
      }
      oldText = oldParts.join("");
      newText = newParts.join("");
    }
  } else if (boxEl.classList.contains("ch-del")) {
    oldText = _extractDiffInlineNodeText(boxEl, false);
  } else if (boxEl.classList.contains("ch-ins")) {
    newText = _extractDiffInlineNodeText(boxEl, false);
  } else if (boxEl.classList.contains("nl-del")) {
    oldText = "↵";
  } else if (boxEl.classList.contains("nl-ins")) {
    newText = "↵";
  } else {
    return "";
  }
  return `${oldText}➤➤${newText}`;
}

async function copyDiffBoxOldNew(boxEl) {
  const txt = extractDiffBoxOldNewText(boxEl);
  if (!txt) return false;
  const ok = await copyTextToClipboard(txt);
  if (ok && boxEl && boxEl.classList) {
    boxEl.classList.add("diff-box-copied");
    setTimeout(() => { if (boxEl.classList) boxEl.classList.remove("diff-box-copied"); }, 900);
  }
  setStatus(ok ? "Copied diff box (OLD➤➤NEW)" : "Copy failed — check clipboard permissions", 2500);
  return ok;
}

function initDiffRowMouseBehavior() {
  if (_diffRowMouseBound || typeof document === "undefined" || typeof document.addEventListener !== "function") return;
  _diffRowMouseBound = true;
  document.addEventListener("mousedown", (ev) => {
    if (!ev) return;
    if (!ev.shiftKey && !ev.ctrlKey && !ev.altKey && !ev.metaKey && (!ev.button || ev.button === 0)) {
      _diffMouseDownPos = { x: Number(ev.clientX || 0), y: Number(ev.clientY || 0) };
    } else {
      _diffMouseDownPos = null;
    }
    if (!ev.shiftKey || ev.ctrlKey || ev.altKey || ev.metaKey) return;
    const tgt = ev.target;
    if (!tgt || typeof tgt.closest !== "function") return;
    if (tgt.closest("button, a, input, textarea, select, summary, .thread-box, .inline-composer")) return;
    if (tgt.closest("tr.diff-row") && typeof ev.preventDefault === "function") {
      ev.preventDefault();
    }
  });
  document.addEventListener("click", (ev) => {
    const downPos = _diffMouseDownPos;
    _diffMouseDownPos = null;
    if (!ev || ev.shiftKey || ev.ctrlKey || ev.altKey || ev.metaKey || (ev.button && ev.button !== 0)) return;
    if (downPos) {
      const dist = Math.hypot(Number(ev.clientX || 0) - downPos.x, Number(ev.clientY || 0) - downPos.y);
      if (dist > 5) return;
    }
    const sel = (typeof window !== "undefined" && window.getSelection) ? String(window.getSelection().toString() || "") : "";
    if (sel) return;
    const menu = document.getElementById("diffRowContextMenu");
    if (menu && menu.style && menu.style.display && menu.style.display !== "none") return;
    const boxEl = findClickedDiffBox(ev.target);
    if (!boxEl) return;
    void copyDiffBoxOldNew(boxEl);
  });
}

function initDiffSettingsPane() {
  initDiffRowMouseBehavior();
  const saved = getCurrentDiffColorMap();
  const pane = document.getElementById("settingsPane");
  if (pane) pane.innerHTML = renderSettingsPaneHtml();
  applyAllDiffColors(saved, false);
  try {
    const savedSandhi = localStorage.getItem(DIFF_SANDHI_STORAGE_KEY);
    if (savedSandhi === "true" || savedSandhi === "false") {
      sandhiHighlightEnabled = savedSandhi === "true";
    }
  } catch (_) {}
  syncSandhiButtonUi();
}

function codepointTitle(txt) {
  const pts = [];
  for (const ch of String(txt || "")) {
    const cp = ch.codePointAt(0).toString(16).toUpperCase().padStart(4, "0");
    pts.push(`U+${cp} ${ch}`);
    if (pts.length >= 12) { pts.push("..."); break; }
  }
  return pts.join(" | ");
}

function renderVisibleTextWithInvisibles(text, tip) {
  const invMap = {
    "\u200b": "[ZWSP]",
    "\u200c": "[ZWNJ]",
    "\u200d": "[ZWJ]",
    "\ufeff": "[BOM]",
    "\u00a0": "[NBSP]",
  };
  let inner = "";
  for (const ch of String(text || "")) {
    if (invMap[ch]) {
      inner += `<span class="ch-inv" data-ch="${esc(ch)}" title="${tip}">${invMap[ch]}</span>`;
    } else if (ch === "\t") {
      inner += "  ";
    } else {
      inner += esc(ch);
    }
  }
  return inner;
}

function renderChangedText(txt, op, subSegments = null, extraCls = "", customTitle = null) {
  const baseCls = op === "del" ? "ch-del" : "ch-ins";
  const cls = extraCls ? `${baseCls} ${extraCls}` : baseCls;
  const raw = String(txt || "");
  const tipRaw = customTitle ? `${customTitle} | ${codepointTitle(raw)}` : codepointTitle(raw);
  const tip = esc(tipRaw);
  const fmtWs = s => esc(s.replace(/\t/g, "  "));

  if (Array.isArray(subSegments) && subSegments.length && raw) {
    const trimmed = subSegments.map(s => ({ op: s.op, text: String(s.text || "") })).filter(s => s.text);
    let subLead = "";
    let subTrail = "";
    if (trimmed.length && trimmed[0].op === "eq") {
      const ml = trimmed[0].text.match(/^([ \t]*)([\s\S]*)$/);
      if (ml) {
        subLead = ml[1];
        trimmed[0].text = ml[2];
      }
    }
    if (trimmed.length && trimmed[trimmed.length - 1].op === "eq") {
      const mt = trimmed[trimmed.length - 1].text.match(/^([\s\S]*?)([ \t]*)$/);
      if (mt) {
        trimmed[trimmed.length - 1].text = mt[1];
        subTrail = mt[2];
      }
    }
    const innerSub = trimmed.filter(s => s.text).map(s => {
      const htmlPart = renderVisibleTextWithInvisibles(s.text, tip);
      return (s.op === "del" || s.op === "ins") ? `<span class="ch-sub">${htmlPart}</span>` : htmlPart;
    }).join("");
    if (innerSub) {
      return `${fmtWs(subLead)}<span class="${cls} ch-has-sub" title="${tip}">${innerSub}</span>${fmtWs(subTrail)}`;
    }
  }

  const inner = renderVisibleTextWithInvisibles(raw, tip);
  return `<span class="${cls}" title="${tip}">${inner}</span>`;
}

function graphemeUnitsClient(str) {
  if (typeof Intl !== "undefined" && Intl.Segmenter) {
    const seg = new Intl.Segmenter(undefined, { granularity: "grapheme" });
    return Array.from(seg.segment(str), s => s.segment);
  }
  return Array.from(str);
}

function computeClientWordSubPair(delText, insText) {
  const mL = String(delText || "").match(/^([ \t]*)([\s\S]*?)([ \t]*)$/);
  const mR = String(insText || "").match(/^([ \t]*)([\s\S]*?)([ \t]*)$/);
  const lLead = mL ? mL[1] : "", lCore = mL ? mL[2] : "", lTrail = mL ? mL[3] : "";
  const rLead = mR ? mR[1] : "", rCore = mR ? mR[2] : "", rTrail = mR ? mR[3] : "";
  if (!lCore || !rCore) return null;
  const a = graphemeUnitsClient(lCore), b = graphemeUnitsClient(rCore);
  if (a.length * b.length > 40000) return null;
  const m = a.length, n = b.length;
  const dp = Array.from({ length: m + 1 }, () => new Int32Array(n + 1));
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? 1 + dp[i + 1][j + 1] : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const lSub = [], rSub = [];
  let i = 0, j = 0, hasNonWsEq = false;
  const pushOp = (arr, op, t) => {
    if (!t) return;
    if (arr.length && arr[arr.length - 1].op === op) arr[arr.length - 1].text += t;
    else arr.push({ op, text: t });
  };
  while (i < m && j < n) {
    if (a[i] === b[j]) {
      if (a[i].trim()) hasNonWsEq = true;
      pushOp(lSub, "eq", a[i]);
      pushOp(rSub, "eq", b[j]);
      i++; j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      pushOp(lSub, "del", a[i++]);
    } else {
      pushOp(rSub, "ins", b[j++]);
    }
  }
  while (i < m) pushOp(lSub, "del", a[i++]);
  while (j < n) pushOp(rSub, "ins", b[j++]);
  if (!hasNonWsEq) return null;
  return {
    l: [...(lLead ? [{ op: "eq", text: lLead }] : []), ...lSub, ...(lTrail ? [{ op: "eq", text: lTrail }] : [])],
    r: [...(rLead ? [{ op: "eq", text: rLead }] : []), ...rSub, ...(rTrail ? [{ op: "eq", text: rTrail }] : [])],
  };
}

function _getDiffSanEngine() {
  if (typeof DiffSanEngine !== "undefined" && DiffSanEngine) return DiffSanEngine;
  if (typeof window !== "undefined" && window.DiffSanEngine) return window.DiffSanEngine;
  if (typeof globalThis !== "undefined" && globalThis.DiffSanEngine) return globalThis.DiffSanEngine;
  return null;
}

function _wrapOldNewPairHtml(innerHtml, isSandhi, oldRaw = null, newRaw = null) {
  if (!innerHtml) return "";
  const mLead = innerHtml.match(/^([ \t]*)([\s\S]*)$/);
  const leadWs = (mLead && mLead[2]) ? mLead[1] : "";
  const afterLead = (mLead && mLead[2]) ? mLead[2] : innerHtml;
  const mTrail = afterLead.match(/^([\s\S]*?)([ \t]*)$/);
  const coreHtml = (mTrail && mTrail[1]) ? mTrail[1] : afterLead;
  const trailWs = (mTrail && mTrail[1]) ? mTrail[2] : "";
  const cls = isSandhi ? "ch-pair ch-sandhi" : "ch-pair";
  const attrs = (oldRaw != null && newRaw != null)
    ? ` data-old="${esc(oldRaw)}" data-new="${esc(newRaw)}"`
    : "";
  return `${leadWs}<span class="${cls}"${attrs}>${coreHtml}</span>${trailWs}`;
}

function findFirstVisibleRowMeta(rows, paneEl = null) {
  if (!Array.isArray(rows) || !rows.length) return null;
  const pane = paneEl || document.getElementById("mainPane");
  if (!pane || typeof pane.getBoundingClientRect !== "function") {
    return rows[0] || null;
  }
  const paneRect = pane.getBoundingClientRect();
  if (!paneRect || paneRect.height <= 0) {
    return rows[0] || null;
  }
  const paneTop = paneRect.top;
  const paneBottom = paneRect.bottom;
  let partialFallback = null;
  for (const r of rows) {
    const el = document.getElementById(r.domId);
    if (!el || typeof el.getBoundingClientRect !== "function") continue;
    const rect = el.getBoundingClientRect();
    if (!rect || (rect.width === 0 && rect.height === 0)) continue;
    if (rect.bottom <= paneTop) continue;
    if (rect.top >= paneBottom) break;
    if (!partialFallback) partialFallback = r;
    const visiblePx = Math.min(rect.bottom, paneBottom) - Math.max(rect.top, paneTop);
    const minReadable = Math.min(20, Math.max(4, rect.height * 0.5));
    if (rect.top >= paneTop - 2 || visiblePx >= minReadable) {
      return r;
    }
  }
  return partialFallback || null;
}

function pageScrollDiffPane(direction, rows, selectRowFn, paneEl = null) {
  const pane = paneEl || document.getElementById("mainPane");
  if (!pane) return null;
  const viewH = pane.clientHeight || (typeof window !== "undefined" ? window.innerHeight : 0) || 600;
  const step = Math.max(60, Math.round(viewH * 0.875));
  const maxScroll = Math.max(0, (pane.scrollHeight || 0) - viewH);
  const nextTop = Math.max(0, Math.min(maxScroll, (pane.scrollTop || 0) + direction * step));
  pane.scrollTop = nextTop;
  const firstMeta = findFirstVisibleRowMeta(rows, pane);
  if (firstMeta && typeof selectRowFn === "function") {
    selectRowFn(firstMeta.rowKey);
  }
  return firstMeta;
}

function renderDiffSegments(segments, diffMode = null, sandhiContext = null) {
  if (!segments || !segments.length) return "";
  const activeMode = diffMode !== null ? diffMode : (typeof getDiffMode === "function" ? getDiffMode() : "word");
  const fallbackSubs = {};
  if (activeMode === "word") {
    for (let i = 0; i < segments.length; i++) {
      const s = segments[i];
      if (s.op === "del" && !s.sub_segments) {
        let j = i + 1;
        while (j < segments.length && (segments[j].op === "del_nl" || segments[j].op === "ins_nl")) j++;
        if (j < segments.length && segments[j].op === "ins" && !segments[j].sub_segments) {
          const pair = computeClientWordSubPair(s.text, segments[j].text);
          if (pair) {
            fallbackSubs[i] = pair.l;
            fallbackSubs[j] = pair.r;
          }
        }
      }
    }
  }
  let sandhiMarks = null;
  const eng = _getDiffSanEngine();
  if (sandhiHighlightEnabled && eng && typeof eng.findSandhiMarkedIndices === "function") {
    sandhiMarks = eng.findSandhiMarkedIndices(segments, sandhiContext);
  }
  const renderSubParts = (parts, isSandhi) => parts.map(p => (
    p.op === "eq"
      ? esc(p.text)
      : renderChangedText(
          p.text,
          p.op,
          null,
          isSandhi ? "ch-sandhi" : "",
          isSandhi ? "Interword sandhi change" : null
        )
  )).join("");
  const renderOneSegment = (idx) => {
    const s = segments[idx];
    if (s.op === "eq") return esc(s.text);
    const isS = !!(sandhiMarks && sandhiMarks.has(idx));
    if (s.op === "del_nl") return `<span class="nl-badge nl-del${isS ? " ch-sandhi" : ""}" title="${isS ? "Interword sandhi change | Removed line break" : "Removed line break"}">↵</span>`;
    if (s.op === "ins_nl") return `<span class="nl-badge nl-ins${isS ? " ch-sandhi" : ""}" title="${isS ? "Interword sandhi change | Inserted line break" : "Inserted line break"}">↵</span>`;
    return renderChangedText(
      s.text,
      s.op,
      s.sub_segments || fallbackSubs[idx],
      isS ? "ch-sandhi" : "",
      isS ? "Interword sandhi change" : null
    );
  };
  let out = "";
  for (let i = 0; i < segments.length; i++) {
    const seg = segments[i];
    if (seg.op === "del" && !sandhiContext) {
      let k = i;
      let sawIns = false;
      let lastContentIdx = i;
      while (k < segments.length) {
        const op = segments[k].op;
        if (op === "eq") break;
        if (op === "del") {
          if (sawIns) break;
          lastContentIdx = k;
        } else if (op === "ins") {
          sawIns = true;
          lastContentIdx = k;
        } else if (op !== "del_nl" && op !== "ins_nl") {
          break;
        }
        k += 1;
      }
      if (sawIns) {
        if (
          lastContentIdx === i + 1 &&
          segments[i + 1].op === "ins" &&
          sandhiHighlightEnabled &&
          eng &&
          typeof eng.splitMixedSandhiPair === "function" &&
          !(sandhiMarks && (sandhiMarks.has(i) || sandhiMarks.has(i + 1))) &&
          (/\s/.test(seg.text || "") || /\s/.test(segments[i + 1].text || ""))
        ) {
          const splitSegs = eng.splitMixedSandhiPair(seg.text, segments[i + 1].text);
          if (splitSegs) {
            out += renderDiffSegments(splitSegs, activeMode, null);
            i = lastContentIdx;
            continue;
          }
        }
        if (lastContentIdx === i + 1 && segments[i + 1].op === "ins" && activeMode === "word") {
          const nxt = segments[i + 1];
          let dSub = seg.sub_segments || fallbackSubs[i];
          let iSub = nxt.sub_segments || fallbackSubs[i + 1];
          if (!dSub || !iSub) {
            const pair = computeClientWordSubPair(seg.text, nxt.text);
            if (pair) {
              dSub = pair.l;
              iSub = pair.r;
            }
          }
          if (dSub && iSub) {
            const pairIsSandhi = !!(sandhiMarks && (sandhiMarks.has(i) || sandhiMarks.has(i + 1)));
            const pureDel = iSub.every(p => p.op === "eq") &&
              dSub.some(p => p.op === "del") &&
              dSub.some(p => p.op === "eq" && String(p.text || "").trim()) &&
              dSub.filter(p => p.op === "eq").map(p => p.text).join("") === String(nxt.text || "");
            if (pureDel) {
              out += renderSubParts(dSub, pairIsSandhi);
              i = lastContentIdx;
              continue;
            }
            const pureIns = dSub.every(p => p.op === "eq") &&
              iSub.some(p => p.op === "ins") &&
              iSub.some(p => p.op === "eq" && String(p.text || "").trim()) &&
              iSub.filter(p => p.op === "eq").map(p => p.text).join("") === String(seg.text || "");
            if (pureIns) {
              out += renderSubParts(iSub, pairIsSandhi);
              i = lastContentIdx;
              continue;
            }
          }
        }
        let oldHtml = "";
        let newHtml = "";
        let oldParts = [];
        let newParts = [];
        let pairIsSandhi = false;
        for (let p = i; p <= lastContentIdx; p++) {
          if (sandhiMarks && sandhiMarks.has(p)) pairIsSandhi = true;
          const ps = segments[p];
          if (ps.op === "del") oldParts.push(ps.text || "");
          else if (ps.op === "del_nl") oldParts.push("\n");
          else if (ps.op === "ins") newParts.push(ps.text || "");
          else if (ps.op === "ins_nl") newParts.push("\n");
          const segHtml = renderOneSegment(p);
          if (ps.op === "ins" || ps.op === "ins_nl") newHtml += segHtml;
          else oldHtml += segHtml;
        }
        const pairSepHtml = (oldHtml && newHtml) ? `<span class="ch-sep">➤➤</span>` : "";
        const pairHtml = `${oldHtml}${pairSepHtml}${newHtml}`;
        out += _wrapOldNewPairHtml(pairHtml, pairIsSandhi, oldParts.join(""), newParts.join(""));
        i = lastContentIdx;
        continue;
      }
    }
    out += renderOneSegment(i);
  }
  return out;
}

async function lookupSelectedGoldenDict() {
  const sel = (window.getSelection ? window.getSelection().toString() : "").trim();
  if (!sel) {
    setStatus("Select a Sanskrit word first to look up in GoldenDict.", 2500);
    return;
  }
  try {
    const data = await apiJson("/api/open_in_goldendict", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ word: sel }),
    }, "GoldenDict lookup failed");
    setStatus(`GoldenDict: ${data.word}`, 2000);
  } catch (e) {
    showError(e.message);
  }
}

let diffExpandedGaps = {};

function clearExpandedGaps(prefix = "") {
  if (!prefix) {
    diffExpandedGaps = {};
    return;
  }
  for (const k of Object.keys(diffExpandedGaps)) {
    if (k.startsWith(prefix)) delete diffExpandedGaps[k];
  }
}

function setGapExpandedState(gapKey, dir, count = 20) {
  const cur = (diffExpandedGaps[gapKey] ||= { top: 0, bottom: 0, all: false });
  if (dir === "all") cur.all = true;
  else if (dir === "top") cur.top += Number(count || 20);
  else if (dir === "bottom") cur.bottom += Number(count || 20);
}

function buildGapAwareRenderItems(rows, {
  gapPrefix = "g",
  mode = "none", // "none" | "changed_only" | "long_runs"
  contextRadius = 3,
  minCollapse = 8,
  isPinnedFn = null,
} = {}) {
  const n = (rows || []).length;
  if (!n) return [];
  if (mode === "none") {
    return rows.map((r, ri) => ({ type: "row", r, ri }));
  }

  const pinned = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    const r = rows[i];
    if (r.kind !== "context" || (isPinnedFn && isPinnedFn(r, i))) {
      pinned[i] = 1;
    }
  }

  const baseVisible = new Uint8Array(n);
  if (mode === "changed_only") {
    for (let i = 0; i < n; i++) baseVisible[i] = pinned[i];
  } else {
    // long_runs: keep contextRadius lines around pinned rows
    for (let i = 0; i < n; i++) {
      if (!pinned[i]) continue;
      const lo = Math.max(0, i - contextRadius);
      const hi = Math.min(n - 1, i + contextRadius);
      for (let k = lo; k <= hi; k++) baseVisible[k] = 1;
    }
  }

  const out = [];
  let i = 0;
  while (i < n) {
    if (baseVisible[i]) {
      out.push({ type: "row", r: rows[i], ri: i });
      i += 1;
      continue;
    }
    const gStart = i;
    while (i < n && !baseVisible[i]) i += 1;
    const gEnd = i - 1;
    const spanLen = gEnd - gStart + 1;
    const anchorL = rows[gStart].left_num ?? "L";
    const anchorR = rows[gStart].right_num ?? "R";
    const gapKey = `${gapPrefix}:${gStart}:${anchorL}_${anchorR}`;
    const exp = diffExpandedGaps[gapKey];

    if (exp?.all || (mode === "long_runs" && !exp && spanLen < minCollapse)) {
      for (let k = gStart; k <= gEnd; k++) out.push({ type: "row", r: rows[k], ri: k });
      continue;
    }

    const topN = Math.min(spanLen, exp?.top || 0);
    const botN = Math.min(spanLen - topN, exp?.bottom || 0);
    for (let k = gStart; k < gStart + topN; k++) out.push({ type: "row", r: rows[k], ri: k });

    const remStart = gStart + topN;
    const remEnd = gEnd - botN;
    if (remStart <= remEnd) {
      const remCount = remEnd - remStart + 1;
      if (mode === "long_runs" && !exp && remCount < minCollapse) {
        for (let k = remStart; k <= remEnd; k++) out.push({ type: "row", r: rows[k], ri: k });
      } else {
        out.push({
          type: "gap",
          gapKey,
          hiddenCount: remCount,
          canTop: gStart > 0,
          canBottom: gEnd < n - 1,
          firstLeft: rows[remStart].left_num,
          lastLeft: rows[remEnd].left_num,
          firstRight: rows[remStart].right_num,
          lastRight: rows[remEnd].right_num,
        });
      }
    }

    for (let k = gEnd - botN + 1; k <= gEnd; k++) out.push({ type: "row", r: rows[k], ri: k });
  }
  return out;
}

function renderGapRowHtml(gap, colspan, fnName = "expandGapRow") {
  const encodedKey = esc(JSON.stringify(gap.gapKey));
  const rangeTxt = (gap.firstRight != null && gap.lastRight != null)
    ? ` (lines ${gap.firstRight}–${gap.lastRight})`
    : ((gap.firstLeft != null && gap.lastLeft != null) ? ` (LHS ${gap.firstLeft}–${gap.lastLeft})` : "");
  return `<tr class="diff-gap-row" title="Click to expand collapsed lines" onclick="${fnName}(${encodedKey}, 'all', 0)">
    <td colspan="${colspan}">
      <div class="diff-gap-bar">
        <span>⋯ ${gap.hiddenCount} collapsed context line${gap.hiddenCount === 1 ? '' : 's'}${esc(rangeTxt)}</span>
      </div>
    </td>
  </tr>`;
}

function _rowMatchesSelector(r, selector) {
  if (!r || !r.domId || typeof document === "undefined" || typeof document.getElementById !== "function") return false;
  const el = document.getElementById(r.domId);
  if (!el) return false;
  if (typeof el.querySelector === "function") return Boolean(el.querySelector(selector));
  if (selector === ".ch-sandhi" && typeof el.innerHTML === "string") return el.innerHTML.includes("ch-sandhi");
  return false;
}

function moveSelectionBySelector(rows, currentRowKey, selectRowFn, step = 1, selector = ".ch-sandhi", label = "Sandhi diff") {
  if (!Array.isArray(rows) || !rows.length) {
    setStatus(`No ${label.toLowerCase()} matches in current view.`, 2500);
    return null;
  }
  const matches = [];
  for (let i = 0; i < rows.length; i++) {
    if (_rowMatchesSelector(rows[i], selector)) {
      matches.push({ idx: i, matchNum: matches.length + 1, row: rows[i] });
    }
  }
  if (!matches.length) {
    setStatus(`No ${label.toLowerCase()} matches in current view.`, 2500);
    return null;
  }
  const curIdx = currentRowKey ? rows.findIndex(r => r.rowKey === currentRowKey) : -1;
  let chosen = null;
  if (step >= 0) {
    chosen = matches.find(m => m.idx > curIdx) || matches[0];
  } else {
    for (let k = matches.length - 1; k >= 0; k--) {
      if (curIdx < 0 || matches[k].idx < curIdx) { chosen = matches[k]; break; }
    }
    if (!chosen) chosen = matches[matches.length - 1];
  }
  if (chosen && typeof selectRowFn === "function") {
    selectRowFn(chosen.row.rowKey);
    const el = document.getElementById(chosen.row.domId);
    if (el && typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ block: "center" });
    }
    setStatus(`${label} ${chosen.matchNum} / ${matches.length}`, 2000);
  }
  return chosen ? chosen.row : null;
}

function _extractRowSideText(meta, targetSide) {
  if (!meta) return "";
  if (targetSide === "left" && meta.leftText != null) return String(meta.leftText);
  if (targetSide === "right" && meta.rightText != null) return String(meta.rightText);
  const segs = meta.segments;
  if (!Array.isArray(segs) || !segs.length) return "";
  let out = "";
  for (const s of segs) {
    if (s.op === "eq") out += s.text || "";
    else if (targetSide === "left" && s.op === "del") out += s.text || "";
    else if (targetSide === "left" && s.op === "del_nl") out += "\n";
    else if (targetSide === "right" && s.op === "ins") out += s.text || "";
    else if (targetSide === "right" && s.op === "ins_nl") out += "\n";
  }
  return out;
}

function buildRowBugReportDiffText(meta, appLabel = "diff_san") {
  if (!meta) return "";
  const opts = typeof getDiffOptions === "function" ? getDiffOptions() : {};
  const singlePane = typeof isSinglePaneMode === "function" ? isSinglePaneMode() : null;
  const lineParts = [];
  if (meta.leftNum != null) lineParts.push(`L${meta.leftNum}`);
  if (meta.rightNum != null) lineParts.push(`R${meta.rightNum}`);
  const headerMeta = [
    appLabel,
    meta.filename || "",
    lineParts.join("->") || meta.pos || "",
    `unit=${opts.diff_mode || "word"}`,
    `norm=${opts.normalize || "none"}`,
    `ignore_punct=${Boolean(opts.ignore_punct)}`,
    `sandhi=${Boolean(sandhiHighlightEnabled)}`,
    singlePane === null ? "" : `single_pane=${singlePane}`,
  ].filter(Boolean).join(" · ");
  const oldTxt = _extractRowSideText(meta, "left");
  const newTxt = _extractRowSideText(meta, "right");
  return `Diff bug report (${headerMeta})\nOLD:\n${oldTxt}\nNEW:\n${newTxt}`;
}

async function copyTextToClipboard(text) {
  const s = String(text ?? "");
  if (typeof navigator !== "undefined" && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    try {
      await navigator.clipboard.writeText(s);
      return true;
    } catch (_) {}
  }
  if (typeof document !== "undefined" && typeof document.createElement === "function" && document.body) {
    try {
      const ta = document.createElement("textarea");
      ta.value = s;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = typeof document.execCommand === "function" ? document.execCommand("copy") : false;
      document.body.removeChild(ta);
      if (ok) return true;
    } catch (_) {}
  }
  return false;
}

let _ctxMenuBound = false;
let _activeCtxMeta = null;
let _activeCtxTarget = null;
let _ctxSelectFn = null;
let _ctxAppLabelFn = null;
let _ctxExtraItems = [];

function sha256Hex(input) {
  const s = String(input ?? "");
  let bytes;
  if (typeof TextEncoder !== "undefined") {
    bytes = new TextEncoder().encode(s);
  } else {
    const utf8 = unescape(encodeURIComponent(s));
    bytes = new Uint8Array(utf8.length);
    for (let i = 0; i < utf8.length; i++) bytes[i] = utf8.charCodeAt(i);
  }
  const K = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ];
  const H = [
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ];
  const bitLen = bytes.length * 8;
  const totalLen = Math.ceil((bytes.length + 9) / 64) * 64;
  const padded = new Uint8Array(totalLen);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const hiBits = Math.floor(bitLen / 0x100000000);
  const loBits = bitLen >>> 0;
  const dv = new DataView(padded.buffer);
  dv.setUint32(totalLen - 8, hiBits, false);
  dv.setUint32(totalLen - 4, loBits, false);
  const w = new Uint32Array(64);
  const rotr = (x, n) => (x >>> n) | (x << (32 - n));
  for (let off = 0; off < totalLen; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = dv.getUint32(off + i * 4, false);
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = H;
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ ((~e) & g);
      const t1 = (h + S1 + ch + K[i] + w[i]) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + maj) >>> 0;
      h = g; g = f; f = e; e = (d + t1) >>> 0;
      d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    H[0] = (H[0] + a) >>> 0; H[1] = (H[1] + b) >>> 0; H[2] = (H[2] + c) >>> 0; H[3] = (H[3] + d) >>> 0;
    H[4] = (H[4] + e) >>> 0; H[5] = (H[5] + f) >>> 0; H[6] = (H[6] + g) >>> 0; H[7] = (H[7] + h) >>> 0;
  }
  return H.map(v => v.toString(16).padStart(8, "0")).join("");
}

function resolveDiffRowSideAndLine(meta, targetEl = null) {
  if (!meta) return null;
  const leftNum = meta.leftNum != null ? Number(meta.leftNum) : null;
  const rightNum = meta.rightNum != null ? Number(meta.rightNum) : null;
  if (leftNum == null && rightNum == null) return null;
  const preferLeft = Boolean(
    targetEl && typeof targetEl.closest === "function" && targetEl.closest(".lhs-cell, .ln-left, .ch-del, .nl-del")
  );
  const side = preferLeft
    ? (leftNum != null ? "LEFT" : "RIGHT")
    : (rightNum != null ? "RIGHT" : "LEFT");
  const line = side === "LEFT" ? leftNum : rightNum;
  if (line == null) return null;
  return { side, sideChar: side === "LEFT" ? "L" : "R", line };
}

function toggleWordGraphemeDiffMode(onChangedFn = null) {
  const sel = typeof document !== "undefined" ? document.getElementById("diffModeSelect") : null;
  if (!sel) return null;
  const next = sel.value === "word" ? "grapheme" : "word";
  sel.value = next;
  const opt = sel.options && sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex] : null;
  const label = opt ? opt.textContent : next;
  setStatus(`Diff unit: ${label}`, 2000);
  const res = typeof onChangedFn === "function"
    ? onChangedFn(next)
    : (typeof sel.onchange === "function" ? sel.onchange() : null);
  if (res && typeof res.then === "function") {
    res.then(() => {
      const errEl = typeof document !== "undefined" ? document.getElementById("errorBanner") : null;
      if (!errEl || errEl.style.display === "none" || !errEl.textContent) {
        setStatus(`Diff unit: ${label}`, 2000);
      }
    }).catch(() => {});
  }
  return next;
}

function closeDiffRowContextMenu() {
  const menu = typeof document !== "undefined" ? document.getElementById("diffRowContextMenu") : null;
  if (menu) menu.style.display = "none";
  _activeCtxMeta = null;
  _activeCtxTarget = null;
}

function _ensureDiffRowContextMenuEl() {
  if (typeof document === "undefined" || typeof document.getElementById !== "function") return null;
  let menu = document.getElementById("diffRowContextMenu");
  if (!menu && typeof document.createElement === "function" && document.body) {
    menu = document.createElement("div");
    menu.id = "diffRowContextMenu";
    menu.className = "diff-ctx-menu";
    const extraHtml = (_ctxExtraItems || [])
      .map(it => `<button id="${esc(it.id)}" class="diff-ctx-item" type="button">${esc(it.label)}</button>`)
      .join("");
    menu.innerHTML = `
      ${extraHtml}
      <button id="ctxCopyBoxDiffBtn" class="diff-ctx-item" type="button">📋 Copy this diff box (OLD➤➤NEW)</button>
      <button id="ctxCopyBugDiffBtn" class="diff-ctx-item" type="button">📋 Copy diff for bug report</button>
      <button id="ctxSelectLineBtn" class="diff-ctx-item" type="button">☑ Select line</button>
    `;
    document.body.appendChild(menu);
    for (const it of (_ctxExtraItems || [])) {
      const btn = document.getElementById(it.id);
      if (btn) {
        btn.onclick = async () => {
          const meta = _activeCtxMeta;
          const tgt = _activeCtxTarget;
          closeDiffRowContextMenu();
          if (meta && typeof it.onClick === "function") await it.onClick(meta, tgt);
        };
      }
    }
    const copyBoxBtn = document.getElementById("ctxCopyBoxDiffBtn");
    if (copyBoxBtn) {
      copyBoxBtn.onclick = async () => {
        const tgt = _activeCtxTarget;
        closeDiffRowContextMenu();
        const boxEl = findClickedDiffBox(tgt);
        if (boxEl) await copyDiffBoxOldNew(boxEl);
      };
    }
    const copyBtn = document.getElementById("ctxCopyBugDiffBtn");
    if (copyBtn) {
      copyBtn.onclick = async () => {
        const meta = _activeCtxMeta;
        const appLabel = typeof _ctxAppLabelFn === "function" ? _ctxAppLabelFn() : "diff_san";
        closeDiffRowContextMenu();
        if (!meta) return;
        const txt = buildRowBugReportDiffText(meta, appLabel);
        const ok = await copyTextToClipboard(txt);
        setStatus(ok ? "Copied line diff for bug report" : "Copy failed — check clipboard permissions", 2500);
      };
    }
    const selBtn = document.getElementById("ctxSelectLineBtn");
    if (selBtn) {
      selBtn.onclick = () => {
        const meta = _activeCtxMeta;
        closeDiffRowContextMenu();
        if (meta && typeof _ctxSelectFn === "function") _ctxSelectFn(meta.rowKey);
      };
    }
  }
  return menu;
}

function bindDiffRowContextMenu(getRowsFn, selectRowFn, getAppLabelFn = () => "diff_san", extraItems = []) {
  _ctxSelectFn = selectRowFn;
  _ctxAppLabelFn = getAppLabelFn;
  _ctxExtraItems = Array.isArray(extraItems) ? extraItems : [];
  if (_ctxMenuBound || typeof document === "undefined" || typeof document.addEventListener !== "function") return;
  _ctxMenuBound = true;
  document.addEventListener("contextmenu", (ev) => {
    if (!ev || ev.shiftKey) return;
    const sel = (typeof window !== "undefined" && window.getSelection) ? String(window.getSelection().toString() || "").trim() : "";
    if (sel) return;
    const tgt = ev.target;
    if (!tgt || typeof tgt.closest !== "function") return;
    if (tgt.closest("button, a, input, textarea, select, summary, .thread-box, .inline-composer")) return;
    const rowEl = tgt.closest("tr.diff-row");
    if (!rowEl || !rowEl.id) return;
    const rows = typeof getRowsFn === "function" ? (getRowsFn() || []) : [];
    const meta = rows.find(r => r.domId === rowEl.id);
    if (!meta) return;
    const menu = _ensureDiffRowContextMenuEl();
    if (!menu) return;
    ev.preventDefault();
    _activeCtxMeta = meta;
    _activeCtxTarget = tgt;
    const boxBtn = document.getElementById("ctxCopyBoxDiffBtn");
    if (boxBtn && boxBtn.style) {
      boxBtn.style.display = findClickedDiffBox(tgt) ? "block" : "none";
    }
    for (const it of (_ctxExtraItems || [])) {
      const btn = document.getElementById(it.id);
      if (btn && btn.style) {
        const vis = typeof it.isVisible === "function" ? it.isVisible(meta, tgt) : true;
        btn.style.display = vis ? "block" : "none";
      }
    }
    menu.style.display = "flex";
    const vw = typeof window !== "undefined" ? (window.innerWidth || 1024) : 1024;
    const vh = typeof window !== "undefined" ? (window.innerHeight || 768) : 768;
    const mw = Math.max(260, menu.offsetWidth || 260);
    const mh = Math.max(100, menu.offsetHeight || 160);
    const leftPx = Math.max(8, Math.min(vw - mw - 8, Number(ev.clientX || 0)));
    const topPx = Math.max(8, Math.min(vh - mh - 8, Number(ev.clientY || 0)));
    menu.style.left = `${leftPx}px`;
    menu.style.top = `${topPx}px`;
  });
  document.addEventListener("click", (ev) => {
    const tgt = ev?.target;
    if (tgt && typeof tgt.closest === "function" && tgt.closest("#diffRowContextMenu")) return;
    closeDiffRowContextMenu();
  });
}

// --- Codepoint-exact Find Bar (Ctrl+F) supporting combining marks like ँ across spans ---
let _diffFindMatches = [];
let _diffFindIdx = -1;
let _diffFindQuery = "";

function _foldAsciiForFind(s) {
  return String(s || "").replace(/[A-Z]/g, ch => ch.toLowerCase());
}

function _locateTextOffset(entries, idx, isEnd = false) {
  for (let i = 0; i < entries.length; i++) {
    const e = entries[i];
    if (isEnd ? (idx <= e.start + e.len) : (idx < e.start + e.len)) {
      return [e.node, Math.max(0, Math.min(e.len, idx - e.start))];
    }
  }
  const last = entries[entries.length - 1];
  return last ? [last.node, last.len] : null;
}

function _isElementVisibleForFind(el) {
  if (!el) return false;
  if (el.closest && el.closest("#diffFindBar, #settingsPane, #helpPane, #diffRowContextMenu, #shortcutFooter, #aboutFooter, script, style")) return false;
  if (el.closest && el.closest(".file-card.collapsed .file-card-body")) return false;
  if (typeof el.getClientRects === "function" && el.getClientRects().length === 0) return false;
  let cur = el;
  while (cur && cur !== document.body) {
    if (cur.style && cur.style.display === "none") return false;
    if (cur.tagName === "DETAILS" && !cur.open) {
      if (!el.closest || !el.closest("summary")) return false;
    }
    cur = cur.parentElement;
  }
  return true;
}

function collectCodepointMatchesInRoot(rootEl, rawQuery) {
  const query = _foldAsciiForFind(rawQuery);
  if (!rootEl || !query || typeof document === "undefined" || typeof document.createTreeWalker !== "function") {
    return [];
  }
  const blockSel = "td, th, div.hunk-bar, div.diff-gap-bar, .comment-body, .pr-body-box, h2, .file-card-header, li, p";
  const walker = document.createTreeWalker(rootEl, NodeFilter.SHOW_TEXT);
  const groups = [];
  let curBlock = Symbol("init");
  let curEntries = [];
  let curFull = "";
  let node;
  while ((node = walker.nextNode())) {
    if (!node.nodeValue) continue;
    const par = node.parentElement;
    if (!par || !_isElementVisibleForFind(par)) continue;
    const blk = par.closest ? (par.closest(blockSel) || par) : par;
    if (blk !== curBlock) {
      if (curEntries.length) groups.push({ block: curBlock, entries: curEntries, full: curFull });
      curBlock = blk;
      curEntries = [];
      curFull = "";
    }
    curEntries.push({ node, start: curFull.length, len: node.nodeValue.length });
    curFull += node.nodeValue;
  }
  if (curEntries.length) groups.push({ block: curBlock, entries: curEntries, full: curFull });

  const hits = [];
  for (const g of groups) {
    const folded = _foldAsciiForFind(g.full);
    let pos = 0;
    while ((pos = folded.indexOf(query, pos)) >= 0) {
      const endPos = pos + query.length;
      const startLoc = _locateTextOffset(g.entries, pos, false);
      const endLoc = _locateTextOffset(g.entries, endPos, true);
      if (startLoc && endLoc && typeof document.createRange === "function") {
        const range = document.createRange();
        range.setStart(startLoc[0], startLoc[1]);
        range.setEnd(endLoc[0], endLoc[1]);
        let visualRange = range;
        if (typeof range.getBoundingClientRect === "function") {
          const rRect = range.getBoundingClientRect();
          if (rRect && rRect.width < 4 && pos > 0) {
            let vStart = pos - 1;
            const chCode = g.full.charCodeAt(vStart);
            if (vStart > 0 && chCode >= 0xDC00 && chCode <= 0xDFFF) vStart -= 1;
            const vLoc = _locateTextOffset(g.entries, vStart, false);
            if (vLoc) {
              const vr = document.createRange();
              vr.setStart(vLoc[0], vLoc[1]);
              vr.setEnd(endLoc[0], endLoc[1]);
              visualRange = vr;
            }
          }
        }
        const rowEl = g.block && g.block.closest ? g.block.closest("tr.diff-row") : null;
        hits.push({ range, visualRange, block: g.block, rowEl });
      }
      pos = Math.max(pos + 1, endPos);
    }
  }
  return hits;
}

function _clearFindHighlights() {
  if (typeof CSS !== "undefined" && CSS.highlights) {
    CSS.highlights.delete("diff-find");
    CSS.highlights.delete("diff-find-current");
  }
  if (typeof document !== "undefined" && typeof document.querySelectorAll === "function") {
    document.querySelectorAll("tr.diff-row-find-cur").forEach(el => el.classList.remove("diff-row-find-cur"));
  }
}

let _diffNavRows = [];
let _diffSelectRowFn = null;

function _findEnclosingDiffFocusTargetIdx(activeHit, rowEl) {
  if (!activeHit || !rowEl) return -1;
  const targets = getRowDiffFocusTargets(rowEl);
  if (!targets.length) return -1;
  const r = activeHit.range;
  const candidates = [];
  if (r && r.startContainer) {
    candidates.push(r.startContainer.nodeType === 3 ? r.startContainer.parentElement : r.startContainer);
  }
  if (r && r.endContainer) {
    candidates.push(r.endContainer.nodeType === 3 ? r.endContainer.parentElement : r.endContainer);
  }
  for (const el of candidates) {
    if (!el) continue;
    for (let ti = 0; ti < targets.length; ti++) {
      const t = targets[ti];
      if (t === el || (typeof t.contains === "function" && t.contains(el))) return ti;
    }
  }
  return -1;
}

function _selectDiffFindMatchTarget(activeHit) {
  if (!activeHit) return;
  const rowEl = activeHit.rowEl;
  if (!rowEl || !rowEl.id || !Array.isArray(_diffNavRows) || !_diffNavRows.length) return;
  const rowMeta = _diffNavRows.find(r => r.domId === rowEl.id);
  if (!rowMeta) return;
  const ti = _findEnclosingDiffFocusTargetIdx(activeHit, rowEl);
  if (typeof _diffSelectRowFn === "function") {
    _diffSelectRowFn(rowMeta.rowKey, { scroll: false, preserveDiffFocus: true });
  }
  clearActiveDiffFocus();
  if (ti >= 0) {
    _activeDiffFocus = { rowKey: rowMeta.rowKey, domId: rowMeta.domId, tokenIdx: ti };
    _applyDiffFocusDom();
  }
}

function _applyDiffFindUi(scrollToActive = true, selectTarget = false) {
  _clearFindHighlights();
  const countEl = typeof document !== "undefined" ? document.getElementById("diffFindCount") : null;
  if (!_diffFindMatches.length) {
    if (countEl) countEl.textContent = _diffFindQuery ? "0 / 0" : "";
    return;
  }
  if (_diffFindIdx < 0 || _diffFindIdx >= _diffFindMatches.length) _diffFindIdx = 0;
  const activeHit = _diffFindMatches[_diffFindIdx];
  if (typeof Highlight !== "undefined" && typeof CSS !== "undefined" && CSS.highlights) {
    CSS.highlights.set("diff-find", new Highlight(..._diffFindMatches.map(m => m.visualRange)));
    CSS.highlights.set("diff-find-current", new Highlight(activeHit.visualRange));
  }
  if (activeHit.rowEl && activeHit.rowEl.classList) {
    activeHit.rowEl.classList.add("diff-row-find-cur");
  }
  if (selectTarget) {
    _selectDiffFindMatchTarget(activeHit);
  }
  if (countEl) countEl.textContent = `${_diffFindIdx + 1} / ${_diffFindMatches.length}`;
  if (scrollToActive) {
    const scrollTgt = activeHit.rowEl || (activeHit.visualRange.startContainer && activeHit.visualRange.startContainer.parentElement) || activeHit.block;
    if (scrollTgt && typeof scrollTgt.scrollIntoView === "function") {
      scrollTgt.scrollIntoView({ block: "center" });
    }
  }
}

function _findPreferredDiffFindIdx(matches) {
  if (!Array.isArray(matches) || !matches.length) return -1;
  if (typeof document === "undefined") return 0;
  let curRowEl = null;
  if (_activeDiffFocus && _activeDiffFocus.domId && typeof document.getElementById === "function") {
    curRowEl = document.getElementById(_activeDiffFocus.domId);
  }
  if (!curRowEl && typeof document.querySelector === "function") {
    curRowEl = document.querySelector("tr.diff-row-selected");
  }
  if (curRowEl) {
    const FOLLOW_BIT = (typeof Node !== "undefined" && Node.DOCUMENT_POSITION_FOLLOWING) || 4;
    for (let k = 0; k < matches.length; k++) {
      const m = matches[k];
      if (m.rowEl && m.rowEl === curRowEl) return k;
      const node = m.range && m.range.startContainer;
      if (!node) continue;
      if (typeof curRowEl.contains === "function" && curRowEl.contains(node)) return k;
      if (typeof curRowEl.compareDocumentPosition === "function" && (curRowEl.compareDocumentPosition(node) & FOLLOW_BIT)) {
        return k;
      }
    }
    return 0;
  }
  const pane = typeof document.getElementById === "function" ? document.getElementById("mainPane") : null;
  if (pane && typeof pane.getBoundingClientRect === "function") {
    const paneRect = pane.getBoundingClientRect();
    if (paneRect && paneRect.height > 0) {
      for (let k = 0; k < matches.length; k++) {
        const m = matches[k];
        const rect = (m.visualRange && typeof m.visualRange.getBoundingClientRect === "function" && m.visualRange.getBoundingClientRect())
          || (m.block && typeof m.block.getBoundingClientRect === "function" && m.block.getBoundingClientRect());
        if (rect && rect.height > 0 && rect.bottom > paneRect.top + 2) return k;
      }
    }
  }
  return 0;
}

function runDiffFind(queryStr = null, { preserveIndex = false, scrollToActive = true } = {}) {
  const inp = typeof document !== "undefined" ? document.getElementById("diffFindInput") : null;
  const q = queryStr !== null ? String(queryStr) : String(inp ? inp.value : _diffFindQuery);
  _diffFindQuery = q;
  const root = (typeof document !== "undefined" && document.getElementById("mainPane")) || null;
  _diffFindMatches = q ? collectCodepointMatchesInRoot(root, q) : [];
  if (!_diffFindMatches.length) _diffFindIdx = -1;
  else if (!preserveIndex || _diffFindIdx < 0 || _diffFindIdx >= _diffFindMatches.length) {
    _diffFindIdx = _findPreferredDiffFindIdx(_diffFindMatches);
  }
  _applyDiffFindUi(scrollToActive);
  return _diffFindMatches.length;
}

function stepDiffFind(delta = 1) {
  if (!_diffFindMatches.length) {
    const found = runDiffFind(null, { preserveIndex: false, scrollToActive: true });
    if (found > 0) _selectDiffFindMatchTarget(_diffFindMatches[_diffFindIdx]);
    return;
  }
  const n = _diffFindMatches.length;
  _diffFindIdx = ((_diffFindIdx + delta) % n + n) % n;
  _applyDiffFindUi(true, true);
}

function isDiffFindBarOpen() {
  const bar = typeof document !== "undefined" ? document.getElementById("diffFindBar") : null;
  return !!(bar && bar.style && bar.style.display && bar.style.display !== "none");
}

function openDiffFindBar(initialText = null) {
  const bar = _ensureDiffFindBarEl();
  if (!bar) return;
  bar.style.display = "flex";
  const inp = document.getElementById("diffFindInput");
  if (inp) {
    if (initialText !== null) inp.value = initialText;
    else if (!inp.value && typeof window !== "undefined" && window.getSelection) {
      const sel = String(window.getSelection().toString() || "").trim();
      if (sel && !sel.includes("\n") && sel.length <= 64) inp.value = sel;
    }
    if (typeof inp.focus === "function") inp.focus();
    if (typeof inp.select === "function") inp.select();
    if (inp.value) runDiffFind(inp.value, { preserveIndex: false, scrollToActive: true });
  }
}

function closeDiffFindBar() {
  const inp = typeof document !== "undefined" ? document.getElementById("diffFindInput") : null;
  if (inp && typeof inp.blur === "function") inp.blur();
  const bar = typeof document !== "undefined" ? document.getElementById("diffFindBar") : null;
  if (bar) bar.style.display = "none";
  _diffFindMatches = [];
  _diffFindIdx = -1;
  _clearFindHighlights();
}

function refreshDiffFindIfOpen() {
  if (isDiffFindBarOpen() && _diffFindQuery) {
    runDiffFind(_diffFindQuery, { preserveIndex: true, scrollToActive: false });
  }
}

function _ensureDiffFindBarEl() {
  if (typeof document === "undefined" || typeof document.getElementById !== "function") return null;
  let bar = document.getElementById("diffFindBar");
  if (!bar && typeof document.createElement === "function" && document.body) {
    bar = document.createElement("div");
    bar.id = "diffFindBar";
    bar.className = "diff-find-bar";
    bar.innerHTML = `
      <input id="diffFindInput" type="text" placeholder="Find (supports ँ, ᳓, spans)..." oninput="runDiffFind(this.value)" onkeydown="if(event.key==='Enter'){event.preventDefault(); stepDiffFind(event.shiftKey ? -1 : 1);}else if(event.key==='Escape'){event.preventDefault(); event.stopPropagation(); closeDiffFindBar();}" />
      <span id="diffFindCount" class="diff-find-count"></span>
      <button id="diffFindPrevBtn" class="small" type="button" title="Previous match (Shift+Enter / Ctrl+Shift+G)" onclick="stepDiffFind(-1)">▲</button>
      <button id="diffFindNextBtn" class="small" type="button" title="Next match (Enter / Ctrl+G)" onclick="stepDiffFind(1)">▼</button>
      <button id="diffFindCloseBtn" class="small" type="button" title="Close (Esc)" onclick="closeDiffFindBar()">✕</button>
    `;
    document.body.appendChild(bar);
  }
  return bar;
}

// --- Diff-chunk Tab Stepping & "Checked until here" (Ctrl+Space) Marker ---
let _activeDiffFocus = null; // { rowKey, domId, tokenIdx, el } | null
let _lastCheckedRowEl = null;
let _lastCheckedTokEl = null;
const _checkedUntilMemCache = {};

function blurStrayUiFocus() {
  if (typeof document === "undefined") return;
  const active = document.activeElement;
  if (!active || active === document.body || typeof active.blur !== "function") return;
  if (active.closest && active.closest(".inline-composer, .thread-box, #diffFindBar, #settingsPane")) return;
  active.blur();
}

function focusMainPane() {
  if (typeof document === "undefined") return false;
  const mainEl = typeof document.getElementById === "function" ? document.getElementById("mainPane") : null;
  const active = document.activeElement;
  if (active && active !== document.body && active !== mainEl && typeof active.blur === "function") {
    active.blur();
  }
  if (mainEl && typeof mainEl.focus === "function") {
    try {
      mainEl.focus({ preventScroll: true });
    } catch (_) {
      mainEl.focus();
    }
    return true;
  }
  return false;
}

function clearActiveDiffFocus() {
  if (_activeDiffFocus && _activeDiffFocus.el && _activeDiffFocus.el.classList) {
    _activeDiffFocus.el.classList.remove("diff-focus-cur");
  }
  _activeDiffFocus = null;
  if (typeof document !== "undefined" && typeof document.querySelectorAll === "function") {
    document.querySelectorAll(".diff-focus-cur").forEach(el => {
      if (el.classList) el.classList.remove("diff-focus-cur");
    });
  }
}

function getRowDiffFocusTargets(rowEl) {
  if (!rowEl || typeof rowEl.querySelectorAll !== "function") return [];
  const raw = Array.from(rowEl.querySelectorAll(".ch-pair, .ch-del, .ch-ins, .nl-del, .nl-ins"));
  const top = raw.filter(el => {
    const par = el.parentElement;
    if (!par || typeof par.closest !== "function") return true;
    return !par.closest(".ch-pair, .ch-del, .ch-ins");
  });
  if (top.length) return top;
  if (rowEl.classList && (
    rowEl.classList.contains("diff-row-modify")
    || rowEl.classList.contains("diff-row-del")
    || rowEl.classList.contains("diff-row-add")
    || rowEl.classList.contains("single-row-modify")
    || rowEl.classList.contains("single-row-del")
    || rowEl.classList.contains("single-row-add")
  )) {
    const cells = Array.from(rowEl.querySelectorAll("td.code-cell:not(.diff-empty)"));
    return cells.length ? cells : [rowEl];
  }
  return [];
}

function _applyDiffFocusDom() {
  if (_activeDiffFocus && _activeDiffFocus.el && _activeDiffFocus.el.classList) {
    _activeDiffFocus.el.classList.remove("diff-focus-cur");
  }
  if (typeof document !== "undefined" && typeof document.querySelectorAll === "function") {
    document.querySelectorAll(".diff-focus-cur").forEach(el => {
      if (el.classList) el.classList.remove("diff-focus-cur");
    });
  }
  if (!_activeDiffFocus || typeof document === "undefined" || typeof document.getElementById !== "function") return null;
  const rowEl = document.getElementById(_activeDiffFocus.domId || "");
  if (!rowEl) return null;
  const targets = getRowDiffFocusTargets(rowEl);
  const tgt = targets[_activeDiffFocus.tokenIdx];
  if (!tgt) return null;
  _activeDiffFocus.el = tgt;
  if (tgt.classList) tgt.classList.add("diff-focus-cur");
  return tgt;
}

function getActiveDiffFocusEl() {
  if (!_activeDiffFocus || typeof document === "undefined" || typeof document.getElementById !== "function") return null;
  const rowEl = document.getElementById(_activeDiffFocus.domId || "");
  if (!rowEl) return null;
  const targets = getRowDiffFocusTargets(rowEl);
  return targets[_activeDiffFocus.tokenIdx] || null;
}

function stepRowDiffFocus(rows, currentRowKey, selectRowFn, step = 1) {
  if (!Array.isArray(rows) || !rows.length || !currentRowKey) return null;
  const curRowIdx = rows.findIndex(r => r.rowKey === currentRowKey);
  if (curRowIdx < 0) return null;

  const entries = [];
  for (let ri = 0; ri < rows.length; ri++) {
    const r = rows[ri];
    const rowEl = (typeof document !== "undefined" && typeof document.getElementById === "function")
      ? document.getElementById(r.domId)
      : null;
    const targets = getRowDiffFocusTargets(rowEl);
    for (let ti = 0; ti < targets.length; ti++) {
      entries.push({ row: r, rowIdx: ri, tokenIdx: ti, targetEl: targets[ti] });
    }
  }
  if (!entries.length) {
    setStatus("No differences in current view.", 2000);
    return null;
  }

  let chosenPos = -1;
  let wrapped = false;
  if (_activeDiffFocus && _activeDiffFocus.rowKey === currentRowKey) {
    const curEntryIdx = entries.findIndex(e => e.row.rowKey === currentRowKey && e.tokenIdx === _activeDiffFocus.tokenIdx);
    if (curEntryIdx >= 0) {
      const nextRaw = curEntryIdx + (step >= 0 ? 1 : -1);
      if (nextRaw < 0 || nextRaw >= entries.length) wrapped = true;
      chosenPos = ((nextRaw % entries.length) + entries.length) % entries.length;
    }
  }
  if (chosenPos < 0) {
    if (step >= 0) {
      chosenPos = entries.findIndex(e => e.rowIdx >= curRowIdx);
      if (chosenPos < 0) { chosenPos = 0; wrapped = true; }
    } else {
      for (let k = entries.length - 1; k >= 0; k--) {
        if (entries[k].rowIdx <= curRowIdx) { chosenPos = k; break; }
      }
      if (chosenPos < 0) { chosenPos = entries.length - 1; wrapped = true; }
    }
  }

  const chosen = entries[chosenPos];
  const prevEl = _activeDiffFocus?.el || null;
  if (prevEl && prevEl.classList) prevEl.classList.remove("diff-focus-cur");
  if (typeof selectRowFn === "function") {
    selectRowFn(chosen.row.rowKey, { preserveDiffFocus: true, scroll: false });
  }
  _activeDiffFocus = { rowKey: chosen.row.rowKey, domId: chosen.row.domId, tokenIdx: chosen.tokenIdx, el: chosen.targetEl };
  const focusedEl = _applyDiffFocusDom() || chosen.targetEl;
  if (focusedEl && typeof focusedEl.scrollIntoView === "function") {
    focusedEl.scrollIntoView({ block: "nearest", inline: "nearest" });
  } else {
    const rowEl = document.getElementById(chosen.row.domId);
    if (rowEl && typeof rowEl.scrollIntoView === "function") rowEl.scrollIntoView({ block: "nearest" });
  }
  setStatus(`Diff ${chosenPos + 1} / ${entries.length}${wrapped ? " (wrapped)" : ""}`, 2000);
  return { row: chosen.row, tokenIdx: chosen.tokenIdx, index: chosenPos, total: entries.length };
}

function shouldHandleDiffTabKey(ev, rows, selectedRowKey) {
  if (!ev || ev.key !== "Tab" || ev.ctrlKey || ev.altKey || ev.metaKey) return false;
  if (!selectedRowKey || !Array.isArray(rows) || !rows.some(r => r.rowKey === selectedRowKey)) return false;
  const tgt = ev.target;
  const tag = (tgt && tgt.tagName || "").toUpperCase();
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || tgt?.isContentEditable) return false;
  if (tgt && typeof tgt.closest === "function" && tgt.closest(".inline-composer, .thread-box, .review-panel, #diffFindBar, #settingsPane, #diffRowContextMenu, header, aside.sidebar")) {
    return false;
  }
  return true;
}

function _loadCheckedUntilMap(storageKey) {
  if (_checkedUntilMemCache[storageKey]) return _checkedUntilMemCache[storageKey];
  let map = {};
  try {
    const raw = typeof localStorage !== "undefined" ? localStorage.getItem(storageKey) : null;
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) map = parsed;
    }
  } catch (_) {}
  _checkedUntilMemCache[storageKey] = map;
  return map;
}

function _saveCheckedUntilMap(storageKey, map) {
  _checkedUntilMemCache[storageKey] = map;
  try {
    if (typeof localStorage !== "undefined") {
      const keys = Object.keys(map);
      if (keys.length > 50) {
        const sorted = keys.sort((a, b) => (map[b]?.updatedAt || 0) - (map[a]?.updatedAt || 0));
        const trimmed = {};
        for (const k of sorted.slice(0, 50)) trimmed[k] = map[k];
        map = trimmed;
        _checkedUntilMemCache[storageKey] = map;
      }
      localStorage.setItem(storageKey, JSON.stringify(map));
    }
  } catch (_) {}
}

function _resolveCheckedRowMeta(rows, cp) {
  if (!cp || !Array.isArray(rows) || !rows.length) return null;
  if (cp.rowKey) {
    const exact = rows.find(r => r.rowKey === cp.rowKey);
    if (exact) return exact;
  }
  const fRows = cp.filename ? rows.filter(r => r.filename === cp.filename) : rows;
  if (cp.rightNum != null) {
    const rMatch = fRows.find(r => r.rightNum === cp.rightNum);
    if (rMatch) return rMatch;
  }
  if (cp.leftNum != null) {
    const lMatch = fRows.find(r => r.leftNum === cp.leftNum);
    if (lMatch) return lMatch;
  }
  return null;
}

function _formatCheckedCheckpointLabel(cp) {
  if (!cp) return "";
  const fileBase = String(cp.filename || "").split("/").pop() || cp.filename || "";
  const ln = cp.rightNum ?? cp.leftNum;
  const lineStr = ln != null ? `:${ln}` : "";
  const tokStr = cp.tokenIdx != null ? ` (#${cp.tokenIdx + 1})` : "";
  return `${fileBase}${lineStr}${tokStr}` || "point";
}

function _renderCheckedUntilHeaderPill(cp, onJump, onClear) {
  const pill = typeof document !== "undefined" ? document.getElementById("checkedUntilPill") : null;
  if (!pill) return;
  if (!cp) {
    if (pill.style) pill.style.display = "none";
    pill.innerHTML = "";
    return;
  }
  const shortLbl = _formatCheckedCheckpointLabel(cp);
  pill.className = "checked-until-pill";
  if (pill.style) pill.style.display = "inline-flex";
  pill.innerHTML = `<button type="button" class="checked-until-jump-btn" title="Jump to Checked until here (${esc(shortLbl)})">✓ Checked: ${esc(shortLbl)}</button><button type="button" class="checked-until-clear-btn" title="Clear Checked until here marker">✕</button>`;
  if (typeof pill.querySelector === "function") {
    const jBtn = pill.querySelector(".checked-until-jump-btn");
    const cBtn = pill.querySelector(".checked-until-clear-btn");
    if (jBtn && typeof onJump === "function") jBtn.onclick = onJump;
    if (cBtn && typeof onClear === "function") cBtn.onclick = onClear;
  }
}

function clearCheckedUntilHere(rows, scopeKey, { storageKey, selectRowFn = null } = {}) {
  if (scopeKey && storageKey) {
    const map = _loadCheckedUntilMap(storageKey);
    if (Object.prototype.hasOwnProperty.call(map, scopeKey)) {
      delete map[scopeKey];
      _saveCheckedUntilMap(storageKey, map);
    }
  }
  syncDiffFocusAndCheckedUi(rows, scopeKey, { storageKey, selectRowFn });
  setStatus("Cleared Checked until here marker", 2000);
}

function jumpToCheckedUntilHere(rows, scopeKey, { storageKey, selectRowFn = null } = {}) {
  if (!scopeKey || !storageKey) return null;
  const cp = _loadCheckedUntilMap(storageKey)[scopeKey];
  if (!cp) return null;
  const meta = _resolveCheckedRowMeta(rows, cp);
  if (!meta) {
    setStatus("Checked checkpoint row is collapsed or in another file.", 2500);
    return null;
  }
  if (typeof selectRowFn === "function") {
    selectRowFn(meta.rowKey, { preserveDiffFocus: true, scroll: false });
  }
  if (cp.tokenIdx != null) {
    _activeDiffFocus = { rowKey: meta.rowKey, domId: meta.domId, tokenIdx: cp.tokenIdx };
  } else {
    clearActiveDiffFocus();
  }
  syncDiffFocusAndCheckedUi(rows, scopeKey, { storageKey, selectRowFn });
  const rowEl = typeof document !== "undefined" && typeof document.getElementById === "function"
    ? document.getElementById(meta.domId)
    : null;
  if (rowEl && typeof rowEl.scrollIntoView === "function") rowEl.scrollIntoView({ block: "center" });
  setStatus(`Jumped to Checked until ${_formatCheckedCheckpointLabel(cp)}`, 2000);
  return meta;
}

function setCheckedUntilHere(rows, currentRowKey, scopeKey, { storageKey, selectRowFn = null } = {}) {
  if (!scopeKey || !storageKey) return null;
  if (!currentRowKey || !Array.isArray(rows)) {
    setStatus("Select a line first (Shift+Click / ↑ / ↓) to set Checked until here.", 2500);
    return null;
  }
  const meta = rows.find(r => r.rowKey === currentRowKey);
  if (!meta) {
    setStatus("Select a line first (Shift+Click / ↑ / ↓) to set Checked until here.", 2500);
    return null;
  }
  const tokenIdx = (_activeDiffFocus && _activeDiffFocus.rowKey === currentRowKey) ? _activeDiffFocus.tokenIdx : null;
  const cp = {
    rowKey: meta.rowKey,
    filename: meta.filename || "",
    leftNum: meta.leftNum ?? null,
    rightNum: meta.rightNum ?? null,
    tokenIdx,
    updatedAt: Date.now(),
  };
  const map = _loadCheckedUntilMap(storageKey);
  map[scopeKey] = cp;
  _saveCheckedUntilMap(storageKey, map);
  syncDiffFocusAndCheckedUi(rows, scopeKey, { storageKey, selectRowFn });
  setStatus(`✓ Checked until ${_formatCheckedCheckpointLabel(cp)}`, 2500);
  return cp;
}

function syncDiffFocusAndCheckedUi(rows, scopeKey, { storageKey, selectRowFn = null } = {}) {
  _diffNavRows = Array.isArray(rows) ? rows : [];
  if (typeof selectRowFn === "function") _diffSelectRowFn = selectRowFn;
  if (_lastCheckedRowEl && _lastCheckedRowEl.classList) _lastCheckedRowEl.classList.remove("diff-checked-until");
  if (_lastCheckedTokEl && _lastCheckedTokEl.classList) _lastCheckedTokEl.classList.remove("diff-checked-token");
  _lastCheckedRowEl = null;
  _lastCheckedTokEl = null;
  if (typeof document !== "undefined" && typeof document.querySelectorAll === "function") {
    document.querySelectorAll(".diff-checked-until").forEach(el => {
      if (el.classList) el.classList.remove("diff-checked-until");
    });
    document.querySelectorAll(".diff-checked-token").forEach(el => {
      if (el.classList) el.classList.remove("diff-checked-token");
    });
  }
  if (_activeDiffFocus && Array.isArray(rows)) {
    const focusMeta = rows.find(r => r.rowKey === _activeDiffFocus.rowKey);
    if (focusMeta) {
      _activeDiffFocus.domId = focusMeta.domId;
      _applyDiffFocusDom();
    } else {
      clearActiveDiffFocus();
    }
  }
  const cp = (scopeKey && storageKey) ? _loadCheckedUntilMap(storageKey)[scopeKey] || null : null;
  _renderCheckedUntilHeaderPill(
    cp,
    () => jumpToCheckedUntilHere(rows, scopeKey, { storageKey, selectRowFn }),
    () => clearCheckedUntilHere(rows, scopeKey, { storageKey, selectRowFn })
  );
  if (cp && Array.isArray(rows) && typeof document !== "undefined" && typeof document.getElementById === "function") {
    const meta = _resolveCheckedRowMeta(rows, cp);
    if (meta) {
      const rowEl = document.getElementById(meta.domId);
      if (rowEl && rowEl.classList) {
        rowEl.classList.add("diff-checked-until");
        _lastCheckedRowEl = rowEl;
        if (cp.tokenIdx != null) {
          const targets = getRowDiffFocusTargets(rowEl);
          const tokEl = targets[cp.tokenIdx];
          if (tokEl && tokEl.classList) {
            tokEl.classList.add("diff-checked-token");
            _lastCheckedTokEl = tokEl;
          }
        }
      }
    }
  }
}

let _sidebarPageStepFn = null;
let _mainFilePageStepFn = null;
let _sidebarPageNavBound = false;
let _modifierHintBound = false;
const _fileTreeCollapsed = {};

function setFileTreeDirCollapsed(scopeKey, dirKey, isOpen) {
  const fullKey = `${scopeKey || "default"}:${dirKey}`;
  if (isOpen) delete _fileTreeCollapsed[fullKey];
  else _fileTreeCollapsed[fullKey] = true;
}

function buildFileTreeData(entries) {
  const parsed = (entries || []).map((e, pos) => {
    const raw = String(e?.path ?? "");
    if (!raw || raw.includes(" ↔ ")) {
      return { ...e, pos, dirs: [], name: raw || "(unnamed)", nonPath: true };
    }
    const clean = raw.replace(/\\/g, "/");
    const slashIdx = clean.lastIndexOf("/");
    const dirPart = slashIdx >= 0 ? clean.slice(0, slashIdx) : "";
    const dirs = dirPart ? dirPart.split("/").filter(Boolean) : [];
    const name = slashIdx >= 0 ? clean.slice(slashIdx + 1) : clean;
    return { ...e, pos, dirs, name: name || raw, hasLeadingSlash: clean.startsWith("/") };
  });
  let lcpDirs = [];
  const pathItems = parsed.filter(p => !p.nonPath);
  if (pathItems.length) {
    lcpDirs = pathItems[0].dirs.slice();
    for (let i = 1; i < pathItems.length; i++) {
      const d = pathItems[i].dirs;
      let k = 0;
      while (k < lcpDirs.length && k < d.length && lcpDirs[k] === d[k]) k++;
      lcpDirs = lcpDirs.slice(0, k);
    }
  }
  const allLeadingSlash = pathItems.length > 0 && pathItems.every(p => p.hasLeadingSlash);
  const rootLabel = lcpDirs.length ? `${allLeadingSlash ? "/" : ""}${lcpDirs.join("/")}/` : "";
  const rootNode = { key: "__root__", name: rootLabel, children: new Map(), files: [] };

  for (const item of parsed) {
    let cur = rootNode;
    const relDirs = item.nonPath ? [] : item.dirs.slice(lcpDirs.length);
    let prefix = "";
    for (const seg of relDirs) {
      prefix = prefix ? `${prefix}/${seg}` : seg;
      if (!cur.children.has(seg)) {
        cur.children.set(seg, { key: prefix, name: seg, children: new Map(), files: [] });
      }
      cur = cur.children.get(seg);
    }
    cur.files.push(item);
  }

  function compact(node, isRoot) {
    for (const child of node.children.values()) {
      compact(child, false);
    }
    if (!isRoot) {
      while (node.files.length === 0 && node.children.size === 1) {
        const only = Array.from(node.children.values())[0];
        node.name = `${node.name}/${only.name}`;
        node.key = only.key;
        node.files = only.files;
        node.children = only.children;
      }
    }
  }
  compact(rootNode, true);
  return { rootNode, rootLabel };
}

function renderFileTreeHtml(entries, renderLeafHtml, { scopeKey = "files" } = {}) {
  if (!entries || !entries.length) return "";
  const { rootNode, rootLabel } = buildFileTreeData(entries);

  function countNode(node) {
    let total = node.files.length;
    let hasActive = node.files.some(f => Boolean(f.isActive));
    for (const child of node.children.values()) {
      const sub = countNode(child);
      total += sub.total;
      if (sub.hasActive) hasActive = true;
    }
    node._total = total;
    node._hasActive = hasActive;
    return { total, hasActive };
  }
  countNode(rootNode);

  function renderChildren(node) {
    const subHtml = Array.from(node.children.values()).map(child => renderDir(child, false)).join("");
    const leafHtml = node.files.map(f => renderLeafHtml(f)).join("");
    return `${subHtml}${leafHtml}`;
  }

  function renderDir(node, isRoot) {
    const collapsed = Boolean(_fileTreeCollapsed[`${scopeKey}:${node.key}`]);
    const isOpen = node._hasActive || !collapsed;
    const cls = isRoot ? "file-tree-dir file-tree-root" : "file-tree-dir";
    const labelTxt = isRoot ? node.name : `${node.name}/`;
    const toggleJs = `setFileTreeDirCollapsed(${esc(JSON.stringify(scopeKey))}, ${esc(JSON.stringify(node.key))}, this.open)`;
    return `<details class="${cls}" data-dir-key="${esc(node.key)}" ${isOpen ? "open" : ""} ontoggle="${toggleJs}">
      <summary title="${esc(labelTxt)}"><span class="file-tree-caret">▸</span><span>📁 ${esc(labelTxt)}</span><span class="file-tree-count">${node._total}</span></summary>
      <div class="file-tree-children">${renderChildren(node)}</div>
    </details>`;
  }

  if (rootLabel) {
    return renderDir(rootNode, true);
  }
  return renderChildren(rootNode);
}

function _syncModifierClasses(ev) {
  if (typeof document === "undefined" || !document.body || !document.body.classList) return;
  const altDown = Boolean(ev && (ev.altKey || (ev.type === "keydown" && ev.key === "Alt")));
  const ctrlDown = Boolean(ev && (ev.ctrlKey || (ev.type === "keydown" && ev.key === "Control")));
  document.body.classList.toggle("mod-alt-down", ev?.type === "keyup" && ev?.key === "Alt" ? false : altDown);
  document.body.classList.toggle("mod-ctrl-down", ev?.type === "keyup" && ev?.key === "Control" ? false : ctrlDown);
}

function bindModifierHintTracking() {
  if (_modifierHintBound || typeof window === "undefined" || typeof window.addEventListener !== "function") return;
  _modifierHintBound = true;
  window.addEventListener("keydown", _syncModifierClasses, true);
  window.addEventListener("keyup", _syncModifierClasses, true);
  window.addEventListener("blur", () => {
    if (typeof document !== "undefined" && document.body && document.body.classList) {
      document.body.classList.remove("mod-alt-down", "mod-ctrl-down");
    }
  });
  if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
    document.addEventListener("visibilitychange", () => {
      if (document.hidden && document.body && document.body.classList) {
        document.body.classList.remove("mod-alt-down", "mod-ctrl-down");
      }
    });
  }
}

function getCtrlPageStepDelta(ev) {
  if (!ev || !ev.ctrlKey || ev.altKey || ev.metaKey || ev.shiftKey) return 0;
  if (ev.key === "PageDown" || ev.code === "PageDown") return 1;
  if (ev.key === "PageUp" || ev.code === "PageUp") return -1;
  return 0;
}

function getShiftPageStepDelta(ev) {
  if (!ev || !ev.shiftKey || ev.ctrlKey || ev.altKey || ev.metaKey) return 0;
  if (ev.key === "PageDown" || ev.code === "PageDown") return 1;
  if (ev.key === "PageUp" || ev.code === "PageUp") return -1;
  return 0;
}

function _isTextEditingTarget(tgt) {
  const tag = (tgt && tgt.tagName || "").toUpperCase();
  return tag === "TEXTAREA" || tag === "SELECT"
    || (tag === "INPUT" && tgt.type !== "checkbox" && tgt.type !== "radio" && tgt.type !== "button")
    || Boolean(tgt?.isContentEditable);
}

function _ensurePageNavCaptureListener() {
  if (_sidebarPageNavBound || typeof window === "undefined" || typeof window.addEventListener !== "function") return;
  _sidebarPageNavBound = true;
  window.addEventListener("keydown", (ev) => {
    const sideDelta = getCtrlPageStepDelta(ev);
    if (sideDelta && typeof _sidebarPageStepFn === "function") {
      ev.__diffSanSidebarPageHandled = true;
      if (typeof ev.preventDefault === "function") ev.preventDefault();
      if (typeof ev.stopPropagation === "function") ev.stopPropagation();
      _sidebarPageStepFn(sideDelta);
      return;
    }
    const mainDelta = getShiftPageStepDelta(ev);
    if (mainDelta && typeof _mainFilePageStepFn === "function" && !_isTextEditingTarget(ev.target)) {
      if (_mainFilePageStepFn(mainDelta)) {
        ev.__diffSanMainFilePageHandled = true;
        if (typeof ev.preventDefault === "function") ev.preventDefault();
        if (typeof ev.stopPropagation === "function") ev.stopPropagation();
      }
    }
  }, true);
}

function bindSidebarPageNav(stepFn) {
  _sidebarPageStepFn = typeof stepFn === "function" ? stepFn : null;
  _ensurePageNavCaptureListener();
  bindModifierHintTracking();
}

function bindMainFilePageNav(stepFn) {
  _mainFilePageStepFn = typeof stepFn === "function" ? stepFn : null;
  _ensurePageNavCaptureListener();
  bindModifierHintTracking();
}

function handleSharedDiffKeydown(ev, navCtx = null) {
  if (!ev) return false;
  _syncModifierClasses(ev);
  const sidebarDelta = !ev.__diffSanSidebarPageHandled ? getCtrlPageStepDelta(ev) : 0;
  const sidebarStepFn = navCtx && typeof navCtx.stepSidebar === "function" ? navCtx.stepSidebar : _sidebarPageStepFn;
  if (sidebarDelta && typeof sidebarStepFn === "function") {
    ev.__diffSanSidebarPageHandled = true;
    if (typeof ev.preventDefault === "function") ev.preventDefault();
    if (typeof ev.stopPropagation === "function") ev.stopPropagation();
    sidebarStepFn(sidebarDelta);
    return true;
  }
  const mainFileDelta = !ev.__diffSanMainFilePageHandled ? getShiftPageStepDelta(ev) : 0;
  const mainFileStepFn = navCtx && typeof navCtx.stepMainFile === "function" ? navCtx.stepMainFile : _mainFilePageStepFn;
  if (mainFileDelta && typeof mainFileStepFn === "function" && !_isTextEditingTarget(ev.target)) {
    if (mainFileStepFn(mainFileDelta)) {
      ev.__diffSanMainFilePageHandled = true;
      if (typeof ev.preventDefault === "function") ev.preventDefault();
      if (typeof ev.stopPropagation === "function") ev.stopPropagation();
      return true;
    }
  }
  if ((ev.key === "f" || ev.key === "F") && ev.ctrlKey && !ev.altKey && !ev.metaKey && !ev.shiftKey) {
    if (typeof ev.preventDefault === "function") ev.preventDefault();
    openDiffFindBar();
    return true;
  }
  if ((ev.key === "g" || ev.key === "G") && ev.ctrlKey && !ev.altKey && !ev.metaKey) {
    if (typeof ev.preventDefault === "function") ev.preventDefault();
    if (navCtx && typeof navCtx.selectRow === "function") {
      _diffSelectRowFn = navCtx.selectRow;
      if (typeof navCtx.getRows === "function") _diffNavRows = navCtx.getRows() || [];
    }
    const delta = ev.shiftKey ? -1 : 1;
    if (!isDiffFindBarOpen()) {
      openDiffFindBar();
      if (_diffFindMatches.length) {
        _selectDiffFindMatchTarget(_diffFindMatches[_diffFindIdx]);
      }
      return true;
    }
    stepDiffFind(delta);
    return true;
  }
  if (ev.key === "Escape") {
    const menu = typeof document !== "undefined" ? document.getElementById("diffRowContextMenu") : null;
    if (menu && menu.style && menu.style.display && menu.style.display !== "none") {
      if (typeof ev.preventDefault === "function") ev.preventDefault();
      closeDiffRowContextMenu();
      return true;
    }
    if (isDiffFindBarOpen()) {
      if (typeof ev.preventDefault === "function") ev.preventDefault();
      closeDiffFindBar();
      return true;
    }
    if (isHelpPaneOpen()) {
      if (typeof ev.preventDefault === "function") ev.preventDefault();
      closeHelpPane();
      return true;
    }
    if (typeof document !== "undefined" && typeof document.querySelector === "function" && document.querySelector("dialog[open]")) {
      return false;
    }
    if (navCtx && typeof navCtx.hasOtherEscapeMeaning === "function" && navCtx.hasOtherEscapeMeaning()) {
      return false;
    }
    if (!ev.altKey && !ev.ctrlKey && !ev.metaKey && focusMainPane()) {
      if (typeof ev.preventDefault === "function") ev.preventDefault();
      return true;
    }
  }
  if (ev.key === "?" && !ev.ctrlKey && !ev.altKey && !ev.metaKey) {
    const tgt = ev.target;
    const tag = (tgt && tgt.tagName || "").toUpperCase();
    const isTextControl = tag === "TEXTAREA" || tag === "SELECT"
      || (tag === "INPUT" && tgt.type !== "checkbox" && tgt.type !== "radio" && tgt.type !== "button")
      || !!tgt?.isContentEditable;
    if (!isTextControl) {
      const pane = typeof document !== "undefined" && typeof document.getElementById === "function"
        ? document.getElementById("helpPane")
        : null;
      if (pane) {
        if (typeof ev.preventDefault === "function") ev.preventDefault();
        toggleHelpPane();
        return true;
      }
    }
  }
  if (navCtx) {
    const rows = typeof navCtx.getRows === "function" ? (navCtx.getRows() || []) : [];
    const selKey = typeof navCtx.getSelectedRowKey === "function" ? navCtx.getSelectedRowKey() : null;
    if (shouldHandleDiffTabKey(ev, rows, selKey)) {
      if (typeof ev.preventDefault === "function") ev.preventDefault();
      stepRowDiffFocus(rows, selKey, navCtx.selectRow, ev.shiftKey ? -1 : 1);
      return true;
    }
    if (ev.ctrlKey && !ev.altKey && !ev.metaKey && !ev.shiftKey && (ev.code === "Space" || ev.key === " ")) {
      const tgt = ev.target;
      const tag = (tgt && tgt.tagName || "").toUpperCase();
      const isTextControl = tag === "TEXTAREA" || tag === "SELECT"
        || (tag === "INPUT" && tgt.type !== "checkbox" && tgt.type !== "radio" && tgt.type !== "button")
        || !!tgt?.isContentEditable;
      if (!isTextControl) {
        if (typeof ev.preventDefault === "function") ev.preventDefault();
        const scopeKey = typeof navCtx.getScopeKey === "function" ? navCtx.getScopeKey() : "";
        setCheckedUntilHere(rows, selKey, scopeKey, {
          storageKey: navCtx.checkedStorageKey,
          selectRowFn: navCtx.selectRow,
        });
        return true;
      }
    }
  }
  return false;
}
