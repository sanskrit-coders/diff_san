const DIFF_COLOR_STORAGE_KEY = "diff_san_theme_colors_v1";
const DIFF_SANDHI_STORAGE_KEY = "diff_san_highlight_sandhi_v1";
let sandhiHighlightEnabled = false;

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
  if (autoClearMs > 0 && msg) {
    setTimeout(() => { if (el.textContent === msg) el.textContent = ""; }, autoClearMs);
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
        <span class="ch-pair ch-sandhi"><span class="ch-del ch-has-sub ch-sandhi">अस्<span class="ch-sub">ति इ</span>ति</span><span class="ch-ins ch-has-sub ch-sandhi">अस्<span class="ch-sub">ती</span>ति</span></span> (interword sandhi preview)
      </div>
    </div>
    ${groupsHtml}
  `;
}

function isSettingsPaneOpen() {
  const pane = document.getElementById("settingsPane");
  return !!(pane && pane.style.display !== "none" && pane.style.display !== "");
}

function toggleSettingsPane(forceOpen = null) {
  const pane = document.getElementById("settingsPane");
  if (!pane) return;
  if (!pane.innerHTML) pane.innerHTML = renderSettingsPaneHtml();
  const nextOpen = forceOpen === null ? !isSettingsPaneOpen() : !!forceOpen;
  if (nextOpen && typeof document.querySelector === "function") {
    const hdr = document.querySelector("header");
    if (hdr && typeof hdr.getBoundingClientRect === "function") {
      const topPx = Math.max(48, Math.round(hdr.getBoundingClientRect().bottom + 8));
      pane.style.top = `${topPx}px`;
      pane.style.maxHeight = `calc(100vh - ${topPx + 14}px)`;
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

function initDiffSettingsPane() {
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
      inner += `<span class="ch-inv" title="${tip}">${invMap[ch]}</span>`;
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
  let lead = "";
  let core = raw;
  let trail = "";
  const m = raw.match(/^([ \t]*)([\s\S]*?\S[\s\S]*?)([ \t]*)$/);
  if (m) {
    lead = m[1];
    core = m[2];
    trail = m[3];
  }
  const tipRaw = customTitle ? `${customTitle} | ${codepointTitle(core)}` : codepointTitle(core);
  const tip = esc(tipRaw);
  const fmtWs = s => esc(s.replace(/\t/g, "  "));

  if (Array.isArray(subSegments) && subSegments.length && core) {
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
      return `${fmtWs(subLead || lead)}<span class="${cls} ch-has-sub" title="${tip}">${innerSub}</span>${fmtWs(subTrail || trail)}`;
    }
  }

  const inner = renderVisibleTextWithInvisibles(core, tip);
  return `${fmtWs(lead)}<span class="${cls}" title="${tip}">${inner}</span>${fmtWs(trail)}`;
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

function _wrapOldNewPairHtml(innerHtml, isSandhi) {
  if (!innerHtml) return "";
  const mLead = innerHtml.match(/^([ \t]*)([\s\S]*)$/);
  const leadWs = (mLead && mLead[2]) ? mLead[1] : "";
  const afterLead = (mLead && mLead[2]) ? mLead[2] : innerHtml;
  const mTrail = afterLead.match(/^([\s\S]*?)([ \t]*)$/);
  const coreHtml = (mTrail && mTrail[1]) ? mTrail[1] : afterLead;
  const trailWs = (mTrail && mTrail[1]) ? mTrail[2] : "";
  const cls = isSandhi ? "ch-pair ch-sandhi" : "ch-pair";
  return `${leadWs}<span class="${cls}">${coreHtml}</span>${trailWs}`;
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
    if (s.op === "del_nl") return `<span class="nl-badge nl-del" title="Removed line break">↵</span>`;
    if (s.op === "ins_nl") return `<span class="nl-badge nl-ins" title="Inserted line break">↵</span>`;
    const isS = !!(sandhiMarks && sandhiMarks.has(idx));
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
        let pairHtml = "";
        let pairIsSandhi = false;
        for (let p = i; p <= lastContentIdx; p++) {
          if (sandhiMarks && sandhiMarks.has(p)) pairIsSandhi = true;
          pairHtml += renderOneSegment(p);
        }
        out += _wrapOldNewPairHtml(pairHtml, pairIsSandhi);
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
