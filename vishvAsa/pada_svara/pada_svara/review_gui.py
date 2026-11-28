"""FastAPI web GUI for reviewing Vedic accent fix suggestions, marking accepted files for commit, and inserting ``+++(FEEDBACK!!)+++`` comments.

Run from the command line::

  pada_svara \\
    --dir /home/vvasuki/gitland/vishvAsa/vedAH_yajuH/content/maitrAyaNIyam/saMhitA/sarva-prastutiH
"""

from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
import time
import webbrowser
from typing import Any, Sequence

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

from pada_svara import review

def _discover_vedah_dirs(vishvasa_root: Path | None = None) -> list[str]:
  """Return existing ``vedAH*/static`` and ``vedAH*/content`` directory paths."""
  root = vishvasa_root or (Path.home() / "gitland" / "vishvAsa")
  if not root.is_dir():
    return []
  found: list[str] = []
  for repo in sorted(root.glob("vedAH*")):
    if not repo.is_dir():
      continue
    for sub in ("static", "content"):
      cand = repo / sub
      if cand.is_dir():
        found.append(str(cand))
  return found


class _ScanRequest(BaseModel):
  dir_path: str


class _DecisionRequest(BaseModel):
  item_id: str
  decision: str
  sync_prefills: bool = True


class _FileAcceptRequest(BaseModel):
  file_path: str
  decision: str = "accept"
  sync_prefills: bool = True


class _FileMarkRequest(BaseModel):
  file_path: str
  marked: bool | None = None


class _FeedbackRequest(BaseModel):
  item_id: str
  feedback_text: str
  target_mode: str = "auto"
  sync_prefills: bool = True


class _SubmitRequest(BaseModel):
  item_id: str
  decision: str = "pending"
  feedback_text: str = ""
  target_mode: str = "auto"
  sync_prefills: bool = True


class _CommitRequest(BaseModel):
  file_paths: list[str]
  commit_message: str = "Review accepted accent fix suggestions"


class _OpenIdeaRequest(BaseModel):
  file_path: str
  line: int | None = None
  needle: str | None = None


class _OpenGoldenDictRequest(BaseModel):
  word: str


class _SessionStore:
  def __init__(self, initial_dir: str | None = None, default_n: int = 3, cache_dir: str = review.DEFAULT_REVIEW_CACHE_DIR) -> None:
    self.lock = threading.RLock()
    self.cache_dir = cache_dir
    self.default_n = max(0, default_n)
    self.scan_data: dict[str, Any] | None = None
    self.item_by_id: dict[str, review.ReviewItem] = {}
    self.state: dict[str, Any] = {"decisions": {}, "file_overrides": {}}
    if initial_dir and os.path.isdir(initial_dir):
      self.load_dir(initial_dir)

  def load_dir(self, dir_path: str) -> dict[str, Any]:
    abs_d = os.path.abspath(os.path.expanduser(dir_path))
    data = review.scan_directory(abs_d)
    st = review.load_review_state(abs_d, cache_dir=self.cache_dir)
    with self.lock:
      self.scan_data = data
      self.item_by_id = {it.item_id: it for it in data["items"]}
      self.state = st
    return data

  def refresh_git_statuses(self) -> None:
    if self.scan_data is None:
      return
    file_statuses: dict[str, str] = {}
    repo_staged: dict[str, list[str]] = {}
    for rr in self.scan_data["repo_roots"]:
      smap, staged = review.get_git_status_map(rr)
      file_statuses.update(smap)
      repo_staged[rr] = staged
    self.scan_data["file_statuses"] = file_statuses
    self.scan_data["repo_staged"] = repo_staged

  def summary_payload(self) -> dict[str, Any]:
    if self.scan_data is None:
      return {
        "dir_path": None,
        "total_items": 0,
        "counts": {"pending": 0, "accept": 0, "reject": 0, "with_feedback": 0},
        "files": [],
        "marked_files": [],
        "repo_staged": {},
      }
    decisions = self.state["decisions"]
    cnt = {"pending": 0, "accept": 0, "reject": 0, "with_feedback": 0}
    for it in self.scan_data["items"]:
      d = decisions.get(it.item_id, "pending")
      if it.existing_feedback:
        cnt["with_feedback"] += 1
        if d in ("accept", "reject"):
          cnt[d] += 1
      else:
        cnt[d if d in cnt else "pending"] += 1
    files = review.compute_file_summaries(self.scan_data, self.state)
    marked = [f["file_path"] for f in files if f["marked"]]
    return {
      "dir_path": self.scan_data["dir_path"],
      "total_items": len(self.scan_data["items"]),
      "counts": cnt,
      "files": files,
      "marked_files": marked,
      "repo_staged": self.scan_data["repo_staged"],
    }


def _pick_directory_dialog(initial_dir: str | None = None) -> str | None:
  start = initial_dir if initial_dir and os.path.isdir(initial_dir) else "/home/vvasuki/gitland/vishvAsa"
  if shutil.which("zenity"):
    try:
      out = subprocess.check_output(
        ["zenity", "--file-selection", "--directory", f"--filename={start.rstrip('/')}/"],
        stderr=subprocess.DEVNULL,
        text=True,
      ).strip()
      return out or None
    except subprocess.CalledProcessError:
      return None
  return None


def _resolve_executable(candidates: Sequence[Path]) -> str | None:
  """Return the first existing executable path from ``candidates``."""
  for cand in candidates:
    try:
      resolved = cand.resolve(strict=True)
    except OSError:
      continue
    if resolved.is_file() and os.access(resolved, os.X_OK):
      return str(cand if os.access(cand, os.X_OK) else resolved)
  return None


def _resolve_idea_binary() -> str | None:
  """Locate an executable IntelliJ IDEA CLI launcher on the local machine."""
  candidates: list[Path] = []
  which_idea = shutil.which("idea")
  if which_idea:
    candidates.append(Path(which_idea))
  candidates.append(Path.home() / ".local/share/JetBrains/Toolbox/apps/intellij-idea/bin/idea")
  scripts_dir = Path.home() / ".local/share/JetBrains/Toolbox/scripts"
  if scripts_dir.is_dir():
    candidates.extend(sorted(p for p in scripts_dir.glob("idea*") if p.is_file()))
  for fallback in ("idea.sh", "jetbrains-idea"):
    w = shutil.which(fallback)
    if w:
      candidates.append(Path(w))
  return _resolve_executable(candidates)


def _resolve_vscode_binary() -> str | None:
  """Locate an executable VS Code CLI launcher on the local machine."""
  candidates: list[Path] = []
  for name in ("code", "code-insiders", "codium"):
    w = shutil.which(name)
    if w:
      candidates.append(Path(w))
  for p_str in ("/usr/bin/code", "/usr/local/bin/code", "/snap/bin/code"):
    candidates.append(Path(p_str))
  candidates.append(Path.home() / ".local/bin/code")
  return _resolve_executable(candidates)


def _infer_line_for_file(file_path: Path, *, line: int | None = None, needle: str | None = None) -> int | None:
  """Return 1-based line number from ``line`` or by locating ``needle`` inside ``file_path``."""
  if line is not None and line >= 1:
    return line
  if not needle or not needle.strip():
    return None
  try:
    lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
  except OSError:
    return None
  clean_needle = needle.strip()
  for idx, ln in enumerate(lines, 1):
    if clean_needle in ln:
      return idx
  first_ln = next((p.strip() for p in clean_needle.splitlines() if p.strip()), "")
  if first_ln:
    for idx, ln in enumerate(lines, 1):
      if first_ln in ln:
        return idx
  return None


def _open_file_in_editor(
  file_path: str,
  *,
  binary: str | None,
  not_found_msg: str,
  build_cmd: Any,
  line: int | None = None,
  needle: str | None = None,
) -> dict[str, Any]:
  """Launch/focus ``file_path`` inside a local editor binary with optional resolved line."""
  abs_path = Path(file_path).expanduser().resolve()
  if not abs_path.is_file():
    raise FileNotFoundError(f"File not found: {file_path}")
  if not binary:
    raise RuntimeError(not_found_msg)
  target_line = _infer_line_for_file(abs_path, line=line, needle=needle)
  cmd = build_cmd(binary, abs_path, target_line)
  subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  return {"ok": True, "file_path": str(abs_path), "line": target_line, "command": cmd}


def open_in_intellij(file_path: str, *, line: int | None = None, needle: str | None = None) -> dict[str, Any]:
  """Launch/focus ``file_path`` inside the local IntelliJ IDEA IDE (optionally at ``line``)."""
  return _open_file_in_editor(
    file_path,
    binary=_resolve_idea_binary(),
    not_found_msg="IntelliJ IDEA CLI binary ('idea') not found",
    build_cmd=lambda idea_bin, abs_path, ln: [idea_bin, *(["--line", str(ln)] if ln is not None else []), str(abs_path)],
    line=line,
    needle=needle,
  )


def open_in_vscode(file_path: str, *, line: int | None = None, needle: str | None = None) -> dict[str, Any]:
  """Launch/focus ``file_path`` inside VS Code (optionally at ``line``)."""
  return _open_file_in_editor(
    file_path,
    binary=_resolve_vscode_binary(),
    not_found_msg="VS Code CLI binary ('code') not found",
    build_cmd=lambda code_bin, abs_path, ln: [code_bin, *(["--goto", f"{abs_path}:{ln}"] if ln is not None else [str(abs_path)])],
    line=line,
    needle=needle,
  )


def _resolve_goldendict_binary() -> str | None:
  """Locate an executable GoldenDict CLI binary on the local machine."""
  for name in ("goldendict", "goldendict-ng"):
    path = shutil.which(name)
    if path:
      return path
  return None


def open_in_goldendict(word: str) -> dict[str, Any]:
  """Look up unaccented ``word`` in the running GoldenDict application."""
  query = re.sub(r"[\u0951-\u0954\u1cd0-\u1cff\u0300-\u036f]", "", (word or "")).strip()
  if not query:
    raise ValueError("Empty lookup word")
  gd_bin = _resolve_goldendict_binary()
  if not gd_bin:
    raise RuntimeError("GoldenDict CLI binary ('goldendict') not found")
  cmd = [gd_bin, query]
  subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  return {"ok": True, "word": query, "command": cmd}


_HTML_CACHE: dict[str, Any] = {"mtime_ns": None, "html": None}
_MODULE_LOAD_MTIME_NS = Path(__file__).stat().st_mtime_ns if os.path.isfile(__file__) else None


def _load_rendered_html() -> str:
  """Return _RENDERED_HTML, hot-reloading from disk if review_gui.py changed since import."""
  try:
    mtime_ns = Path(__file__).stat().st_mtime_ns
  except OSError:
    return _RENDERED_HTML
  if _MODULE_LOAD_MTIME_NS is not None and mtime_ns == _MODULE_LOAD_MTIME_NS:
    return _RENDERED_HTML
  if _HTML_CACHE["mtime_ns"] == mtime_ns and isinstance(_HTML_CACHE["html"], str):
    return _HTML_CACHE["html"]
  try:
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    for node in tree.body:
      if (
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_RENDERED_HTML" for t in node.targets)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
      ):
        _HTML_CACHE["mtime_ns"] = mtime_ns
        _HTML_CACHE["html"] = node.value.value
        return node.value.value
  except Exception:
    pass
  return _RENDERED_HTML


