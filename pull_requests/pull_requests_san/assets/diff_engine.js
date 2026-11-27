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
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (globalScope) {
    globalScope.DiffSanEngine = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
