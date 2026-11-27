"""Unified diff parsing, line alignment, and character/grapheme-level LHS/RHS diffing."""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any
import unicodedata

import regex

from pull_requests_san.models import AlignedDiffRow, DiffHunk


VALID_DIFF_MODES = ("grapheme", "codepoint", "word")
VALID_NORMALIZATIONS = ("none", "nfc", "nfd", "nfkc", "nfkd")
_PUNCT_KEY = "\x00punct\x00"

_HUNK_HEADER_RE = re.compile(
  r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<context>.*)$"
)
_DIFF_GIT_HEADER_RE = re.compile(
  r'^diff --git (?:"(?P<qa>(?:[^"\\]|\\.)+)"|(?P<ua>\S+)) (?:"(?P<qb>(?:[^"\\]|\\.)+)"|(?P<ub>\S+))$'
)
# Attach trailing Vedic accent marks (notably U+1CD3, categorized Po in Unicode) to preceding grapheme.
_GRAPHEME_TOKEN_RE = regex.compile(r"\X[\u1cd3\u0951-\u0954\u1cd0-\u1ce8\u1ced\u1cf4\u1cf8\u1cf9\u0300-\u036f]*")
# Word tokenizer: matches space-separated strings (\S+), separating horizontal whitespace from newlines.
_WORD_TOKEN_RE = regex.compile(r"[^\S\n]+|\n|\S+")


def tokenize_for_diff(text: str, mode: str = "grapheme") -> list[str]:
  """Split ``text`` into diff tokens according to ``mode`` (``grapheme``, ``codepoint``, or ``word``)."""
  if not text:
    return []
  if mode == "codepoint":
    return list(text)
  if mode == "word":
    return _WORD_TOKEN_RE.findall(text)
  return _GRAPHEME_TOKEN_RE.findall(text)


def _merge_segments(segs: list[dict[str, str]]) -> list[dict[str, str]]:
  merged: list[dict[str, str]] = []
  for seg in segs:
    txt = seg["text"]
    if not txt:
      continue
    if merged and merged[-1]["op"] == seg["op"]:
      merged[-1]["text"] += txt
    else:
      merged.append({"op": seg["op"], "text": txt})
  return merged


def _trim_common_edges(lhs_toks: list[str], rhs_toks: list[str]) -> tuple[int, int]:
  pref = 0
  min_len = min(len(lhs_toks), len(rhs_toks))
  while pref < min_len and lhs_toks[pref] == rhs_toks[pref]:
    pref += 1
  suff = 0
  while suff < (min_len - pref) and lhs_toks[-1 - suff] == rhs_toks[-1 - suff]:
    suff += 1
  return pref, suff


def apply_normalization(text: str, form: str = "none") -> str:
  """Normalize Unicode characters in ``text`` according to ``form`` (``none``, ``nfc``, ``nfd``, ``nfkc``, ``nfkd``)."""
  norm = (form or "none").strip().lower()
  if norm not in VALID_NORMALIZATIONS:
    raise ValueError(f"Unsupported normalization {form!r}; expected one of {VALID_NORMALIZATIONS}")
  if not text or norm == "none":
    return text
  return unicodedata.normalize(norm.upper(), text)


def strip_punctuation(text: str) -> str:
  """Remove Unicode punctuation characters (`P*`, e.g. danda/double-danda/quotes) for comparison."""
  if not text:
    return text
  return "".join(ch for ch in text if not unicodedata.category(ch).startswith("P"))


def _tok_compare_key(tok: str) -> str:
  if tok.strip() == "":
    return tok
  cleaned = strip_punctuation(tok)
  return cleaned if cleaned else _PUNCT_KEY