def create_app(
  initial_dir: str | None = None,
  *,
  default_n: int = 3,
  cache_dir: str = review.DEFAULT_REVIEW_CACHE_DIR,
) -> FastAPI:
  """Create the FastAPI application for interactive accent suggestion review."""
  app = FastAPI(title="Vedic Accent Fix Suggestion Reviewer")
  store = _SessionStore(initial_dir=initial_dir, default_n=default_n, cache_dir=cache_dir)

  @app.get("/", response_class=HTMLResponse)
  def index() -> HTMLResponse:
    return HTMLResponse(_load_rendered_html())

  @app.get("/api/presets")
  def api_presets() -> dict[str, Any]:
    presets = _discover_vedah_dirs()
    with store.lock:
      cur_dir = store.scan_data["dir_path"] if store.scan_data else (initial_dir or "")
    return {"presets": presets, "default_n": store.default_n, "current_dir": cur_dir}

  @app.post("/api/browse")
  def api_browse() -> dict[str, Any]:
    with store.lock:
      cur = store.scan_data["dir_path"] if store.scan_data else initial_dir
    picked = _pick_directory_dialog(cur)
    return {"dir_path": picked}

  @app.post("/api/scan")
  def api_scan(req: _ScanRequest) -> dict[str, Any]:
    try:
      store.load_dir(req.dir_path)
    except FileNotFoundError as exc:
      raise HTTPException(status_code=404, detail=str(exc)) from exc
    with store.lock:
      return store.summary_payload()

  @app.get("/api/items")
  def api_items(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    n_context: int = Query(3, ge=0, le=30),
    file_filter: str = Query(""),
    git_filter: str = Query("all"),  # "all" | "dirty" | "clean"
    status_filter: str = Query("all"),  # "all" | "pending" | "accept" | "hold" | "has_feedback"
    kind_filter: str = Query("all"),  # "all" | "both" | "pp_only" | "vp_only"
    search: str = Query(""),
  ) -> dict[str, Any]:
    with store.lock:
      summary = store.summary_payload()
      if store.scan_data is None:
        return {"summary": summary, "filtered_total": 0, "page": 1, "total_pages": 1, "items": []}

      decisions = store.state["decisions"]
      fstat = store.scan_data["file_statuses"]
      q = search.strip().lower()
      matched: list[review.ReviewItem] = []

      for it in store.scan_data["items"]:
        if file_filter and file_filter != it.display_file and file_filter not in it.relevant_files and file_filter not in it.host_files:
          continue
        if kind_filter != "all" and it.kind != kind_filter:
          continue
        dec = decisions.get(it.item_id, "pending")
        if status_filter == "has_feedback":
          if not it.existing_feedback:
            continue
        elif status_filter == "pending":
          if dec != "pending" or it.existing_feedback:
            continue
        elif status_filter != "all" and dec != status_filter:
          continue
        if git_filter != "all":
          any_dirty = any(fstat.get(rf, "clean") != "clean" for rf in it.relevant_files)
          if git_filter == "dirty" and not any_dirty:
            continue
          if git_filter == "clean" and any_dirty:
            continue
        if q:
          hay = "\n".join([
            it.rel_path,
            it.vp_file or "",
            it.pp_file or "",
            *it.host_files,
            it.vp_raw_body,
            it.pp_line_core,
            it.vp_sug_old,
            it.vp_sug_new,
            it.existing_feedback,
          ]).lower()
          if q not in hay:
            continue
        matched.append(it)

      total = len(matched)
      total_pages = max(1, (total + page_size - 1) // page_size)
      clamped_page = min(page, total_pages)
      s_idx = (clamped_page - 1) * page_size
      e_idx = min(total, s_idx + page_size)
      hugo_base = store.scan_data.get("hugo_base_dir")
      page_items = [
        it.to_dict(
          n_context=n_context,
          decision=decisions.get(it.item_id, "pending"),
          file_statuses=fstat,
          hugo_base_dir=hugo_base,
        )
        for it in matched[s_idx:e_idx]
      ]
      return {
        "summary": summary,
        "filtered_total": total,
        "page": clamped_page,
        "page_size": page_size,
        "total_pages": total_pages,
        "items": page_items,
      }

  @app.post("/api/submit")
  def api_submit(req: _SubmitRequest) -> dict[str, Any]:
    if req.decision not in ("pending", "accept", "reject"):
      raise HTTPException(status_code=400, detail=f"Invalid decision: {req.decision}")
    with store.lock:
      if store.scan_data is None or req.item_id not in store.item_by_id:
        raise HTTPException(status_code=404, detail="Review item not found")
      it = store.item_by_id[req.item_id]
      try:
        res = review.submit_review_item(
          it,
          decision=req.decision,
          feedback_text=req.feedback_text,
          sibling_items=store.scan_data["items"],
          target_mode=req.target_mode,
          sync_prefills=req.sync_prefills,
          hugo_base_dir=store.scan_data["hugo_base_dir"],
          scan_dir=store.scan_data["dir_path"],
        )
      except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

      store.state["decisions"][it.item_id] = req.decision
      for rf in it.relevant_files:
        store.state["file_overrides"].pop(rf, None)
      store.refresh_git_statuses()
      review.save_review_state(store.scan_data["dir_path"], store.state, cache_dir=store.cache_dir)
      it_dict = it.to_dict(
        decision=req.decision,
        file_statuses=store.scan_data["file_statuses"],
        hugo_base_dir=store.scan_data["hugo_base_dir"],
      )
      return {
        **res,
        "item": it_dict,
        "summary": store.summary_payload(),
      }

  @app.post("/api/decision")
  def api_decision(req: _DecisionRequest) -> dict[str, Any]:
    if req.decision not in ("pending", "accept", "reject"):
      raise HTTPException(status_code=400, detail=f"Invalid decision: {req.decision}")
    with store.lock:
      if store.scan_data is None or req.item_id not in store.item_by_id:
        raise HTTPException(status_code=404, detail="Review item not found")
      it = store.item_by_id[req.item_id]
      try:
        res = review.apply_vp_decision_for_item(
          it,
          req.decision,
          sibling_items=store.scan_data["items"],
          sync_prefills=req.sync_prefills,
          hugo_base_dir=store.scan_data["hugo_base_dir"],
          scan_dir=store.scan_data["dir_path"],
        )
      except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
      store.state["decisions"][req.item_id] = req.decision
      for rf in it.relevant_files:
        store.state["file_overrides"].pop(rf, None)
      store.refresh_git_statuses()
      review.save_review_state(store.scan_data["dir_path"], store.state, cache_dir=store.cache_dir)
      return {
        **res,
        "summary": store.summary_payload(),
        "item_id": req.item_id,
        "decision": req.decision,
      }

  @app.post("/api/file_accept_all")
  def api_file_accept_all(req: _FileAcceptRequest) -> dict[str, Any]:
    if req.decision not in ("pending", "accept", "reject"):
      raise HTTPException(status_code=400, detail=f"Invalid decision: {req.decision}")
    norm_f = os.path.normpath(req.file_path)
    with store.lock:
      if store.scan_data is None:
        raise HTTPException(status_code=400, detail="No directory loaded")
      touched = 0
      previous_states: list[dict[str, str]] = []
      for it in store.scan_data["items"]:
        if norm_f == it.display_file or norm_f in it.relevant_files:
          prev_dec = store.state["decisions"].get(it.item_id, "pending")
          previous_states.append({"item_id": it.item_id, "decision": prev_dec})
          review.apply_vp_decision_for_item(
            it,
            req.decision,
            sibling_items=store.scan_data["items"],
            sync_prefills=req.sync_prefills,
            hugo_base_dir=store.scan_data["hugo_base_dir"],
            scan_dir=store.scan_data["dir_path"],
          )
          store.state["decisions"][it.item_id] = req.decision
          for rf in it.relevant_files:
            store.state["file_overrides"].pop(rf, None)
          touched += 1
      store.refresh_git_statuses()
      review.save_review_state(store.scan_data["dir_path"], store.state, cache_dir=store.cache_dir)
      return {"touched": touched, "previous_states": previous_states, "summary": store.summary_payload()}

  @app.post("/api/file_mark")
  def api_file_mark(req: _FileMarkRequest) -> dict[str, Any]:
    norm_f = os.path.normpath(req.file_path)
    with store.lock:
      if store.scan_data is None:
        raise HTTPException(status_code=400, detail="No directory loaded")
      if req.marked is None:
        store.state["file_overrides"].pop(norm_f, None)
      else:
        store.state["file_overrides"][norm_f] = bool(req.marked)
      review.save_review_state(store.scan_data["dir_path"], store.state, cache_dir=store.cache_dir)
      return {"summary": store.summary_payload()}

  @app.post("/api/feedback")
  def api_feedback(req: _FeedbackRequest) -> dict[str, Any]:
    with store.lock:
      if store.scan_data is None or req.item_id not in store.item_by_id:
        raise HTTPException(status_code=404, detail="Review item not found")
      it = store.item_by_id[req.item_id]
      try:
        res = review.insert_feedback_for_item(
          it,
          req.feedback_text,
          target_mode=req.target_mode,
          sibling_items=store.scan_data["items"],
          sync_prefills=req.sync_prefills,
          hugo_base_dir=store.scan_data["hugo_base_dir"],
          scan_dir=store.scan_data["dir_path"],
        )
      except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

      if res["comment"] and store.state["decisions"].get(it.item_id, "pending") == "pending":
        pass  # keep Accept/Reject independent of Feedback; feedback pill shows has_feedback
      for rf in it.relevant_files:
        store.state["file_overrides"].pop(rf, None)
      store.refresh_git_statuses()
      review.save_review_state(store.scan_data["dir_path"], store.state, cache_dir=store.cache_dir)
      return {
        **res,
        "decision": store.state["decisions"].get(it.item_id, "pending"),
        "summary": store.summary_payload(),
      }

  @app.get("/api/diff")
  def api_diff(file_path: str = Query(...)) -> dict[str, Any]:
    if not os.path.isfile(file_path):
      raise HTTPException(status_code=404, detail=f"File not found: {file_path}")
    return {"file_path": file_path, "diff": review.get_file_diff(file_path)}

  def _open_editor_response(opener: Any, req: _OpenIdeaRequest) -> dict[str, Any]:
    try:
      return opener(req.file_path, line=req.line, needle=req.needle)
    except FileNotFoundError as exc:
      raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
      raise HTTPException(status_code=500, detail=str(exc)) from exc

  @app.post("/api/open_in_idea")
  def api_open_in_idea(req: _OpenIdeaRequest) -> dict[str, Any]:
    return _open_editor_response(open_in_intellij, req)

  @app.post("/api/open_in_vscode")
  def api_open_in_vscode(req: _OpenIdeaRequest) -> dict[str, Any]:
    return _open_editor_response(open_in_vscode, req)

  @app.post("/api/open_in_goldendict")
  def api_open_in_goldendict(req: _OpenGoldenDictRequest) -> dict[str, Any]:
    try:
      return open_in_goldendict(req.word)
    except ValueError as exc:
      raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
      raise HTTPException(status_code=500, detail=str(exc)) from exc

  @app.post("/api/commit")
  def api_commit(req: _CommitRequest) -> dict[str, Any]:
    if not req.file_paths:
      raise HTTPException(status_code=400, detail="No files selected for committing")
    with store.lock:
      if store.scan_data is None:
        raise HTTPException(status_code=400, detail="No directory loaded")
      try:
        results = review.commit_marked_files(req.file_paths, req.commit_message)
      except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
      store.refresh_git_statuses()
      for fp in req.file_paths:
        store.state["file_overrides"].pop(os.path.normpath(fp), None)
      review.save_review_state(store.scan_data["dir_path"], store.state, cache_dir=store.cache_dir)
      return {"results": results, "summary": store.summary_payload()}

  return app


def _find_free_port(preferred: int = 8765) -> int:
  for port in [preferred, *range(8766, 8790), 0]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
      try:
        s.bind(("127.0.0.1", port))
        return s.getsockname()[1]
      except OSError:
        continue
  return 8765


def _launch_browser(url: str) -> None:
  time.sleep(0.6)
  chrome = shutil.which("google-chrome") or shutil.which("chromium")
  if chrome and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    try:
      subprocess.Popen(
        [chrome, f"--app={url}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
      )
      return
    except Exception:
      pass
  webbrowser.open(url)


def main(argv: Sequence[str] | None = None) -> int:
  p = argparse.ArgumentParser(
    description="Launch the Vedic accent fix suggestion reviewer GUI."
  )
  p.add_argument("--dir", default=None, help="Initial directory to scan for accent fix suggestions.")
  p.add_argument("-n", "--context-lines", type=int, default=3, help="Number of preceding/succeeding padapāṭha lines to show (default: 3).")
  p.add_argument("--port", type=int, default=8765, help="Port to bind on 127.0.0.1 (default: 8765 or next free port).")
  p.add_argument("--no-browser", action="store_true", help="Do not auto-open browser window.")
  args = p.parse_args(argv)

  app = create_app(initial_dir=args.dir, default_n=args.context_lines)
  port = _find_free_port(args.port)
  url = f"http://127.0.0.1:{port}/"
  print(f"Vedic Accent Fix Reviewer running at {url}", flush=True)
  if not args.no_browser:
    threading.Thread(target=_launch_browser, args=(url,), daemon=True).start()
  uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
  return 0


_RENDERED_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Vedic Accent Fix Reviewer</title>
<style>
  :root {
    --bg: #f6f4ef;
    --surface: #ffffff;
    --surface-2: #fbfaf7;
    --border: #ddd6c7;
    --text: #1f1c18;
    --muted: #6a6257;
    --accent: #8b4513;
    --accent-soft: #fdf3e7;
    --green: #186a3b;
    --green-soft: #eafaf1;
    --amber: #9c5700;
    --amber-soft: #fff8e1;
    --red: #943126;
    --red-soft: #fdedec;
    --blue: #1b4f72;
    --blue-soft: #ebf5fb;
    --dev: "Adishila SanVedic", "Tiro Devanagari Sanskrit", "Noto Sans Devanagari", "Annapurna SIL", serif;
    --dev-font-size: 26px;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    font-size: 14px;
    line-height: 1.45;
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
  }
  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 5px 10px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex-shrink: 0;
    font-size: 12px;
    line-height: 1.25;
  }
  .row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  header .row { gap: 5px; }
  header label {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    font-size: 12px;
    white-space: nowrap;
  }
  input[type="text"], input[type="number"], select {
    padding: 6px 9px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: #fff;
    color: var(--text);
    font-size: 13px;
  }
  header input[type="text"], header input[type="number"], header select {
    padding: 2px 6px;
    font-size: 12px;
    border-radius: 5px;
  }
  #dirInput { flex: 1; min-width: 220px; font-family: monospace; }
  button {
    padding: 6px 12px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--surface-2);
    color: var(--text);
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
  }
  button:hover { background: #efeae0; }
  button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  button.primary:hover { background: #70360d; }
  button.green { background: var(--green); color: #fff; border-color: var(--green); }
  button.green:hover { background: #11502c; }
  button.small { padding: 3px 8px; font-size: 12px; }
  header button { padding: 3px 7px; font-size: 12px; border-radius: 5px; white-space: nowrap; }
  header button.small { padding: 2px 6px; font-size: 11.5px; }
  header .kbd-hint { font-size: 9.5px; padding: 0 3px; }
  header #scanSpinner {
    max-width: 240px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  header #scanSpinner:empty { display: none; }

  .stat-pill {
    padding: 3px 9px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    border: 1px solid var(--border);
    background: var(--surface-2);
  }
  button.filter-pill {
    padding: 3px 9px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: box-shadow 0.12s, filter 0.12s;
  }
  header .stat-pill, header button.filter-pill {
    padding: 2px 7px;
    font-size: 11.5px;
    white-space: nowrap;
  }
  button.filter-pill:hover { filter: brightness(0.96); }
  button.filter-pill.active { box-shadow: 0 0 0 2px var(--accent); }
  u.sc-key {
    text-decoration: underline dotted currentColor;
    text-underline-offset: 2px;
    text-decoration-thickness: 1.2px;
    border-radius: 2px;
  }
  body.mod-alt-down:not(.mod-ctrl-down) u.sc-key.sc-alt,
  body.mod-ctrl-down:not(.mod-alt-down) u.sc-key.sc-ctrl,
  body.mod-alt-down.mod-ctrl-down u.sc-key {
    text-decoration: underline solid currentColor;
    text-decoration-thickness: 2.5px;
    background: rgba(139, 69, 19, 0.16);
    font-weight: 800;
    padding: 0 1px;
  }
  button.primary u.sc-key {
    text-decoration-color: rgba(255, 255, 255, 0.85);
  }
  body.mod-alt-down button.primary u.sc-key.sc-alt,
  body.mod-ctrl-down button.primary u.sc-key.sc-ctrl {
    background: rgba(255, 255, 255, 0.28);
    text-decoration-color: #ffffff;
  }
  body.mod-alt-down:not(.mod-ctrl-down) u.sc-key.sc-ctrl:not(.sc-alt),
  body.mod-ctrl-down:not(.mod-alt-down) u.sc-key.sc-alt:not(.sc-ctrl) {
    text-decoration: none;
  }
  .pill-accept { background: var(--green-soft); color: var(--green); border-color: #a9dfbf; }
  .pill-hold { background: var(--amber-soft); color: var(--amber); border-color: #f9e79f; }
  .pill-pending { background: var(--surface-2); color: var(--muted); }
  .pill-dirty { background: #fef5e7; color: #b9770e; border-color: #f5cba7; }
  .pill-clean { background: #f4f6f7; color: #7f8c8d; border-color: #d5dbdb; }

  .workspace {
    display: grid;
    grid-template-columns: 290px minmax(0, 1fr) 300px;
    flex: 1;
    min-height: 0;
  }
  aside.sidebar, aside.commit-pane {
    background: var(--surface);
    border-right: 1px solid var(--border);
    min-height: 0;
    overflow: hidden;
    padding: 10px 0 10px 10px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  aside.sidebar > :not(.pane-scroll),
  aside.commit-pane > :not(.pane-scroll) {
    margin-right: 10px;
  }
  aside.commit-pane {
    border-right: none;
    border-left: 1px solid var(--border);
    overflow-y: auto;
  }
  .pane-scroll {
    flex: 1;
    min-height: 0;
    overflow-y: scroll;
    scrollbar-gutter: stable;
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding-right: 4px;
  }
  main {
    min-width: 0;
    min-height: 0;
    overflow-y: scroll;
    scrollbar-gutter: stable;
    padding: 14px 18px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  main:focus { outline: none; }
  main::-webkit-scrollbar,
  .pane-scroll::-webkit-scrollbar,
  aside.commit-pane::-webkit-scrollbar {
    width: 14px;
    height: 14px;
  }
  main::-webkit-scrollbar-track,
  .pane-scroll::-webkit-scrollbar-track,
  aside.commit-pane::-webkit-scrollbar-track {
    background: var(--surface-2);
    border-left: 1px solid var(--border);
  }
  main::-webkit-scrollbar-thumb,
  .pane-scroll::-webkit-scrollbar-thumb,
  aside.commit-pane::-webkit-scrollbar-thumb {
    background: #a58f74;
    border-radius: 7px;
    border: 2px solid var(--surface-2);
    min-height: 36px;
  }
  main::-webkit-scrollbar-thumb:hover,
  .pane-scroll::-webkit-scrollbar-thumb:hover,
  aside.commit-pane::-webkit-scrollbar-thumb:hover {
    background: var(--accent);
  }
  main::-webkit-scrollbar-thumb:active,
  .pane-scroll::-webkit-scrollbar-thumb:active,
  aside.commit-pane::-webkit-scrollbar-thumb:active {
    background: #6c340d;
  }
  @supports not selector(::-webkit-scrollbar) {
    main,
    .pane-scroll,
    aside.commit-pane {
      scrollbar-width: auto;
      scrollbar-color: #a58f74 var(--surface-2);
    }
  }
  .pane-title {
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--muted);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .file-item {
    padding: 6px 8px;
    border-radius: 6px;
    border: 1px solid transparent;
    cursor: pointer;
    font-size: 11px;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .file-item:hover { background: var(--surface-2); border-color: var(--border); }
  .file-item.active {
    background: var(--accent-soft);
    border-color: var(--accent);
    box-shadow: inset 4px 0 0 var(--accent);
    font-weight: 700;
  }
  .file-tree-dir { margin: 1px 0; }
  .file-tree-dir > summary {
    list-style: none;
    display: flex;
    align-items: baseline;
    gap: 4px;
    padding: 4px 6px;
    border-radius: 6px;
    cursor: pointer;
    font-family: monospace;
    font-size: 11px;
    font-weight: 600;
    color: var(--muted);
    word-break: break-all;
  }
  .file-tree-dir > summary::-webkit-details-marker { display: none; }
  .file-tree-dir > summary:hover { background: var(--surface-2); color: var(--fg); }
  .file-tree-root > summary {
    background: var(--surface-2);
    border: 1px solid var(--border);
    color: var(--accent);
    font-weight: 700;
  }
  .file-tree-caret {
    flex-shrink: 0;
    display: inline-block;
    width: 11px;
    font-size: 10px;
    color: var(--muted);
    transition: transform 0.1s ease;
  }
  .file-tree-dir[open] > summary .file-tree-caret { transform: rotate(90deg); }
  .file-tree-count {
    margin-left: auto;
    font-size: 10px;
    color: var(--muted);
    font-weight: 500;
    flex-shrink: 0;
  }
  .file-tree-children {
    margin-left: 10px;
    padding-left: 6px;
    border-left: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .file-item-head { display: flex; align-items: baseline; gap: 5px; flex-wrap: wrap; }
  .file-name { font-family: monospace; word-break: break-all; font-weight: 600; font-size: 11px; }
  .file-meta { color: var(--muted); font-size: 11px; display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 16px;
    box-shadow: 0 1px 2px rgba(0,0,0,0.04);
    display: flex;
    flex-direction: column;
    gap: 10px;
    overflow-wrap: anywhere;
    transition: background 0.12s ease, box-shadow 0.12s ease, border-color 0.12s ease;
  }
  .card.dec-accept { border-left: 5px solid var(--green); }
  .card.dec-reject { border-left: 5px solid var(--red); }
  .card.dec-pending { border-left: 5px solid #d5cfc2; }
  .card.focused {
    background: var(--accent-soft);
    border-top-color: var(--accent);
    border-right-color: var(--accent);
    border-bottom-color: var(--accent);
    outline: 3px solid var(--accent);
    box-shadow: 0 0 0 5px rgba(139, 69, 19, 0.16), 0 6px 18px rgba(139, 69, 19, 0.14);
  }
  .card.focused.dec-pending { border-left-color: var(--accent); }
  .card.focused .card-head { border-bottom-color: #e4c8ab; }
  .card.focused .vp-box,
  .card.focused .pp-box { border-color: #d5b28f; }
  .active-badge {
    display: none;
    align-items: center;
    padding: 1px 7px;
    border-radius: 999px;
    background: var(--accent);
    color: #fff;
    font-family: system-ui, sans-serif;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    margin-right: 6px;
    vertical-align: middle;
  }
  .card.focused .active-badge { display: inline-flex; }
  .card.collapsed { padding: 8px 14px; gap: 0; opacity: 0.88; cursor: pointer; }
  .card.collapsed:hover,
  .card.focused.collapsed { opacity: 1; }
  .card.collapsed .card-head { border-bottom: none; padding-bottom: 0; align-items: center; }
  .card.collapsed .path-row,
  .card.collapsed .mula-details,
  .card.collapsed > :not(.card-head) { display: none; }
  .collapse-summary {
    display: none;
    align-items: center;
    gap: 8px;
    margin-left: 8px;
    font-size: 12px;
  }
  .card.collapsed .collapse-summary { display: inline-flex; flex-wrap: wrap; }
  .collapse-correction {
    font-family: var(--dev);
    font-size: var(--dev-font-size);
    line-height: 1.35;
    display: inline-flex;
    align-items: baseline;
    gap: 5px;
    flex-wrap: wrap;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 1px 8px;
  }
  .collapse-correction .old-txt { color: var(--red); text-decoration: line-through; opacity: 0.85; }
  .collapse-correction .arrow { color: var(--muted); font-size: calc(var(--dev-font-size) - 4px); }
  .collapse-correction .new-txt { font-weight: 700; color: var(--green); }
  .collapse-btn {
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--muted);
    border-radius: 5px;
    padding: 2px 8px;
    font-size: 11px;
    cursor: pointer;
    flex-shrink: 0;
  }
  .collapse-btn:hover { background: #ebe4d5; color: var(--fg); }

  .card-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 10px;
    border-bottom: 1px solid #efeae0;
    padding-bottom: 8px;
  }
  .card-title { font-family: monospace; font-size: 12px; color: var(--blue); font-weight: 600; word-break: break-all; }
  .file-link {
    color: inherit;
    text-decoration: underline dotted rgba(31, 95, 168, 0.55);
    text-underline-offset: 2px;
    cursor: pointer;
  }
  .file-link:hover {
    color: var(--blue);
    text-decoration: underline solid var(--blue);
  }
  .vscode-link {
    color: var(--muted);
    font-family: monospace;
    font-size: 10px;
    font-weight: 600;
    text-decoration: none;
    flex-shrink: 0;
  }
  .vscode-link:hover {
    color: var(--blue);
    text-decoration: underline solid var(--blue);
  }
  .card-section { font-family: var(--dev); font-size: calc(var(--dev-font-size) - 4px); color: var(--muted); margin-left: 8px; }
  .path-row { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin-top: 4px; font-family: monospace; font-size: 11px; }

  .vp-box {
    background: var(--surface-2);
    border: 1px solid #e6dfd1;
    border-radius: 8px;
    padding: 12px 16px;
    font-family: var(--dev);
    font-size: var(--dev-font-size);
    line-height: 1.65;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .vp-label {
    font-family: system-ui, sans-serif;
    font-size: 11px;
    font-weight: 700;
    color: var(--muted);
    text-transform: uppercase;
    margin-bottom: 4px;
    display: flex;
    justify-content: space-between;
  }
  .sug-focus {
    background: #fff0b3;
    border: 1.5px solid #d48806;
    border-radius: 5px;
    padding: 1px 6px;
  }
  .sug-focus .old-txt {
    text-decoration: line-through;
    color: var(--red);
    opacity: 0.85;
    margin-right: 4px;
  }
  .sug-focus .arrow { color: var(--muted); margin: 0 3px; font-size: calc(var(--dev-font-size) - 4px); }
  .sug-focus .new-txt { font-weight: 700; color: var(--green); }
  .sug-other {
    background: var(--blue-soft);
    border: 1px solid #aed6f1;
    border-radius: 4px;
    padding: 0 4px;
  }
  .sug-other .old-txt { text-decoration: line-through; color: #7f8c8d; margin-right: 3px; }
  .sug-other .new-txt { font-weight: 600; color: var(--blue); }
  .plain-focus {
    background: #fff0b3;
    border-bottom: 2px solid #d48806;
    border-radius: 3px;
    padding: 0 3px;
  }
  .accepted-focus {
    background: var(--green-soft);
    border: 1.5px solid #2e8b57;
    color: var(--green);
    font-weight: 700;
    border-radius: 5px;
    padding: 1px 6px;
  }
  .rejected-focus {
    background: var(--red-soft);
    border: 1.5px solid #c0392b;
    color: var(--red);
    font-weight: 700;
    border-radius: 5px;
    padding: 1px 6px;
  }
  .inline-note {
    font-size: 80%;
    color: rgb(114, 47, 55);
    font-style: italic;
    vertical-align: sub;
  }
  .inline-feedback-tag {
    display: inline-block;
    background: var(--amber-soft);
    color: var(--amber);
    border: 1px solid #f5b041;
    border-radius: 4px;
    padding: 1px 7px;
    font-size: calc(var(--dev-font-size) - 8px);
    font-family: system-ui, sans-serif;
    margin-left: 6px;
  }

  .mula-box {
    background: #faf6ee;
    border: 1px dashed #d5c8b4;
    border-radius: 6px;
    padding: 8px 12px;
    font-family: var(--dev);
    font-size: calc(var(--dev-font-size) - 3px);
    line-height: 1.55;
    white-space: pre-wrap;
    margin-top: 4px;
  }

  .pp-box {
    border: 1px solid #e2ddd2;
    border-radius: 8px;
    overflow: hidden;
  }
  .pp-header {
    background: #f0ece3;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 600;
    color: #4f483f;
    display: flex;
    justify-content: space-between;
  }
  .pp-line {
    padding: 7px 14px;
    font-family: var(--dev);
    font-size: calc(var(--dev-font-size) - 2px);
    line-height: 1.55;
    border-top: 1px solid #f2eee6;
    overflow-wrap: anywhere;
  }
  .pp-line.ctx { color: #5f584e; background: #fdfcf9; }
  .pp-line.target {
    background: #fff9e6;
    border-left: 4px solid #d48806;
    font-size: var(--dev-font-size);
    line-height: 1.6;
    color: #111;
    padding: 10px 14px;
  }
  .pp-tag { color: #6c3483; font-style: italic; font-size: calc(var(--dev-font-size) - 4px); }
  .pp-arrow-word { color: var(--green); font-weight: 700; }
  .gd-inline {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 5px;
    margin-left: 10px;
    padding: 1px 7px;
    background: rgba(31, 95, 168, 0.08);
    border: 1px solid rgba(31, 95, 168, 0.22);
    border-radius: 5px;
    font-family: system-ui, sans-serif;
    font-size: 12px;
    color: var(--blue);
    vertical-align: middle;
  }
  .gd-link { font-family: var(--dev); font-size: calc(var(--dev-font-size) - 6px); color: var(--blue); }
  .sutra-link {
    color: var(--blue);
    text-decoration: none;
    border-bottom: 1px dotted rgba(27, 79, 114, 0.45);
  }
  .sutra-link:hover { text-decoration: underline; color: var(--accent); }

  .controls-bar {
    display: flex;
    flex-direction: column;
    gap: 0;
    align-items: stretch;
    padding-top: 2px;
  }
  .decision-row {
    display: flex;
    gap: 10px;
    align-items: center;
    margin: 0;
    padding: 0;
  }
  .radio-group {
    display: inline-flex;
    border: 1px solid var(--border);
    border-bottom: none;
    border-radius: 6px 6px 0 0;
    overflow: hidden;
    background: var(--surface-2);
  }
  .radio-group label {
    padding: 4px 11px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    border-right: 1px solid var(--border);
    user-select: none;
  }
  .radio-group label:last-child { border-right: none; }
  .radio-group label.sel-accept { background: var(--green-soft); color: var(--green); font-weight: 700; }
  .radio-group label.sel-reject { background: var(--red-soft); color: var(--red); font-weight: 700; }
  .radio-group label.sel-pending { background: #ede9df; color: var(--text); }
  .kbd-hint {
    font-size: 10px;
    font-family: monospace;
    color: var(--muted);
    background: rgba(0,0,0,0.05);
    padding: 1px 4px;
    border-radius: 3px;
  }

  .fb-row {
    display: flex;
    gap: 8px;
    align-items: center;
    margin: 0;
    padding: 0;
  }
  .fb-input {
    flex: 1;
    padding: 6px 10px;
    border: 1px solid var(--border);
    border-radius: 0 6px 6px 6px;
    font-size: 13px;
    font-family: system-ui, sans-serif;
  }
  .submit-row {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-top: 6px;
  }
  .fb-status { font-size: 11px; color: var(--green); font-weight: 600; }

  .commit-file-row {
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 7px 8px;
    font-size: 12px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    background: var(--surface-2);
  }
  .commit-file-row.marked { background: var(--green-soft); border-color: #a9dfbf; }
  .commit-box {
    margin-top: auto;
    border-top: 1px solid var(--border);
    padding-top: 10px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  textarea#commitMsg {
    width: 100%;
    min-height: 54px;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 8px;
    font-size: 12px;
    resize: vertical;
  }
  .pager {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 0;
  }
  dialog {
    border: 1px solid var(--border);
    border-radius: 10px;
    max-width: 900px;
    width: 90vw;
    max-height: 85vh;
    padding: 16px;
    box-shadow: 0 12px 32px rgba(0,0,0,0.22);
  }
  pre.diff-pre {
    background: #1e1e1e;
    color: #e8e8e8;
    padding: 12px;
    border-radius: 6px;
    overflow: auto;
    max-height: 62vh;
    font-size: 12px;
    line-height: 1.45;
  }
  .help-drawer {
    position: fixed;
    top: 78px;
    right: 18px;
    width: 680px;
    max-width: calc(100vw - 28px);
    max-height: calc(100vh - 96px);
    overflow-y: auto;
    background: var(--surface);
    border: 1px solid var(--border);
    border-top: 4px solid var(--accent);
    border-radius: 10px;
    box-shadow: 0 14px 36px rgba(31, 28, 24, 0.22);
    padding: 14px 16px;
    z-index: 1200;
    display: none;
    flex-direction: column;
    gap: 10px;
  }
  #shortcutFooter { line-height: 1.65; }
  #shortcutFooter strong { color: var(--fg); font-weight: 700; }
</style>
</head>
<body>
<header>
  <div class="row">
    <input id="dirInput" type="text" list="dirPresets" placeholder="/home/vvasuki/gitland/vishvAsa/vedAH_yajuH/static" />
    <datalist id="dirPresets"></datalist>
    <button onclick="browseDir()">Browse</button>
    <button class="primary" onclick="scanDirectory()">Scan</button>
    <button type="button" class="stat-pill filter-pill" id="statTotal" data-status-filter="all" title="Show all matching suggestions (Alt+L / Alt+0)" onclick="setStatusFilter('all')">A<u class="sc-key sc-alt">l</u>l: 0 <span class="kbd-hint">Alt+L</span></button>
    <button type="button" class="stat-pill filter-pill pill-pending active" id="statPending" data-status-filter="pending" title="Show only pending suggestions without !! (Alt+U)" onclick="setStatusFilter('pending')">Pend: 0 <span class="kbd-hint">Alt+U</span></button>
    <button type="button" class="stat-pill filter-pill pill-accept" id="statAccept" data-status-filter="accept" title="Show only accepted suggestions (Alt+A)" onclick="setStatusFilter('accept')"><u class="sc-key sc-alt">A</u>cc: 0 <span class="kbd-hint">Alt+A</span></button>
    <button type="button" class="stat-pill filter-pill" id="statReject" data-status-filter="reject" title="Show only rejected suggestions (Alt+R)" style="background:var(--red-soft); color:var(--red); border-color:#f5b7b1;" onclick="setStatusFilter('reject')"><u class="sc-key sc-alt">R</u>ej: 0 <span class="kbd-hint">Alt+R</span></button>
    <button type="button" class="stat-pill filter-pill pill-hold" id="statHold" data-status-filter="has_feedback" title="Show only suggestions with !! comments (Alt+W)" onclick="setStatusFilter('has_feedback')">!!: 0 <span class="kbd-hint">Alt+W</span></button>
  </div>
  <div class="row">
    <label title="Context lines (n)">Ctx: <input id="nInput" type="number" min="0" max="30" value="3" style="width:46px" onchange="loadItems(1)" /></label>
    <button id="refreshBtn" class="small" title="Reload UI (Ctrl+R rescans directory)" onclick="reloadUi()">↻ <u class="sc-key sc-ctrl">R</u>eload</button>
    <label title="Editor font size in px — matches VS Code (25) / IntelliJ IDEA (28)">Font: <input id="fontPxInput" type="number" min="14" max="42" value="26" style="width:46px" onchange="applyEditorFont(this.value)" /></label>
    <label>Git:
      <select id="gitFilter" onchange="loadItems(1)">
        <option value="all">All</option>
        <option value="dirty">Dirty</option>
        <option value="clean">Clean</option>
      </select>
    </label>
    <label>State:
      <select id="statusFilter" onchange="loadItems(1)">
        <option value="all">All</option>
        <option value="pending" selected>Pending</option>
        <option value="accept">Accepted</option>
        <option value="reject">Rejected</option>
        <option value="has_feedback">With !!</option>
      </select>
    </label>
    <label>Pair:
      <select id="kindFilter" onchange="loadItems(1)">
        <option value="all">All VP</option>
        <option value="both">Paired</option>
        <option value="vp_only">Unpaired</option>
      </select>
    </label>
    <input id="searchInput" type="text" placeholder="Filter..." style="width:100px;" oninput="debouncedSearch()" />
    <button id="undoBtn" class="small" title="Undo last review action (Ctrl+Z)" onclick="undoLastAction()" disabled>↶ Undo</button>
    <button id="helpBtn" class="small" type="button" title="About &amp; keyboard shortcuts (?)" onclick="toggleHelpPane()">?</button>
    <span id="scanSpinner" style="color:var(--muted); font-size:11.5px;"></span>
  </div>
</header>
<div id="helpPane" class="help-drawer" style="display:none;">
  <div class="row" style="justify-content:space-between;">
    <strong style="font-size:15px;">Vedic Accent Fix Reviewer (pada_svara)</strong>
    <button id="closeHelpBtn" class="small" type="button" onclick="closeHelpPane()">✕ Close</button>
  </div>
  <div id="shortcutFooter" style="color:var(--muted); font-size:12px;"><strong>Shortcuts:</strong> <strong>Hold Alt/Ctrl</strong> Highlight button shortcut letters • <strong>Alt+U</strong> Pending • <strong>Alt+A</strong> Accepted • <strong>Alt+R</strong> Rejected • <strong>Alt+W</strong> With !! • <strong>Alt+L</strong>/<strong>0</strong> All • <strong>Alt+1</strong> Accept • <strong>Alt+2</strong> Reject • <strong>Alt+3</strong> Feedback • <strong>Enter</strong> Submit • <strong>Ctrl+Enter</strong> Accept+Submit • <strong>Ctrl+R</strong> Rescan • <strong>Ctrl+Z</strong> Undo • <strong>j</strong>/<strong>k</strong>/<strong>↑</strong>/<strong>↓</strong> navigate • <strong>←</strong>/<strong>→</strong> Collapse/Expand • <strong>Shift+PgUp</strong>/<strong>PgDn</strong> Prev/Next file group • <strong>Ctrl+PgUp</strong>/<strong>PgDn</strong> Prev/Next file • <strong>?</strong> About/Shortcuts • <strong>Esc</strong> Close help</div>
  <div id="aboutFooter" style="color:var(--muted); font-size:12px; border-top:1px solid var(--border); padding-top:8px;">
    <div><strong>pada_svara</strong> (<a href="https://github.com/sanskrit-coders/diff_san/tree/master/vishvAsa/pada_svara" target="_blank" rel="noopener noreferrer">github.com/sanskrit-coders/diff_san</a>) — FastAPI review GUI and scanner for Vedic <code>पद-पाठः</code> and <code>विश्वास-प्रस्तुतिः</code> accent fix suggestions with scoped <code>git commit --only</code> support.</div>
  </div>
</div>

<div class="workspace">
  <aside class="sidebar">
    <div class="pane-title">
      <span>Files (<span id="fileCount">0</span>)</span>
      <button class="small" onclick="selectFileFilter('')">Show All</button>
    </div>
    <div id="fileList" class="pane-scroll"></div>
  </aside>

  <main id="mainPane" tabindex="-1">
    <div class="pager">
      <div id="pageInfo" style="color:var(--muted); font-size:12px;">Enter a directory above and click Scan.</div>
      <div class="row">
        <button class="small" onclick="changePage(-1)">← Prev</button>
        <span id="pageLabel" style="font-size:12px;">1 / 1</span>
        <button class="small" onclick="changePage(1)">Next →</button>
      </div>
    </div>
    <div id="cardsContainer"></div>
    <div class="pager" style="justify-content:flex-end;">
      <div class="row">
        <button class="small" onclick="changePage(-1)">← Prev</button>
        <span id="pageLabelBottom" style="font-size:12px;">1 / 1</span>
        <button class="small" onclick="changePage(1)">Next →</button>
      </div>
    </div>
  </main>

  <aside class="commit-pane">
    <div class="pane-title">
      <span>Relevant Files for Commit</span>
      <span class="stat-pill pill-accept" id="markedCountPill">0 marked</span>
    </div>
    <div style="font-size:11px; color:var(--muted);">
      Accepting suggestions marks uncommitted files below (only checked dirty files are committed via scoped <code>git commit --only</code>).
    </div>
    <div id="markedFilesList" class="pane-scroll" style="gap:6px; min-height:80px;"></div>
    <div class="commit-box">
      <label style="font-size:12px; font-weight:600;">Commit message:
        <textarea id="commitMsg">Review accepted accent fix suggestions</textarea>
      </label>
      <label style="font-size:12px; display:flex; align-items:center; gap:6px;">
        <input type="checkbox" id="syncPrefillsChk" checked /> Refresh prefilled js_includes on edit
      </label>
      <button class="green" onclick="commitMarked()">Commit Marked Files</button>
      <div id="commitResult" style="font-size:12px; white-space:pre-wrap;"></div>
    </div>
  </aside>
</div>

<dialog id="diffDialog">
  <div class="row" style="justify-content:space-between; margin-bottom:8px;">
    <strong id="diffTitle" style="font-family:monospace; font-size:13px;"></strong>
    <button class="small" onclick="document.getElementById('diffDialog').close()">Close</button>
  </div>
  <pre class="diff-pre" id="diffBody"></pre>
</dialog>

<script>
let currentDir = "";
let selectedFile = "";
let currentPage = 1;
let totalPages = 1;
let lastFilteredTotal = 0;
let lastPageSize = 25;
let currentItems = [];
let draftDecisions = {};
let collapsedCards = {};
let latestSummary = null;
let focusedIdx = -1;
let searchTimer = null;
let undoStack = [];
let _recordingInFlight = 0;

function updateUndoButton() {
  const btn = document.getElementById("undoBtn");
  if (!btn) return;
  btn.disabled = !undoStack.length;
  const top = undoStack[undoStack.length - 1];
  btn.title = top ? `Undo ${top.label || "last action"} (Ctrl+Z)` : "Undo last review action (Ctrl+Z)";
}

function setStatus(msg) {
  const sp = typeof document !== "undefined" ? document.getElementById("scanSpinner") : null;
  if (!sp) return;
  sp.textContent = msg || "";
  sp.title = msg || "";
}

function flashStatus(msg) {
  if (!msg) return;
  setStatus(msg);
  setTimeout(() => {
    const sp = typeof document !== "undefined" ? document.getElementById("scanSpinner") : null;
    if (sp && sp.textContent === msg) setStatus("");
  }, 2500);
}

async function _withRecordingStatus(fn) {
  _recordingInFlight += 1;
  setStatus("⏳ Recording...");
  try {
    return await fn();
  } finally {
    _recordingInFlight = Math.max(0, _recordingInFlight - 1);
    if (_recordingInFlight === 0) {
      const sp = typeof document !== "undefined" ? document.getElementById("scanSpinner") : null;
      if (sp && sp.textContent === "⏳ Recording...") setStatus("");
    }
  }
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

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function debouncedSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadItems(1), 220);
}

function applyEditorFont(pxVal) {
  const px = Math.max(14, Math.min(42, Number(pxVal) || 26));
  document.documentElement.style.setProperty("--dev-font-size", `${px}px`);
  const inp = document.getElementById("fontPxInput");
  if (inp) inp.value = px;
  try { localStorage.setItem("accent_review_font_px", String(px)); } catch (_) {}
}

async function init() {
  bindModifierHintTracking();
  try {
    const savedPx = localStorage.getItem("accent_review_font_px");
    if (savedPx) applyEditorFont(savedPx);
  } catch (_) {}
  const resp = await fetch("/api/presets");
  const data = await resp.json();
  document.getElementById("nInput").value = data.default_n ?? 3;
  const dl = document.getElementById("dirPresets");
  const presets = Array.isArray(data.presets) ? data.presets : [];
  if (dl) {
    dl.innerHTML = presets.map(p => `<option value="${esc(p)}"></option>`).join("");
  }
  const dirInp = document.getElementById("dirInput");
  if (data.current_dir) {
    if (dirInp) dirInp.value = data.current_dir;
    await loadItems(1);
  } else if (presets.length && dirInp) {
    dirInp.value = presets[0];
  }
}

async function browseDir() {
  const resp = await fetch("/api/browse", { method: "POST" });
  const data = await resp.json();
  if (data.dir_path) {
    document.getElementById("dirInput").value = data.dir_path;
    await scanDirectory();
  }
}

async function _runScan(dirPath, { preserveView = false } = {}) {
  setStatus("⏳ Scanning...");
  try {
    const resp = await fetch("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dir_path: dirPath }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      alert(data.detail || "Scan failed");
      return;
    }
    if (!preserveView) {
      selectedFile = "";
      collapsedCards = {};
      undoStack = [];
      updateUndoButton();
    }
    currentDir = data.dir_path;
    await loadItems(preserveView ? currentPage : 1);
    if (preserveView) flashStatus("Refreshed");
  } finally {
    const sp = typeof document !== "undefined" ? document.getElementById("scanSpinner") : null;
    if (sp && sp.textContent === "⏳ Scanning...") setStatus("");
  }
}

async function scanDirectory() {
  const dirPath = document.getElementById("dirInput").value.trim();
  if (!dirPath) return;
  await _runScan(dirPath, { preserveView: false });
}

async function refreshDirectory() {
  const dirPath = (document.getElementById("dirInput").value || currentDir || "").trim();
  if (!dirPath) return;
  await _runScan(dirPath, { preserveView: true });
}

function reloadUi() {
  window.location.reload();
}

function updateStatusFilterPills(statusVal) {
  const active = statusVal || document.getElementById("statusFilter").value || "all";
  const pillMap = [
    ["statTotal", "all"],
    ["statPending", "pending"],
    ["statAccept", "accept"],
    ["statReject", "reject"],
    ["statHold", "has_feedback"],
  ];
  for (const [id, val] of pillMap) {
    const el = document.getElementById(id);
    if (el && el.classList) {
      el.classList.toggle("active", active === val);
    }
  }
}

function setStatusFilter(statusVal) {
  const target = statusVal || "all";
  const sel = document.getElementById("statusFilter");
  sel.value = target;
  updateStatusFilterPills(target);
  return loadItems(1);
}

async function loadItems(page) {
  currentPage = page || 1;
  const nCtx = document.getElementById("nInput").value || 3;
  const statusVal = document.getElementById("statusFilter").value;
  updateStatusFilterPills(statusVal);
  const params = new URLSearchParams({
    page: currentPage,
    page_size: 25,
    n_context: nCtx,
    file_filter: selectedFile,
    git_filter: document.getElementById("gitFilter").value,
    status_filter: statusVal,
    kind_filter: document.getElementById("kindFilter").value,
    search: document.getElementById("searchInput").value,
  });
  const resp = await fetch(`/api/items?${params.toString()}`);
  const data = await resp.json();
  latestSummary = data.summary;
  currentItems = data.items || [];
  draftDecisions = {};
  for (const it of currentItems) {
    draftDecisions[it.item_id] = it.decision || "pending";
  }
  currentPage = data.page || 1;
  totalPages = data.total_pages || 1;
  lastFilteredTotal = data.filtered_total || 0;
  lastPageSize = data.page_size || 25;
  focusedIdx = currentItems.length ? 0 : -1;
  renderSummary(data.summary);
  renderCards(data);
}

let collapsedPadaSvaraTreeDirs = {};
let _padaSvaraModifierBound = false;

function _syncModifierClasses(ev) {
  if (typeof document === "undefined" || !document.body || !document.body.classList) return;
  const altDown = Boolean(ev && (ev.altKey || (ev.type === "keydown" && ev.key === "Alt")));
  const ctrlDown = Boolean(ev && (ev.ctrlKey || (ev.type === "keydown" && ev.key === "Control")));
  document.body.classList.toggle("mod-alt-down", ev?.type === "keyup" && ev?.key === "Alt" ? false : altDown);
  document.body.classList.toggle("mod-ctrl-down", ev?.type === "keyup" && ev?.key === "Control" ? false : ctrlDown);
}

function bindModifierHintTracking() {
  if (_padaSvaraModifierBound || typeof window === "undefined" || typeof window.addEventListener !== "function") return;
  _padaSvaraModifierBound = true;
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

function onPadaSvaraTreeDirToggle(detailsEl) {
  if (!detailsEl) return;
  const dirPath = detailsEl.dataset ? detailsEl.dataset.dirPath : "";
  if (!dirPath) return;
  if (detailsEl.open) delete collapsedPadaSvaraTreeDirs[dirPath];
  else collapsedPadaSvaraTreeDirs[dirPath] = true;
}

function getActiveSidebarFilePath() {
  if (selectedFile) return selectedFile;
  if (focusedIdx < 0 || focusedIdx >= currentItems.length) return "";
  const it = currentItems[focusedIdx];
  const files = latestSummary?.files || [];
  const match = files.find(f => f.file_path === it.vp_file || f.file_path === it.display_file || f.file_path === it.pp_file || (it.rel_path && f.rel_display === it.rel_path));
  return match ? match.file_path : (it.vp_file || it.display_file || "");
}

function syncActiveSidebarFileHighlight({ scroll = false } = {}) {
  const fileListEl = typeof document !== "undefined" && typeof document.getElementById === "function"
    ? document.getElementById("fileList")
    : null;
  if (!fileListEl || typeof fileListEl.querySelectorAll !== "function") return;
  const activeFp = getActiveSidebarFilePath();
  let activeEl = null;
  fileListEl.querySelectorAll(".file-item[data-file-path]").forEach(el => {
    const fp = el.getAttribute("data-file-path") || "";
    const isAct = Boolean(activeFp) && fp === activeFp;
    if (el.classList) el.classList.toggle("active", isAct);
    if (isAct) activeEl = el;
  });
  if (activeEl) {
    let cur = activeEl.parentElement;
    while (cur && cur !== fileListEl) {
      if (cur.tagName === "DETAILS") {
        cur.open = true;
        if (cur.dataset?.dirPath) delete collapsedPadaSvaraTreeDirs[cur.dataset.dirPath];
      }
      cur = cur.parentElement;
    }
    if (scroll && typeof activeEl.scrollIntoView === "function") {
      activeEl.scrollIntoView({ block: "nearest" });
    }
  }
}

function _buildPadaSvaraFileTreeData(files) {
  const entries = (files || []).map((f, fi) => {
    const norm = String(f.rel_display || f.file_path || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
    const segs = norm ? norm.split("/").filter(Boolean) : [norm];
    return {
      f,
      fi,
      fileName: segs[segs.length - 1] || norm,
      dirSegs: segs.slice(0, -1),
    };
  });
  let lcpSegs = entries.length ? entries[0].dirSegs.slice() : [];
  for (let i = 1; i < entries.length; i++) {
    const ds = entries[i].dirSegs;
    let k = 0;
    while (k < lcpSegs.length && k < ds.length && lcpSegs[k] === ds[k]) k++;
    lcpSegs = lcpSegs.slice(0, k);
  }
  const makeDir = (name, keyPath) => ({ name, keyPath, dirs: new Map(), leaves: [], count: 0 });
  const root = makeDir(lcpSegs.join("/"), lcpSegs.join("/") || ".");
  for (const e of entries) {
    const relDirs = e.dirSegs.slice(lcpSegs.length);
    let cur = root;
    cur.count += 1;
    let curKey = lcpSegs.join("/");
    for (const d of relDirs) {
      curKey = curKey ? `${curKey}/${d}` : d;
      if (!cur.dirs.has(d)) cur.dirs.set(d, makeDir(d, curKey));
      cur = cur.dirs.get(d);
      cur.count += 1;
    }
    cur.leaves.push(e);
  }
  const compactDir = (node) => {
    const children = [];
    for (const child of node.dirs.values()) {
      let cur = child;
      let label = cur.name;
      while (cur.dirs.size === 1 && cur.leaves.length === 0) {
        const only = Array.from(cur.dirs.values())[0];
        label = `${label}/${only.name}`;
        cur = only;
      }
      const compacted = compactDir(cur);
      compacted.label = label;
      children.push(compacted);
    }
    children.sort((a, b) => a.label.localeCompare(b.label));
    return { name: node.name, label: node.name, keyPath: node.keyPath, dirs: children, leaves: node.leaves, count: node.count };
  };
  return { lcpSegs, root: compactDir(root) };
}

function renderPadaSvaraFileTreeHtml(files, activePath) {
  if (!files || !files.length) return "";
  const { lcpSegs, root } = _buildPadaSvaraFileTreeData(files);
  const renderLeaf = ({ f, fi, fileName }) => {
    const active = activePath && activePath === f.file_path ? "active" : "";
    const gitCls = f.dirty ? "pill-dirty" : "pill-clean";
    const mk = f.marked ? `<span class="stat-pill pill-accept" style="padding:1px 6px;">✓ commit</span>` : "";
    return `<div class="file-item ${active}" data-file-path="${esc(f.file_path)}" data-file-idx="${fi}" title="${esc(f.rel_display)}" onclick="selectFileFilterIdx(${fi})">
      <div class="file-item-head">
        <div class="file-name"><a href="#" class="file-link" title="Open ${esc(f.file_path)} in IntelliJ IDEA (click card margin to filter)" onclick="event.stopPropagation(); event.preventDefault(); openSummaryFileInIdea(${fi});">${esc(fileName)}</a></div>
        <a href="#" class="vscode-link" title="Open ${esc(f.file_path)} in VS Code" onclick="event.stopPropagation(); event.preventDefault(); openSummaryFileInVscode(${fi});">[V]</a>
      </div>
      <div class="file-meta">
        <span class="stat-pill ${gitCls}" style="padding:1px 6px;">${esc(f.git_label)}</span>
        <span>${f.counts.accepted} acc / ${f.counts.rejected || 0} rej / ${f.counts.total}</span>
        ${f.counts.with_feedback ? `<span style="color:var(--amber)">${f.counts.with_feedback} !!</span>` : ""}
        ${mk}
      </div>
    </div>`;
  };
  const nodeContainsActive = (node) => {
    if (!activePath) return false;
    if (node.leaves.some(e => e.f.file_path === activePath)) return true;
    return node.dirs.some(d => nodeContainsActive(d));
  };
  const renderNode = (node, isRoot) => {
    const childrenHtml = node.dirs.map(d => renderNode(d, false)).join("") + node.leaves.map(renderLeaf).join("");
    if (isRoot && !lcpSegs.length) return childrenHtml;
    const isCollapsed = Boolean(collapsedPadaSvaraTreeDirs[node.keyPath]) && !nodeContainsActive(node);
    const rootCls = isRoot ? " file-tree-root" : "";
    return `<details class="file-tree-dir${rootCls}" data-dir-path="${esc(node.keyPath)}" ${isCollapsed ? "" : "open"} ontoggle="onPadaSvaraTreeDirToggle(this)">
      <summary><span class="file-tree-caret">▸</span><span class="file-tree-label">${esc(node.label || node.name)}</span><span class="file-tree-count">${node.count}</span></summary>
      <div class="file-tree-children">${childrenHtml}</div>
    </details>`;
  };
  return renderNode(root, true);
}

function renderSummary(sum) {
  if (!sum) return;
  document.getElementById("statTotal").innerHTML = `A<u class="sc-key sc-alt">l</u>l: ${sum.total_items} <span class="kbd-hint">Alt+L</span>`;
  document.getElementById("statPending").innerHTML = `Pend: ${sum.counts.pending} <span class="kbd-hint">Alt+U</span>`;
  document.getElementById("statAccept").innerHTML = `<u class="sc-key sc-alt">A</u>cc: ${sum.counts.accept} <span class="kbd-hint">Alt+A</span>`;
  document.getElementById("statReject").innerHTML = `<u class="sc-key sc-alt">R</u>ej: ${sum.counts.reject || 0} <span class="kbd-hint">Alt+R</span>`;
  document.getElementById("statHold").innerHTML = `!!: ${sum.counts.with_feedback || 0} <span class="kbd-hint">Alt+W</span>`;
  updateStatusFilterPills();
  document.getElementById("markedCountPill").textContent = `${sum.marked_files.length} marked`;

  const files = sum.files || [];
  document.getElementById("fileCount").textContent = files.length;
  const activeSidebarPath = getActiveSidebarFilePath();
  document.getElementById("fileList").innerHTML = renderPadaSvaraFileTreeHtml(files, activeSidebarPath);
  const fileListEl = document.getElementById("fileList");
  const actFileEl = fileListEl && typeof fileListEl.querySelector === "function" ? fileListEl.querySelector(".file-item.active") : null;
  if (actFileEl && typeof actFileEl.scrollIntoView === "function") actFileEl.scrollIntoView({ block: "nearest" });

  const indexed = files.map((f, fi) => ({ f, fi }));
  const dirtyFiles = indexed.filter(({ f }) => f.dirty || f.counts.accepted > 0 || (f.counts.rejected || 0) > 0 || (f.counts.with_feedback || 0) > 0);
  dirtyFiles.sort((a, b) => (b.f.marked - a.f.marked) || (b.f.dirty - a.f.dirty) || a.f.rel_display.localeCompare(b.f.rel_display));
  document.getElementById("markedFilesList").innerHTML = dirtyFiles.map(({ f, fi }) => {
    const chk = f.marked ? "checked" : "";
    const disabled = f.dirty ? "" : "disabled";
    const rowCls = f.marked ? "marked" : "";
    const gitBadge = f.dirty ? `<span class="stat-pill pill-dirty" style="padding:0 5px;">${esc(f.git_xy)}</span>` : `<span class="stat-pill pill-clean" style="padding:0 5px;">clean</span>`;
    return `<div class="commit-file-row ${rowCls}">
      <div style="display:flex; gap:6px; align-items:flex-start;">
        <input type="checkbox" ${chk} ${disabled} style="cursor:pointer; margin-top:2px;" onchange="toggleFileMarkIdx(${fi}, this.checked)" />
        <span style="font-family:monospace; font-size:11px; word-break:break-all;"><a href="#" class="file-link" title="Open ${esc(f.file_path)} in IntelliJ IDEA" onclick="event.preventDefault(); openSummaryFileInIdea(${fi});">${esc(f.rel_display)}</a> <a href="#" class="vscode-link" title="Open ${esc(f.file_path)} in VS Code" onclick="event.preventDefault(); openSummaryFileInVscode(${fi});">[V]</a></span>
      </div>
      <div class="file-meta">
        ${gitBadge}
        <span>${f.counts.accepted} acc / ${f.counts.rejected || 0} rej / ${f.counts.total}</span>
        ${f.counts.with_feedback ? `<span style="color:var(--amber)">${f.counts.with_feedback} !!</span>` : ""}
        ${f.dirty ? `<button class="small" onclick="viewDiffIdx(${fi})">Diff</button>` : ""}
        ${f.counts.pending > 0 ? `<button class="small" onclick="acceptAllInFileIdx(${fi})">Accept all (${f.counts.total})</button>` : ""}
      </div>
    </div>`;
  }).join("") || `<div style="color:var(--muted); font-size:12px;">No modified or reviewed files yet.</div>`;
}

function selectFileFilter(fp) {
  selectedFile = selectedFile === fp ? "" : fp;
  loadItems(1);
}

function selectFileFilterIdx(fi) {
  const f = latestSummary?.files?.[fi];
  if (f) selectFileFilter(f.file_path);
}

function stepSidebarFile(delta) {
  const files = latestSummary?.files || [];
  if (!files.length) return false;
  const fileListEl = typeof document !== "undefined" && typeof document.getElementById === "function"
    ? document.getElementById("fileList")
    : null;
  let ordered = files;
  if (fileListEl && typeof fileListEl.querySelectorAll === "function") {
    const domFiles = Array.from(fileListEl.querySelectorAll(".file-item[data-file-idx]"))
      .map(el => files[Number(el.getAttribute("data-file-idx"))])
      .filter(Boolean);
    if (domFiles.length) ordered = domFiles;
  }
  const curIdx = selectedFile ? ordered.findIndex(f => f.file_path === selectedFile) : -1;
  if (curIdx < 0) {
    if (delta > 0) {
      selectFileFilter(ordered[0].file_path);
    } else {
      flashStatus("Showing all files");
    }
    return true;
  }
  if (delta < 0 && curIdx === 0) {
    selectFileFilter(ordered[0].file_path);
    flashStatus("Showing all files");
    return true;
  }
  const nextIdx = Math.max(0, Math.min(ordered.length - 1, curIdx + delta));
  if (nextIdx === curIdx) {
    flashStatus(delta > 0 ? `At last file (${ordered.length} / ${ordered.length})` : `At first file (1 / ${ordered.length})`);
    return true;
  }
  selectFileFilter(ordered[nextIdx].file_path);
  return true;
}

function toggleFileMarkIdx(fi, checked) {
  const f = latestSummary?.files?.[fi];
  if (f) toggleFileMark(f.file_path, checked);
}

function viewDiffIdx(fi) {
  const f = latestSummary?.files?.[fi];
  if (f) viewDiff(f.file_path);
}

function _itemPrimaryFile(it) {
  if (!it) return "";
  if (it.vp_file || it.display_file) return it.vp_file || it.display_file;
  const vpRf = (it.relevant_files || []).find(rf => rf.role === "विश्वास-प्रस्तुतिः");
  return vpRf?.path || "";
}

function openSummaryFileInEditor(fi, editor) {
  const f = latestSummary?.files?.[fi];
  if (f) openInEditor(editor, f.file_path, null);
}

function openSummaryFileInIdea(fi) {
  openSummaryFileInEditor(fi, "idea");
}

function openSummaryFileInVscode(fi) {
  openSummaryFileInEditor(fi, "vscode");
}

function _ideaNeedleForItemFile(it, filePath) {
  if (!it) return null;
  if (it.pp_file && filePath === it.pp_file) {
    return it.pp_line_core || it.pp_target_line || null;
  }
  if ((it.decision || "pending") === "pending" && it.vp_sug_old && it.vp_sug_new) {
    return `[[${it.vp_sug_old}|${it.vp_sug_new}]]`;
  }
  return it.vp_raw_body || it.pp_line_core || null;
}

function openItemFileInEditor(idx, editor) {
  const it = currentItems?.[idx];
  if (!it) return;
  const fp = _itemPrimaryFile(it);
  if (fp) openInEditor(editor, fp, _ideaNeedleForItemFile(it, fp), it.item_id);
}

function openItemFileInIdea(idx) {
  openItemFileInEditor(idx, "idea");
}

function openItemFileInVscode(idx) {
  openItemFileInEditor(idx, "vscode");
}

function _gdTargetsForItem(it) {
  const gd = it?.pp_goldendict;
  if (!gd || !gd.primary) return [];
  return [gd.primary, ...(gd.alternatives || [])];
}

function openItemGoldenDict(idx, candIdx) {
  const it = currentItems?.[idx];
  const targets = _gdTargetsForItem(it);
  const w = targets[candIdx];
  if (w) openInGoldenDict(w, it.item_id);
}

function openRelevantFileInEditor(idx, rfi, editor) {
  const it = currentItems?.[idx];
  const rf = it?.relevant_files?.[rfi];
  if (rf?.path) openInEditor(editor, rf.path, _ideaNeedleForItemFile(it, rf.path), it.item_id);
}

function openRelevantFileInIdea(idx, rfi) {
  openRelevantFileInEditor(idx, rfi, "idea");
}

function openRelevantFileInVscode(idx, rfi) {
  openRelevantFileInEditor(idx, rfi, "vscode");
}

function acceptAllInFileIdx(fi) {
  const f = latestSummary?.files?.[fi];
  if (f) acceptAllInFile(f.file_path);
}

function formatTextSegment(text) {
  // Render `[[OLD|NEW]]`, `+++(...)+++`, and `**bold**` inside an unfocused VP segment
  let out = "";
  const tokRe = /(\[\[[^|\]]+\|[^\]]+\]\]|\+\+\+[\s\S]*?\+\+\+)/g;
  let last = 0;
  let m;
  while ((m = tokRe.exec(text)) !== null) {
    out += esc(text.slice(last, m.index)).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    const tok = m[0];
    if (tok.startsWith("[[")) {
      const bar = tok.indexOf("|");
      const oldW = tok.slice(2, bar);
      const newW = tok.slice(bar + 1, -2);
      out += `<span class="sug-other"><span class="old-txt">${esc(oldW)}</span>→ <span class="new-txt">${esc(newW)}</span></span>`;
    } else {
      const inner = tok.slice(3, -3);
      if (inner.includes("!!")) {
        out += `<span class="inline-feedback-tag">${linkPaniniSutrasInText(esc(tok))}</span>`;
      } else {
        out += `<span class="inline-note">${linkPaniniSutrasInText(esc(inner))}</span>`;
      }
    }
    last = m.index + tok.length;
  }
  out += linkPaniniSutras(esc(text.slice(last)).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>"));
  return out;
}

function renderFocusedSpan(tok, decision) {
  if (tok.startsWith("[[") && tok.includes("|") && tok.endsWith("]]")) {
    const bar = tok.indexOf("|");
    const oldW = tok.slice(2, bar);
    const newW = tok.slice(bar + 1, -2);
    return `<span class="sug-focus"><span class="old-txt">${esc(oldW)}</span><span class="arrow">→</span><span class="new-txt">${esc(newW)}</span></span>`;
  }
  if (decision === "accept") return `<span class="accepted-focus" title="Accepted">✓ ${esc(tok)}</span>`;
  if (decision === "reject") return `<span class="rejected-focus" title="Rejected (kept original)">✗ ${esc(tok)}</span>`;
  return `<span class="plain-focus">${esc(tok)}</span>`;
}

function renderFullVP(vpText, ranges, decision) {
  if (!vpText) return `<em style="color:var(--muted)">(No विश्वास-प्रस्तुतिः paired)</em>`;
  const sorted = [...(ranges || [])].sort((a, b) => a[0] - b[0]);
  let out = "";
  let pos = 0;
  for (const [s, e] of sorted) {
    if (s < pos || s >= vpText.length) continue;
    out += formatTextSegment(vpText.slice(pos, s));
    out += renderFocusedSpan(vpText.slice(s, e), decision);
    pos = e;
  }
  out += formatTextSegment(vpText.slice(pos));
  return out;
}

function _devaDigitsToAscii(s) {
  return String(s || "").replace(/[०-९]/g, ch => String(ch.charCodeAt(0) - 0x0966));
}

function paniniSutraUrl(a, p, n) {
  return `https://ashtadhyayi.com/sutraani/${_devaDigitsToAscii(a)}/${_devaDigitsToAscii(p)}/${_devaDigitsToAscii(n)}`;
}

function _makeSutraAnchor(labelHtml, a, p, n) {
  const asc = `${_devaDigitsToAscii(a)}.${_devaDigitsToAscii(p)}.${_devaDigitsToAscii(n)}`;
  const url = paniniSutraUrl(a, p, n);
  return `<a class="sutra-link" href="${url}" target="_blank" rel="noopener noreferrer" title="Open ${asc} on ashtadhyayi.com" onclick="event.stopPropagation()">${labelHtml}</a>`;
}

function linkPaniniSutrasInText(escapedText) {
  const sutraChainRe = /(पा(?:[॰०]|\.?\s*सू\.?)\s*)([1-8१-८])\.([1-4१-४])\.([0-9०-९]+)((?:[–\-]([0-9०-९]+)(?![.0-9०-९]))?)((?:\s*[,/;–\-+]\s*[1-8१-८]\.[1-4१-४]\.[0-9०-९]+(?:[–\-][0-9०-९]+(?![.0-9०-९]))?)*)/g;
  return escapedText.replace(sutraChainRe, (_, prefix, a, p, n, rangeFull, rangeEnd, tail) => {
    let out = _makeSutraAnchor(`${prefix}${a}.${p}.${n}`, a, p, n);
    if (rangeFull && rangeEnd) {
      const sep = rangeFull[0];
      out += `${sep}${_makeSutraAnchor(rangeEnd, a, p, rangeEnd)}`;
    }
    if (tail) {
      out += tail.replace(/([1-8१-८])\.([1-4१-४])\.([0-9०-९]+)([–\-]([0-9०-९]+)(?![.0-9०-९]))?/g,
        (__, sa, sp, sn, sRangeFull, sRangeEnd) => {
          let seg = _makeSutraAnchor(`${sa}.${sp}.${sn}`, sa, sp, sn);
          if (sRangeFull && sRangeEnd) {
            seg += `${sRangeFull[0]}${_makeSutraAnchor(sRangeEnd, sa, sp, sRangeEnd)}`;
          }
          return seg;
        }
      );
    }
    return out;
  });
}

function linkPaniniSutras(htmlChunk) {
  return htmlChunk.split(/(<[^>]+>)/g).map(part =>
    (part && part[0] === "<") ? part : linkPaniniSutrasInText(part)
  ).join("");
}

function formatPadapathaLineHtml(line) {
  let html = esc(line);
  html = html.replace(/\*\*([^*]+)\*\*/g, '<span class="pp-arrow-word">$1</span>');
  html = html.replace(/\*([A-Za-z0-9-]+|ŚB)\*/g, '<span class="pp-tag">*$1*</span>');
  html = html.replace(/(\+\+\+[\s\S]*?\+\+\+)/g, tok =>
    tok.includes("!!")
      ? `<span class="inline-feedback-tag">${tok}</span>`
      : `<span class="inline-note">${tok.slice(3, -3)}</span>`
  );
  return linkPaniniSutras(html);
}

function renderOneCardHtml(it, idx, pageOffset) {
  const savedDec = it.decision || "pending";
  const draftDec = draftDecisions[it.item_id] ?? savedDec;
  const focCls = idx === focusedIdx ? "focused" : "";
  const isCollapsed = !!collapsedCards[it.item_id];
  const colCls = isCollapsed ? "collapsed" : "";
  const primaryFile = it.vp_file || it.display_file || "";
  const primaryRf = (it.relevant_files || []).find(rf =>
    (primaryFile && rf.path === primaryFile) ||
    (rf.role === "विश्वास-प्रस्तुतिः" && (rf.vishvasa_rel || rf.short) === it.rel_path)
  );
  const primaryTarget = primaryFile || primaryRf?.path || "";
  const primaryStatusPill = primaryRf
    ? ` <span class="stat-pill ${primaryRf.git_status === 'clean' ? 'pill-clean' : 'pill-dirty'}" style="padding:1px 6px; font-weight:500;" title="${esc(primaryRf.path)}">${esc(primaryRf.git_status.trim() || 'clean')}</span>`
    : "";
  const fileBadges = (it.relevant_files || []).map((rf, rfi) => {
    if (rf === primaryRf || (rf.path && primaryTarget && rf.path === primaryTarget)) return "";
    return `<span class="stat-pill ${rf.git_status === 'clean' ? 'pill-clean' : 'pill-dirty'}" style="padding:2px 7px;" title="${esc(rf.path)}"><strong>${esc(rf.role || '')}:</strong> <a href="#" class="file-link" title="Open ${esc(rf.path)} in IntelliJ IDEA" onclick="event.stopPropagation(); event.preventDefault(); openRelevantFileInIdea(${idx}, ${rfi});">${esc(rf.vishvasa_rel || rf.short)}</a> <a href="#" class="vscode-link" title="Open ${esc(rf.path)} in VS Code" onclick="event.stopPropagation(); event.preventDefault(); openRelevantFileInVscode(${idx}, ${rfi});">[V]</a> (${esc(rf.git_status.trim() || 'clean')})</span>`;
  }).filter(Boolean).join(" ");

  const gdTargets = _gdTargetsForItem(it);
  const gdInline = gdTargets.length
    ? `<span class="gd-inline" title="Look up in GoldenDict"><span>📖</span>${gdTargets.map((w, ci) =>
        `<a href="#" class="file-link gd-link" style="${ci === 0 ? 'font-weight:700;' : 'opacity:0.85;'}" title="Look up ${esc(w)} in GoldenDict" onclick="event.stopPropagation(); event.preventDefault(); openItemGoldenDict(${idx}, ${ci});">${esc(w)}</a>`
      ).join('<span style="color:var(--border);">·</span>')}</span>`
    : "";

  const ctxBefore = (it.pp_context_before || []).map(l => `<div class="pp-line ctx">${formatPadapathaLineHtml(l)}</div>`).join("");
  const ctxTarget = it.pp_target_line
    ? `<div class="pp-line target" id="ppTarget_${esc(it.item_id)}">${formatPadapathaLineHtml(it.pp_target_line)}${gdInline}</div>`
    : `<div class="pp-line ctx"><em>(No aligned पद-पाठः line)</em>${gdInline}</div>`;
  const ctxAfter = (it.pp_context_after || []).map(l => `<div class="pp-line ctx">${formatPadapathaLineHtml(l)}</div>`).join("");
  const fbTargetOpts = it.pp_file
    ? `<option value="auto" selected>End of पद-पाठः line</option><option value="vp">विश्वास-प्रस्तुतिः text</option><option value="both">Both</option>`
    : `<option value="vp" selected>विश्वास-प्रस्तुतिः text</option>`;
  const unsaved = draftDec !== savedDec ? `<span style="color:var(--amber);">Unsaved (Enter submits)</span>` : "";
  const colDecPill = savedDec === "accept"
    ? `<span class="stat-pill pill-accept">Accepted ✓</span>`
    : (savedDec === "reject"
      ? `<span class="stat-pill pill-reject">Rejected ✗</span>`
      : `<span class="stat-pill pill-pending">Pending</span>`);
  const colFbPill = it.existing_feedback
    ? `<span class="stat-pill" style="background:var(--amber-soft); color:var(--amber);">!! ${esc(it.existing_feedback)}</span>`
    : "";
  let colOld = it.vp_sug_old || "";
  let colNew = it.vp_sug_new || "";
  if ((!colOld || !colNew) && it.pp_line_core) {
    const pm = it.pp_line_core.match(/^-\s+(.+?)\s*→\s*\*\*(.+?)\*\*/);
    if (pm) {
      colOld = pm[1].trim();
      colNew = pm[2].trim();
    }
  }
  const colTokPreview = (colOld && colNew)
    ? `<span class="collapse-correction"><span class="old-txt">${esc(colOld)}</span><span class="arrow">→</span><span class="new-txt">${esc(colNew)}</span></span>`
    : (it.pp_line_core ? `<span class="collapse-correction">${esc(it.pp_line_core.replace(/^-\s+/, ""))}</span>` : "");

  return `<div class="card dec-${savedDec} ${focCls} ${colCls}" id="card_${idx}" onclick="handleCardClick(${idx})" onfocusin="setFocused(${idx})">
    <div class="card-head">
      <div style="min-width:0; flex:1;">
        <span class="active-badge">Active</span><span class="card-title">#${pageOffset + idx + 1} · <a href="#" class="file-link" title="Open ${esc(primaryTarget)} in IntelliJ IDEA" onclick="event.stopPropagation(); event.preventDefault(); openItemFileInIdea(${idx});">${esc(it.rel_path)}</a> <a href="#" class="vscode-link" title="Open ${esc(primaryTarget)} in VS Code" onclick="event.stopPropagation(); event.preventDefault(); openItemFileInVscode(${idx});">[V]</a>${primaryStatusPill}</span>
        <span class="card-section">${esc(it.section_heading)}</span>
        <span class="collapse-summary">${colDecPill}${colFbPill}${colTokPreview}</span>
        <div class="path-row">
          ${fileBadges}
          <span class="stat-pill" style="padding:1px 6px;">${esc(it.kind)}</span>
        </div>
      </div>
      <div class="row" style="align-items:flex-start; flex-shrink:0;">
        ${it.mula_preview ? `<details class="mula-details" style="font-size:12px; max-width:36vw;"><summary style="cursor:pointer; color:var(--muted);">मूलम्</summary><div class="mula-box">${esc(it.mula_preview)}</div></details>` : ""}
        <button type="button" class="collapse-btn" onclick="event.stopPropagation(); toggleCardCollapse(${idx})">${isCollapsed ? "▸ Expand" : "▾ Collapse"}</button>
      </div>
    </div>

    <div>
      <div class="vp-label"><span>${esc(it.vp_summary || "विश्वास-प्रस्तुतिः")}</span></div>
      <div class="vp-box">${renderFullVP(it.vp_raw_body, it.vp_highlight_ranges, savedDec)}</div>
    </div>

    <div class="pp-box">
      <div class="pp-header">
        <span>${esc(it.pp_summary || "पद-पाठः (यान्त्रिकः)")}</span>
        <span>line ${it.pp_target_idx !== null ? it.pp_target_idx + 1 : "-"}/${it.pp_total_lines} (±${document.getElementById("nInput").value} context)</span>
      </div>
      ${ctxBefore}
      ${ctxTarget}
      ${ctxAfter}
    </div>

    <div class="controls-bar">
      <div class="decision-row">
        <div class="radio-group" id="radioGroup_${idx}">
          <label class="${draftDec === 'accept' ? 'sel-accept' : ''}">
            <input type="radio" name="dec_${esc(it.item_id)}" value="accept" ${draftDec === 'accept' ? 'checked' : ''} onchange="selectRadio(${idx}, 'accept')" /> Accept <span class="kbd-hint">Alt+1</span>
          </label>
          <label class="${draftDec === 'reject' ? 'sel-reject' : ''}">
            <input type="radio" name="dec_${esc(it.item_id)}" value="reject" ${draftDec === 'reject' ? 'checked' : ''} onchange="selectRadio(${idx}, 'reject')" /> Reject <span class="kbd-hint">Alt+2</span>
          </label>
        </div>
        <span class="fb-status" id="fbStatus_${esc(it.item_id)}">${unsaved}</span>
      </div>

      <div class="fb-row">
        <input class="fb-input" id="fbInput_${esc(it.item_id)}" type="text"
          value="${esc(it.existing_feedback)}"
          placeholder="Feedback (Alt+3) -> +++(FEEDBACK!!)+++ · Enter submits" />
        <select id="fbTarget_${esc(it.item_id)}">${fbTargetOpts}</select>
      </div>

      <div class="submit-row">
        <button class="primary" onclick="event.stopPropagation(); submitCard(${idx})">Submit <span class="kbd-hint" style="background:rgba(255,255,255,0.2); color:#fff;">Enter</span></button>
        <span style="font-size:11px; color:var(--muted);">Ctrl+Enter = Accept + Submit</span>
        ${(savedDec !== 'pending' || it.existing_feedback) ? `<button class="small" title="Revert to [[OLD|NEW]] and clear feedback" onclick="event.stopPropagation(); resetCard(${idx})">Reset</button>` : ""}
      </div>
    </div>
  </div>`;
}

function renderCards(data) {
  document.getElementById("pageInfo").textContent =
    `Showing ${data.filtered_total ? ((currentPage - 1) * data.page_size + 1) : 0}–${Math.min(data.filtered_total, currentPage * data.page_size)} of ${data.filtered_total} matching suggestions`;
  document.getElementById("pageLabel").textContent = `${currentPage} / ${totalPages}`;
  document.getElementById("pageLabelBottom").textContent = `${currentPage} / ${totalPages}`;

  const container = document.getElementById("cardsContainer");
  if (!currentItems.length) {
    container.innerHTML = `<div class="card">No suggestions match current filters.</div>`;
    return;
  }
  const pageOffset = (currentPage - 1) * lastPageSize;
  container.innerHTML = currentItems.map((it, idx) => renderOneCardHtml(it, idx, pageOffset)).join("");
}

function setFocused(idx) {
  if (idx < 0 || idx >= currentItems.length) return;
  focusedIdx = idx;
  if (typeof document !== "undefined" && typeof document.querySelectorAll === "function") {
    document.querySelectorAll("#cardsContainer > .card").forEach((el, i) => el.classList.toggle("focused", i === idx));
  }
  syncActiveSidebarFileHighlight({ scroll: true });
}

function toggleCardCollapse(idx, forceState) {
  if (idx < 0 || idx >= currentItems.length) return;
  const it = currentItems[idx];
  const next = typeof forceState === "boolean" ? forceState : !collapsedCards[it.item_id];
  if (next) collapsedCards[it.item_id] = true;
  else delete collapsedCards[it.item_id];
  const cardEl = document.getElementById(`card_${idx}`);
  if (cardEl) {
    const pageOffset = (currentPage - 1) * lastPageSize;
    cardEl.outerHTML = renderOneCardHtml(it, idx, pageOffset);
  }
  setFocused(idx);
}

function handleCardClick(idx) {
  if (idx < 0 || idx >= currentItems.length) return;
  const it = currentItems[idx];
  if (collapsedCards[it.item_id]) {
    toggleCardCollapse(idx, false);
  } else {
    setFocused(idx);
  }
}

function selectRadio(idx, choice, toggle = false) {
  if (idx < 0 || idx >= currentItems.length) return;
  const it = currentItems[idx];
  if (collapsedCards[it.item_id]) toggleCardCollapse(idx, false);
  setFocused(idx);
  const cur = draftDecisions[it.item_id] ?? it.decision ?? "pending";
  const nextChoice = (toggle && cur === choice) ? "pending" : choice;
  draftDecisions[it.item_id] = nextChoice;
  const rg = document.getElementById(`radioGroup_${idx}`);
  if (rg) {
    rg.querySelectorAll("label").forEach(lbl => {
      const inp = lbl.querySelector("input");
      lbl.classList.remove("sel-accept", "sel-reject");
      if (inp) {
        inp.checked = (inp.value === nextChoice);
        if (inp.checked) lbl.classList.add(`sel-${nextChoice}`);
      }
    });
  }
  const st = document.getElementById(`fbStatus_${it.item_id}`);
  if (st) {
    st.innerHTML = draftDecisions[it.item_id] !== (it.decision || "pending")
      ? `<span style="color:var(--amber);">Unsaved (Enter submits)</span>`
      : "";
  }
}

function focusFeedback(idx) {
  if (idx < 0 || idx >= currentItems.length) return;
  const it = currentItems[idx];
  if (collapsedCards[it.item_id]) toggleCardCollapse(idx, false);
  setFocused(idx);
  const inp = document.getElementById(`fbInput_${it.item_id}`);
  if (inp) {
    inp.scrollIntoView({ block: "nearest" });
    inp.focus();
  }
}

async function submitCard(idx, collapseOnDone = true, recordUndo = true) {
  if (idx < 0 || idx >= currentItems.length) return;
  const it = currentItems[idx];
  const itemId = it.item_id;
  const decision = draftDecisions[itemId] ?? it.decision ?? "pending";
  const txt = document.getElementById(`fbInput_${itemId}`)?.value ?? "";
  const targetMode = document.getElementById(`fbTarget_${itemId}`)?.value ?? "auto";
  const syncPrefills = document.getElementById("syncPrefillsChk").checked;
  const undoSnap = {
    type: "submit",
    itemId,
    prevDecision: it.decision || "pending",
    prevFeedback: it.existing_feedback || "",
    prevTargetMode: it.pp_file ? "auto" : "vp",
    prevCollapsed: !!collapsedCards[itemId],
    label: it.vp_sug_old || it.pp_line_core || it.rel_path || itemId,
  };

  await _withRecordingStatus(async () => {
    const resp = await fetch("/api/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        item_id: itemId,
        decision,
        feedback_text: txt,
        target_mode: targetMode,
        sync_prefills: syncPrefills,
      }),
    });
    const data = await resp.json();
    if (!resp.ok) return alert(data.detail || "Submit failed");

    if (recordUndo && (decision !== undoSnap.prevDecision || txt.trim() !== undoSnap.prevFeedback.trim())) {
      undoStack.push(undoSnap);
      updateUndoButton();
    }

    const activeEl = document.activeElement;
    if (activeEl && activeEl.id === `fbInput_${itemId}`) activeEl.blur();

    if (data.item) {
      currentItems[idx] = data.item;
      draftDecisions[itemId] = data.item.decision || "pending";
    } else {
      currentItems[idx] = { ...it, decision, existing_feedback: data.existing_feedback ?? txt };
    }
    if (collapseOnDone) collapsedCards[itemId] = true;
    else delete collapsedCards[itemId];
    renderSummary(data.summary);

    if (document.getElementById("statusFilter").value !== "all") {
      await loadItems(currentPage);
      return;
    }

    // Refresh the whole page cards when VP text changed in case sibling cards share the same VP block offsets
    const pageOffset = (currentPage - 1) * lastPageSize;
    const cardEl = document.getElementById(`card_${idx}`);
    if (cardEl) {
      const itemObj = data.item || currentItems[idx];
      cardEl.outerHTML = renderOneCardHtml(itemObj, idx, pageOffset);
      const st = document.getElementById(`fbStatus_${itemId}`);
      if (st) {
        const parts = [];
        if (decision !== "pending") parts.push(decision === "accept" ? "Saved Accept ✓" : "Saved Reject ✗");
        if (data.comment) parts.push(`Saved ${data.comment}`);
        st.textContent = parts.join(" · ") || "Saved";
      }
    }
    // If sibling card on this page shares the same VP block, refresh its raw snapshot too
    if (data.item) {
      await refreshSamePageSiblings(idx, data.item);
    }
    if (idx + 1 < currentItems.length) {
      setFocused(idx + 1);
      document.getElementById(`card_${idx + 1}`)?.scrollIntoView({ block: "nearest" });
    }
  });
}

async function refreshSamePageSiblings(submittedIdx, updatedItem) {
  const hasSibling = currentItems.some((other, j) => j !== submittedIdx && other.vp_file && other.vp_file === updatedItem.vp_file);
  if (!hasSibling) return;
  const nCtx = document.getElementById("nInput").value || 3;
  const params = new URLSearchParams({
    page: currentPage,
    page_size: lastPageSize,
    n_context: nCtx,
    file_filter: selectedFile,
    git_filter: document.getElementById("gitFilter").value,
    status_filter: document.getElementById("statusFilter").value,
    kind_filter: document.getElementById("kindFilter").value,
    search: document.getElementById("searchInput").value,
  });
  const resp = await fetch(`/api/items?${params.toString()}`);
  const fresh = await resp.json();
  currentItems = fresh.items || [];
  for (const it of currentItems) {
    if (!(it.item_id in draftDecisions)) draftDecisions[it.item_id] = it.decision || "pending";
  }
  const pageOffset = (currentPage - 1) * lastPageSize;
  document.getElementById("cardsContainer").innerHTML = currentItems.map((it, i) => renderOneCardHtml(it, i, pageOffset)).join("");
}

async function resetCard(idx) {
  if (idx < 0 || idx >= currentItems.length) return;
  const it = currentItems[idx];
  draftDecisions[it.item_id] = "pending";
  delete collapsedCards[it.item_id];
  const inp = document.getElementById(`fbInput_${it.item_id}`);
  if (inp) inp.value = "";
  await submitCard(idx, false);
}

async function acceptAllInFile(fp) {
  await _withRecordingStatus(async () => {
    const syncPrefills = document.getElementById("syncPrefillsChk").checked;
    const resp = await fetch("/api/file_accept_all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_path: fp, decision: "accept", sync_prefills: syncPrefills }),
    });
    const data = await resp.json();
    if (!resp.ok) return alert(data.detail || "Failed");
    const changed = (data.previous_states || []).filter(s => s.decision !== "accept");
    if (changed.length) {
      undoStack.push({
        type: "batch_decisions",
        prevStates: changed,
        syncPrefills,
        label: `Accept all (${changed.length})`,
      });
      updateUndoButton();
    }
    await loadItems(currentPage);
  });
}

async function undoLastAction() {
  if (!undoStack.length) return;
  const snap = undoStack.pop();
  updateUndoButton();
  const syncPrefills = document.getElementById("syncPrefillsChk").checked;
  if (snap.type === "submit") {
    const ok = await _withRecordingStatus(async () => {
      const resp = await fetch("/api/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          item_id: snap.itemId,
          decision: snap.prevDecision,
          feedback_text: snap.prevFeedback,
          target_mode: snap.prevTargetMode,
          sync_prefills: syncPrefills,
        }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        undoStack.push(snap);
        updateUndoButton();
        alert(data.detail || "Undo failed");
        return false;
      }
      if (snap.prevCollapsed) collapsedCards[snap.itemId] = true;
      else delete collapsedCards[snap.itemId];
      await loadItems(currentPage);
      const restoredIdx = currentItems.findIndex(x => x.item_id === snap.itemId);
      if (restoredIdx >= 0) {
        setFocused(restoredIdx);
        document.getElementById(`card_${restoredIdx}`)?.scrollIntoView({ block: "nearest" });
      }
      return true;
    });
    if (ok) flashStatus(`Undone: ${snap.label}`);
  } else if (snap.type === "batch_decisions") {
    const ok = await _withRecordingStatus(async () => {
      for (const prev of (snap.prevStates || [])) {
        const resp = await fetch("/api/decision", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            item_id: prev.item_id,
            decision: prev.decision,
            sync_prefills: snap.syncPrefills ?? syncPrefills,
          }),
        });
        if (!resp.ok) {
          const data = await resp.json().catch(() => ({}));
          alert(data.detail || "Batch undo failed");
          return false;
        }
      }
      await loadItems(currentPage);
      return true;
    });
    if (ok) flashStatus(`Undone: ${snap.label}`);
  }
}

async function toggleFileMark(fp, marked) {
  const resp = await fetch("/api/file_mark", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file_path: fp, marked }),
  });
  const data = await resp.json();
  renderSummary(data.summary);
}

async function viewDiff(fp) {
  const resp = await fetch(`/api/diff?file_path=${encodeURIComponent(fp)}`);
  const data = await resp.json();
  document.getElementById("diffTitle").textContent = fp;
  document.getElementById("diffBody").textContent = data.diff || "";
  document.getElementById("diffDialog").showModal();
}

async function openInGoldenDict(word, itemId) {
  const resp = await fetch("/api/open_in_goldendict", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ word }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    return alert(data.detail || `Failed to open GoldenDict for: ${word}`);
  }
  const msg = `Looked up ${data.word || word} in GoldenDict`;
  const cardSt = itemId ? document.getElementById(`saveStatus_${itemId}`) : null;
  if (cardSt) cardSt.textContent = msg;
  flashStatus(msg);
}

const EDITOR_TARGETS = {
  idea: { endpoint: "/api/open_in_idea", label: "IntelliJ IDEA" },
  vscode: { endpoint: "/api/open_in_vscode", label: "VS Code" },
};

async function openInEditor(editor, filePath, needleOrLine, itemId) {
  const cfg = EDITOR_TARGETS[editor] || EDITOR_TARGETS.idea;
  const payload = { file_path: filePath };
  if (typeof needleOrLine === "number" && needleOrLine >= 1) {
    payload.line = needleOrLine;
  } else if (typeof needleOrLine === "string" && needleOrLine.trim()) {
    payload.needle = needleOrLine;
  }
  const resp = await fetch(cfg.endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    return alert(data.detail || `Failed to open in ${cfg.label}: ${filePath}`);
  }
  const baseName = filePath.split("/").pop() || filePath;
  const loc = data.line ? `${baseName}:${data.line}` : baseName;
  const msg = `Opened ${loc} in ${cfg.label}`;
  const cardSt = itemId ? document.getElementById(`saveStatus_${itemId}`) : null;
  if (cardSt) {
    cardSt.textContent = msg;
  }
  flashStatus(msg);
}

function openInIdea(filePath, needleOrLine, itemId) {
  return openInEditor("idea", filePath, needleOrLine, itemId);
}

function openInVscode(filePath, needleOrLine, itemId) {
  return openInEditor("vscode", filePath, needleOrLine, itemId);
}

async function commitMarked() {
  if (!latestSummary) return;
  const toCommit = (latestSummary.files || []).filter(f => f.marked && f.dirty).map(f => f.file_path);
  if (!toCommit.length) {
    alert("No dirty marked files to commit.");
    return;
  }
  const msg = document.getElementById("commitMsg").value.trim() || "Review accepted accent fix suggestions";
  const resp = await fetch("/api/commit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file_paths: toCommit, commit_message: msg }),
  });
  const data = await resp.json();
  if (!resp.ok) return alert(data.detail || "Commit failed");
  document.getElementById("commitResult").textContent = (data.results || [])
    .map(r => `${r.repo_root}: ${r.sha || "no-op"} (${r.committed.length} files)`).join("\n");
  await loadItems(currentPage);
}

function changePage(delta) {
  const nxt = Math.max(1, Math.min(totalPages, currentPage + delta));
  if (nxt !== currentPage) loadItems(nxt);
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
  if (nextOpen && typeof document.querySelector === "function") {
    const hdr = document.querySelector("header");
    if (hdr && typeof hdr.getBoundingClientRect === "function") {
      const topPx = Math.max(48, Math.round(hdr.getBoundingClientRect().bottom + 8));
      pane.style.top = `${topPx}px`;
      pane.style.maxHeight = `calc(100vh - ${topPx + 14}px)`;
    }
  }
  if (pane.style) pane.style.display = nextOpen ? "flex" : "none";
  const btn = typeof document.getElementById === "function" ? document.getElementById("helpBtn") : null;
  if (btn && btn.classList) btn.classList.toggle("active", nextOpen);
}

function closeHelpPane() {
  toggleHelpPane(false);
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
    || (tag === "INPUT" && tgt.type !== "radio" && tgt.type !== "checkbox" && tgt.type !== "button")
    || Boolean(tgt?.isContentEditable);
}

function getItemFileKey(it) {
  if (!it) return "";
  return it.vp_file || it.display_file || it.pp_file || it.rel_path || "";
}

function stepMainFileGroup(delta) {
  if (!currentItems.length) return false;
  const distinct = [];
  for (const it of currentItems) {
    const k = getItemFileKey(it);
    if (k && !distinct.includes(k)) distinct.push(k);
  }
  if (distinct.length < 2) return false;
  const curIdx = focusedIdx >= 0 && focusedIdx < currentItems.length ? focusedIdx : 0;
  const curKey = getItemFileKey(currentItems[curIdx]);
  const filePos = distinct.indexOf(curKey);
  const nextFilePos = delta > 0
    ? (filePos < 0 ? 0 : Math.min(distinct.length - 1, filePos + 1))
    : (filePos <= 0 ? 0 : filePos - 1);
  const targetKey = distinct[nextFilePos];
  const targetIdx = currentItems.findIndex(it => getItemFileKey(it) === targetKey);
  if (targetIdx < 0) return false;
  setFocused(targetIdx);
  document.getElementById(`card_${targetIdx}`)?.scrollIntoView({ block: "start" });
  return true;
}

bindModifierHintTracking();

if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
  window.addEventListener("keydown", (ev) => {
    const delta = getCtrlPageStepDelta(ev);
    if (delta) {
      ev.__padaSvaraSidebarPageHandled = true;
      if (typeof ev.preventDefault === "function") ev.preventDefault();
      if (typeof ev.stopPropagation === "function") ev.stopPropagation();
      stepSidebarFile(delta);
      return;
    }
    const mainDelta = getShiftPageStepDelta(ev);
    if (mainDelta && !_isTextEditingTarget(ev.target)) {
      if (stepMainFileGroup(mainDelta)) {
        ev.__padaSvaraMainFilePageHandled = true;
        if (typeof ev.preventDefault === "function") ev.preventDefault();
        if (typeof ev.stopPropagation === "function") ev.stopPropagation();
      }
    }
  }, true);
}

document.addEventListener("keydown", (ev) => {
  _syncModifierClasses(ev);
  const sidebarDelta = !ev?.__padaSvaraSidebarPageHandled ? getCtrlPageStepDelta(ev) : 0;
  if (sidebarDelta) {
    ev.__padaSvaraSidebarPageHandled = true;
    if (typeof ev.preventDefault === "function") ev.preventDefault();
    if (typeof ev.stopPropagation === "function") ev.stopPropagation();
    stepSidebarFile(sidebarDelta);
    return;
  }
  const mainDelta = !ev?.__padaSvaraMainFilePageHandled ? getShiftPageStepDelta(ev) : 0;
  if (mainDelta && !_isTextEditingTarget(ev.target)) {
    if (stepMainFileGroup(mainDelta)) {
      ev.__padaSvaraMainFilePageHandled = true;
      if (typeof ev.preventDefault === "function") ev.preventDefault();
      if (typeof ev.stopPropagation === "function") ev.stopPropagation();
      return;
    }
  }
  const target = ev.target;
  const id = (target && target.id) || "";
  const tag = (target && target.tagName || "").toUpperCase();

  if (ev.key === "Escape") {
    if (isHelpPaneOpen()) {
      ev.preventDefault();
      closeHelpPane();
      return;
    }
    const diffDlg = typeof document !== "undefined" && typeof document.getElementById === "function"
      ? document.getElementById("diffDialog")
      : null;
    if (diffDlg && diffDlg.open) {
      return;
    }
    if (!ev.altKey && !ev.ctrlKey && !ev.metaKey) {
      ev.preventDefault();
      focusMainPane();
      return;
    }
  }
  if (ev.key === "?" && !ev.ctrlKey && !ev.altKey && !ev.metaKey) {
    const isTextInput = tag === "TEXTAREA" || tag === "SELECT"
      || (tag === "INPUT" && target.type !== "radio" && target.type !== "checkbox" && target.type !== "button")
      || !!target?.isContentEditable;
    if (!isTextInput) {
      ev.preventDefault();
      toggleHelpPane();
      return;
    }
  }

  // Ctrl+R rescans directory even when list is empty
  if ((ev.key === "r" || ev.key === "R") && ev.ctrlKey && !ev.altKey && !ev.metaKey && !ev.shiftKey) {
    ev.preventDefault();
    refreshDirectory();
    return;
  }

  // Ctrl+Z undoes last submitted review action (outside text fields so inline typing keeps native undo)
  if ((ev.key === "z" || ev.key === "Z") && ev.ctrlKey && !ev.altKey && !ev.metaKey && !ev.shiftKey) {
    const isTextInput = tag === "TEXTAREA" || (tag === "INPUT" && target.type !== "radio" && target.type !== "checkbox");
    if (!isTextInput) {
      ev.preventDefault();
      undoLastAction();
      return;
    }
  }

  // Alt+U Pending, Alt+A Accepted, Alt+R Rejected, Alt+W With !!, Alt+L/0 All (work even when list is empty)
  if (ev.altKey && !ev.ctrlKey && !ev.metaKey) {
    const ak = (ev.key || "").toLowerCase();
    const ac = ev.code || "";
    const statusShortcuts = {
      u: "pending",
      a: "accept",
      r: "reject",
      w: "has_feedback",
      l: "all",
      "0": "all",
    };
    const codeShortcuts = {
      KeyU: "pending",
      KeyA: "accept",
      KeyR: "reject",
      KeyW: "has_feedback",
      KeyL: "all",
      Digit0: "all",
    };
    const nextFilter = statusShortcuts[ak] || codeShortcuts[ac];
    if (nextFilter) {
      ev.preventDefault();
      setStatusFilter(nextFilter);
      return;
    }
  }

  if (!currentItems.length) return;

  // Alt+1 Accept, Alt+2 Reject, Alt+3 Focus feedback (work even inside feedback textbox or radio)
  if (ev.altKey && !ev.ctrlKey && !ev.metaKey) {
    if (ev.key === "1" || ev.code === "Digit1") {
      ev.preventDefault();
      if (focusedIdx >= 0) selectRadio(focusedIdx, "accept", true);
      return;
    }
    if (ev.key === "2" || ev.code === "Digit2") {
      ev.preventDefault();
      if (focusedIdx >= 0) selectRadio(focusedIdx, "reject", true);
      return;
    }
    if (ev.key === "3" || ev.code === "Digit3") {
      ev.preventDefault();
      if (focusedIdx >= 0) focusFeedback(focusedIdx);
      return;
    }
  }

  // Ctrl+Enter = Accept + Submit in one step (works anywhere on card, including inside feedback input)
  if (ev.key === "Enter" && ev.ctrlKey && !ev.altKey && !ev.metaKey) {
    if (focusedIdx >= 0 && id !== "dirInput" && id !== "commitMsg") {
      ev.preventDefault();
      selectRadio(focusedIdx, "accept");
      submitCard(focusedIdx);
      return;
    }
  }

  // Enter submits the focused card (when inside a card's feedback input, radio, or outside header controls)
  if (ev.key === "Enter" && !ev.shiftKey && !ev.altKey && !ev.ctrlKey && !ev.metaKey) {
    if (id === "dirInput") {
      ev.preventDefault();
      scanDirectory();
      return;
    }
    const isFbInput = id.startsWith("fbInput_");
    const isCardRadio = target && target.type === "radio" && target.name && target.name.startsWith("dec_");
    const isBlockedControl = tag === "TEXTAREA" || tag === "SELECT" || tag === "BUTTON" || tag === "A" || (tag === "INPUT" && !isFbInput && !isCardRadio);
    if (!isBlockedControl && focusedIdx >= 0) {
      ev.preventDefault();
      submitCard(focusedIdx);
      return;
    }
  }

  if (tag === "TEXTAREA" || tag === "SELECT" || (tag === "INPUT" && target.type !== "radio")) return;
  if (ev.key === "j" || ev.key === "ArrowDown") {
    ev.preventDefault();
    setFocused(Math.min(currentItems.length - 1, focusedIdx + 1));
    document.getElementById(`card_${focusedIdx}`)?.scrollIntoView({ block: "nearest" });
  } else if (ev.key === "k" || ev.key === "ArrowUp") {
    ev.preventDefault();
    setFocused(Math.max(0, focusedIdx - 1));
    document.getElementById(`card_${focusedIdx}`)?.scrollIntoView({ block: "nearest" });
  } else if (ev.key === "ArrowLeft" && !ev.ctrlKey && !ev.altKey && !ev.metaKey && !ev.shiftKey && focusedIdx >= 0) {
    ev.preventDefault();
    toggleCardCollapse(focusedIdx, true);
  } else if (ev.key === "ArrowRight" && !ev.ctrlKey && !ev.altKey && !ev.metaKey && !ev.shiftKey && focusedIdx >= 0) {
    ev.preventDefault();
    toggleCardCollapse(focusedIdx, false);
  } else if (ev.key === "u" && focusedIdx >= 0) {
    resetCard(focusedIdx);
  }
});

init();
</script>
</body>
</html>
"""


if __name__ == "__main__":
  raise SystemExit(main())
