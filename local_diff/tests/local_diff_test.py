"""Tests for local-diff-san (2-file diff, Git HEAD diff, FastAPI server, and shared UI)."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest

from local_diff_san import core, server


def _git(repo: Path, *args: str) -> None:
  subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_git_repo(tmp_path: Path) -> Path:
  repo = tmp_path / "repo"
  repo.mkdir()
  _git(repo, "init")
  _git(repo, "config", "user.name", "Tester")
  _git(repo, "config", "user.email", "tester@example.com")
  return repo


def test_compare_two_files_word_intraword_identical_and_binary(tmp_path: Path):
  left = tmp_path / "left.md"
  right = tmp_path / "right.md"
  left.write_text("अग्निमीळे पा० पुरोहितम् ।\nसमाप्तम्\n", encoding="utf-8")
  right.write_text("अग्निमीळे पा॰ पुरोहितम् ॥\nसमाप्तम्\n", encoding="utf-8")

  item = core.compare_two_files(left, right, char_diff_mode="word")
  assert item.mode == "file_pair"
  assert item.status == "modified"
  assert item.left_path == str(left.resolve())
  assert item.right_path == str(right.resolve())
  assert item.additions == 1 and item.deletions == 1
  assert len(item.hunks) == 1
  inline_segs = item.hunks[0].inline_rows[0]["segments"]
  del_seg = next(s for s in inline_segs if s["op"] == "del" and "पा" in s["text"])
  ins_seg = next(s for s in inline_segs if s["op"] == "ins" and "पा" in s["text"])
  assert {"op": "del", "text": "०"} in del_seg["sub_segments"]
  assert {"op": "ins", "text": "॰"} in ins_seg["sub_segments"]

  # Punctuation-only differences collapse when ignore_punct=True
  p1 = tmp_path / "p1.md"
  p2 = tmp_path / "p2.md"
  p1.write_text("राम।\n", encoding="utf-8")
  p2.write_text("राम॥\n", encoding="utf-8")
  h_ip = core.compare_two_files(p1, p2, char_diff_mode="word", ignore_punct=True).hunks[0]
  assert h_ip.rows[0].kind == "context"

  # Identical files show clean context with unchanged status
  same = tmp_path / "same.md"
  same.write_text("समानम्\n", encoding="utf-8")
  unch = core.compare_two_files(same, same)
  assert unch.status == "unchanged"
  assert unch.additions == 0 and unch.deletions == 0
  assert unch.hunks[0].rows[0].kind == "context"

  # Binary files are flagged without crashing UTF-8 decoding
  b1 = tmp_path / "a.bin"
  b2 = tmp_path / "b.bin"
  b1.write_bytes(b"\x00\x01\x02")
  b2.write_bytes(b"\x00\x03\x04")
  bin_item = core.compare_two_files(b1, b2)
  assert bin_item.is_binary is True

  with pytest.raises(FileNotFoundError):
    core.compare_two_files(tmp_path / "missing.md", right)


def test_compare_git_head_directory_discovery_and_rev(tmp_path: Path):
  repo = _init_git_repo(tmp_path)
  f_deva = repo / "sub" / "ऋक् 01.md"
  f_deva.parent.mkdir(parents=True)
  f_deva.write_text("इषे᳓ त्वा ।\n", encoding="utf-8")
  f_del = repo / "deleted.md"
  f_del.write_text("लुप्तम्\n", encoding="utf-8")
  _git(repo, "add", ".")
  _git(repo, "commit", "-m", "v1")

  f_deva.write_text("इषे᳓ त्वा ऊर्जे᳓ त्वा ।\n", encoding="utf-8")
  _git(repo, "add", str(f_deva))
  _git(repo, "commit", "-m", "v2")

  # Modify tracked file on disk, add untracked file, delete tracked file
  f_deva.write_text("इषे᳓ त्वा ऊर्जे᳓ पा॰ ।\n", encoding="utf-8")
  f_untracked = repo / "new_untracked.md"
  f_untracked.write_text("नवीनम्\n", encoding="utf-8")
  f_del.unlink()

  # Single file vs HEAD
  head_item = core.compare_file_with_head(f_deva, rev="HEAD", char_diff_mode="word")
  assert head_item.status == "modified"
  assert head_item.left_label == "HEAD:sub/ऋक् 01.md"
  assert head_item.right_label == "WORKING:sub/ऋक् 01.md"
  assert head_item.left_path is None
  assert head_item.right_path == str(f_deva.resolve())
  assert any(s["op"] == "ins" and "पा॰" in s["text"] for s in head_item.hunks[0].inline_rows[0]["segments"])

  # Compare vs earlier revision HEAD~1
  rev1_item = core.compare_file_with_head(f_deva, rev="HEAD~1", char_diff_mode="word")
  assert any(s["op"] == "ins" and "ऊर्जे᳓" in s["text"] for s in rev1_item.hunks[0].inline_rows[0]["segments"])

  # Deleted file on disk still compares cleanly vs HEAD
  del_item = core.compare_file_with_head(f_del)
  assert del_item.status == "deleted"
  assert del_item.deletions == 1 and del_item.additions == 0

  # Untracked file compares against empty base cleanly
  ut_item = core.compare_file_with_head(f_untracked)
  assert ut_item.status == "untracked"
  assert ut_item.additions == 1 and ut_item.deletions == 0

  # Directory workspace discovers modified, deleted, and untracked files
  spec_dir = core.resolve_spec_from_paths([str(repo)])
  assert spec_dir.mode == "git_dir"
  ws = core.build_diff_workspace(spec_dir, active_id=f"head:sub/ऋक् 01.md")
  titles = {it["title"] for it in ws["items"]}
  assert titles == {"sub/ऋक् 01.md", "deleted.md", "new_untracked.md"}
  assert ws["active_item"]["title"] == "sub/ऋक् 01.md"
  assert ws["active_item"]["hunks"]

  # 2-file CLI arg resolution vs --head override
  spec_pair = core.resolve_spec_from_paths([str(f_deva), str(f_untracked)])
  assert spec_pair.mode == "file_pair"
  spec_force = core.resolve_spec_from_paths([str(f_deva), str(f_untracked)], force_head=True)
  assert spec_force.mode == "git_head" and len(spec_force.paths) == 2

  # File outside any git repo raises ValueError cleanly
  outside = tmp_path / "outside.md"
  outside.write_text("x", encoding="utf-8")
  with pytest.raises(ValueError, match="Not inside a git repository"):
    core.compare_file_with_head(outside)


def test_local_diff_fastapi_endpoints_and_cli_errors(tmp_path: Path, monkeypatch):
  repo = _init_git_repo(tmp_path)
  f1 = repo / "doc.md"
  f1.write_text("आदिः पा०\n", encoding="utf-8")
  _git(repo, "add", "doc.md")
  _git(repo, "commit", "-m", "init")
  f1.write_text("आदिः पा॰\n", encoding="utf-8")

  f2 = tmp_path / "other.md"
  f2.write_text("द्वितीयम् पा॰\n", encoding="utf-8")

  app = server.create_app(initial_spec=core.resolve_spec_from_paths([str(f1)]))
  tc = TestClient(app)

  assert tc.get("/").status_code == 200
  meta = tc.get("/api/meta").json()
  assert meta["spec"]["mode"] == "git_head"
  assert "word" in meta["diff_modes"]

  diff_res = tc.get("/api/diff?diff_mode=word").json()
  assert diff_res["active_item"]["status"] == "modified"
  assert diff_res["active_item"]["right_path"] == str(f1.resolve())

  # Dynamically switch server workspace to 2-file mode via /api/compare
  cmp_res = tc.post(
    "/api/compare",
    json={"mode": "file_pair", "left_path": str(f1), "right_path": str(f2), "diff_mode": "grapheme"},
  )
  assert cmp_res.status_code == 200
  body = cmp_res.json()
  assert body["spec"]["mode"] == "file_pair"
  assert body["active_item"]["left_path"] == str(f1.resolve())
  assert body["active_item"]["right_path"] == str(f2.resolve())

  # IDEA and GoldenDict desktop helpers reuse pull_requests_san.local_helpers
  launched: list[list[str]] = []
  with monkeypatch.context() as mp:
    mp.setattr(server, "_resolve_idea_binary", lambda: "/usr/bin/idea-mock")
    mp.setattr(server, "_resolve_goldendict_binary", lambda: "/usr/bin/gd-mock")
    mp.setattr(server.subprocess, "Popen", lambda cmd, **kw: launched.append(list(cmd)))

    r_idea = tc.post("/api/open_in_idea", json={"path": str(f1), "line": 3})
    assert r_idea.status_code == 200
    assert launched[-1] == ["/usr/bin/idea-mock", "--line", "3", str(f1.resolve())]

    r_gd = tc.post("/api/open_in_goldendict", json={"word": "सुपेश᳓सा"})
    assert r_gd.status_code == 200
    assert launched[-1] == ["/usr/bin/gd-mock", "सुपेशसा"]

  # CLI validates missing paths before launching uvicorn
  assert server.main([str(tmp_path / "does_not_exist.md"), "--no-browser"]) == 2


def test_local_diff_frontend_js_renders_settings_and_diff(tmp_path: Path):
  if not shutil.which("node"):
    pytest.skip("node binary not installed")
  html_src = server.load_html_template()
  assert "--red-char: #ffeded;" in html_src
  assert "--del-char-sub: #ffd6db;" in html_src
  assert 'id="settingsBtn"' in html_src and 'id="settingsPane"' in html_src

  m = re.search(r"<script>\n(.*?)\n</script>", html_src, re.S)
  assert m is not None
  js = m.group(1)

  sample_ws = {
    "spec": {"mode": "file_pair", "left_path": "/tmp/a.md", "right_path": "/tmp/b.md", "paths": [], "rev": "HEAD"},
    "repo_root": None,
    "items": [
      {
        "id": "pair:0",
        "title": "a.md ↔ b.md",
        "mode": "file_pair",
        "status": "modified",
        "left_label": "/tmp/a.md",
        "right_label": "/tmp/b.md",
        "left_path": "/tmp/a.md",
        "right_path": "/tmp/b.md",
        "rev": "",
        "additions": 1,
        "deletions": 1,
        "is_binary": False,
        "patch": "@@ -1 +1 @@\n-पा०\n+पा॰",
        "hunks": [
          {
            "header": "@@ -1,1 +1,1 @@ (Full File)",
            "rows": [
              {
                "kind": "modify",
                "left_num": 1,
                "right_num": 1,
                "left_text": "पा०",
                "right_text": "पा॰",
                "lhs_segments": [{"op": "del", "text": "पा०"}],
                "rhs_segments": [{"op": "ins", "text": "पा॰"}],
              }
            ],
            "inline_rows": [
              {
                "kind": "modify",
                "left_num": 1,
                "right_num": 1,
                "segments": [
                  {"op": "del", "text": "पा०", "sub_segments": [{"op": "eq", "text": "पा"}, {"op": "del", "text": "०"}]},
                  {"op": "ins", "text": "पा॰", "sub_segments": [{"op": "eq", "text": "पा"}, {"op": "ins", "text": "॰"}]},
                ],
              }
            ],
          }
        ],
      }
    ],
  }
  sample_ws["active_item"] = sample_ws["items"][0]

  harness = (
    "const elems = {};\n"
    "const listeners = {};\n"
    "const cssProps = {};\n"
    "const lsStore = {};\n"
    "function makeClassList() {\n"
    "  const set = new Set();\n"
    "  return { add(c){ set.add(c); }, remove(c){ set.delete(c); }, toggle(c, f){ if (f === undefined) { if (set.has(c)) set.delete(c); else set.add(c); } else if (f) set.add(c); else set.delete(c); }, contains(c){ return set.has(c); } };\n"
    "}\n"
    "function makeEl(id) { return { id, value: '', checked: false, innerHTML: '', textContent: '', style: {}, classList: makeClassList() }; }\n"
    "global.document = {\n"
    "  documentElement: { style: { setProperty(k, v){ cssProps[k] = v; } } },\n"
    "  getElementById(id) { if (!elems[id]) elems[id] = makeEl(id); return elems[id]; },\n"
    "  addEventListener(evt, fn) { listeners[evt] = fn; },\n"
    "};\n"
    "elems['diffModeSelect'] = Object.assign(makeEl('diffModeSelect'), { value: 'word' });\n"
    "elems['normalizeSelect'] = Object.assign(makeEl('normalizeSelect'), { value: 'none' });\n"
    "elems['ignorePunctChk'] = Object.assign(makeEl('ignorePunctChk'), { checked: false });\n"
    "elems['singlePaneChk'] = Object.assign(makeEl('singlePaneChk'), { checked: true });\n"
    "elems['collapseContextChk'] = Object.assign(makeEl('collapseContextChk'), { checked: true });\n"
    "elems['changedOnlyChk'] = Object.assign(makeEl('changedOnlyChk'), { checked: false });\n"
    "elems['wrapLinesChk'] = Object.assign(makeEl('wrapLinesChk'), { checked: true });\n"
    "elems['settingsPane'] = Object.assign(makeEl('settingsPane'), { style: { display: 'none' } });\n"
    "global.localStorage = { getItem(k){ return Object.prototype.hasOwnProperty.call(lsStore, k) ? lsStore[k] : null; }, setItem(k, v){ lsStore[k] = String(v); } };\n"
    "global.window = { location: { reload(){} }, getSelection(){ return { toString(){ return ''; } }; } };\n"
    "global.window.DiffSanEngine = global.DiffSanEngine;\n"
    f"const sampleWs = {json.dumps(sample_ws)};\n"
    "const fetched = [];\n"
    "global.fetch = async (url, opts) => {\n"
    "  fetched.push({ url, opts });\n"
    "  if (url.startsWith('/api/meta')) return { ok: true, json: async () => ({ spec: sampleWs.spec }) };\n"
    "  return { ok: true, json: async () => sampleWs };\n"
    "};\n"
    + js.replace("init();", "")
    + "\n(async () => {\n"
    "  await init();\n"
    "  const html = elems['diffContainer'].innerHTML;\n"
    "  if (!html.includes('diff-table-single') || !html.includes('class=\"ch-sub\">०</span>') || !html.includes('Open Left in IDEA')) {\n"
    "    throw new Error('Local diff UI failed to render word-flow sub_segments / IDEA buttons: ' + html);\n"
    "  }\n"
    "  listeners['keydown']({ key: 's', code: 'KeyS', altKey: true, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!elems['diffContainer'].innerHTML.includes('lhs-cell')) throw new Error('Alt+S failed to switch to 2-pane mode');\n"
    "  toggleSettingsPane(true);\n"
    "  setDiffColor('--red-char', '#fff2f2');\n"
    "  if (cssProps['--red-char'] !== '#fff2f2' || !lsStore[DIFF_COLOR_STORAGE_KEY].includes('#fff2f2')) throw new Error('Color picker failed');\n"
    "  listeners['keydown']({ key: 'Escape', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (isSettingsPaneOpen()) throw new Error('Escape failed to close settings pane');\n"
    "})().catch(err => { console.error(err); process.exit(1); });\n"
  )
  js_file = tmp_path / "test_local_diff_gui.js"
  js_file.write_text(harness, encoding="utf-8")
  subprocess.check_call(["node", str(js_file)])


def test_pure_js_diff_engine_parity_and_static_export_with_gap_expand(tmp_path: Path):
  if not shutil.which("node"):
    pytest.skip("node binary not installed")
  from pull_requests_san.diff import build_full_file_hunk, compute_char_diff, compute_stream_diff

  # 1. Verify pure-JS DiffSanEngine matches Python diff logic on Sanskrit/Vedic cases
  engine_path = (
    Path(__file__).resolve().parents[2] / "pull_requests" / "pull_requests_san" / "assets" / "diff_engine.js"
  )
  py_lhs, py_rhs = compute_char_diff("इमा᳓म् अ᳓रण्या पा०", "इमा᳓म् अर᳓ण्या पा॰", mode="grapheme")
  py_nfc_l, py_nfc_r = compute_char_diff("क\u093c", "\u0958", mode="codepoint", normalize="nfc")
  py_ip = build_full_file_hunk("राम।\nपा० ख\n", "राम॥\nपा॰ ख\n", char_diff_mode="word", ignore_punct=True)
  py_pure_del = compute_stream_diff("<details><summary>वीरराघवः [यान्त्रिकम्]</summary>", "<details><summary>वीरराघवः</summary>", mode="word")
  py_pure_ins = compute_stream_diff("<details><summary>वीरराघवः</summary>", "<details><summary>वीरराघवः [यान्त्रिकम्]</summary>", mode="word")
  assert py_pure_del == [
    {"op": "eq", "text": "<details><summary>वीरराघवः"},
    {"op": "del", "text": " [यान्त्रिकम्]"},
    {"op": "eq", "text": "</summary>"},
  ]
  assert py_pure_ins == [
    {"op": "eq", "text": "<details><summary>वीरराघवः"},
    {"op": "ins", "text": " [यान्त्रिकम्]"},
    {"op": "eq", "text": "</summary>"},
  ]

  parity_script = (
    f"const E = require({json.dumps(str(engine_path))});\n"
    "const [gL, gR] = E.computeCharDiff('इमा᳓म् अ᳓रण्या पा०', 'इमा᳓म् अर᳓ण्या पा॰', 'grapheme');\n"
    f"if (JSON.stringify(gL) !== JSON.stringify({json.dumps(py_lhs)}) || JSON.stringify(gR) !== JSON.stringify({json.dumps(py_rhs)})) throw new Error('Grapheme mismatch');\n"
    "const [nL, nR] = E.computeCharDiff('क\\u093c', '\\u0958', 'codepoint', { normalize: 'nfc' });\n"
    f"if (JSON.stringify(nL) !== JSON.stringify({json.dumps(py_nfc_l)}) || JSON.stringify(nR) !== JSON.stringify({json.dumps(py_nfc_r)})) throw new Error('NFC mismatch');\n"
    "const ip = E.buildFullFileHunk('राम।\\nपा० ख\\n', 'राम॥\\nपा॰ ख\\n', { charDiffMode: 'word', ignorePunct: true });\n"
    "if (ip.rows[0].kind !== 'context') throw new Error('ignore_punct failed in JS');\n"
    "const wordH = E.buildFullFileHunk('पा०\\n', 'पा॰\\n', { charDiffMode: 'word' });\n"
    "const delSeg = wordH.inline_rows[0].segments.find(s => s.op === 'del');\n"
    "if (!delSeg || !delSeg.sub_segments.some(ss => ss.op === 'del' && ss.text === '०')) throw new Error('Intra-word ० missing in JS');\n"
    "const pd = E.computeStreamDiff('<details><summary>वीरराघवः [यान्त्रिकम्]</summary>', '<details><summary>वीरराघवः</summary>', { mode: 'word' });\n"
    f"if (JSON.stringify(pd) !== JSON.stringify({json.dumps(py_pure_del)})) throw new Error('Pure deletion mismatch in JS: ' + JSON.stringify(pd));\n"
    "const pi = E.computeStreamDiff('<details><summary>वीरराघवः</summary>', '<details><summary>वीरराघवः [यान्त्रिकम्]</summary>', { mode: 'word' });\n"
    f"if (JSON.stringify(pi) !== JSON.stringify({json.dumps(py_pure_ins)})) throw new Error('Pure insertion mismatch in JS: ' + JSON.stringify(pi));\n"
    "const sandhiCases = [\n"
    "  ['अस्ति इति', 'अस्तीति', '', '', true], ['महा ईशः', 'महेशः', '', '', true], ['इति आदि', 'इत्यादि', '', '', true],\n"
    "  ['देवः अस्ति', 'देवोऽस्ति', '', '', true], ['प्रणयो ऽस्येति', 'प्रणयोऽस्येति', '', '', true], ['तत् इति', 'तदिति', '', '', true],\n"
    "  ['रामः च', 'रामश्च', '', '', true], ['बालः', 'बालो', '', 'गच्छति', true], ['पा०', 'पा॰', '', '', false],\n"
    "  ['अस्ति', 'नास्ति', '', '', false], ['अ᳓रण्या', 'अर᳓ण्या', '', '', false], ['बालः', 'बालो', '', 'करोति', false]\n"
    "];\n"
    "for (const [a, b, p, n, exp] of sandhiCases) {\n"
    "  if (E.isInterwordSandhiPair(a, b, p, n) !== exp) throw new Error('Sandhi failure: ' + a + ' vs ' + b);\n"
    "}\n"
  )
  parity_js = tmp_path / "parity.js"
  parity_js.write_text(parity_script, encoding="utf-8")
  subprocess.check_call(["node", str(parity_js)])

  # 2. Export static standalone page via CLI (--export-static) and run without any server (fetch throws!)
  out_static = tmp_path / "static_site" / "index.html"
  assert server.main(["--export-static", str(out_static)]) == 0
  static_html = out_static.read_text(encoding="utf-8")
  assert "const IS_STATIC_HOST = true" in static_html
  assert "https://github.com/sanskrit-coders/diff_san" in static_html
  assert "https://pypi.org/project/local-diff-san/" in static_html
  assert "https://pypi.org/project/pull-requests-san/" in static_html
  assert "pip install local-diff-san" in static_html
  assert "character-level" in static_html
  m = re.search(r"<script>\n(.*?)\n</script>", static_html, re.S)
  assert m is not None
  static_js = m.group(1)

  long_left = "अग्निमीळे\n" + "".join(f"पङ्क्तिः {i}\n" for i in range(1, 36)) + "समाप्तम्\n"
  long_right = "अग्निमीळे\n" + "".join(f"पङ्क्तिः {i}\n" for i in range(1, 36)) + "समाप्तम् नूतनम्\n"

  static_harness = (
    "const elems = {};\n"
    "const listeners = {};\n"
    "function makeClassList(){ const s = new Set(); return { add(c){s.add(c);}, remove(c){s.delete(c);}, toggle(c,f){ if(f===undefined){if(s.has(c))s.delete(c);else s.add(c);} else if(f)s.add(c); else s.delete(c); }, contains(c){return s.has(c);} }; }\n"
    "function makeEl(id){ return { id, value: '', checked: false, innerHTML: '', textContent: '', style: {}, classList: makeClassList() }; }\n"
    "global.document = {\n"
    "  documentElement: { style: { setProperty(){} } },\n"
    "  getElementById(id){ if (!elems[id]) elems[id] = makeEl(id); return elems[id]; },\n"
    "  addEventListener(e, fn){ listeners[e] = fn; },\n"
    "};\n"
    "elems['diffModeSelect'] = Object.assign(makeEl('diffModeSelect'), { value: 'word' });\n"
    "elems['normalizeSelect'] = Object.assign(makeEl('normalizeSelect'), { value: 'none' });\n"
    "elems['ignorePunctChk'] = Object.assign(makeEl('ignorePunctChk'), { checked: false });\n"
    "elems['singlePaneChk'] = Object.assign(makeEl('singlePaneChk'), { checked: true });\n"
    "elems['collapseContextChk'] = Object.assign(makeEl('collapseContextChk'), { checked: true });\n"
    "elems['changedOnlyChk'] = Object.assign(makeEl('changedOnlyChk'), { checked: false });\n"
    "elems['wrapLinesChk'] = Object.assign(makeEl('wrapLinesChk'), { checked: true });\n"
    "global.localStorage = { getItem(){ return null; }, setItem(){} };\n"
    "global.window = global;\n"
    "global.location = { protocol: 'https:', reload(){} };\n"
    "global.getSelection = () => ({ toString(){ return ''; } });\n"
    "global.fetch = async () => { throw new Error('Static mode must not issue network fetch calls'); };\n"
    + static_js.replace("init();", "")
    + "\n(async () => {\n"
    "  await init();\n"
    "  if (!elems['diffContainer'].innerHTML.includes('आविध्याऽऽलोल्य')) throw new Error('Sample did not render in static mode');\n"
    "  if (elems['diffContainer'].innerHTML.split('वीरराघवः').length !== 2 || !elems['diffContainer'].innerHTML.includes('>[यान्त्रिकम्]</span>')) {\n"
    "    throw new Error('Sample repeated वीरराघवः on pure deletion: ' + elems['diffContainer'].innerHTML);\n"
    "  }\n"
    f"  elems['staticLeftText'].value = {json.dumps(long_left)};\n"
    f"  elems['staticRightText'].value = {json.dumps(long_right)};\n"
    "  computeStaticTextDiff();\n"
    "  const collapsedHtml = elems['diffContainer'].innerHTML;\n"
    "  if (!collapsedHtml.includes('diff-gap-row') || collapsedHtml.includes('diff-gap-btn') || collapsedHtml.includes('पङ्क्तिः 1<')) {\n"
    "    throw new Error('Long context gap did not collapse cleanly as single clickable bar: ' + collapsedHtml);\n"
    "  }\n"
    "  const mKey = collapsedHtml.match(/expandGapRow\\(&quot;([^&]+)&quot;,/);\n"
    "  if (!mKey) throw new Error('Missing expandGapRow key');\n"
    "  const gapKey = mKey[1];\n"
    "  // Navigate lines with ArrowDown / Alt+ArrowDown before expanding\n"
    "  listeners['keydown']({ key: 'ArrowDown', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  const firstRow = selectedLocalRowKey;\n"
    "  listeners['keydown']({ key: 'ArrowDown', altKey: true, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!firstRow || !selectedLocalRowKey || selectedLocalRowKey === firstRow) throw new Error('Alt+ArrowDown did not jump to modified line');\n"
    "  expandGapRow(gapKey, 'all', 0);\n"
    "  if (elems['diffContainer'].innerHTML.includes('diff-gap-row') || !elems['diffContainer'].innerHTML.includes('पङ्क्तिः 25')) {\n"
    "    throw new Error('Click-expand all did not reveal full collapsed gap');\n"
    "  }\n"
    "  // Interword sandhi toggle highlights अस्ति इति vs अस्तीति with ch-sandhi\n"
    "  elems['staticLeftText'].value = 'अस्ति इति\\nपा०\\n';\n"
    "  elems['staticRightText'].value = 'अस्तीति\\nपा॰\\n';\n"
    "  computeStaticTextDiff();\n"
    "  onToggleSandhiHighlight();\n"
    "  const sHtml = elems['diffContainer'].innerHTML;\n"
    "  if (!sHtml.includes('ch-sandhi') || sHtml.split('ch-sandhi').length !== 3 || !sHtml.includes('पा<span class=\"ch-sub\">०</span>')) {\n"
    "    throw new Error('Local diff sandhi toggle did not mark sandhi pair only: ' + sHtml);\n"
    "  }\n"
    "})().catch(err => { console.error(err); process.exit(1); });\n"
  )
  s_js = tmp_path / "static_test.js"
  s_js.write_text(static_harness, encoding="utf-8")
  subprocess.check_call(["node", str(s_js)])

