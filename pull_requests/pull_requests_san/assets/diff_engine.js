(function (globalScope) {
  "use strict";

  const VALID_DIFF_MODES = ["grapheme", "codepoint", "word"];
  const VALID_NORMALIZATIONS = ["none", "nfc", "nfd", "nfkc", "nfkd"];
  const PUNCT_KEY = "\x00punct\x00";
  const VEDIC_MARK_ONLY_RE = /^[\u1cd3\u0951-\u0954\u1cd0-\u1ce8\u1ced\u1cf4\u1cf8\u1cf9\u0300-\u036f]+$/;
  const WORD_TOKEN_RE = /[^\S\n]+|\n|\S+/g;
  const _GRAPHEME_SEGMENTER = (typeof Intl !== "undefined" && Intl.Segmenter)
    ? new Intl.Segmenter("und", { granularity: "grapheme" })
    : null;

  function applyNormalization(text, form = "none") {
    const norm = String(form || "none").trim().toLowerCase();
    if (!VALID_NORMALIZATIONS.includes(norm)) {
      throw new Error(`Unsupported normalization ${JSON.stringify(form)}; expected one of ${VALID_NORMALIZATIONS.join(", ")}`);
    }
    if (!text || norm === "none") return text || "";
    return String(text).normalize(norm.toUpperCase());
  }

  function stripPunctuation(text) {
    if (!text) return text || "";
    return String(text).replace(/\p{P}+/gu, "");
  }

  function tokenizeForDiff(text, mode = "grapheme") {
    if (!text) return [];
    const s = String(text);
    if (mode === "codepoint") return Array.from(s);
    if (mode === "word") return s.match(WORD_TOKEN_RE) || [];
    if (!_GRAPHEME_SEGMENTER) return Array.from(s);
    const out = [];
    for (const seg of _GRAPHEME_SEGMENTER.segment(s)) {
      const t = seg.segment;
      if (out.length && VEDIC_MARK_ONLY_RE.test(t)) {
        out[out.length - 1] += t;
      } else {
        out.push(t);
      }
    }
    return out;
  }

  function _mergeSegments(segs) {
    const merged = [];
    for (const seg of (segs || [])) {
      const txt = seg && seg.text ? String(seg.text) : "";
      if (!txt) continue;
      if (merged.length && merged[merged.length - 1].op === seg.op) {
        merged[merged.length - 1].text += txt;
      } else {
        const copy = { op: seg.op, text: txt };
        if (seg.sub_segments) copy.sub_segments = seg.sub_segments;
        merged.push(copy);
      }
    }
    return merged;
  }

  function _trimCommonEdges(lhsToks, rhsToks) {
    let pref = 0;
    const minLen = Math.min(lhsToks.length, rhsToks.length);
    while (pref < minLen && lhsToks[pref] === rhsToks[pref]) pref += 1;
    let suff = 0;
    while (suff < (minLen - pref) && lhsToks[lhsToks.length - 1 - suff] === rhsToks[rhsToks.length - 1 - suff]) {
      suff += 1;
    }
    return [pref, suff];
  }

  function _lcsMatchesDp(a, aStart, aEnd, b, bStart, bEnd) {
    const m = aEnd - aStart;
    const n = bEnd - bStart;
    const stride = n + 1;
    const dp = new Int32Array((m + 1) * stride);
    for (let i = m - 1; i >= 0; i--) {
      const aVal = a[aStart + i];
      const rowOff = i * stride;
      const nextOff = (i + 1) * stride;
      for (let j = n - 1; j >= 0; j--) {
        if (aVal === b[bStart + j]) {
          dp[rowOff + j] = 1 + dp[nextOff + j + 1];
        } else {
          const down = dp[nextOff + j];
          const right = dp[rowOff + j + 1];
          dp[rowOff + j] = down >= right ? down : right;
        }
      }
    }
    const matches = [];
    let i = 0;
    let j = 0;
    while (i < m && j < n) {
      if (a[aStart + i] === b[bStart + j]) {
        matches.push([aStart + i, bStart + j]);
        i += 1;
        j += 1;
      } else if (dp[(i + 1) * stride + j] >= dp[i * stride + j + 1]) {
        i += 1;
      } else {
        j += 1;
      }
    }
    return matches;
  }

  function _uniqueAnchorMatches(a, aStart, aEnd, b, bStart, bEnd) {
    const aCounts = new Map();
    const aPos = new Map();
    for (let i = aStart; i < aEnd; i++) {
      const k = a[i];
      aCounts.set(k, (aCounts.get(k) || 0) + 1);
      aPos.set(k, i);
    }
    const bCounts = new Map();
    const bPos = new Map();
    for (let j = bStart; j < bEnd; j++) {
      const k = b[j];
      bCounts.set(k, (bCounts.get(k) || 0) + 1);
      bPos.set(k, j);
    }
    const cands = [];
    for (const [k, cnt] of aCounts.entries()) {
      if (cnt === 1 && bCounts.get(k) === 1) {
        cands.push([aPos.get(k), bPos.get(k)]);
      }
    }
    if (!cands.length) return [];
    cands.sort((x, y) => x[0] - y[0]);
    // Longest increasing subsequence on b index
    const tails = [];
    const tailCands = [];
    const prev = new Int32Array(cands.length);
    prev.fill(-1);
    for (let idx = 0; idx < cands.length; idx++) {
      const bIdx = cands[idx][1];
      let lo = 0;
      let hi = tails.length;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (tails[mid] < bIdx) lo = mid + 1;
        else hi = mid;
      }
      tails[lo] = bIdx;
      tailCands[lo] = idx;
      if (lo > 0) prev[idx] = tailCands[lo - 1];
    }
    const lis = [];
    let cur = tailCands[tailCands.length - 1];
    while (cur !== -1) {
      lis.push(cands[cur]);
      cur = prev[cur];
    }
    lis.reverse();
    return lis;
  }

  function _collectMatchesRange(a, aStart, aEnd, b, bStart, bEnd, out) {
    while (aStart < aEnd && bStart < bEnd && a[aStart] === b[bStart]) {
      out.push([aStart, bStart]);
      aStart += 1;
      bStart += 1;
    }
    const tail = [];
    while (aEnd > aStart && bEnd > bStart && a[aEnd - 1] === b[bEnd - 1]) {
      aEnd -= 1;
      bEnd -= 1;
      tail.push([aEnd, bEnd]);
    }
    const m = aEnd - aStart;
    const n = bEnd - bStart;
    if (m > 0 && n > 0) {
      if (m * n <= 160000) {
        for (const pt of _lcsMatchesDp(a, aStart, aEnd, b, bStart, bEnd)) out.push(pt);
      } else {
        const anchors = _uniqueAnchorMatches(a, aStart, aEnd, b, bStart, bEnd);
        if (anchors.length) {
          let pa = aStart;
          let pb = bStart;
          for (const [ai, bi] of anchors) {
            _collectMatchesRange(a, pa, ai, b, pb, bi, out);
            out.push([ai, bi]);
            pa = ai + 1;
            pb = bi + 1;
          }
          _collectMatchesRange(a, pa, aEnd, b, pb, bEnd, out);
        } else {
          // Greedy hash fallback on very large unanchored slices
          const bMap = new Map();
          for (let j = bStart; j < bEnd; j++) {
            const k = b[j];
            if (!bMap.has(k)) bMap.set(k, []);
            bMap.get(k).push(j);
          }
          let curB = bStart;
          for (let i = aStart; i < aEnd; i++) {
            const lst = bMap.get(a[i]);
            if (!lst) continue;
            for (const j of lst) {
              if (j >= curB) {
                out.push([i, j]);
                curB = j + 1;
                break;
              }
            }
          }
        }
      }
    }
    for (let k = tail.length - 1; k >= 0; k--) out.push(tail[k]);
  }

  function sequenceOpcodes(a, b) {
    const matches = [];
    _collectMatchesRange(a, 0, a.length, b, 0, b.length, matches);
    matches.push([a.length, b.length]);
    const opcodes = [];
    let i = 0;
    let j = 0;
    for (const [ai, bi] of matches) {
      let tag = "";
      if (i < ai && j < bi) tag = "replace";
      else if (i < ai) tag = "delete";
      else if (j < bi) tag = "insert";
      if (tag) opcodes.push([tag, i, ai, j, bi]);
      i = ai;
      j = bi;
      if (ai < a.length) {
        let endA = ai;
        let endB = bi;
        // coalescing occurs cleanly per match, merge adjacent equal below
        opcodes.push(["equal", ai, ai + 1, bi, bi + 1]);
        i = ai + 1;
        j = bi + 1;
      }
    }
    const merged = [];
    for (const op of opcodes) {
      if (merged.length && merged[merged.length - 1][0] === "equal" && op[0] === "equal" &&
          merged[merged.length - 1][2] === op[1] && merged[merged.length - 1][4] === op[3]) {
        merged[merged.length - 1][2] = op[2];
        merged[merged.length - 1][4] = op[4];
      } else {
        merged.push(op);
      }
    }
    return merged;
  }

  function _tokCompareKey(tok) {
    if (!tok || tok.trim() === "") return tok;
    const cleaned = stripPunctuation(tok);
    return cleaned || PUNCT_KEY;
  }

  function _lineSimilarity(a, b) {
    const sA = String(a || "");
    const sB = String(b || "");
    if (!sA.trim() && !sB.trim()) return sA === sB ? 0.08 : 0.04;
    if (!sA.trim() || !sB.trim()) return 0.0;
    if (sA === sB) return 1.0;
    const ca = Array.from(sA.slice(0, 800));
    const cb = Array.from(sB.slice(0, 800));
    const total = ca.length + cb.length;
    if (!total) return 1.0;
    const ops = sequenceOpcodes(ca, cb);
    let matches = 0;
    for (const [tag, i1, i2] of ops) {
      if (tag === "equal") matches += (i2 - i1);
    }
    return (2.0 * matches) / total;
  }

  function _diffRawTokens(lhs, rhs, mode = "grapheme", ignorePunct = false) {
    const lhsToks = tokenizeForDiff(lhs, mode);
    const rhsToks = tokenizeForDiff(rhs, mode);
    const lhsKeys = ignorePunct ? lhsToks.map(_tokCompareKey) : lhsToks;
    const rhsKeys = ignorePunct ? rhsToks.map(_tokCompareKey) : rhsToks;
    const [pref, suff] = _trimCommonEdges(lhsKeys, rhsKeys);
    const lhsEnd = lhsToks.length - suff;
    const rhsEnd = rhsToks.length - suff;
    const lhsMid = lhsToks.slice(pref, lhsEnd);
    const rhsMid = rhsToks.slice(pref, rhsEnd);
    const lhsMidK = lhsKeys.slice(pref, lhsEnd);
    const rhsMidK = rhsKeys.slice(pref, rhsEnd);

    const chunks = [];
    if (pref) {
      chunks.push(["equal", lhsToks.slice(0, pref).join(""), rhsToks.slice(0, pref).join("")]);
    }
    if (lhsMid.length * rhsMid.length > 4000000) {
      chunks.push(["replace", lhsMid.join(""), rhsMid.join("")]);
    } else if (lhsMid.length || rhsMid.length) {
      for (const [tag, i1, i2, j1, j2] of sequenceOpcodes(lhsMidK, rhsMidK)) {
        const lPart = lhsMid.slice(i1, i2).join("");
        const rPart = rhsMid.slice(j1, j2).join("");
        if (tag === "equal") chunks.push(["equal", lPart, rPart]);
        else if (tag === "delete") chunks.push(["delete", lPart, ""]);
        else if (tag === "insert") chunks.push(["insert", "", rPart]);
        else chunks.push(["replace", lPart, rPart]);
      }
    }
    if (suff) {
      chunks.push(["equal", lhsToks.slice(lhsEnd).join(""), rhsToks.slice(rhsEnd).join("")]);
    }
    if (mode === "word" && chunks.length >= 3) {
      const cleaned = [];
      let idx = 0;
      while (idx < chunks.length) {
        const [tag, lp, rp] = chunks[idx];
        if (
          tag === "equal" &&
          !lp.includes("\n") &&
          !rp.includes("\n") &&
          !lp.trim() &&
          !rp.trim() &&
          cleaned.length &&
          cleaned[cleaned.length - 1][0] !== "equal" &&
          idx + 1 < chunks.length &&
          chunks[idx + 1][0] !== "equal"
        ) {
          const prev = cleaned[cleaned.length - 1];
          const nxt = chunks[idx + 1];
          cleaned[cleaned.length - 1] = ["replace", prev[1] + lp + nxt[1], prev[2] + rp + nxt[2]];
          idx += 2;
        } else {
          cleaned.push([tag, lp, rp]);
          idx += 1;
        }
      }
      return cleaned;
    }
    return chunks;
  }

  function _refineWordPureEdits(chunks, ignorePunct = false) {
    const out = [];
    for (const [tag, lPart, rPart] of chunks) {
      if (tag !== "replace" || !lPart || !rPart || Math.max(lPart.length, rPart.length) > 4000) {
        out.push([tag, lPart, rPart]);
        continue;
      }
      const sub = _diffRawTokens(lPart, rPart, "grapheme", ignorePunct);
      let hasDel = false;
      let hasIns = false;
      let hasRep = false;
      let hasNonWsEq = false;
      for (const [st, sl, sr] of sub) {
        if (st === "delete") hasDel = true;
        else if (st === "insert") hasIns = true;
        else if (st === "replace") hasRep = true;
        else if (st === "equal" && (sl.trim() || sr.trim())) hasNonWsEq = true;
      }
      if (!hasRep && hasNonWsEq && (hasDel !== hasIns)) {
        out.push(...sub);
      } else {
        out.push([tag, lPart, rPart]);
      }
    }
    return out;
  }

  function _iterDiffChunks(lhs, rhs, { mode = "grapheme", normalize = "none", ignorePunct = false } = {}) {
    if (!VALID_DIFF_MODES.includes(mode)) {
      throw new Error(`Unsupported diff mode ${JSON.stringify(mode)}`);
    }
    const lhsN = applyNormalization(lhs, normalize);
    const rhsN = applyNormalization(rhs, normalize);
    const lhsCmp = ignorePunct ? stripPunctuation(lhsN) : lhsN;
    const rhsCmp = ignorePunct ? stripPunctuation(rhsN) : rhsN;
    if (lhsCmp === rhsCmp) {
      return (lhsN || rhsN) ? [["equal", lhsN, rhsN]] : [];
    }
    if (!lhsN) return rhsN ? [["insert", "", rhsN]] : [];
    if (!rhsN) return lhsN ? [["delete", lhsN, ""]] : [];
    if (mode === "word") {
      return _refineWordPureEdits(_diffRawTokens(lhsN, rhsN, mode, ignorePunct), ignorePunct);
    }
    if (Math.max(lhsN.length, rhsN.length) <= 120) {
      return _diffRawTokens(lhsN, rhsN, mode, ignorePunct);
    }
    const wordChunks = _diffRawTokens(lhsN, rhsN, "word", ignorePunct);
    const refined = [];
    for (const [tag, lPart, rPart] of wordChunks) {
      if (tag !== "replace") {
        refined.push([tag, lPart, rPart]);
        continue;
      }
      const maxChunk = Math.max(lPart.length, rPart.length);
      if (maxChunk <= 400 && (maxChunk <= 32 || _lineSimilarity(lPart, rPart) >= 0.35)) {
        refined.push(..._diffRawTokens(lPart, rPart, mode, ignorePunct));
      } else {
        refined.push(["replace", lPart, rPart]);
      }
    }
    return refined;
  }

  function computeCharDiff(lhs, rhs, mode = "grapheme", { normalize = "none", ignorePunct = false } = {}) {
    const lhsSegs = [];
    const rhsSegs = [];
    for (const [tag, lPart, rPart] of _iterDiffChunks(lhs, rhs, { mode, normalize, ignorePunct })) {
      if (tag === "equal") {
        if (lPart) lhsSegs.push({ op: "eq", text: lPart });
        if (rPart) rhsSegs.push({ op: "eq", text: rPart });
      } else {
        if (lPart) lhsSegs.push({ op: "del", text: lPart });
        if (rPart) rhsSegs.push({ op: "ins", text: rPart });
      }
    }
    return [_mergeSegments(lhsSegs), _mergeSegments(rhsSegs)];
  }

  function computeStreamDiff(lhs, rhs, { mode = "grapheme", normalize = "none", ignorePunct = false } = {}) {
    const segs = [];
    for (const [tag, lPart, rPart] of _iterDiffChunks(lhs, rhs, { mode, normalize, ignorePunct })) {
      if (tag === "equal") {
        const outTxt = rPart || lPart;
        if (outTxt) segs.push({ op: "eq", text: outTxt });
      } else {
        if (lPart) segs.push({ op: "del", text: lPart });
        if (rPart) segs.push({ op: "ins", text: rPart });
      }
    }
    return _mergeSegments(segs);
  }

  function _alignLargeBlock(delLines, addLines) {
    const dTexts = delLines.map(x => x[1]);
    const aTexts = addLines.map(x => x[1]);
    const paired = [];
    for (const [tag, i1, i2, j1, j2] of sequenceOpcodes(dTexts, aTexts)) {
      if (tag === "equal") {
        for (let k = 0; k < i2 - i1; k++) paired.push([delLines[i1 + k], addLines[j1 + k]]);
      } else if (tag === "delete") {
        for (let di = i1; di < i2; di++) paired.push([delLines[di], null]);
      } else if (tag === "insert") {
        for (let aj = j1; aj < j2; aj++) paired.push([null, addLines[aj]]);
      } else {
        const common = Math.min(i2 - i1, j2 - j1);
        for (let k = 0; k < common; k++) {
          const dl = delLines[i1 + k];
          const al = addLines[j1 + k];
          if (_lineSimilarity(dl[1], al[1]) >= 0.25 || (!dl[1].trim() && !al[1].trim())) {
            paired.push([dl, al]);
          } else {
            paired.push([dl, null]);
            paired.push([null, al]);
          }
        }
        for (let di = i1 + common; di < i2; di++) paired.push([delLines[di], null]);
        for (let aj = j1 + common; aj < j2; aj++) paired.push([null, addLines[aj]]);
      }
    }
    return paired;
  }

  function _alignDpBlock(delLines, addLines) {
    const m = delLines.length;
    const n = addLines.length;
    const minPairSim = 0.25;
    const stride = n + 1;
    const scores = new Float64Array((m + 1) * stride);
    const back = new Uint8Array((m + 1) * stride);
    for (let i = 1; i <= m; i++) back[i * stride] = 2;
    for (let j = 1; j <= n; j++) back[j] = 3;

    for (let i = 1; i <= m; i++) {
      const dText = delLines[i - 1][1];
      const rowOff = i * stride;
      const prevOff = (i - 1) * stride;
      for (let j = 1; j <= n; j++) {
        const aText = addLines[j - 1][1];
        let diagDelta;
        if (!dText.trim() && !aText.trim()) {
          diagDelta = dText === aText ? 0.08 : 0.04;
        } else {
          const sim = _lineSimilarity(dText, aText);
          diagDelta = sim >= minPairSim ? sim : -1.0;
        }
        const diag = scores[prevOff + j - 1] + diagDelta;
        const up = scores[prevOff + j];
        const left = scores[rowOff + j - 1];
        if (diag >= up && diag >= left && diagDelta > 0) {
          scores[rowOff + j] = diag;
          back[rowOff + j] = 1;
        } else if (up >= left) {
          scores[rowOff + j] = up;
          back[rowOff + j] = 2;
        } else {
          scores[rowOff + j] = left;
          back[rowOff + j] = 3;
        }
      }
    }

    const alignedRev = [];
    let i = m;
    let j = n;
    while (i > 0 || j > 0) {
      const step = back[i * stride + j];
      if (i > 0 && j > 0 && step === 1) {
        alignedRev.push([delLines[i - 1], addLines[j - 1]]);
        i -= 1;
        j -= 1;
      } else if (i > 0 && (j === 0 || step === 2)) {
        alignedRev.push([delLines[i - 1], null]);
        i -= 1;
      } else {
        alignedRev.push([null, addLines[j - 1]]);
        j -= 1;
      }
    }
    alignedRev.reverse();
    return alignedRev;
  }

  function alignChangeBlockLines(delLines, addLines) {
    const m = delLines.length;
    const n = addLines.length;
    if (m === 0) return addLines.map(x => [null, x]);
    if (n === 0) return delLines.map(x => [x, null]);
    if (m === 1 && n === 1) {
      const dTxt = delLines[0][1];
      const aTxt = addLines[0][1];
      if ((!dTxt.trim() && !aTxt.trim()) || _lineSimilarity(dTxt, aTxt) >= 0.22 || Math.max(dTxt.length, aTxt.length) <= 24) {
        return [[delLines[0], addLines[0]]];
      }
      return [[delLines[0], null], [null, addLines[0]]];
    }
    if (m * n > 2500) return _alignLargeBlock(delLines, addLines);
    return _alignDpBlock(delLines, addLines);
  }

  function _recomputeRowSegments(row, mode, normalize, ignorePunct) {
    if (row.kind === "del") {
      row.lhs_segments = row.left_text ? [{ op: "del", text: row.left_text }] : [];
      row.rhs_segments = [];
    } else if (row.kind === "add") {
      row.lhs_segments = [];
      row.rhs_segments = row.right_text ? [{ op: "ins", text: row.right_text }] : [];
    } else if (row.kind === "context" && normalize === "none" && !ignorePunct) {
      row.lhs_segments = row.left_text ? [{ op: "eq", text: row.left_text }] : [];
      row.rhs_segments = row.right_text ? [{ op: "eq", text: row.right_text }] : [];
    } else {
      const [lhsSegs, rhsSegs] = computeCharDiff(row.left_text || "", row.right_text || "", mode, { normalize, ignorePunct });
      row.lhs_segments = lhsSegs;
      row.rhs_segments = rhsSegs;
      const hasChange = lhsSegs.some(s => s.op !== "eq") || rhsSegs.some(s => s.op !== "eq");
      row.kind = hasChange ? "modify" : "context";
    }
    return row;
  }

  function _buildRowsFromPairs(pairs, { charDiffMode = "grapheme", inDiffHunk = true, normalize = "none", ignorePunct = false } = {}) {
    return pairs.map(([left, right]) => {
      let row;
      if (left !== null && right !== null) {
        const [lnum, ltxt] = left;
        const [rnum, rtxt] = right;
        row = { kind: ltxt === rtxt ? "context" : "modify", left_num: lnum, right_num: rnum, left_text: ltxt, right_text: rtxt, in_diff_hunk: inDiffHunk };
      } else if (left !== null) {
        row = { kind: "del", left_num: left[0], right_num: null, left_text: left[1], right_text: null, in_diff_hunk: inDiffHunk };
      } else {
        row = { kind: "add", left_num: null, right_num: right[0], left_text: null, right_text: right[1], in_diff_hunk: inDiffHunk };
      }
      return _recomputeRowSegments(row, charDiffMode, normalize, ignorePunct);
    });
  }

  function _realignRowsAcrossBlanks(rawRows, { charDiffMode = "grapheme", normalize = "none", ignorePunct = false } = {}) {
    const out = [];
    const n = rawRows.length;
    let i = 0;
    while (i < n) {
      const r = rawRows[i];
      if (r.kind === "context" && (r.left_text || "").trim() !== "") {
        out.push(r);
        i += 1;
        continue;
      }
      let j = i;
      while (j < n && !(rawRows[j].kind === "context" && (rawRows[j].left_text || "").trim() !== "")) {
        j += 1;
      }
      const island = rawRows.slice(i, j);
      i = j;
      let lead = 0;
      while (lead < island.length && island[lead].kind === "context") {
        out.push(island[lead]);
        lead += 1;
      }
      let trail = island.length;
      while (trail > lead && island[trail - 1].kind === "context") trail -= 1;
      const core = island.slice(lead, trail);
      if (core.length) {
        const oldLines = core.filter(cr => cr.left_text != null && cr.left_num != null).map(cr => [cr.left_num, cr.left_text]).sort((a, b) => a[0] - b[0]);
        const newLines = core.filter(cr => cr.right_text != null && cr.right_num != null).map(cr => [cr.right_num, cr.right_text]).sort((a, b) => a[0] - b[0]);
        const pairs = alignChangeBlockLines(oldLines, newLines);
        const anyInHunk = core.some(cr => cr.in_diff_hunk);
        out.push(..._buildRowsFromPairs(pairs, { charDiffMode, inDiffHunk: anyInHunk, normalize, ignorePunct }));
      }
      for (let k = trail; k < island.length; k++) out.push(island[k]);
    }
    return out;
  }

  function _combineAlignedPairSegments(r) {
    const lhs = (r.lhs_segments || []).map(x => ({ ...x }));
    const rhs = (r.rhs_segments || []).map(x => ({ ...x }));
    const out = [];
    let li = 0;
    let ri = 0;
    while (li < lhs.length || ri < rhs.length) {
      if (li < lhs.length && lhs[li].op !== "eq") {
        out.push({ ...lhs[li++] });
        continue;
      }
      if (ri < rhs.length && rhs[ri].op !== "eq") {
        out.push({ ...rhs[ri++] });
        continue;
      }
      if (li < lhs.length && ri < rhs.length) {
        const lTxt = lhs[li].text;
        const rTxt = rhs[ri].text;
        if (lTxt === rTxt) {
          out.push({ op: "eq", text: lTxt });
          li += 1;
          ri += 1;
        } else if (lTxt.length <= rTxt.length && rTxt.startsWith(lTxt)) {
          out.push({ op: "eq", text: lTxt });
          rhs[ri] = { op: "eq", text: rTxt.slice(lTxt.length) };
          li += 1;
        } else if (rTxt.length < lTxt.length && lTxt.startsWith(rTxt)) {
          out.push({ op: "eq", text: rTxt });
          lhs[li] = { op: "eq", text: lTxt.slice(rTxt.length) };
          ri += 1;
        } else {
          out.push({ op: "del", text: lTxt });
          out.push({ op: "ins", text: rTxt });
          li += 1;
          ri += 1;
        }
      } else if (li < lhs.length) {
        out.push({ ...lhs[li++] });
      } else {
        out.push({ ...rhs[ri++] });
      }
    }
    return _mergeSegments(out);
  }

  function _alignedRowToInlineDict(r) {
    let segs;
    if (r.kind === "context") {
      segs = r.left_text ? [{ op: "eq", text: r.left_text }] : [];
    } else if (r.kind === "del") {
      segs = r.left_text ? [{ op: "del", text: r.left_text }] : [];
    } else if (r.kind === "add") {
      segs = r.right_text ? [{ op: "ins", text: r.right_text }] : [];
    } else {
      segs = _combineAlignedPairSegments(r);
    }
    return { kind: r.kind, left_num: r.left_num, right_num: r.right_num, in_diff_hunk: !!r.in_diff_hunk, segments: segs };
  }

  function _hasEqBeforeNextNl(segments, segIdx, curParts, partIdx) {
    if (partIdx + 1 < curParts.length - 1) return false;
    for (let j = segIdx + 1; j < segments.length; j++) {
      const s = segments[j];
      const txt = s.text || "";
      if (txt.includes("\n")) return s.op === "eq" && Boolean(txt.split("\n", 1)[0]);
      if (s.op === "eq" && txt) return true;
    }
    return false;
  }

  function _streamSegmentsToInlineRows(segments, leftEntries, rightEntries) {
    const rows = [];
    let lIdx = 0;
    let rIdx = 0;
    let curSegs = [];
    let curLNum = null;
    let curRNum = null;
    let usedL = false;
    let usedR = false;
    let hasEq = false;
    let hasDel = false;
    let hasIns = false;

    const touchLeft = () => {
      usedL = true;
      if (curLNum === null && lIdx < leftEntries.length) curLNum = leftEntries[lIdx][0];
    };
    const touchRight = () => {
      usedR = true;
      if (curRNum === null && rIdx < rightEntries.length) curRNum = rightEntries[rIdx][0];
    };
    const flushRow = () => {
      let kind = "context";
      if (hasDel || hasIns) kind = (hasEq || (usedL && usedR)) ? "modify" : (hasDel ? "del" : "add");
      rows.push({ kind, left_num: curLNum, right_num: curRNum, in_diff_hunk: true, segments: _mergeSegments(curSegs) });
      curSegs = [];
      curLNum = null;
      curRNum = null;
      usedL = false;
      usedR = false;
      hasEq = false;
      hasDel = false;
      hasIns = false;
    };

    for (let si = 0; si < segments.length; si++) {
      const seg = segments[si];
      const op = seg.op;
      const parts = String(seg.text || "").split("\n");
      for (let pi = 0; pi < parts.length; pi++) {
        const part = parts[pi];
        if (part) {
          if (op === "eq") { touchLeft(); touchRight(); hasEq = true; }
          else if (op === "del") { touchLeft(); hasDel = true; }
          else { touchRight(); hasIns = true; }
          curSegs.push({ op, text: part });
        }
        if (pi < parts.length - 1) {
          if (op === "eq") {
            touchLeft(); touchRight(); flushRow();
            if (lIdx < leftEntries.length) lIdx += 1;
            if (rIdx < rightEntries.length) rIdx += 1;
          } else if (op === "del") {
            touchLeft(); hasDel = true;
            if ((hasEq || hasIns) && _hasEqBeforeNextNl(segments, si, parts, pi)) {
              curSegs.push({ op: "del_nl", text: "↵" });
            } else {
              flushRow();
            }
            if (lIdx < leftEntries.length) lIdx += 1;
          } else {
            touchRight(); hasIns = true;
            if ((hasEq || hasDel) && _hasEqBeforeNextNl(segments, si, parts, pi)) {
              curSegs.push({ op: "ins_nl", text: "↵" });
            } else {
              flushRow();
            }
            if (rIdx < rightEntries.length) rIdx += 1;
          }
        }
      }
    }
    if (curSegs.length || usedL || usedR) flushRow();
    return rows;
  }

  function _splitOuterWs(text) {
    const m = String(text || "").match(/^([ \t]*)([\s\S]*?)([ \t]*)$/);
    return m ? [m[1], m[2], m[3]] : ["", String(text || ""), ""];
  }

  function _annotatePairSubSegments(delSeg, insSeg, { normalize = "none", ignorePunct = false } = {}) {
    const [lLead, lCore, lTrail] = _splitOuterWs(delSeg.text);
    const [rLead, rCore, rTrail] = _splitOuterWs(insSeg.text);
    if (!lCore || !rCore) return false;
    const [lSub, rSub] = computeCharDiff(lCore, rCore, "grapheme", { normalize, ignorePunct });
    if (!lSub.some(s => s.op === "eq" && s.text.trim())) return false;
    delSeg.sub_segments = _mergeSegments([
      ...(lLead ? [{ op: "eq", text: lLead }] : []),
      ...lSub,
      ...(lTrail ? [{ op: "eq", text: lTrail }] : []),
    ]);
    insSeg.sub_segments = _mergeSegments([
      ...(rLead ? [{ op: "eq", text: rLead }] : []),
      ...rSub,
      ...(rTrail ? [{ op: "eq", text: rTrail }] : []),
    ]);
    return true;
  }

  function _annotateWordSubSegments(flowRows, { normalize = "none", ignorePunct = false } = {}) {
    const flat = [];
    for (const fr of flowRows) {
      if (fr.kind === "context") {
        flat.push(null);
        continue;
      }
      for (const seg of (fr.segments || [])) {
        if (seg.op === "del_nl" || seg.op === "ins_nl") continue;
        flat.push(seg);
      }
    }
    let i = 0;
    const n = flat.length;
    while (i < n) {
      const s = flat[i];
      if (!s || s.op !== "del") { i += 1; continue; }
      const dStart = i;
      while (i < n && flat[i] && flat[i].op === "del") i += 1;
      const dRun = flat.slice(dStart, i).filter(Boolean);
      const aStart = i;
      while (i < n && flat[i] && flat[i].op === "ins") i += 1;
      const aRun = flat.slice(aStart, i).filter(Boolean);
      if (!dRun.length || !aRun.length) continue;
      if (dRun.length === aRun.length) {
        for (let k = 0; k < dRun.length; k++) {
          _annotatePairSubSegments(dRun[k], aRun[k], { normalize, ignorePunct });
        }
      } else {
        const usedIns = new Set();
        for (const ds of dRun) {
          let bestIdx = -1;
          let bestSim = 0.25;
          for (let idx = 0; idx < aRun.length; idx++) {
            if (usedIns.has(idx)) continue;
            const sim = _lineSimilarity(ds.text || "", aRun[idx].text || "");
            if (sim >= bestSim) { bestSim = sim; bestIdx = idx; }
          }
          if (bestIdx >= 0) {
            usedIns.add(bestIdx);
            _annotatePairSubSegments(ds, aRun[bestIdx], { normalize, ignorePunct });
          }
        }
      }
    }
  }

  function buildInlineFlowRows(rows, { charDiffMode = "grapheme", normalize = "none", ignorePunct = false } = {}) {
    const out = [];
    const n = rows.length;
    let i = 0;
    while (i < n) {
      const r = rows[i];
      if (r.kind === "context") {
        out.push(_alignedRowToInlineDict(r));
        i += 1;
        continue;
      }
      let j = i;
      while (j < n && rows[j].kind !== "context") j += 1;
      const island = rows.slice(i, j);
      i = j;
      const leftEntries = island.filter(cr => cr.left_text != null).map(cr => [cr.left_num, cr.left_text]);
      const rightEntries = island.filter(cr => cr.right_text != null).map(cr => [cr.right_num, cr.right_text]);
      if ((leftEntries.length && !rightEntries.length) || (rightEntries.length && !leftEntries.length)) {
        for (const cr of island) out.push(_alignedRowToInlineDict(cr));
        continue;
      }
      const oldBlock = leftEntries.map(x => x[1]).join("\n");
      const newBlock = rightEntries.map(x => x[1]).join("\n");
      const segs = computeStreamDiff(oldBlock, newBlock, { mode: charDiffMode, normalize, ignorePunct });
      const flowRows = _streamSegmentsToInlineRows(segs, leftEntries, rightEntries);
      const anyInHunk = island.some(cr => cr.in_diff_hunk);
      for (const fr of flowRows) fr.in_diff_hunk = anyInHunk;
      out.push(...flowRows);
    }
    if (charDiffMode === "word") {
      _annotateWordSubSegments(out, { normalize, ignorePunct });
    }
    return out;
  }

  function _splitLinesClean(text) {
    if (text == null || text === "") return [];
    const s = String(text).replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    const lines = s.split("\n");
    if (lines.length && lines[lines.length - 1] === "") lines.pop();
    return lines;
  }

  function buildFullFileHunk(baseText, headText, { charDiffMode = "word", normalize = "none", ignorePunct = false } = {}) {
    const baseLines = _splitLinesClean(baseText);
    const headLines = _splitLinesClean(headText);
    let rawAdditions = 0;
    let rawDeletions = 0;
    let rows = [];
    for (const [tag, i1, i2, j1, j2] of sequenceOpcodes(baseLines, headLines)) {
      if (tag === "equal") {
        for (let k = 0; k < i2 - i1; k++) {
          const di = i1 + k;
          const aj = j1 + k;
          const row = _recomputeRowSegments(
            {
              kind: "context",
              left_num: di + 1,
              right_num: aj + 1,
              left_text: baseLines[di],
              right_text: headLines[aj],
              in_diff_hunk: true,
            },
            charDiffMode,
            normalize,
            ignorePunct
          );
          rows.push(row);
        }
      } else if (tag === "delete") {
        rawDeletions += (i2 - i1);
        const dels = [];
        for (let di = i1; di < i2; di++) dels.push([[di + 1, baseLines[di]], null]);
        rows.push(..._buildRowsFromPairs(dels, { charDiffMode, normalize, ignorePunct }));
      } else if (tag === "insert") {
        rawAdditions += (j2 - j1);
        const adds = [];
        for (let aj = j1; aj < j2; aj++) adds.push([null, [aj + 1, headLines[aj]]]);
        rows.push(..._buildRowsFromPairs(adds, { charDiffMode, normalize, ignorePunct }));
      } else {
        rawDeletions += (i2 - i1);
        rawAdditions += (j2 - j1);
        const dSlice = [];
        for (let di = i1; di < i2; di++) dSlice.push([di + 1, baseLines[di]]);
        const aSlice = [];
        for (let aj = j1; aj < j2; aj++) aSlice.push([aj + 1, headLines[aj]]);
        const pairs = alignChangeBlockLines(dSlice, aSlice);
        rows.push(..._buildRowsFromPairs(pairs, { charDiffMode, normalize, ignorePunct }));
      }
    }
    rows = _realignRowsAcrossBlanks(rows, { charDiffMode, normalize, ignorePunct });
    const inlineRows = buildInlineFlowRows(rows, { charDiffMode, normalize, ignorePunct });
    return {
      header: `@@ -1,${baseLines.length} +1,${headLines.length} @@ (Full File)`,
      old_start: baseLines.length ? 1 : 0,
      old_count: baseLines.length,
      new_start: headLines.length ? 1 : 0,
      new_count: headLines.length,
      context: "Full File",
      rows,
      inline_rows: inlineRows,
      additions: rawAdditions,
      deletions: rawDeletions,
    };
  }

  const _DEV_ACCENT_STRIP_RE = /[\u0951-\u0954\u1CD0-\u1CFF\uA8E0-\uA8FF\u200B-\u200D\uFEFF]/g;
  const _DEV_WORD_RE = /^[\u0900-\u094F\u0958-\u0963\u0971-\u097F]+$/;
  const _DEV_INDEP_VOWELS = new Set(["अ", "आ", "इ", "ई", "उ", "ऊ", "ऋ", "ॠ", "ए", "ऐ", "ओ", "औ"]);
  const _DEV_VOICED_CONS = new Set([
    "ग", "घ", "ङ", "ज", "झ", "ञ", "ड", "ढ", "ण", "द", "ध", "न", "ब", "भ", "म", "य", "र", "ल", "व", "ह",
  ]);
  const _DEV_VOWEL_TO_MATRA = {
    "आ": "ा", "इ": "ि", "ई": "ी", "उ": "ु", "ऊ": "ू",
    "ऋ": "ृ", "ॠ": "ॄ", "ए": "े", "ऐ": "ै", "ओ": "ो", "औ": "ौ",
  };
  const _DEV_VOWEL_MERGER = {
    "|अ": "ा", "|आ": "ा", "ा|अ": "ा", "ा|आ": "ा",
    "|इ": "े", "|ई": "े", "ा|इ": "े", "ा|ई": "े",
    "|उ": "ो", "|ऊ": "ो", "ा|उ": "ो", "ा|ऊ": "ो",
    "|ऋ": "र्", "ा|ऋ": "र्",
    "|ए": "ै", "|ऐ": "ै", "ा|ए": "ै", "ा|ऐ": "ै",
    "|ओ": "ौ", "|औ": "ौ", "ा|ओ": "ौ", "ा|औ": "ौ",
    "ि|इ": "ी", "ि|ई": "ी", "ी|इ": "ी", "ी|ई": "ी",
    "ु|उ": "ू", "ु|ऊ": "ू", "ू|उ": "ू", "ू|ऊ": "ू",
    "ृ|ऋ": "ॄ", "ृ|ॠ": "ॄ",
  };
  const _DEV_YAN = { "ि": "्य्", "ी": "्य्", "ु": "्व्", "ू": "्व्", "ृ": "्र्" };
  const _DEV_LONG_MATRA = { "ि": "ी", "ु": "ू" };
  const _DEV_R_FINAL_WORDS = new Set(["पुनः", "अन्तः", "प्रातः", "स्वः", "बहिः", "अहः"]);
  const _DEV_PRONOUN_SU_LOPA = new Set(["सः", "एषः", "स्यः"]);

  function _cleanDevaForSandhi(text) {
    return String(text || "").replace(_DEV_ACCENT_STRIP_RE, "");
  }

  function _canonDevaNasal(word) {
    return String(word || "")
      .replace(/([ङञणनम])\u094d(?=[कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसह])/g, "ं")
      .replace(/ँ/g, "ं");
  }

  function _stripCommonNonDevaEdges(a, b) {
    let s1 = String(a || "");
    let s2 = String(b || "");
    const isDevaChar = ch => ch >= "\u0900" && ch <= "\u0963" || (ch >= "\u0971" && ch <= "\u097F");
    while (s1.length && s2.length && s1[0] === s2[0] && !isDevaChar(s1[0])) {
      s1 = s1.slice(1);
      s2 = s2.slice(1);
    }
    while (s1.length && s2.length && s1[s1.length - 1] === s2[s2.length - 1] && !isDevaChar(s1[s1.length - 1])) {
      s1 = s1.slice(0, -1);
      s2 = s2.slice(0, -1);
    }
    return [s1, s2];
  }

  function _parseDevaWordList(text) {
    const cleaned = _cleanDevaForSandhi(text).trim();
    if (!cleaned) return [];
    const parts = cleaned.split(/\s+/).filter(Boolean);
    if (!parts.length) return [];
    for (const w of parts) {
      if (!_DEV_WORD_RE.test(w)) return [];
    }
    return parts;
  }

  function _joinConsHalantWithVowel(leftHalant, rightWord) {
    if (!leftHalant.endsWith("\u094d") || !rightWord) return null;
    const base = leftHalant.slice(0, -1);
    const r0 = rightWord[0];
    const rRest = rightWord.slice(1);
    if (r0 === "अ") return base + rRest;
    const m = _DEV_VOWEL_TO_MATRA[r0];
    return m ? (base + m + rRest) : null;
  }

  function _fuseDevaPair(l, r) {
    const outs = new Set();
    if (!l || !r) return outs;
    const r0 = r[0];
    const rRest = r.slice(1);
    const rIsVowel = _DEV_INDEP_VOWELS.has(r0);
    const rIsVoiced = _DEV_VOICED_CONS.has(r0);
    const lLast = l[l.length - 1];

    // Direct fusion with avagraha or simple consonant-ending + consonant
    if (r0 === "ऽ" && (lLast === "े" || lLast === "ो")) {
      outs.add(l + r);
      outs.add(l + " " + r);
    }

    // Independent vowel preverb "आ" + vowel
    if (l === "आ" && rIsVowel) {
      if (r0 === "अ" || r0 === "आ") outs.add("आ" + rRest);
      else if (r0 === "इ" || r0 === "ई") outs.add("ए" + rRest);
      else if (r0 === "उ" || r0 === "ऊ") outs.add("ओ" + rRest);
      else if (r0 === "ए" || r0 === "ऐ") outs.add("ऐ" + rRest);
      else if (r0 === "ओ" || r0 === "औ") outs.add("औ" + rRest);
    }

    // 1. Vowel endings (matra \u093e-\u094c or inherent अ) + independent vowel
    if (!l.endsWith("\u094d") && !l.endsWith("ः") && !l.endsWith("ं") && rIsVowel) {
      let stem = null;
      let matra = null;
      if (lLast >= "\u093e" && lLast <= "\u094c") {
        stem = l.slice(0, -1);
        matra = lLast;
      } else if (lLast >= "\u0915" && lLast <= "\u0939") {
        stem = l;
        matra = "";
      }
      if (matra !== null) {
        const merged = _DEV_VOWEL_MERGER[`${matra}|${r0}`];
        if (merged) outs.add(stem + merged + rRest);
        if (_DEV_YAN[matra]) {
          const yJoined = _joinConsHalantWithVowel(stem + _DEV_YAN[matra], r);
          if (yJoined) outs.add(yJoined);
        }
        if (matra === "े") {
          const aj = _joinConsHalantWithVowel(stem + "य्", r);
          if (aj) outs.add(aj);
          outs.add(stem + " " + r);
        } else if (matra === "ै") {
          const aj = _joinConsHalantWithVowel(stem + "ाय्", r);
          if (aj) outs.add(aj);
          outs.add(stem + "ा " + r);
        } else if (matra === "ो") {
          const aj = _joinConsHalantWithVowel(stem + "व्", r);
          if (aj) outs.add(aj);
          outs.add(stem + " " + r);
        } else if (matra === "औ".slice(0) || matra === "ौ") {
          const aj = _joinConsHalantWithVowel(stem + "ाव्", r);
          if (aj) outs.add(aj);
          outs.add(stem + "ा " + r);
        }
        if ((matra === "े" || matra === "ो") && r0 === "अ") {
          outs.add(l + "ऽ" + rRest);
        }
      }
    }

    // 2. Halant consonant + independent vowel
    if (l.endsWith("\u094d") && l.length >= 2 && rIsVowel) {
      const c = l[l.length - 2];
      const stem2 = l.slice(0, -2);
      const j0 = _joinConsHalantWithVowel(l, r);
      if (j0) outs.add(j0);
      const voicedStop = { "त": "द", "क": "ग", "ट": "ड", "प": "ब" }[c];
      if (voicedStop) {
        const jv = _joinConsHalantWithVowel(stem2 + voicedStop + "\u094d", r);
        if (jv) {
          outs.add(jv);
          outs.add(stem2 + voicedStop + "\u094d " + r);
        }
      }
      if (c === "न" || c === "ङ" || c === "ण") {
        const jg = _joinConsHalantWithVowel(l + c + "\u094d", r);
        if (jg) outs.add(jg);
      }
    }

    // 3. Visarga (ः) sandhi
    if (l.endsWith("ः") && l.length >= 2) {
      const base = l.slice(0, -1);
      const vp = base[base.length - 1];
      if (_DEV_PRONOUN_SU_LOPA.has(l) && r0 !== "अ") {
        outs.add(base + " " + r);
        outs.add(base + r);
      }
      if (r0 === "च" || r0 === "छ") { outs.add(base + "श्" + r); outs.add(base + "श् " + r); }
      else if (r0 === "ट" || r0 === "ठ") { outs.add(base + "ष्" + r); outs.add(base + "ष् " + r); }
      else if (r0 === "त" || r0 === "थ") { outs.add(base + "स्" + r); outs.add(base + "स् " + r); }
      else if (r0 === "श" || r0 === "ष" || r0 === "स") {
        outs.add(base + r0 + "\u094d" + r);
        outs.add(base + "स्" + r);
      }
      if (_DEV_R_FINAL_WORDS.has(l)) {
        if (rIsVowel) {
          const jr = _joinConsHalantWithVowel(base + "र्", r);
          if (jr) outs.add(jr);
        } else if (rIsVoiced && r0 !== "र") {
          outs.add(base + "र्" + r);
          outs.add(base + "र् " + r);
        } else if (r0 === "र" && _DEV_LONG_MATRA[vp]) {
          outs.add(base.slice(0, -1) + _DEV_LONG_MATRA[vp] + " " + r);
        }
      }
      if (vp >= "\u0915" && vp <= "\u0939") {
        if (r0 === "अ") {
          outs.add(base + "ोऽ" + rRest);
          outs.add(base + "ो " + r);
          outs.add(base + " " + r);
        } else if (rIsVowel) {
          outs.add(base + " " + r);
        } else if (rIsVoiced) {
          outs.add(base + "ो " + r);
          outs.add(base + "ो" + r);
        }
      } else if (vp === "ा") {
        if (rIsVowel || rIsVoiced) {
          outs.add(base + " " + r);
          if (rIsVowel) {
            const jvr = _joinConsHalantWithVowel(base + "र्", r);
            if (jvr) outs.add(jvr);
          }
        }
      } else {
        if (rIsVowel) {
          const jr = _joinConsHalantWithVowel(base + "र्", r);
          if (jr) outs.add(jr);
        } else if (r0 === "र" && _DEV_LONG_MATRA[vp]) {
          outs.add(base.slice(0, -1) + _DEV_LONG_MATRA[vp] + " " + r);
        } else if (rIsVoiced) {
          outs.add(base + "र्" + r);
          outs.add(base + "र् " + r);
        }
      }
    }

    // 4. Final त् / द् assimilation before consonants
    if (l.endsWith("त्") || l.endsWith("द्")) {
      const tStem = l.slice(0, -2);
      if (r0 === "च" || r0 === "छ") outs.add(tStem + "च्" + r);
      else if (r0 === "ज" || r0 === "झ") outs.add(tStem + "ज्" + r);
      else if (r0 === "ट" || r0 === "ठ") outs.add(tStem + "ट्" + r);
      else if (r0 === "ड" || r0 === "ढ") outs.add(tStem + "ड्" + r);
      else if (r0 === "ल") outs.add(tStem + "ल्" + r);
      else if (r0 === "न" || r0 === "म" || r0 === "ण" || r0 === "ङ" || r0 === "ञ") outs.add(tStem + "न्" + r);
      else if (r0 === "ह") outs.add(tStem + "द्ध" + rRest);
      else if (r0 === "श") outs.add(tStem + "च्छ" + rRest);
      else if (rIsVoiced) { outs.add(tStem + "द्" + r); outs.add(tStem + "द् " + r); }
    }

    // 5. Final म् / ं sandhi
    if (l.endsWith("म्") || l.endsWith("ं")) {
      const mStem = l.endsWith("म्") ? l.slice(0, -2) : l.slice(0, -1);
      if (!rIsVowel) {
        outs.add(mStem + "ं " + r);
        outs.add(mStem + "ं" + r);
        outs.add(mStem + "म् " + r);
        outs.add(mStem + "म्" + r);
      } else if (l.endsWith("म्")) {
        const jm = _joinConsHalantWithVowel(l, r);
        if (jm) outs.add(jm);
      }
    }

    // 6. Final न् before voiceless / palatal / lateral
    if (l.endsWith("न्")) {
      const nStem = l.slice(0, -2);
      if (r0 === "च" || r0 === "छ") outs.add(nStem + "ंश्" + r);
      else if (r0 === "ट" || r0 === "ठ") outs.add(nStem + "ंष्" + r);
      else if (r0 === "त" || r0 === "थ") outs.add(nStem + "ंस्" + r);
      else if (r0 === "ल") outs.add(nStem + "ंल्" + r);
      else if (r0 === "ज" || r0 === "झ") outs.add(nStem + "ञ्" + r);
      else if (r0 === "श") { outs.add(nStem + "ञ्छ" + rRest); outs.add(nStem + "ञ्" + r); }
    }

    // 7. Final क्/ग् or प्/ब् before voiced/nasal
    if (l.endsWith("क्") || l.endsWith("ग्")) {
      const kStem = l.slice(0, -2);
      if (rIsVoiced) { outs.add(kStem + "ग्" + r); outs.add(kStem + "ग् " + r); }
      if (r0 === "न" || r0 === "म") outs.add(kStem + "ङ्" + r);
      if (r0 === "ह") outs.add(kStem + "ग्घ" + rRest);
    }
    if (l.endsWith("प्") || l.endsWith("ब्")) {
      const pStem = l.slice(0, -2);
      if (rIsVoiced) { outs.add(pStem + "ब्" + r); outs.add(pStem + "ब् " + r); }
      if (r0 === "न" || r0 === "म") outs.add(pStem + "म्" + r);
    }

    return outs;
  }

  function _expandSandhiSequence(words) {
    if (!words.length || words.length > 6) return new Set();
    let states = [[]];
    for (const w of words) {
      const nextStates = [];
      const seen = new Set();
      const addState = arr => {
        if (nextStates.length >= 64) return;
        const key = arr.join(" ");
        if (!seen.has(key)) {
          seen.add(key);
          nextStates.push(arr);
        }
      };
      for (const st of states) {
        addState([...st, w]);
        if (st.length) {
          const prev = st[st.length - 1];
          const fusedSet = _fuseDevaPair(prev, w);
          for (const fused of fusedSet) {
            const fWords = fused.split(/\s+/).filter(Boolean);
            addState([...st.slice(0, -1), ...fWords]);
          }
        }
      }
      states = nextStates;
    }
    return new Set(states.map(st => _canonDevaNasal(st.join(" "))));
  }

  function isInterwordSandhiPair(lhsText, rhsText, prevEqWord = "", nextEqWord = "") {
    const [rawL, rawR] = _stripCommonNonDevaEdges(_cleanDevaForSandhi(lhsText).trim(), _cleanDevaForSandhi(rhsText).trim());
    const cleanL = rawL.trim();
    const cleanR = rawR.trim();
    if (!cleanL || !cleanR || _canonDevaNasal(cleanL) === _canonDevaNasal(cleanR)) return false;
    const wA = _parseDevaWordList(cleanL);
    const wB = _parseDevaWordList(cleanR);
    if (!wA.length || !wB.length) return false;
    if (wA.length === 1 && wB.length === 1 && _canonDevaNasal(wA[0]) === _canonDevaNasal(wB[0])) return false;

    const tryWindow = (aList, bList) => {
      if (!aList.length || !bList.length) return false;
      if (aList.length === 1 && bList.length === 1) {
        return _canonDevaNasal(aList[0]) === _canonDevaNasal(bList[0]);
      }
      const fA = _expandSandhiSequence(aList);
      const fB = _expandSandhiSequence(bList);
      for (const s of fA) {
        if (fB.has(s)) return true;
      }
      return false;
    };

    if ((wA.length > 1 || wB.length > 1 || _canonDevaNasal(wA[0]).includes(" ") !== _canonDevaNasal(wB[0]).includes(" ")) && tryWindow(wA, wB)) {
      return true;
    }
    if (wA.length >= 2 || wB.length >= 2) {
      if (tryWindow(wA, wB)) return true;
    }
    const prevClean = prevEqWord ? _parseDevaWordList(prevEqWord) : [];
    const nextClean = nextEqWord ? _parseDevaWordList(nextEqWord) : [];
    const pW = prevClean.length ? prevClean[prevClean.length - 1] : "";
    const nW = nextClean.length ? nextClean[0] : "";
    if (nW && tryWindow([...wA, nW], [...wB, nW])) return true;
    if (pW && tryWindow([pW, ...wA], [pW, ...wB])) return true;
    if (pW && nW && tryWindow([pW, ...wA, nW], [pW, ...wB, nW])) return true;
    return false;
  }

  function _trailingWordFromEq(text) {
    const s = _cleanDevaForSandhi(text);
    const m = s.match(/([\u0900-\u094F\u0958-\u0963\u0971-\u097F]+)\s*$/);
    if (!m) return { prefixWord: "", touched: false };
    return { prefixWord: m[1], touched: !/\s$/.test(s) };
  }

  function _leadingWordFromEq(text) {
    const s = _cleanDevaForSandhi(text);
    const m = s.match(/^\s*([\u0900-\u094F\u0958-\u0963\u0971-\u097F]+)/);
    if (!m) return { suffixWord: "", touched: false };
    return { suffixWord: m[1], touched: !/^\s/.test(s) };
  }

  function findSandhiMarkedIndices(segments, sandhiContext = null) {
    const marked = new Set();
    if (!Array.isArray(segments) || !segments.length) return marked;

    if (sandhiContext && sandhiContext.otherSegments && sandhiContext.leftText != null && sandhiContext.rightText != null) {
      if (isInterwordSandhiPair(sandhiContext.leftText, sandhiContext.rightText)) {
        for (let i = 0; i < segments.length; i++) {
          if (segments[i].op === "del" || segments[i].op === "ins") marked.add(i);
        }
        return marked;
      }
      const streamSegs = computeStreamDiff(sandhiContext.leftText, sandhiContext.rightText, { mode: "word" });
      const streamMarks = findSandhiMarkedIndices(streamSegs, null);
      if (streamMarks.size) {
        const wantOp = segments.some(s => s.op === "del") ? "del" : "ins";
        const sandhiPieces = [];
        for (const idx of streamMarks) {
          if (streamSegs[idx] && streamSegs[idx].op === wantOp) {
            const t = (streamSegs[idx].text || "").trim();
            if (t) sandhiPieces.push(t);
          }
        }
        for (let i = 0; i < segments.length; i++) {
          if (segments[i].op !== wantOp) continue;
          const t = (segments[i].text || "").trim();
          if (t && sandhiPieces.some(p => p === t || p.includes(t) || t.includes(p))) marked.add(i);
        }
      }
      return marked;
    }

    let i = 0;
    const n = segments.length;
    while (i < n) {
      const seg = segments[i];
      if (seg.op !== "del" && seg.op !== "ins") {
        i += 1;
        continue;
      }
      const runStart = i;
      let lhsMid = "";
      let rhsMid = "";
      while (i < n && (segments[i].op === "del" || segments[i].op === "ins")) {
        if (segments[i].op === "del") lhsMid += segments[i].text || "";
        else rhsMid += segments[i].text || "";
        i += 1;
      }
      let runEnd = i;
      while (runEnd < n && (segments[runEnd].op === "del_nl" || segments[runEnd].op === "ins_nl")) runEnd += 1;
      let pIdx = runStart - 1;
      while (pIdx >= 0 && (segments[pIdx].op === "del_nl" || segments[pIdx].op === "ins_nl")) pIdx -= 1;
      const prevSeg = pIdx >= 0 && segments[pIdx].op === "eq" ? segments[pIdx] : null;
      const nextSeg = runEnd < n && segments[runEnd].op === "eq" ? segments[runEnd] : null;
      const prevInfo = prevSeg ? _trailingWordFromEq(prevSeg.text) : { prefixWord: "", touched: false };
      const nextInfo = nextSeg ? _leadingWordFromEq(nextSeg.text) : { suffixWord: "", touched: false };

      let testL = lhsMid;
      let testR = rhsMid;
      let pCtx = "";
      let nCtx = "";
      if (prevInfo.touched && pIdx === runStart - 1 && prevInfo.prefixWord) {
        testL = prevInfo.prefixWord + testL;
        testR = prevInfo.prefixWord + testR;
      } else if (prevInfo.prefixWord) {
        pCtx = prevInfo.prefixWord;
      }
      if (nextInfo.touched && runEnd === i && nextInfo.suffixWord) {
        testL = testL + nextInfo.suffixWord;
        testR = testR + nextInfo.suffixWord;
      } else if (nextInfo.suffixWord) {
        nCtx = nextInfo.suffixWord;
      }

      if (isInterwordSandhiPair(testL, testR, pCtx, nCtx)) {
        for (let k = runStart; k < i; k++) marked.add(k);
      }
    }
    return marked;
  }

  function splitMixedSandhiPair(delText, insText) {
    const lWords = tokenizeForDiff(delText, "word").filter(t => t.trim());
    const rWords = tokenizeForDiff(insText, "word").filter(t => t.trim());
    const m = lWords.length;
    const n = rWords.length;
    if ((m <= 1 && n <= 1) || m > 16 || n > 16) return null;

    // Find a sandhi-equivalent sub-alignment separating sandhi groups from non-sandhi edits.
    const hits = [];
    for (let li = 0; li < m; li++) {
      for (let lj = li + 1; lj <= Math.min(m, li + 5); lj++) {
        const lStr = lWords.slice(li, lj).join(" ");
        for (let ri = 0; ri < n; ri++) {
          for (let rj = ri + 1; rj <= Math.min(n, ri + 5); rj++) {
            if (lj - li === m && rj - ri === n) continue;
            if (lj - li === 1 && rj - ri === 1 && lWords[li] === rWords[ri]) {
              hits.push({ li, lj, ri, rj, kind: "eq", score: 1 });
              continue;
            }
            const rStr = rWords.slice(ri, rj).join(" ");
            const pCtx = (li > 0 && ri > 0 && lWords[li - 1] === rWords[ri - 1]) ? lWords[li - 1] : "";
            const nCtx = (lj < m && rj < n && lWords[lj] === rWords[rj]) ? lWords[lj] : "";
            if (isInterwordSandhiPair(lStr, rStr, pCtx, nCtx)) {
              hits.push({ li, lj, ri, rj, kind: "sandhi", score: (lj - li) + (rj - ri) });
            }
          }
        }
      }
    }
    if (!hits.some(h => h.kind === "sandhi")) return null;
    hits.sort((a, b) => (a.li - b.li) || (a.ri - b.ri) || (b.score - a.score));

    // Simple highest-scoring non-overlapping monotonic subset over small m,n <= 16
    const bestFrom = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(null));
    function solve(i, j) {
      if (i >= m && j >= n) return { score: 0, chosen: [] };
      if (bestFrom[i][j]) return bestFrom[i][j];
      let best = { score: 0, chosen: [] };
      if (i < m) {
        const sub = solve(i + 1, j);
        if (sub.score > best.score) best = sub;
      }
      if (j < n) {
        const sub = solve(i, j + 1);
        if (sub.score > best.score) best = sub;
      }
      for (const h of hits) {
        if (h.li === i && h.ri === j) {
          const sub = solve(h.lj, h.rj);
          const sc = h.score + sub.score;
          if (sc > best.score) best = { score: sc, chosen: [h, ...sub.chosen] };
        }
      }
      bestFrom[i][j] = best;
      return best;
    }
    const sol = solve(0, 0);
    if (!sol.chosen.some(c => c.kind === "sandhi")) return null;

    const out = [];
    let ci = 0;
    let cj = 0;
    const emitGap = (li1, li2, ri1, ri2) => {
      if (li1 > 0 || ri1 > 0) out.push({ op: "eq", text: " " });
      if (li1 < li2) out.push({ op: "del", text: lWords.slice(li1, li2).join(" ") });
      if (ri1 < ri2) out.push({ op: "ins", text: rWords.slice(ri1, ri2).join(" ") });
    };
    for (const c of sol.chosen) {
      if (ci < c.li || cj < c.ri) emitGap(ci, c.li, cj, c.ri);
      if (out.length) out.push({ op: "eq", text: " " });
      if (c.kind === "eq") {
        out.push({ op: "eq", text: rWords.slice(c.ri, c.rj).join(" ") });
      } else {
        out.push({ op: "del", text: lWords.slice(c.li, c.lj).join(" ") });
        out.push({ op: "ins", text: rWords.slice(c.ri, c.rj).join(" ") });
      }
      ci = c.lj;
      cj = c.rj;
    }
    if (ci < m || cj < n) {
      if (out.length) out.push({ op: "eq", text: " " });
      if (ci < m) out.push({ op: "del", text: lWords.slice(ci, m).join(" ") });
      if (cj < n) out.push({ op: "ins", text: rWords.slice(cj, n).join(" ") });
    }
    const merged = _mergeSegments(out);
    if (merged.length <= 2) return null;
    const marks = findSandhiMarkedIndices(merged, null);
    if (!marks.size) return null;
    return merged;
  }

  const _HUNK_HEADER_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/;

  function parsePatchHunks(patch, { charDiffMode = "grapheme", normalize = "none", ignorePunct = false } = {}) {
    if (!patch) return [];
    const hunks = [];
    let curHunk = null;
    let oldLn = 0;
    let newLn = 0;
    let delBuf = [];
    let addBuf = [];

    const flushChanges = () => {
      if (curHunk && (delBuf.length || addBuf.length)) {
        const pairs = alignChangeBlockLines(delBuf, addBuf);
        curHunk.rows.push(..._buildRowsFromPairs(pairs, { charDiffMode, inDiffHunk: true, normalize, ignorePunct }));
      }
      delBuf = [];
      addBuf = [];
    };

    const finalizeHunk = () => {
      flushChanges();
      if (curHunk && curHunk.rows.length) {
        curHunk.rows = _realignRowsAcrossBlanks(curHunk.rows, { charDiffMode, normalize, ignorePunct });
        curHunk.inline_rows = buildInlineFlowRows(curHunk.rows, { charDiffMode, normalize, ignorePunct });
      }
    };

    for (const rawLine of _splitLinesClean(patch)) {
      const m = _HUNK_HEADER_RE.exec(rawLine);
      if (m) {
        finalizeHunk();
        oldLn = parseInt(m[1], 10);
        const oldCnt = m[2] !== undefined ? parseInt(m[2], 10) : 1;
        newLn = parseInt(m[3], 10);
        const newCnt = m[4] !== undefined ? parseInt(m[4], 10) : 1;
        curHunk = {
          header: rawLine,
          old_start: oldLn,
          old_count: oldCnt,
          new_start: newLn,
          new_count: newCnt,
          context: (m[5] || "").trim(),
          rows: [],
          inline_rows: [],
        };
        hunks.push(curHunk);
        continue;
      }
      if (!curHunk || rawLine.startsWith("\\")) continue;
      if (rawLine.startsWith("-")) {
        delBuf.push([oldLn, rawLine.slice(1)]);
        oldLn += 1;
      } else if (rawLine.startsWith("+")) {
        addBuf.push([newLn, rawLine.slice(1)]);
        newLn += 1;
      } else if (rawLine.startsWith(" ") || rawLine === "") {
        flushChanges();
        const text = rawLine.startsWith(" ") ? rawLine.slice(1) : "";
        const row = _recomputeRowSegments(
          { kind: "context", left_num: oldLn, right_num: newLn, left_text: text, right_text: text, in_diff_hunk: true },
          charDiffMode,
          normalize,
          ignorePunct
        );
        curHunk.rows.push(row);
        oldLn += 1;
        newLn += 1;
      }
    }
    finalizeHunk();
    return hunks;
  }

  const api = {
    VALID_DIFF_MODES,
    VALID_NORMALIZATIONS,
    applyNormalization,
    stripPunctuation,
    tokenizeForDiff,
    sequenceOpcodes,
    computeCharDiff,
    computeStreamDiff,
    buildInlineFlowRows,
    buildFullFileHunk,
    parsePatchHunks,
    isInterwordSandhiPair,
    findSandhiMarkedIndices,
    splitMixedSandhiPair,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (globalScope) {
    globalScope.DiffSanEngine = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