def _diff_raw_tokens(
  lhs: str,
  rhs: str,
  *,
  mode: str,
  ignore_punct: bool = False,
) -> list[tuple[str, str, str]]:
  lhs_toks = tokenize_for_diff(lhs, mode=mode)
  rhs_toks = tokenize_for_diff(rhs, mode=mode)
  lhs_keys = [_tok_compare_key(t) for t in lhs_toks] if ignore_punct else lhs_toks
  rhs_keys = [_tok_compare_key(t) for t in rhs_toks] if ignore_punct else rhs_toks
  pref, suff = _trim_common_edges(lhs_keys, rhs_keys)
  lhs_mid = lhs_toks[pref : len(lhs_toks) - suff if suff else None]
  rhs_mid = rhs_toks[pref : len(rhs_toks) - suff if suff else None]
  lhs_mid_k = lhs_keys[pref : len(lhs_keys) - suff if suff else None]
  rhs_mid_k = rhs_keys[pref : len(rhs_keys) - suff if suff else None]

  chunks: list[tuple[str, str, str]] = []
  if pref:
    chunks.append(("equal", "".join(lhs_toks[:pref]), "".join(rhs_toks[:pref])))

  if len(lhs_mid) * len(rhs_mid) > 4_000_000:
    chunks.append(("replace", "".join(lhs_mid), "".join(rhs_mid)))
  elif lhs_mid or rhs_mid:
    sm = difflib.SequenceMatcher(None, lhs_mid_k, rhs_mid_k, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
      l_part = "".join(lhs_mid[i1:i2])
      r_part = "".join(rhs_mid[j1:j2])
      chunks.append((tag, l_part, r_part))

  if suff:
    chunks.append(("equal", "".join(lhs_toks[len(lhs_toks) - suff :]), "".join(rhs_toks[len(rhs_toks) - suff :])))
  if mode == "word" and len(chunks) >= 3:
    cleaned: list[tuple[str, str, str]] = []
    idx = 0
    while idx < len(chunks):
      tag, lp, rp = chunks[idx]
      if (
        tag == "equal"
        and not lp.strip()
        and not rp.strip()
        and cleaned
        and cleaned[-1][0] != "equal"
        and idx + 1 < len(chunks)
        and chunks[idx + 1][0] != "equal"
      ):
        _, pl, pr = cleaned[-1]
        _, nl, nr = chunks[idx + 1]
        cleaned[-1] = ("replace", pl + lp + nl, pr + rp + nr)
        idx += 2
      else:
        cleaned.append((tag, lp, rp))
        idx += 1
    chunks = cleaned
  return chunks


def _iter_diff_chunks(
  lhs: str,
  rhs: str,
  *,
  mode: str = "grapheme",
  normalize: str = "none",
  ignore_punct: bool = False,
) -> list[tuple[str, str, str]]:
  if mode not in VALID_DIFF_MODES:
    raise ValueError(f"Unsupported diff mode {mode!r}; expected one of {VALID_DIFF_MODES}")
  lhs_n = apply_normalization(lhs, normalize)
  rhs_n = apply_normalization(rhs, normalize)
  lhs_cmp = strip_punctuation(lhs_n) if ignore_punct else lhs_n
  rhs_cmp = strip_punctuation(rhs_n) if ignore_punct else rhs_n
  if lhs_cmp == rhs_cmp:
    return [("equal", lhs_n, rhs_n)] if (lhs_n or rhs_n) else []
  if not lhs_n:
    return [("insert", "", rhs_n)] if rhs_n else []
  if not rhs_n:
    return [("delete", lhs_n, "")] if lhs_n else []

  if mode == "word" or max(len(lhs_n), len(rhs_n)) <= 120:
    return _diff_raw_tokens(lhs_n, rhs_n, mode=mode, ignore_punct=ignore_punct)

  # On longer text/blocks, anchor words first and refine only genuinely similar short spans to avoid character confetti.
  word_chunks = _diff_raw_tokens(lhs_n, rhs_n, mode="word", ignore_punct=ignore_punct)
  refined: list[tuple[str, str, str]] = []
  for tag, l_part, r_part in word_chunks:
    if tag != "replace":
      refined.append((tag, l_part, r_part))
      continue
    max_chunk = max(len(l_part), len(r_part))
    if max_chunk <= 400 and (max_chunk <= 32 or _line_similarity(l_part, r_part) >= 0.35):
      refined.extend(_diff_raw_tokens(l_part, r_part, mode=mode, ignore_punct=ignore_punct))
    else:
      refined.append(("replace", l_part, r_part))
  return refined


def compute_char_diff(
  lhs: str,
  rhs: str,
  mode: str = "grapheme",
  *,
  normalize: str = "none",
  ignore_punct: bool = False,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
  """Compute character/grapheme-level diff segments for an aligned ``(lhs, rhs)`` line pair."""
  lhs_segs: list[dict[str, str]] = []
  rhs_segs: list[dict[str, str]] = []
  for tag, l_part, r_part in _iter_diff_chunks(lhs, rhs, mode=mode, normalize=normalize, ignore_punct=ignore_punct):
    if tag == "equal":
      if l_part:
        lhs_segs.append({"op": "eq", "text": l_part})
      if r_part:
        rhs_segs.append({"op": "eq", "text": r_part})
    else:
      if l_part:
        lhs_segs.append({"op": "del", "text": l_part})
      if r_part:
        rhs_segs.append({"op": "ins", "text": r_part})
  return _merge_segments(lhs_segs), _merge_segments(rhs_segs)


def compute_stream_diff(
  lhs: str,
  rhs: str,
  *,
  mode: str = "grapheme",
  normalize: str = "none",
  ignore_punct: bool = False,
) -> list[dict[str, str]]:
  """Compute a single unified stream of ``eq``/``del``/``ins`` segments without line-by-line pairing."""
  segs: list[dict[str, str]] = []
  for tag, l_part, r_part in _iter_diff_chunks(lhs, rhs, mode=mode, normalize=normalize, ignore_punct=ignore_punct):
    if tag == "equal":
      out_txt = r_part or l_part
      if out_txt:
        segs.append({"op": "eq", "text": out_txt})
    else:
      if l_part:
        segs.append({"op": "del", "text": l_part})
      if r_part:
        segs.append({"op": "ins", "text": r_part})
  return _merge_segments(segs)


def _line_similarity(a: str, b: str) -> float:
  if not a.strip() and not b.strip():
    return 0.08 if a == b else 0.04
  if not a.strip() or not b.strip():
    return 0.0
  if a == b:
    return 1.0
  return difflib.SequenceMatcher(None, a[:1500], b[:1500], autojunk=False).ratio()


def _align_large_block(
  del_lines: list[tuple[int, str]],
  add_lines: list[tuple[int, str]],
) -> list[tuple[tuple[int, str] | None, tuple[int, str] | None]]:
  sm = difflib.SequenceMatcher(None, [t for _, t in del_lines], [t for _, t in add_lines], autojunk=False)
  paired: list[tuple[tuple[int, str] | None, tuple[int, str] | None]] = []
  for tag, i1, i2, j1, j2 in sm.get_opcodes():
    if tag == "equal":
      paired.extend((del_lines[di], add_lines[aj]) for di, aj in zip(range(i1, i2), range(j1, j2)))
    elif tag == "delete":
      paired.extend((del_lines[di], None) for di in range(i1, i2))
    elif tag == "insert":
      paired.extend((None, add_lines[aj]) for aj in range(j1, j2))
    else:
      for k in range(min(i2 - i1, j2 - j1)):
        dl, al = del_lines[i1 + k], add_lines[j1 + k]
        if _line_similarity(dl[1], al[1]) >= 0.25 or (not dl[1].strip() and not al[1].strip()):
          paired.append((dl, al))
        else:
          paired.append((dl, None))
          paired.append((None, al))
      common = min(i2 - i1, j2 - j1)
      paired.extend((del_lines[di], None) for di in range(i1 + common, i2))
      paired.extend((None, add_lines[aj]) for aj in range(j1 + common, j2))
  return paired


def _align_dp_block(
  del_lines: list[tuple[int, str]],
  add_lines: list[tuple[int, str]],
) -> list[tuple[tuple[int, str] | None, tuple[int, str] | None]]:
  m, n = len(del_lines), len(add_lines)
  min_pair_sim = 0.25
  scores = [[0.0] * (n + 1) for _ in range(m + 1)]
  back = [[0] * (n + 1) for _ in range(m + 1)]
  for i in range(1, m + 1):
    back[i][0] = 2
  for j in range(1, n + 1):
    back[0][j] = 3

  for i in range(1, m + 1):
    d_text = del_lines[i - 1][1]
    for j in range(1, n + 1):
      a_text = add_lines[j - 1][1]
      if not d_text.strip() and not a_text.strip():
        diag_delta = 0.08 if d_text == a_text else 0.04
      else:
        sim = _line_similarity(d_text, a_text)
        diag_delta = sim if sim >= min_pair_sim else -1.0
      diag = scores[i - 1][j - 1] + diag_delta
      up = scores[i - 1][j]
      left = scores[i][j - 1]
      if diag >= up and diag >= left and diag_delta > 0:
        scores[i][j], back[i][j] = diag, 1
      elif up >= left:
        scores[i][j], back[i][j] = up, 2
      else:
        scores[i][j], back[i][j] = left, 3

  aligned_rev: list[tuple[tuple[int, str] | None, tuple[int, str] | None]] = []
  i, j = m, n
  while i > 0 or j > 0:
    step = back[i][j]
    if i > 0 and j > 0 and step == 1:
      aligned_rev.append((del_lines[i - 1], add_lines[j - 1]))
      i -= 1
      j -= 1
    elif i > 0 and (j == 0 or step == 2):
      aligned_rev.append((del_lines[i - 1], None))
      i -= 1
    else:
      aligned_rev.append((None, add_lines[j - 1]))
      j -= 1
  aligned_rev.reverse()
  return aligned_rev


def _align_change_block_lines(
  del_lines: list[tuple[int, str]],
  add_lines: list[tuple[int, str]],
) -> list[tuple[tuple[int, str] | None, tuple[int, str] | None]]:
  """Align consecutive deleted and added lines so corresponding lines pair side-by-side."""
  m, n = len(del_lines), len(add_lines)
  if m == 0:
    return [(None, item) for item in add_lines]
  if n == 0:
    return [(item, None) for item in del_lines]
  if m == 1 and n == 1:
    d_txt, a_txt = del_lines[0][1], add_lines[0][1]
    if (not d_txt.strip() and not a_txt.strip()) or _line_similarity(d_txt, a_txt) >= 0.22 or max(len(d_txt), len(a_txt)) <= 24:
      return [(del_lines[0], add_lines[0])]
    return [(del_lines[0], None), (None, add_lines[0])]
  if m * n > 2500:
    return _align_large_block(del_lines, add_lines)
  return _align_dp_block(del_lines, add_lines)


def _build_rows_from_pairs(
  pairs: list[tuple[tuple[int, str] | None, tuple[int, str] | None]],
  *,
  char_diff_mode: str = "grapheme",
  in_diff_hunk: bool = True,
  normalize: str = "none",
  ignore_punct: bool = False,
) -> list[AlignedDiffRow]:
  rows: list[AlignedDiffRow] = []
  for left, right in pairs:
    if left is not None and right is not None:
      lnum, ltxt = left
      rnum, rtxt = right
      row = AlignedDiffRow(
        kind="context" if ltxt == rtxt else "modify",
        left_num=lnum,
        right_num=rnum,
        left_text=ltxt,
        right_text=rtxt,
        in_diff_hunk=in_diff_hunk,
      )
    elif left is not None:
      row = AlignedDiffRow(
        kind="del",
        left_num=left[0],
        right_num=None,
        left_text=left[1],
        right_text=None,
        in_diff_hunk=in_diff_hunk,
      )
    else:
      assert right is not None
      row = AlignedDiffRow(
        kind="add",
        left_num=None,
        right_num=right[0],
        left_text=None,
        right_text=right[1],
        in_diff_hunk=in_diff_hunk,
      )
    row.recompute_segments(mode=char_diff_mode, normalize=normalize, ignore_punct=ignore_punct)
    rows.append(row)
  return rows


def _has_eq_before_next_nl(segments: list[dict[str, str]], seg_idx: int, cur_parts: list[str], part_idx: int) -> bool:
  # Check if remaining content on this line inside current op already has a newline (then any eq is on a later line).
  if part_idx + 1 < len(cur_parts) - 1:
    return False
  for j in range(seg_idx + 1, len(segments)):
    s = segments[j]
    txt = s["text"]
    if "\n" in txt:
      return s["op"] == "eq" and bool(txt.split("\n", 1)[0])
    if s["op"] == "eq" and txt:
      return True
  return False


def _stream_segments_to_inline_rows(
  segments: list[dict[str, str]],
  left_entries: list[tuple[int | None, str]],
  right_entries: list[tuple[int | None, str]],
) -> list[dict[str, Any]]:
  rows: list[dict[str, Any]] = []
  l_idx = 0
  r_idx = 0
  cur_segs: list[dict[str, str]] = []
  cur_l_num: int | None = None
  cur_r_num: int | None = None
  used_l = False
  used_r = False
  has_eq = False
  has_del = False
  has_ins = False

  def touch_left() -> None:
    nonlocal used_l, cur_l_num
    used_l = True
    if cur_l_num is None and l_idx < len(left_entries):
      cur_l_num = left_entries[l_idx][0]

  def touch_right() -> None:
    nonlocal used_r, cur_r_num
    used_r = True
    if cur_r_num is None and r_idx < len(right_entries):
      cur_r_num = right_entries[r_idx][0]

  def flush_row() -> None:
    nonlocal cur_segs, cur_l_num, cur_r_num, used_l, used_r, has_eq, has_del, has_ins
    if has_del or has_ins:
      kind = "modify" if (has_eq or (used_l and used_r)) else ("del" if has_del else "add")
    else:
      kind = "context"
    rows.append(
      {
        "kind": kind,
        "left_num": cur_l_num,
        "right_num": cur_r_num,
        "in_diff_hunk": True,
        "segments": _merge_segments(cur_segs),
      }
    )
    cur_segs = []
    cur_l_num = None
    cur_r_num = None
    used_l = False
    used_r = False
    has_eq = False
    has_del = False
    has_ins = False

  for si, seg in enumerate(segments):
    op = seg["op"]
    parts = seg["text"].split("\n")
    for pi, part in enumerate(parts):
      if part:
        if op == "eq":
          touch_left()
          touch_right()
          has_eq = True
        elif op == "del":
          touch_left()
          has_del = True
        else:
          touch_right()
          has_ins = True
        cur_segs.append({"op": op, "text": part})

      if pi < len(parts) - 1:
        if op == "eq":
          touch_left()
          touch_right()
          flush_row()
          if l_idx < len(left_entries):
            l_idx += 1
          if r_idx < len(right_entries):
            r_idx += 1
        elif op == "del":
          touch_left()
          has_del = True
          if (has_eq or has_ins) and _has_eq_before_next_nl(segments, si, parts, pi):
            cur_segs.append({"op": "del_nl", "text": "↵"})
          else:
            flush_row()
          if l_idx < len(left_entries):
            l_idx += 1
        else:
          touch_right()
          has_ins = True
          if (has_eq or has_del) and _has_eq_before_next_nl(segments, si, parts, pi):
            cur_segs.append({"op": "ins_nl", "text": "↵"})
          else:
            flush_row()
          if r_idx < len(right_entries):
            r_idx += 1

  if cur_segs or used_l or used_r:
    flush_row()
  return rows


def _aligned_row_to_inline_dict(r: AlignedDiffRow) -> dict[str, Any]:
  if r.kind == "context":
    txt = r.left_text or ""
    segs = [{"op": "eq", "text": txt}] if txt else []
  elif r.kind == "del":
    txt = r.left_text or ""
    segs = [{"op": "del", "text": txt}] if txt else []
  elif r.kind == "add":
    txt = r.right_text or ""
    segs = [{"op": "ins", "text": txt}] if txt else []
  else:
    segs = _combine_aligned_pair_segments(r)
  return {
    "kind": r.kind,
    "left_num": r.left_num,
    "right_num": r.right_num,
    "in_diff_hunk": r.in_diff_hunk,
    "segments": segs,
  }


def _combine_aligned_pair_segments(r: AlignedDiffRow) -> list[dict[str, str]]:
  """Interleave deletions and insertions for a 1-to-1 matched ``modify`` row along its ``eq`` anchors."""
  lhs = list(r.lhs_segments or [])
  rhs = list(r.rhs_segments or [])
  out: list[dict[str, str]] = []
  li = 0
  ri = 0
  while li < len(lhs) or ri < len(rhs):
    if li < len(lhs) and lhs[li]["op"] != "eq":
      out.append(dict(lhs[li]))
      li += 1
      continue
    if ri < len(rhs) and rhs[ri]["op"] != "eq":
      out.append(dict(rhs[ri]))
      ri += 1
      continue
    if li < len(lhs) and ri < len(rhs):
      l_txt = lhs[li]["text"]
      r_txt = rhs[ri]["text"]
      if l_txt == r_txt:
        out.append({"op": "eq", "text": l_txt})
        li += 1
        ri += 1
      elif len(l_txt) <= len(r_txt) and r_txt.startswith(l_txt):
        out.append({"op": "eq", "text": l_txt})
        rhs[ri] = {"op": "eq", "text": r_txt[len(l_txt) :]}
        li += 1
      elif len(r_txt) < len(l_txt) and l_txt.startswith(r_txt):
        out.append({"op": "eq", "text": r_txt})
        lhs[li] = {"op": "eq", "text": l_txt[len(r_txt) :]}
        ri += 1
      else:
        out.append({"op": "del", "text": l_txt})
        out.append({"op": "ins", "text": r_txt})
        li += 1
        ri += 1
    elif li < len(lhs):
      out.append(dict(lhs[li]))
      li += 1
    else:
      out.append(dict(rhs[ri]))
      ri += 1
  return _merge_segments(out)


def _split_outer_ws(text: str) -> tuple[str, str, str]:
  m = re.match(r"^([ \t]*)(.*?)([ \t]*)$", text, re.S)
  if not m:
    return "", text, ""
  return m.group(1), m.group(2), m.group(3)


def _annotate_pair_sub_segments(
  del_seg: dict[str, Any],
  ins_seg: dict[str, Any],
  *,
  normalize: str,
  ignore_punct: bool,
) -> bool:
  l_lead, l_core, l_trail = _split_outer_ws(del_seg.get("text") or "")
  r_lead, r_core, r_trail = _split_outer_ws(ins_seg.get("text") or "")
  if not l_core or not r_core:
    return False
  l_sub, r_sub = compute_char_diff(
    l_core,
    r_core,
    mode="grapheme",
    normalize=normalize,
    ignore_punct=ignore_punct,
  )
  if not any(s.get("op") == "eq" and (s.get("text") or "").strip() for s in l_sub):
    return False
  full_l = (
    ([{"op": "eq", "text": l_lead}] if l_lead else [])
    + l_sub
    + ([{"op": "eq", "text": l_trail}] if l_trail else [])
  )
  full_r = (
    ([{"op": "eq", "text": r_lead}] if r_lead else [])
    + r_sub
    + ([{"op": "eq", "text": r_trail}] if r_trail else [])
  )
  del_seg["sub_segments"] = _merge_segments(full_l)
  ins_seg["sub_segments"] = _merge_segments(full_r)
  return True


def _annotate_word_sub_segments(
  flow_rows: list[dict[str, Any]],
  *,
  normalize: str = "none",
  ignore_punct: bool = False,
) -> None:
  """Attach intra-word grapheme ``sub_segments`` to paired del/ins spans in word diff mode."""
  flat: list[dict[str, Any] | None] = []
  for fr in flow_rows:
    if fr.get("kind") == "context":
      flat.append(None)
      continue
    for seg in fr.get("segments") or []:
      if seg.get("op") in ("del_nl", "ins_nl"):
        continue
      flat.append(seg)

  i = 0
  n = len(flat)
  while i < n:
    s = flat[i]
    if s is None or s.get("op") != "del":
      i += 1
      continue
    d_start = i
    while i < n and flat[i] is not None and flat[i].get("op") == "del":
      i += 1
    d_run = [flat[k] for k in range(d_start, i) if flat[k] is not None]
    a_start = i
    while i < n and flat[i] is not None and flat[i].get("op") == "ins":
      i += 1
    a_run = [flat[k] for k in range(a_start, i) if flat[k] is not None]
    if not d_run or not a_run:
      continue
    if len(d_run) == len(a_run):
      for ds, iseg in zip(d_run, a_run):
        _annotate_pair_sub_segments(ds, iseg, normalize=normalize, ignore_punct=ignore_punct)
    else:
      used_ins: set[int] = set()
      for ds in d_run:
        best_idx = -1
        best_sim = 0.25
        for idx, iseg in enumerate(a_run):
          if idx in used_ins:
            continue
          sim = _line_similarity(ds.get("text") or "", iseg.get("text") or "")
          if sim >= best_sim:
            best_sim = sim
            best_idx = idx
        if best_idx >= 0:
          used_ins.add(best_idx)
          _annotate_pair_sub_segments(ds, a_run[best_idx], normalize=normalize, ignore_punct=ignore_punct)


def build_inline_flow_rows(
  rows: list[AlignedDiffRow],
  *,
  char_diff_mode: str = "grapheme",
  normalize: str = "none",
  ignore_punct: bool = False,
) -> list[dict[str, Any]]:
  """Build single-pane inline word/character-diff rows without forcing line pairing on reflowed blocks."""
  out: list[dict[str, Any]] = []
  n = len(rows)
  i = 0

  while i < n:
    r = rows[i]
    if r.kind == "context":
      out.append(_aligned_row_to_inline_dict(r))
      i += 1
      continue

    j = i
    while j < n and rows[j].kind != "context":
      j += 1
    island = rows[i:j]
    i = j

    left_entries = [(cr.left_num, cr.left_text) for cr in island if cr.left_text is not None]
    right_entries = [(cr.right_num, cr.right_text) for cr in island if cr.right_text is not None]
    if left_entries and not right_entries:
      out.extend(_aligned_row_to_inline_dict(cr) for cr in island)
      continue
    if right_entries and not left_entries:
      out.extend(_aligned_row_to_inline_dict(cr) for cr in island)
      continue

    old_block = "\n".join(txt for _, txt in left_entries)
    new_block = "\n".join(txt for _, txt in right_entries)
    segs = compute_stream_diff(
      old_block,
      new_block,
      mode=char_diff_mode,
      normalize=normalize,
      ignore_punct=ignore_punct,
    )
    flow_rows = _stream_segments_to_inline_rows(segs, left_entries, right_entries)
    any_in_hunk = any(cr.in_diff_hunk for cr in island)
    for fr in flow_rows:
      fr["in_diff_hunk"] = any_in_hunk
    out.extend(flow_rows)
  if char_diff_mode == "word":
    _annotate_word_sub_segments(out, normalize=normalize, ignore_punct=ignore_punct)
  return out


def _realign_rows_across_blanks(
  raw_rows: list[AlignedDiffRow],
  *,
  char_diff_mode: str = "grapheme",
  normalize: str = "none",
  ignore_punct: bool = False,
) -> list[AlignedDiffRow]:
  """Re-align side-by-side rows across intervening blank context lines between edits."""
  out: list[AlignedDiffRow] = []
  n = len(raw_rows)
  i = 0
  while i < n:
    r = raw_rows[i]
    if r.kind == "context" and (r.left_text or "").strip() != "":
      out.append(r)
      i += 1
      continue

    j = i
    while j < n and not (raw_rows[j].kind == "context" and (raw_rows[j].left_text or "").strip() != ""):
      j += 1
    island = raw_rows[i:j]
    i = j

    lead = 0
    while lead < len(island) and island[lead].kind == "context":
      out.append(island[lead])
      lead += 1
    trail = len(island)
    while trail > lead and island[trail - 1].kind == "context":
      trail -= 1
    core = island[lead:trail]

    if core:
      old_lines = [(cr.left_num, cr.left_text) for cr in core if cr.left_text is not None and cr.left_num is not None]
      new_lines = [(cr.right_num, cr.right_text) for cr in core if cr.right_text is not None and cr.right_num is not None]
      old_sorted = sorted(old_lines, key=lambda x: x[0])
      new_sorted = sorted(new_lines, key=lambda x: x[0])
      pairs = _align_change_block_lines(old_sorted, new_sorted)
      any_in_hunk = any(cr.in_diff_hunk for cr in core)
      out.extend(
        _build_rows_from_pairs(
          pairs,
          char_diff_mode=char_diff_mode,
          in_diff_hunk=any_in_hunk,
          normalize=normalize,
          ignore_punct=ignore_punct,
        )
      )

    out.extend(island[trail:])
  return out


def parse_patch_hunks(
  patch: str,
  *,
  char_diff_mode: str = "grapheme",
  normalize: str = "none",
  ignore_punct: bool = False,
) -> list[DiffHunk]:
  """Parse a unified diff ``patch`` string into ``DiffHunk`` objects with character-level diffs."""
  if not patch:
    return []
  hunks: list[DiffHunk] = []
  cur_hunk: DiffHunk | None = None
  old_ln = 0
  new_ln = 0
  del_buf: list[tuple[int, str]] = []
  add_buf: list[tuple[int, str]] = []

  def flush_changes() -> None:
    nonlocal del_buf, add_buf
    if cur_hunk is not None and (del_buf or add_buf):
      pairs = _align_change_block_lines(del_buf, add_buf)
      cur_hunk.rows.extend(
        _build_rows_from_pairs(
          pairs,
          char_diff_mode=char_diff_mode,
          in_diff_hunk=True,
          normalize=normalize,
          ignore_punct=ignore_punct,
        )
      )
    del_buf, add_buf = [], []

  def finalize_hunk() -> None:
    flush_changes()
    if cur_hunk is not None and cur_hunk.rows:
      cur_hunk.rows = _realign_rows_across_blanks(
        cur_hunk.rows,
        char_diff_mode=char_diff_mode,
        normalize=normalize,
        ignore_punct=ignore_punct,
      )
      cur_hunk.inline_rows = build_inline_flow_rows(
        cur_hunk.rows,
        char_diff_mode=char_diff_mode,
        normalize=normalize,
        ignore_punct=ignore_punct,
      )

  for raw_line in patch.splitlines():
    m = _HUNK_HEADER_RE.match(raw_line)
    if m:
      finalize_hunk()
      old_ln = int(m.group("old_start"))
      old_cnt = int(m.group("old_count")) if m.group("old_count") is not None else 1
      new_ln = int(m.group("new_start"))
      new_cnt = int(m.group("new_count")) if m.group("new_count") is not None else 1
      cur_hunk = DiffHunk(
        header=raw_line,
        old_start=old_ln,
        old_count=old_cnt,
        new_start=new_ln,
        new_count=new_cnt,
        context=(m.group("context") or "").strip(),
      )
      hunks.append(cur_hunk)
      continue
    if cur_hunk is None or raw_line.startswith("\\"):
      continue
    if raw_line.startswith("-"):
      del_buf.append((old_ln, raw_line[1:]))
      old_ln += 1
    elif raw_line.startswith("+"):
      add_buf.append((new_ln, raw_line[1:]))
      new_ln += 1
    elif raw_line.startswith(" ") or raw_line == "":
      flush_changes()
      text = raw_line[1:] if raw_line.startswith(" ") else ""
      row = AlignedDiffRow(kind="context", left_num=old_ln, right_num=new_ln, left_text=text, right_text=text)
      row.recompute_segments(mode=char_diff_mode, normalize=normalize, ignore_punct=ignore_punct)
      cur_hunk.rows.append(row)
      old_ln += 1
      new_ln += 1

  finalize_hunk()
  return hunks


def _unescape_git_path(path: str) -> str:
  """Decode C-style octal/escaped git diff paths such as ``\\340\\244\\205``."""
  if "\\" not in path:
    return path
  buf = bytearray()
  i = 0
  escapes = {"n": 10, "r": 13, "t": 9, "\\": 92, '"': 34, "a": 7, "b": 8, "f": 12, "v": 11}
  while i < len(path):
    ch = path[i]
    if ch == "\\" and i + 1 < len(path):
      nxt = path[i + 1]
      if nxt in escapes:
        buf.append(escapes[nxt])
        i += 2
        continue
      if i + 3 < len(path) and all("0" <= c <= "7" for c in path[i + 1 : i + 4]):
        buf.append(int(path[i + 1 : i + 4], 8))
        i += 4
        continue
    buf.extend(ch.encode("utf-8"))
    i += 1
  return buf.decode("utf-8", errors="replace")


def _clean_diff_header_path(raw: str, prefix: str = "") -> str:
  val = raw.strip()
  if val.startswith('"') and val.endswith('"') and len(val) >= 2:
    val = _unescape_git_path(val[1:-1])
  else:
    val = _unescape_git_path(val)
  if prefix and val.startswith(prefix):
    val = val[len(prefix) :]
  return val


def parse_full_diff_by_file(diff_text: str) -> dict[str, str]:
  """Split a full git unified diff into ``{path: patch_text}`` for each changed file."""
  patches: dict[str, str] = {}
  if not diff_text:
    return patches

  current_paths: tuple[str, str] | None = None
  collecting_hunks = False
  buf: list[str] = []

  def flush_file() -> None:
    if current_paths and buf:
      patch_str = "\n".join(buf)
      a_path, b_path = current_paths
      if b_path:
        patches[b_path] = patch_str
      if a_path and a_path != b_path:
        patches[a_path] = patch_str

  for line in diff_text.splitlines():
    if line.startswith("diff --git "):
      flush_file()
      m = _DIFF_GIT_HEADER_RE.match(line)
      if m:
        a_path = _clean_diff_header_path(m.group("qa") or m.group("ua") or "", "a/")
        b_path = _clean_diff_header_path(m.group("qb") or m.group("ub") or "", "b/")
        current_paths = (a_path, b_path)
      else:
        current_paths = ("", "")
      collecting_hunks = False
      buf = []
      continue

    if current_paths is None:
      continue
    if not collecting_hunks:
      if line.startswith("rename from "):
        current_paths = (_clean_diff_header_path(line[len("rename from ") :]), current_paths[1])
      elif line.startswith("rename to "):
        current_paths = (current_paths[0], _clean_diff_header_path(line[len("rename to ") :]))
      elif line.startswith("--- ") and line != "--- /dev/null":
        current_paths = (_clean_diff_header_path(line[4:], "a/"), current_paths[1])
      elif line.startswith("+++ ") and line != "+++ /dev/null":
        current_paths = (current_paths[0], _clean_diff_header_path(line[4:], "b/"))
    if line.startswith("@@ "):
      collecting_hunks = True
    if collecting_hunks:
      buf.append(line)

  flush_file()
  return patches


def build_unified_patch_from_texts(
  base_text: str | None,
  head_text: str | None,
  filename: str,
  *,
  previous_filename: str | None = None,
  context_lines: int = 3,
) -> str:
  """Generate a unified diff patch string from base and head file contents."""
  base_lines = (base_text or "").splitlines(keepends=True) if base_text is not None else []
  head_lines = (head_text or "").splitlines(keepends=True) if head_text is not None else []
  diff_lines = list(
    difflib.unified_diff(
      base_lines,
      head_lines,
      fromfile=f"a/{previous_filename or filename}",
      tofile=f"b/{filename}",
      n=context_lines,
    )
  )
  return "\n".join(ln.rstrip("\r\n") for ln in diff_lines[2:])


def build_full_file_hunk(
  base_text: str | None,
  head_text: str | None,
  *,
  existing_patch: str | None = None,
  char_diff_mode: str = "grapheme",
  normalize: str = "none",
  ignore_punct: bool = False,
) -> DiffHunk:
  """Build a single full-file aligned ``DiffHunk`` with ``in_diff_hunk`` markers preserved."""
  hunk_left: set[int] = set()
  hunk_right: set[int] = set()
  if existing_patch:
    for h in parse_patch_hunks(existing_patch, char_diff_mode=char_diff_mode, normalize=normalize, ignore_punct=ignore_punct):
      hunk_left.update(r.left_num for r in h.rows if r.left_num is not None)
      hunk_right.update(r.right_num for r in h.rows if r.right_num is not None)

  base_lines = base_text.splitlines() if base_text is not None else []
  head_lines = head_text.splitlines() if head_text is not None else []
  rows: list[AlignedDiffRow] = []
  sm = difflib.SequenceMatcher(None, base_lines, head_lines, autojunk=False)
  for tag, i1, i2, j1, j2 in sm.get_opcodes():
    if tag == "equal":
      for di, aj in zip(range(i1, i2), range(j1, j2)):
        lnum, rnum = di + 1, aj + 1
        in_hunk = (not existing_patch) or (lnum in hunk_left) or (rnum in hunk_right)
        row = AlignedDiffRow(
          kind="context",
          left_num=lnum,
          right_num=rnum,
          left_text=base_lines[di],
          right_text=head_lines[aj],
          in_diff_hunk=in_hunk,
        )
        row.recompute_segments(mode=char_diff_mode, normalize=normalize, ignore_punct=ignore_punct)
        rows.append(row)
    elif tag == "delete":
      rows.extend(
        _build_rows_from_pairs(
          [((di + 1, base_lines[di]), None) for di in range(i1, i2)],
          char_diff_mode=char_diff_mode,
          normalize=normalize,
          ignore_punct=ignore_punct,
        )
      )
    elif tag == "insert":
      rows.extend(
        _build_rows_from_pairs(
          [(None, (aj + 1, head_lines[aj])) for aj in range(j1, j2)],
          char_diff_mode=char_diff_mode,
          normalize=normalize,
          ignore_punct=ignore_punct,
        )
      )
    else:
      pairs = _align_change_block_lines(
        [(di + 1, base_lines[di]) for di in range(i1, i2)],
        [(aj + 1, head_lines[aj]) for aj in range(j1, j2)],
      )
      rows.extend(
        _build_rows_from_pairs(
          pairs,
          char_diff_mode=char_diff_mode,
          normalize=normalize,
          ignore_punct=ignore_punct,
        )
      )

  rows = _realign_rows_across_blanks(
    rows,
    char_diff_mode=char_diff_mode,
    normalize=normalize,
    ignore_punct=ignore_punct,
  )
  return DiffHunk(
    header=f"@@ -1,{len(base_lines)} +1,{len(head_lines)} @@ (Full File)",
    old_start=1 if base_lines else 0,
    old_count=len(base_lines),
    new_start=1 if head_lines else 0,
    new_count=len(head_lines),
    context="Full File",
    rows=rows,
    inline_rows=build_inline_flow_rows(
      rows,
      char_diff_mode=char_diff_mode,
      normalize=normalize,
      ignore_punct=ignore_punct,
    ),
  )


_CONFLICT_HEAD_LABEL = "__DOC_CURATION_PR_HEAD__"
_CONFLICT_ANCESTOR_LABEL = "__DOC_CURATION_MERGE_BASE__"
_CONFLICT_BASE_LABEL = "__DOC_CURATION_BASE_BRANCH__"


def _make_conflict_hunk(
  base_lines: list[str],
  head_lines: list[str],
  ancestor_lines: list[str],
  *,
  char_diff_mode: str,
) -> dict[str, Any]:
  pairs = _align_change_block_lines(
    [(idx + 1, ln) for idx, ln in enumerate(base_lines)],
    [(idx + 1, ln) for idx, ln in enumerate(head_lines)],
  )
  rows = _build_rows_from_pairs(pairs, char_diff_mode=char_diff_mode, in_diff_hunk=True)
  return {
    "base_text": "\n".join(base_lines),
    "head_text": "\n".join(head_lines),
    "ancestor_text": "\n".join(ancestor_lines),
    "rows": [r.to_dict() for r in rows],
    "inline_rows": build_inline_flow_rows(rows, char_diff_mode=char_diff_mode),
  }


def extract_three_way_conflicts(
  base_tip_text: str,
  ancestor_text: str,
  head_text: str,
  *,
  char_diff_mode: str = "grapheme",
  max_hunks: int = 8,
) -> list[dict[str, Any]]:
  """Extract structured 3-way conflict hunks between ``base_tip_text`` (LHS) and ``head_text`` (RHS)."""
  if base_tip_text == head_text:
    return []
  git_bin = shutil.which("git")
  if not git_bin:
    return []
  with tempfile.TemporaryDirectory() as tmpdir:
    head_path = os.path.join(tmpdir, "head.txt")
    base_path = os.path.join(tmpdir, "ancestor.txt")
    tip_path = os.path.join(tmpdir, "base_tip.txt")
    for p, content in ((head_path, head_text), (base_path, ancestor_text), (tip_path, base_tip_text)):
      with open(p, "w", encoding="utf-8", errors="replace") as fh:
        fh.write(content)
    proc = subprocess.run(
      [
        git_bin,
        "merge-file",
        "-p",
        "--diff3",
        "-L",
        _CONFLICT_HEAD_LABEL,
        "-L",
        _CONFLICT_ANCESTOR_LABEL,
        "-L",
        _CONFLICT_BASE_LABEL,
        head_path,
        base_path,
        tip_path,
      ],
      capture_output=True,
      text=True,
      encoding="utf-8",
      errors="replace",
      check=False,
    )
  if proc.returncode == 0 or proc.returncode < 0 or proc.returncode > 127:
    return []

  hunks: list[dict[str, Any]] = []
  state = "normal"
  head_buf: list[str] = []
  anc_buf: list[str] = []
  base_buf: list[str] = []

  head_marker = f"<<<<<<< {_CONFLICT_HEAD_LABEL}"
  anc_marker = f"||||||| {_CONFLICT_ANCESTOR_LABEL}"
  base_marker = f">>>>>>> {_CONFLICT_BASE_LABEL}"

  for line in proc.stdout.splitlines():
    if state == "normal":
      if line == head_marker:
        state = "head"
        head_buf, anc_buf, base_buf = [], [], []
    elif state == "head":
      if line == anc_marker:
        state = "anc"
      elif line == "=======":
        state = "base"
      else:
        head_buf.append(line)
    elif state == "anc":
      if line == "=======":
        state = "base"
      else:
        anc_buf.append(line)
    elif state == "base":
      if line == base_marker:
        hunks.append(_make_conflict_hunk(base_buf, head_buf, anc_buf, char_diff_mode=char_diff_mode))
        state = "normal"
        if len(hunks) >= max_hunks:
          break
      else:
        base_buf.append(line)
  return hunks
