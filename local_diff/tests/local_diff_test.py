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
  assert head_item.left_label == "HEAD"
  assert head_item.right_label == "WORKING"
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
  idx_html = tc.get("/").text
  hdr_top = idx_html.split("<header>", 1)[1].split("</div>", 1)[0]
  assert "local-diff-san" not in hdr_top
  assert 'id="helpBtn"' in idx_html and 'id="helpPane"' in idx_html
  meta = tc.get("/api/meta").json()
  assert meta["spec"]["mode"] == "git_head"
  assert "word" in meta["diff_modes"]

  diff_res = tc.get("/api/diff?diff_mode=word").json()
  assert diff_res["active_item"]["status"] == "modified"
  assert diff_res["active_item"]["left_label"] == "HEAD"
  assert diff_res["active_item"]["right_label"] == "WORKING"
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

  # IDEA, VS Code, and GoldenDict desktop helpers reuse pull_requests_san.local_helpers
  launched: list[list[str]] = []
  with monkeypatch.context() as mp:
    mp.setattr(server, "_resolve_idea_binary", lambda: "/usr/bin/idea-mock")
    mp.setattr(server, "_resolve_vscode_binary", lambda: "/usr/bin/code-mock")
    mp.setattr(server, "_resolve_goldendict_binary", lambda: "/usr/bin/gd-mock")
    mp.setattr(server.subprocess, "Popen", lambda cmd, **kw: launched.append(list(cmd)))

    r_idea = tc.post("/api/open_in_idea", json={"path": str(f1), "line": 3})
    assert r_idea.status_code == 200
    assert launched[-1] == ["/usr/bin/idea-mock", "--line", "3", str(f1.resolve())]

    r_code = tc.post("/api/open_in_vscode", json={"path": str(f1), "line": 3})
    assert r_code.status_code == 200
    assert launched[-1] == ["/usr/bin/code-mock", "--goto", f"{f1.resolve()}:3"]

    r_code_nol = tc.post("/api/open_in_vscode", json={"path": str(f1)})
    assert r_code_nol.status_code == 200
    assert launched[-1] == ["/usr/bin/code-mock", str(f1.resolve())]

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
  assert "header button {" in html_src and "font-size: 12.5px;" in html_src
  assert '<option value="word" selected>Word</option>' in html_src
  assert "> Wrap\n" in html_src and "> Wrap long lines\n" not in html_src

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
    "elems['helpPane'] = Object.assign(makeEl('helpPane'), { style: { display: 'none' } });\n"
    "global.localStorage = { getItem(k){ return Object.prototype.hasOwnProperty.call(lsStore, k) ? lsStore[k] : null; }, setItem(k, v){ lsStore[k] = String(v); } };\n"
    "global.window = { location: { reload(){} }, getSelection(){ return { toString(){ return ''; } }; } };\n"
    "global.window.DiffSanEngine = global.DiffSanEngine;\n"
    f"const sampleWs = {json.dumps(sample_ws)};\n"
    "const fetched = [];\n"
    "let mockRespWs = sampleWs;\n"
    "global.fetch = async (url, opts) => {\n"
    "  fetched.push({ url, opts });\n"
    "  if (url.startsWith('/api/meta')) return { ok: true, json: async () => ({ spec: sampleWs.spec }) };\n"
    "  const activeIdMatch = url.match(/active_id=([^&]+)/);\n"
    "  if (activeIdMatch) {\n"
    "    const reqId = decodeURIComponent(activeIdMatch[1]);\n"
    "    const found = (mockRespWs.items || []).find(it => it.id === reqId) || mockRespWs.items[0];\n"
    "    return { ok: true, json: async () => Object.assign({}, mockRespWs, { active_item: found }) };\n"
    "  }\n"
    "  return { ok: true, json: async () => mockRespWs };\n"
    "};\n"
    + js.replace("init();", "")
    + "\n(async () => {\n"
    "  await init();\n"
    "  const html = elems['diffContainer'].innerHTML;\n"
    "  if (!html.includes('diff-table-single') || !html.includes('class=\"ch-sub\">०</span>') || !html.includes('id=\"fileTitleVscodeLink\"') || !html.includes('openPathInVscode') || !html.includes('id=\"fileTitleIdeaLink\"') || !html.includes('>[I]</a>')) {\n"
    "    throw new Error('Local diff UI failed to render word-flow sub_segments / VS Code title link / [I] IDEA link: ' + html);\n"
    "  }\n"
    "  if (!html.includes('<strong>Left:</strong>') || !html.includes('<strong>Right:</strong>') || html.includes('Old / Left:')) {\n"
    "    throw new Error('Local diff label chips did not shorten to Left:/Right:: ' + html);\n"
    "  }\n"
    "  const headHtml = renderDiffItemHtml({\n"
    "    id: 'head:sub/foo.md', title: 'sub/foo.md', mode: 'git_head', status: 'modified',\n"
    "    left_label: 'HEAD', right_label: 'WORKING', left_path: null, right_path: '/repo/sub/foo.md',\n"
    "    additions: 1, deletions: 1, is_binary: false, hunks: sampleWs.active_item.hunks,\n"
    "  });\n"
    "  if (!headHtml.includes('<strong>Left:</strong> HEAD') || !headHtml.includes('<strong>Right:</strong> WORKING') || headHtml.includes('HEAD:sub/foo.md') || headHtml.includes('WORKING:sub/foo.md')) {\n"
    "    throw new Error('HEAD diff repeated filename in Left:/Right: labels: ' + headHtml);\n"
    "  }\n"
    "  if (!headHtml.includes('id=\"fileTitleVscodeLink\"') || !headHtml.includes('>[I]</a>') || !headHtml.includes('file-header-main') || !headHtml.includes('file-header-badges')) {\n"
    "    throw new Error('HEAD diff missing single-row file header, VS Code file title link, or [I] IDEA link');\n"
    "  }\n"
    "  listeners['keydown']({ key: '?', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!isHelpPaneOpen()) throw new Error('? key did not open helpPane');\n"
    "  listeners['keydown']({ key: 'Escape', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (isHelpPaneOpen()) throw new Error('Escape did not close helpPane');\n"
    "  listeners['keydown']({ key: 's', code: 'KeyS', altKey: true, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!elems['diffContainer'].innerHTML.includes('lhs-cell')) throw new Error('Alt+S failed to switch to 2-pane mode');\n"
    "  toggleSettingsPane(true);\n"
    "  setDiffColor('--red-char', '#fff2f2');\n"
    "  if (cssProps['--red-char'] !== '#fff2f2' || !lsStore[DIFF_COLOR_STORAGE_KEY].includes('#fff2f2')) throw new Error('Color picker failed');\n"
    "  listeners['keydown']({ key: 'Escape', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (isSettingsPaneOpen()) throw new Error('Escape failed to close settings pane');\n"
    "  // Ctrl+PageDown / Ctrl+PageUp navigate left-pane file items without wrapping\n"
    "  const item2 = Object.assign({}, sampleWs.items[0], { id: 'pair:1', title: 'c.md' });\n"
    "  mockRespWs = Object.assign({}, sampleWs, { items: [sampleWs.items[0], item2], active_item: sampleWs.items[0] });\n"
    "  workspaceData = mockRespWs;\n"
    "  selectedItemId = 'pair:0';\n"
    "  listeners['keydown']({ key: 'PageDown', code: 'PageDown', ctrlKey: true, altKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){}, stopPropagation(){} });\n"
    "  await new Promise(r => setTimeout(r, 15));\n"
    "  if (selectedItemId !== 'pair:1') throw new Error('Ctrl+PageDown did not select next sidebar item: ' + selectedItemId);\n"
    "  listeners['keydown']({ key: 'PageDown', code: 'PageDown', ctrlKey: true, altKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){}, stopPropagation(){} });\n"
    "  await new Promise(r => setTimeout(r, 15));\n"
    "  if (selectedItemId !== 'pair:1') throw new Error('Ctrl+PageDown wrapped past last item: ' + selectedItemId);\n"
    "  listeners['keydown']({ key: 'PageUp', code: 'PageUp', ctrlKey: true, altKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){}, stopPropagation(){} });\n"
    "  await new Promise(r => setTimeout(r, 15));\n"
    "  if (selectedItemId !== 'pair:0') throw new Error('Ctrl+PageUp did not select previous sidebar item: ' + selectedItemId);\n"
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
  assert "https://sanskrit-coders.github.io/diff_san/pull_requests/" in static_html
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
    "const listenerLists = {};\n"
    "const clipboardCopies = [];\n"
    "function makeClassList(){ const s = new Set(); return { add(c){s.add(c);}, remove(c){s.delete(c);}, toggle(c,f){ if(f===undefined){if(s.has(c))s.delete(c);else s.add(c);} else if(f)s.add(c); else s.delete(c); }, contains(c){return s.has(c);} }; }\n"
    "function makeEl(id){ return { id, value: '', checked: false, innerHTML: '', textContent: '', style: {}, classList: makeClassList() }; }\n"
    "global.document = {\n"
    "  documentElement: { style: { setProperty(){} } },\n"
    "  getElementById(id){ if (!elems[id]) elems[id] = makeEl(id); return elems[id]; },\n"
    "  addEventListener(e, fn){ if (!listenerLists[e]) listenerLists[e] = []; listenerLists[e].push(fn); listeners[e] = (ev) => listenerLists[e].forEach(h => h(ev)); },\n"
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
    "Object.defineProperty(global, 'navigator', { value: { clipboard: { writeText: async (txt) => { clipboardCopies.push(String(txt)); } } }, configurable: true });\n"
    "global.fetch = async () => { throw new Error('Static mode must not issue network fetch calls'); };\n"
    + static_js.replace("init();", "")
    + "\n(async () => {\n"
    "  await init();\n"
    "  if (!elems['diffContainer'].innerHTML.includes('आविध्याऽऽलोल्य')) throw new Error('Sample did not render in static mode');\n"
    "  if (elems['diffContainer'].innerHTML.split('वीरराघवः').length !== 2 || !elems['diffContainer'].innerHTML.includes('> [यान्त्रिकम्]</span>')) {\n"
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
    "  // Interword sandhi highlighting is ON by default (including ऽस्मिन्न् इति vs ऽस्मिन् इति) and toggles cleanly;\n"
    "  // OLD/NEW pairs wrap in a single .ch-pair box, and jumpSandhiDiff steps across .ch-sandhi rows\n"
    "  elems['staticLeftText'].value = 'अस्ति इति\\nपा०\\nअतो व्युत्थित-चित्तैः निष्ठीयते शुभाश्रय-भूते ऽस्मिन्न् इति—\\n';\n"
    "  elems['staticRightText'].value = 'अस्तीति\\nपा॰\\nअतो व्युत्थित-चित्तैः निष्ठीयते शुभाश्रय-भूते ऽस्मिन् इति—\\n';\n"
    "  computeStaticTextDiff();\n"
    "  const sHtml = elems['diffContainer'].innerHTML;\n"
    "  if (!isSandhiHighlightEnabled() || !sHtml.includes('ch-pair ch-sandhi') || sHtml.split('ch-pair ch-sandhi').length !== 3 || sHtml.split('<span class=\"ch-pair').length !== 4 || !sHtml.includes('पा<span class=\"ch-sub\">०</span>')) {\n"
    "    throw new Error('Local diff default sandhi / ऽस्मिन्न् इति / single dashed box check failed: ' + sHtml);\n"
    "  }\n"
    "  elems[renderedLocalRows[0].domId] = Object.assign(makeEl(renderedLocalRows[0].domId), { innerHTML: 'class=\"ch-sandhi\"' });\n"
    "  elems[renderedLocalRows[1].domId] = Object.assign(makeEl(renderedLocalRows[1].domId), { innerHTML: 'lexical only' });\n"
    "  elems[renderedLocalRows[2].domId] = Object.assign(makeEl(renderedLocalRows[2].domId), { innerHTML: 'class=\"ch-sandhi\"' });\n"
    "  selectedLocalRowKey = renderedLocalRows[0].rowKey;\n"
    "  listeners['keydown']({ key: 'ArrowDown', altKey: true, shiftKey: true, ctrlKey: false, metaKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (selectedLocalRowKey !== renderedLocalRows[2].rowKey) throw new Error('Alt+Shift+ArrowDown did not jump to next sandhi row');\n"
    "  listeners['keydown']({ key: 'ArrowUp', altKey: true, shiftKey: true, ctrlKey: false, metaKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (selectedLocalRowKey !== renderedLocalRows[0].rowKey) throw new Error('Alt+Shift+ArrowUp did not jump back to prev sandhi row');\n"
    "  const bugRep = buildRowBugReportDiffText(renderedLocalRows[2], 'local-diff-san');\n"
    "  if (!bugRep.includes('ऽस्मिन्न्') || !bugRep.includes('ऽस्मिन्')) throw new Error('buildRowBugReportDiffText missing OLD/NEW: ' + bugRep);\n"
    "  onToggleSandhiHighlight();\n"
    "  if (isSandhiHighlightEnabled() || elems['diffContainer'].innerHTML.includes('ch-sandhi')) throw new Error('Toggling sandhi off did not clear ch-sandhi');\n"
    "  onToggleSandhiHighlight();\n"
    "  // Normal click leaves selection null (preserving drag text selection); Shift+Click selects line\n"
    "  const lr0 = renderedLocalRows[0], lr1 = renderedLocalRows[1];\n"
    "  selectedLocalRowKey = null;\n"
    "  handleLocalRowClick({ ctrlKey: false, altKey: false, metaKey: false, shiftKey: false, target: {} }, lr1.rowKey);\n"
    "  if (selectedLocalRowKey !== null) throw new Error('Plain click unexpectedly selected row in local diff');\n"
    "  handleLocalRowClick({ ctrlKey: false, altKey: false, metaKey: false, shiftKey: true, target: {}, preventDefault(){} }, lr1.rowKey);\n"
    "  if (selectedLocalRowKey !== lr1.rowKey) throw new Error('Shift+Click did not select row in local diff');\n"
    "  // PageDown / PageUp scrolls #mainPane and selects first visible line\n"
    "  elems['mainPane'] = Object.assign(makeEl('mainPane'), {\n"
    "    scrollTop: 0, clientHeight: 200, scrollHeight: 800,\n"
    "    getBoundingClientRect(){ return { top: 80, bottom: 280, height: 200, width: 800 }; },\n"
    "  });\n"
    "  elems[lr0.domId] = Object.assign(makeEl(lr0.domId), { getBoundingClientRect(){ const st = elems['mainPane'].scrollTop; return { top: 80 - st, bottom: 120 - st, height: 40, width: 600 }; } });\n"
    "  elems[lr1.domId] = Object.assign(makeEl(lr1.domId), { getBoundingClientRect(){ const st = elems['mainPane'].scrollTop; return { top: 245 - st, bottom: 285 - st, height: 40, width: 600 }; } });\n"
    "  listeners['keydown']({ key: 'PageDown', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (elems['mainPane'].scrollTop !== 175 || selectedLocalRowKey !== lr1.rowKey) {\n"
    "    throw new Error('Local diff PageDown failed: scrollTop=' + elems['mainPane'].scrollTop + ' selected=' + selectedLocalRowKey);\n"
    "  }\n"
    "  listeners['keydown']({ key: 'PageUp', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (elems['mainPane'].scrollTop !== 0 || selectedLocalRowKey !== lr0.rowKey) {\n"
    "    throw new Error('Local diff PageUp failed: scrollTop=' + elems['mainPane'].scrollTop + ' selected=' + selectedLocalRowKey);\n"
    "  }\n"
    "  // Alt+W toggles diffModeSelect between word and grapheme\n"
    "  listeners['keydown']({ key: 'w', code: 'KeyW', altKey: true, shiftKey: false, ctrlKey: false, metaKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (elems['diffModeSelect'].value !== 'grapheme') throw new Error('Local Alt+W word->grapheme failed: ' + elems['diffModeSelect'].value);\n"
    "  listeners['keydown']({ key: 'w', code: 'KeyW', altKey: true, shiftKey: false, ctrlKey: false, metaKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (elems['diffModeSelect'].value !== 'word') throw new Error('Local Alt+W grapheme->word failed: ' + elems['diffModeSelect'].value);\n"
    "  // Tab steps diff tokens & Ctrl+Space marks checked-until-here in local_diff\n"
    "  const lMkTok = () => ({ classList: makeClassList(), parentElement: { closest(){ return null; } }, scrollIntoView(){} });\n"
    "  const lToks0 = [lMkTok(), lMkTok()];\n"
    "  const lToks1 = [lMkTok()];\n"
    "  elems[lr0.domId].querySelectorAll = () => lToks0;\n"
    "  elems[lr1.domId].querySelectorAll = () => lToks1;\n"
    "  selectLocalRow(lr0.rowKey, { scroll: false });\n"
    "  listeners['keydown']({ key: 'Tab', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (selectedLocalRowKey !== lr0.rowKey || !lToks0[0].classList.contains('diff-focus-cur')) throw new Error('Local Tab did not focus first diff on lr0');\n"
    "  listeners['keydown']({ key: 'Tab', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!lToks0[1].classList.contains('diff-focus-cur')) throw new Error('Local 2nd Tab did not advance on lr0');\n"
    "  listeners['keydown']({ key: 'Space', code: 'Space', altKey: false, ctrlKey: true, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!elems[lr0.domId].classList.contains('diff-checked-until') || !lToks0[1].classList.contains('diff-checked-token')) throw new Error('Local Ctrl+Space failed');\n"
    "  listeners['keydown']({ key: 'Tab', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (selectedLocalRowKey !== lr1.rowKey || !lToks1[0].classList.contains('diff-focus-cur')) throw new Error('Local 3rd Tab did not advance selected row to lr1');\n"
    "  elems['diffFindBar'] = Object.assign(makeEl('diffFindBar'), { style: { display: 'flex' } });\n"
    "  const lFindTxt0 = { nodeType: 3, parentElement: lToks0[1] };\n"
    "  lToks0[1].contains = (n) => n === lToks0[1] || n === lFindTxt0;\n"
    "  const lFindTxt1 = { nodeType: 3, parentElement: lToks1[0] };\n"
    "  lToks1[0].contains = (n) => n === lToks1[0] || n === lFindTxt1;\n"
    "  _diffFindMatches = [\n"
    "    { visualRange: {}, block: lToks0[1], rowEl: elems[lr0.domId], range: { startContainer: lFindTxt0, endContainer: lFindTxt0 } },\n"
    "    { visualRange: {}, block: lToks1[0], rowEl: elems[lr1.domId], range: { startContainer: lFindTxt1, endContainer: lFindTxt1 } },\n"
    "  ];\n"
    "  _diffFindIdx = 0;\n"
    "  listeners['keydown']({ key: 'g', ctrlKey: true, altKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (_diffFindIdx !== 1 || selectedLocalRowKey !== lr1.rowKey || !lToks1[0].classList.contains('diff-focus-cur')) throw new Error('Local Ctrl+G did not select row+box: ' + _diffFindIdx + ' sel=' + selectedLocalRowKey);\n"
    "  listeners['keydown']({ key: 'G', ctrlKey: true, altKey: false, metaKey: false, shiftKey: true, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (_diffFindIdx !== 0 || selectedLocalRowKey !== lr0.rowKey || !lToks0[1].classList.contains('diff-focus-cur')) throw new Error('Local Ctrl+Shift+G did not select row+box: ' + _diffFindIdx + ' sel=' + selectedLocalRowKey);\n"
    "  // Samasa-hyphenated sandhi highlights in word and grapheme modes, and clicking a diff box copies OLD/NEW\n"
    "  elems['staticLeftText'].value = 'अतो व्युत्थित-चित्तैः निष्ठीयते शुभाश्रय-भूते ऽस्मिन्न् इति—  \\n';\n"
    "  elems['staticRightText'].value = 'अतो व्युत्थित-चित्तैः निष्ठीयते शुभाश्रय-भूतेऽस्मिन् इति—\\n';\n"
    "  elems['diffModeSelect'].value = 'word';\n"
    "  computeStaticTextDiff();\n"
    "  const wordCompHtml = elems['diffContainer'].innerHTML;\n"
    "  if (!wordCompHtml.includes('ch-pair ch-sandhi') || !wordCompHtml.includes('data-old=\"शुभाश्रय-भूते ऽस्मिन्न्\"')) {\n"
    "    throw new Error('Local word-mode compound sandhi failed: ' + wordCompHtml);\n"
    "  }\n"
    "  elems['diffModeSelect'].value = 'grapheme';\n"
    "  computeStaticTextDiff();\n"
    "  if (!elems['diffContainer'].innerHTML.includes('ch-sandhi')) throw new Error('Local grapheme-mode compound sandhi failed');\n"
    "  const lRowMock = { contains(){ return true; }, closest(sel){ return sel.includes('tr.diff-row') ? lRowMock : null; } };\n"
    "  const lPairCls = makeClassList(); lPairCls.add('ch-pair');\n"
    "  const lPairEl = { classList: lPairCls, getAttribute(k){ return k === 'data-old' ? 'भूते ऽस्मिन्न्' : (k === 'data-new' ? 'भूतेऽस्मिन्' : null); }, closest(sel){ if (sel.includes('.ch-pair')) return lPairEl; if (sel.includes('tr.diff-row')) return lRowMock; return null; } };\n"
    "  listeners['mousedown']({ button: 0, clientX: 50, clientY: 50, shiftKey: false, ctrlKey: false, altKey: false, metaKey: false, target: lPairEl });\n"
    "  listeners['click']({ button: 0, clientX: 50, clientY: 50, shiftKey: false, ctrlKey: false, altKey: false, metaKey: false, target: lPairEl });\n"
    "  await new Promise(r => setTimeout(r, 15));\n"
    "  if (clipboardCopies[0] !== 'OLD:\\nभूते ऽस्मिन्न्\\nNEW:\\nभूतेऽस्मिन्') throw new Error('Local click-to-copy diff box failed: ' + JSON.stringify(clipboardCopies));\n"
    "  // Deleted trailing space after '>' highlights & short intervening context+newlines preserve long paragraph alignment\n"
    "  elems['staticLeftText'].value = '> उच्चलद्-भङ्गाक्रान्त\\nलब्धवन्तश् चेत् ? तर्हि पुनः कृत्यांशः कः ? इति चेत् साध्यांशो अस्तीत्य् आह ॥ ४ ॥\\n\\n[^202_1]: अवाऽऽदरे बहुवचनम् ।\\n';\n"
    "  elems['staticRightText'].value = 'उच्चलद्-भङ्गाक्रान्त  \\nलब्धवन्तश् चेत् ?\\n\\n[^_1]: अवाऽऽदरे बहुवचनम् ।\\n\\n[[P203]]\\n\\nतर्हि पुनः कृत्यांशः कः ? इति चेत् साध्यांशो अस्तीत्य् आह ॥ ४ ॥\\n';\n"
    "  elems['diffModeSelect'].value = 'word';\n"
    "  computeStaticTextDiff();\n"
    "  const edgeHtml = elems['diffContainer'].innerHTML;\n"
    "  if (!edgeHtml.includes('>&gt; </span>') || !edgeHtml.includes('[[P203]]') || edgeHtml.split('तर्हि पुनः कृत्यांशः कः').length !== 2) {\n"
    "    throw new Error('Local diff > space or intervening context bridge failed: ' + edgeHtml);\n"
    "  }\n"
    "  // Sidebar LCP collapsible tree + active highlight + modifier underline tracking\n"
    "  if (typeof renderFileTreeHtml !== 'function') throw new Error('Missing renderFileTreeHtml in local_diff');\n"
    "  const lTree = renderFileTreeHtml([\n"
    "    { path: 'vedAH/yajuH/01/a.md', idx: 0 },\n"
    "    { path: 'vedAH/yajuH/02/b.md', idx: 1, isActive: true },\n"
    "  ], (e) => `<div class=\"file-item ${e.isActive ? 'active' : ''}\" data-idx=\"${e.idx}\">${e.name}</div>`, { scopeKey: 'local_t' });\n"
    "  if (!lTree.includes('file-tree-root') || !lTree.includes('vedAH/yajuH/') || !lTree.includes('file-item active') || !lTree.includes('>b.md</div>')) {\n"
    "    throw new Error('Local diff tree missing LCP root or active item: ' + lTree);\n"
    "  }\n"
    "  const pairTree = renderFileTreeHtml([{ path: 'a.md ↔ b.md', idx: 0 }], (e) => `<div class=\"file-item\">${e.name}</div>`, { scopeKey: 'pair_t' });\n"
    "  if (pairTree.includes('file-tree-dir') || !pairTree.includes('a.md ↔ b.md')) throw new Error('Pair comparison label should remain a flat leaf: ' + pairTree);\n"
    "  document.body = document.body || { classList: makeClassList() };\n"
    "  _syncModifierClasses({ type: 'keydown', key: 'Alt', altKey: true, ctrlKey: false });\n"
    "  if (!document.body.classList.contains('mod-alt-down')) throw new Error('local_diff Alt modifier tracking failed');\n"
    "  _syncModifierClasses({ type: 'keydown', key: 'Control', altKey: false, ctrlKey: true });\n"
    "  if (!document.body.classList.contains('mod-ctrl-down') || document.body.classList.contains('mod-alt-down')) throw new Error('local_diff Ctrl modifier tracking failed');\n"
    "})().catch(err => { console.error(err); process.exit(1); });\n"
  )
  assert '<u class="sc-key sc-ctrl">R</u>efresh' in static_html
  assert '<u class="sc-key sc-alt">S</u>ingle' in static_html
  assert '<u class="sc-key sc-ctrl">F</u>ind' in static_html
  shortcut_line = static_html.split('id="shortcutFooter"')[1].split('</div>')[0]
  assert "<strong>Ctrl+F</strong>" in shortcut_line and "<strong>Alt+S</strong>" in shortcut_line
  assert "#shortcutFooter strong" in static_html
  s_js = tmp_path / "static_test.js"
  s_js.write_text(static_harness, encoding="utf-8")
  subprocess.check_call(["node", str(s_js)])

