"""FastAPI web GUI and CLI for local_diff_san."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any, Sequence

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

from pull_requests_san import local_helpers
from pull_requests_san.assets import render_html_with_shared_assets
from pull_requests_san.diff import VALID_DIFF_MODES, VALID_NORMALIZATIONS

from local_diff_san import core


_TEMPLATE_PATH = Path(__file__).with_name("templates") / "local_diff.html"


def load_html_template(*, static_mode: bool = False) -> str:
  """Read the local diff viewer HTML template with shared diff CSS/JS/engine assets injected."""
  raw = _TEMPLATE_PATH.read_text(encoding="utf-8")
  flag = "true" if static_mode else "false"
  raw = raw.replace("/* {{LOCAL_DIFF_STATIC_FLAG}} */ false", flag)
  return render_html_with_shared_assets(raw)


def render_static_html() -> str:
  """Render a self-contained static HTML website bundle for client-side file/text diffing."""
  return load_html_template(static_mode=True)


def export_static_html(output_path: str | Path) -> Path:
  """Write the self-contained static website HTML bundle to ``output_path``."""
  dest = Path(output_path).expanduser().resolve()
  dest.parent.mkdir(parents=True, exist_ok=True)
  dest.write_text(render_static_html(), encoding="utf-8")
  return dest


def _resolve_idea_binary() -> str | None:
  return local_helpers.resolve_idea_binary()


def _resolve_vscode_binary() -> str | None:
  return local_helpers.resolve_vscode_binary()


def _resolve_goldendict_binary() -> str | None:
  return local_helpers.resolve_goldendict_binary()


class _CompareRequest(BaseModel):
  mode: str = "git_head"  # "file_pair" | "git_head" | "git_dir"
  left_path: str | None = None
  right_path: str | None = None
  path: str | None = None
  rev: str = core.DEFAULT_REV
  active_id: str | None = None
  diff_mode: str = "word"
  normalize: str = "none"
  ignore_punct: bool = False


class _CommitRequest(BaseModel):
  file_paths: list[str]
  commit_message: str = ""
  push: bool = False
  active_id: str | None = None
  diff_mode: str = "word"
  normalize: str = "none"
  ignore_punct: bool = False


class _StateHolder:
  def __init__(self, initial_spec: core.LocalDiffSpec) -> None:
    self.lock = threading.RLock()
    self.spec = initial_spec


def _spec_from_request(req: _CompareRequest, current: core.LocalDiffSpec) -> core.LocalDiffSpec:
  m = (req.mode or current.mode).strip().lower()
  rev = (req.rev or current.rev or core.DEFAULT_REV).strip() or core.DEFAULT_REV
  if m == "file_pair":
    lp = (req.left_path or "").strip()
    rp = (req.right_path or "").strip()
    return core.LocalDiffSpec(
      mode="file_pair",
      left_path=str(Path(lp).expanduser().resolve()) if lp else current.left_path,
      right_path=str(Path(rp).expanduser().resolve()) if rp else current.right_path,
      rev=rev,
    )
  raw_p = (req.path or "").strip()
  if raw_p:
    p = Path(raw_p).expanduser()
    resolved = p.resolve() if p.exists() else p.absolute()
    if resolved.is_dir():
      return core.LocalDiffSpec(mode="git_dir", paths=[str(resolved)], rev=rev)
    return core.LocalDiffSpec(mode="git_head", paths=[str(resolved)], rev=rev)
  return core.LocalDiffSpec(mode=current.mode, left_path=current.left_path, right_path=current.right_path, paths=list(current.paths), rev=rev)


def create_app(*, initial_spec: core.LocalDiffSpec | None = None) -> FastAPI:
  """Create the FastAPI application for local file and Git HEAD diff viewing."""
  holder = _StateHolder(initial_spec or core.resolve_spec_from_paths([]))
  app = FastAPI(title="local-diff-san — Sanskrit Local & Git Diff Viewer")

  @app.get("/", response_class=HTMLResponse)
  def index() -> HTMLResponse:
    return HTMLResponse(load_html_template())

  @app.get("/api/meta")
  def api_meta() -> dict[str, Any]:
    with holder.lock:
      return {
        "spec": holder.spec.to_dict(),
        "diff_modes": list(VALID_DIFF_MODES),
        "normalizations": list(VALID_NORMALIZATIONS),
      }

  @app.get("/api/diff")
  def api_diff(
    active_id: str | None = Query(None),
    diff_mode: str = Query("word"),
    normalize: str = Query("none"),
    ignore_punct: bool = Query(False),
    rev: str | None = Query(None),
  ) -> dict[str, Any]:
    try:
      with holder.lock:
        eff_spec = holder.spec
        if rev is not None and rev.strip():
          eff_spec = core.LocalDiffSpec(
            mode=holder.spec.mode,
            left_path=holder.spec.left_path,
            right_path=holder.spec.right_path,
            paths=list(holder.spec.paths),
            rev=rev.strip(),
          )
        return core.build_diff_workspace(
          eff_spec,
          active_id=active_id,
          char_diff_mode=diff_mode,
          normalize=normalize,
          ignore_punct=ignore_punct,
        )
    except FileNotFoundError as exc:
      raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
      raise HTTPException(status_code=400, detail=str(exc)) from exc

  @app.post("/api/compare")
  def api_compare(req: _CompareRequest) -> dict[str, Any]:
    try:
      with holder.lock:
        next_spec = _spec_from_request(req, holder.spec)
        res = core.build_diff_workspace(
          next_spec,
          active_id=req.active_id,
          char_diff_mode=req.diff_mode,
          normalize=req.normalize,
          ignore_punct=req.ignore_punct,
        )
        holder.spec = next_spec
        return res
    except FileNotFoundError as exc:
      raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
      raise HTTPException(status_code=400, detail=str(exc)) from exc

  @app.post("/api/open_in_idea")
  def api_open_in_idea(req: local_helpers.OpenPathIdeaRequest) -> dict[str, Any]:
    try:
      return local_helpers.open_path_in_intellij(
        req.path,
        line=req.line,
        idea_resolver=_resolve_idea_binary,
        popen_fn=subprocess.Popen,
      )
    except FileNotFoundError as exc:
      raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
      raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
      raise HTTPException(status_code=500, detail=str(exc)) from exc

  @app.post("/api/open_in_vscode")
  def api_open_in_vscode(req: local_helpers.OpenPathIdeaRequest) -> dict[str, Any]:
    try:
      return local_helpers.open_path_in_vscode(
        req.path,
        line=req.line,
        code_resolver=_resolve_vscode_binary,
        popen_fn=subprocess.Popen,
      )
    except FileNotFoundError as exc:
      raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
      raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
      raise HTTPException(status_code=500, detail=str(exc)) from exc

  @app.post("/api/open_in_goldendict")
  def api_open_in_goldendict(req: local_helpers.OpenGoldenDictRequest) -> dict[str, Any]:
    try:
      return local_helpers.open_in_goldendict(
        req.word,
        gd_resolver=_resolve_goldendict_binary,
        popen_fn=subprocess.Popen,
      )
    except ValueError as exc:
      raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
      raise HTTPException(status_code=500, detail=str(exc)) from exc

  @app.post("/api/commit")
  def api_commit(req: _CommitRequest) -> dict[str, Any]:
    try:
      with holder.lock:
        results = core.commit_files(req.file_paths, req.commit_message, push=req.push)
        workspace = core.build_diff_workspace(
          holder.spec,
          active_id=req.active_id,
          char_diff_mode=req.diff_mode,
          normalize=req.normalize,
          ignore_punct=req.ignore_punct,
        )
        push_ok = all(r.get("pushed", False) for r in results if r.get("committed")) if req.push else True
        return {"results": results, "push_ok": push_ok, "workspace": workspace}
    except FileNotFoundError as exc:
      raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
      raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
      raise HTTPException(status_code=500, detail=str(exc)) from exc

  return app


def main(argv: Sequence[str] | None = None) -> int:
  p = argparse.ArgumentParser(
    description="Launch local-diff-san — compare a local file vs Git HEAD (or 2 given files) with Sanskrit word/char highlighting.",
  )
  p.add_argument(
    "paths",
    nargs="*",
    help="0 args: changed files in current repo vs --rev; 1 file: compare file vs --rev; 1 dir: changed files in dir; 2 files: compare file1 vs file2.",
  )
  p.add_argument("--rev", default=core.DEFAULT_REV, help="Git revision to compare against (default: HEAD).")
  p.add_argument("--head", action="store_true", help="Always compare passed path(s) against --rev even when 2 files are given.")
  p.add_argument("--export-static", default="", help="Write a self-contained pure-JS static HTML page to FILE and exit.")
  p.add_argument("--port", type=int, default=8772, help="Port to bind on 127.0.0.1 (default: 8772 or next free port).")
  p.add_argument("--no-browser", action="store_true", help="Do not auto-open browser window.")
  args = p.parse_args(argv)

  if args.export_static:
    out = export_static_html(args.export_static)
    print(f"Exported standalone static local-diff-san page to {out}", flush=True)
    return 0

  spec = core.resolve_spec_from_paths(args.paths, rev=args.rev, force_head=args.head)
  try:
    core.build_diff_workspace(spec)
  except (FileNotFoundError, ValueError) as exc:
    print(f"Error: {exc}", file=sys.stderr)
    return 2

  app = create_app(initial_spec=spec)
  local_helpers.kill_other_server_instances(
    entry_names=("local-diff-san", "local_diff_san"),
    module_names=("local_diff_san", "local_diff_san.__main__", "local_diff_san.server"),
    script_suffixes=("local_diff_san/__main__.py", "local_diff_san/server.py"),
    label="local-diff-san",
  )
  port = local_helpers.find_free_port(args.port)
  url = f"http://127.0.0.1:{port}/"
  print(f"local-diff-san running at {url} (mode={spec.mode})", flush=True)
  if not args.no_browser:
    threading.Thread(target=local_helpers.launch_browser, args=(url,), daemon=True).start()
  uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
  return 0
