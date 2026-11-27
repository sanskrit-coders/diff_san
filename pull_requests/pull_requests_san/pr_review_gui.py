"""FastAPI web GUI for reviewing GitHub pull requests across Sanskrit repositories.

Run from the command line::

  pull_requests_san --owner vishvAsa
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import Any, Callable, Sequence, TypeVar
from urllib.parse import urlparse
import webbrowser

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import requests
import uvicorn

from pull_requests_san import pr_review


_T = TypeVar("_T")
_TEMPLATE_PATH = Path(__file__).with_name("templates") / "pr_review.html"


def load_html_template() -> str:
  """Read the standalone HTML/CSS/JS review application template."""
  return _TEMPLATE_PATH.read_text(encoding="utf-8")


class _DiffOptionsModel(BaseModel):
  diff_mode: str = "grapheme"
  normalize: str = "none"
  ignore_punct: bool = False


class _RefreshRequest(_DiffOptionsModel):
  owner: str = pr_review.DEFAULT_OWNER
  state: str = "open"
  repo: str = ""
  active_owner: str | None = None
  active_repo: str | None = None
  active_number: int | None = None


class _IssueCommentRequest(_DiffOptionsModel):
  body: str


class _InlineCommentRequest(_DiffOptionsModel):
  path: str
  line: int
  side: str = "RIGHT"
  body: str
  start_line: int | None = None
  start_side: str | None = None
  commit_id: str | None = None


class _ReplyCommentRequest(_DiffOptionsModel):
  comment_id: int
  body: str


class _DraftCommentRequest(BaseModel):
  path: str
  line: int
  side: str = "RIGHT"
  body: str
  start_line: int | None = None
  start_side: str | None = None


class _SubmitReviewRequest(_DiffOptionsModel):
  event: str = "COMMENT"
  body: str = ""
  include_drafts: bool = True
  extra_comments: list[dict[str, Any]] | None = None


class _ResolveThreadRequest(_DiffOptionsModel):
  thread_id: str
  resolve: bool = True


class _PrStateRequest(_DiffOptionsModel):
  state: str = "closed"


class _MergePrRequest(_DiffOptionsModel):
  merge_method: str = "merge"
  commit_title: str = ""
  commit_message: str = ""
  sha: str | None = None


class _OpenIdeaRequest(BaseModel):
  repo: str
  path: str
  line: int | None = None


class _OpenGoldenDictRequest(BaseModel):
  word: str


class _OpenBrowserRequest(BaseModel):
  url: str


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
  for cand in candidates:
    try:
      resolved = cand.resolve(strict=True)
    except OSError:
      continue
    if resolved.is_file() and os.access(resolved, os.X_OK):
      return str(cand if os.access(cand, os.X_OK) else resolved)
  return None


def open_local_file_in_intellij(
  local_org_dir: str | Path,
  repo: str,
  rel_path: str,
  *,
  line: int | None = None,
) -> dict[str, Any]:
  """Open a locally checked-out file under ``local_org_dir/repo/rel_path`` in IntelliJ IDEA."""
  if not repo or "/" in repo or "\\" in repo or repo in (".", ".."):
    raise ValueError("Invalid repository name")
  if not rel_path or Path(rel_path).is_absolute():
    raise ValueError("Invalid relative file path")
  org_root = Path(local_org_dir).expanduser().resolve()
  repo_root = (org_root / repo).resolve()
  if not repo_root.is_relative_to(org_root):
    raise ValueError("Invalid repository path outside org directory")
  target = (repo_root / rel_path).resolve()
  if not target.is_relative_to(repo_root):
    raise ValueError("Invalid local path outside repository directory")
  if not target.is_file():
    raise FileNotFoundError(f"Local file not found: {target}")
  idea_bin = _resolve_idea_binary()
  if not idea_bin:
    raise RuntimeError("IntelliJ IDEA CLI binary ('idea') not found")
  cmd = [idea_bin]
  if line is not None and line >= 1:
    cmd.extend(["--line", str(line)])
  cmd.append(str(target))
  subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  return {"ok": True, "file_path": str(target), "line": line, "command": cmd}


def _resolve_goldendict_binary() -> str | None:
  for name in ("goldendict", "goldendict-ng"):
    path = shutil.which(name)
    if path:
      return path
  return None


def open_in_goldendict(word: str) -> dict[str, Any]:
  """Look up unaccented ``word`` in GoldenDict."""
  query = re.sub(r"[\u0951-\u0954\u1cd0-\u1cff\u0300-\u036f]", "", (word or "")).strip()
  if not query:
    raise ValueError("Empty lookup word")
  gd_bin = _resolve_goldendict_binary()
  if not gd_bin:
    raise RuntimeError("GoldenDict CLI binary ('goldendict') not found")
  cmd = [gd_bin, query]
  subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  return {"ok": True, "word": query, "command": cmd}


def open_url_in_browser(url: str) -> dict[str, Any]:
  """Open a validated GitHub PR/repo URL in the user's main web browser."""
  cleaned = (url or "").strip()
  parsed = urlparse(cleaned)
  if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or parsed.username or parsed.password:
    raise ValueError(f"Only https://github.com/ URLs are allowed, got {url!r}")
  opened = webbrowser.open_new_tab(cleaned)
  return {"ok": bool(opened), "url": cleaned}


class _AppStore:
  def __init__(self, session: pr_review.PRReviewSession) -> None:
    self.lock = threading.RLock()
    self.session = session

  def run(self, fn: Callable[[], _T]) -> _T:
    try:
      with self.lock:
        return fn()
    except pr_review.GitHubAPIError as exc:
      status = exc.status_code if 400 <= exc.status_code < 600 else 502
      raise HTTPException(status_code=status, detail=str(exc)) from exc
    except requests.RequestException as exc:
      raise HTTPException(status_code=502, detail=f"GitHub network error: {exc}") from exc
    except FileNotFoundError as exc:
      raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
      raise HTTPException(status_code=400, detail=str(exc)) from exc


def _pr_detail_payload(session: pr_review.PRReviewSession, detail: pr_review.PullRequestDetail) -> dict[str, Any]:
  s = detail.summary
  return {**detail.to_dict(), "pending_drafts": session.get_drafts(s.owner, s.repo, s.number)}


def _pr_list_payload(owner: str, state: str, prs: list[pr_review.PullRequestSummary]) -> dict[str, Any]:
  repos = sorted({p.repo for p in prs if p.repo})
  return {
    "owner": pr_review.normalize_owner(owner),
    "state": state,
    "total": len(prs),
    "repos": repos,
    "prs": [p.to_dict() for p in prs],
  }


def _register_read_routes(
  app: FastAPI,
  store: _AppStore,
  *,
  default_owner: str,
  default_state: str,
  default_repo: str,
) -> None:
  session = store.session

  @app.get("/", response_class=HTMLResponse)
  def index() -> HTMLResponse:
    return HTMLResponse(load_html_template())

  @app.get("/api/meta")
  def api_meta() -> dict[str, Any]:
    return store.run(lambda: {
      "default_owner": session.default_owner,
      "default_state": default_state,
      "default_repo": default_repo,
      "has_token": bool(session.client.token),
      "auth_user": session.client.get_authenticated_user(),
      "diff_modes": list(pr_review.VALID_DIFF_MODES),
      "normalizations": list(pr_review.VALID_NORMALIZATIONS),
      "review_events": list(pr_review.VALID_REVIEW_EVENTS),
      "merge_methods": list(pr_review.VALID_MERGE_METHODS),
      "local_org_dir": session.client.local_org_dir,
    })

  @app.get("/api/prs")
  def api_prs(
    owner: str = Query(default_owner),
    state: str = Query(default_state),
    repo: str = Query(default_repo),
    refresh: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
  ) -> dict[str, Any]:
    return store.run(
      lambda: _pr_list_payload(owner, state, session.list_prs(owner=owner, state=state, repo=repo, refresh=refresh, limit=limit))
    )

  @app.post("/api/refresh")
  def api_refresh(req: _RefreshRequest) -> dict[str, Any]:
    def _op() -> dict[str, Any]:
      active_tuple = (req.active_owner, req.active_repo, req.active_number) if (req.active_owner and req.active_repo and req.active_number) else None
      res = session.refresh_all(
        owner=req.owner,
        state=req.state,
        repo=req.repo,
        active_pr=active_tuple,
        char_diff_mode=req.diff_mode,
        normalize=req.normalize,
        ignore_punct=req.ignore_punct,
      )
      out = _pr_list_payload(req.owner, req.state, res["prs"])
      out["active_pr"] = _pr_detail_payload(session, res["active_detail"]) if res["active_detail"] else None
      return out

    return store.run(_op)

  @app.get("/api/pr/{owner}/{repo}/{number}")
  def api_pr_detail(
    owner: str,
    repo: str,
    number: int,
    refresh: bool = Query(False),
    diff_mode: str = Query("grapheme"),
    normalize: str = Query("none"),
    ignore_punct: bool = Query(False),
  ) -> dict[str, Any]:
    return store.run(
      lambda: _pr_detail_payload(
        session,
        session.get_pr(
          owner,
          repo,
          number,
          refresh=refresh,
          char_diff_mode=diff_mode,
          normalize=normalize,
          ignore_punct=ignore_punct,
        ),
      )
    )

  @app.get("/api/pr/{owner}/{repo}/{number}/changelist")
  def api_pr_changelist(
    owner: str,
    repo: str,
    number: int,
    sha: str = Query(""),
    refresh: bool = Query(False),
    diff_mode: str = Query("grapheme"),
    normalize: str = Query("none"),
    ignore_punct: bool = Query(False),
  ) -> dict[str, Any]:
    def _op() -> dict[str, Any]:
      res = session.get_changelist(
        owner,
        repo,
        number,
        sha,
        refresh=refresh,
        char_diff_mode=diff_mode,
        normalize=normalize,
        ignore_punct=ignore_punct,
      )
      c_obj = res.get("commit")
      return {
        "sha": res.get("sha") or "",
        "parent_sha": res.get("parent_sha") or "",
        "commit": c_obj.to_dict() if hasattr(c_obj, "to_dict") else c_obj,
        "char_diff_mode": res.get("char_diff_mode") or diff_mode,
        "normalize": res.get("normalize") or normalize,
        "ignore_punct": bool(res.get("ignore_punct", False)),
        "files": [f.to_dict() for f in (res.get("files") or [])],
      }

    return store.run(_op)

  @app.get("/api/pr/{owner}/{repo}/{number}/file_full")
  def api_file_full(
    owner: str,
    repo: str,
    number: int,
    filename: str = Query(...),
    commit_sha: str = Query(""),
    diff_mode: str = Query("grapheme"),
    normalize: str = Query("none"),
    ignore_punct: bool = Query(False),
  ) -> dict[str, Any]:
    return store.run(
      lambda: {
        "file": session.get_full_file(
          owner,
          repo,
          number,
          filename,
          commit_sha=commit_sha,
          char_diff_mode=diff_mode,
          normalize=normalize,
          ignore_punct=ignore_punct,
        ).to_dict()
      }
    )


def _register_mutation_routes(app: FastAPI, store: _AppStore) -> None:
  session = store.session

  @app.post("/api/pr/{owner}/{repo}/{number}/issue_comment")
  def api_issue_comment(owner: str, repo: str, number: int, req: _IssueCommentRequest) -> dict[str, Any]:
    def _op() -> dict[str, Any]:
      res = session.submit_issue_comment(
        owner, repo, number, req.body,
        char_diff_mode=req.diff_mode, normalize=req.normalize, ignore_punct=req.ignore_punct,
      )
      return {"created": res["created"], "pr": _pr_detail_payload(session, res["detail"])}

    return store.run(_op)

  @app.post("/api/pr/{owner}/{repo}/{number}/inline_comment")
  def api_inline_comment(owner: str, repo: str, number: int, req: _InlineCommentRequest) -> dict[str, Any]:
    def _op() -> dict[str, Any]:
      res = session.submit_inline_comment(
        owner, repo, number,
        path=req.path, line=req.line, side=req.side, body=req.body,
        start_line=req.start_line, start_side=req.start_side, commit_id=req.commit_id,
        char_diff_mode=req.diff_mode, normalize=req.normalize, ignore_punct=req.ignore_punct,
      )
      return {"created": res["created"], "pr": _pr_detail_payload(session, res["detail"])}

    return store.run(_op)

  @app.post("/api/pr/{owner}/{repo}/{number}/reply")
  def api_reply_comment(owner: str, repo: str, number: int, req: _ReplyCommentRequest) -> dict[str, Any]:
    def _op() -> dict[str, Any]:
      res = session.reply_to_comment(
        owner, repo, number, req.comment_id, req.body,
        char_diff_mode=req.diff_mode, normalize=req.normalize, ignore_punct=req.ignore_punct,
      )
      return {"created": res["created"], "pr": _pr_detail_payload(session, res["detail"])}

    return store.run(_op)

  @app.post("/api/pr/{owner}/{repo}/{number}/draft_comment")
  def api_add_draft(owner: str, repo: str, number: int, req: _DraftCommentRequest) -> dict[str, Any]:
    def _op() -> dict[str, Any]:
      draft = session.add_draft(
        owner, repo, number, path=req.path, line=req.line, side=req.side, body=req.body,
        start_line=req.start_line, start_side=req.start_side,
      )
      return {"draft": draft, "pending_drafts": session.get_drafts(owner, repo, number)}

    return store.run(_op)

  @app.delete("/api/pr/{owner}/{repo}/{number}/draft_comment/{draft_id}")
  def api_delete_draft(owner: str, repo: str, number: int, draft_id: str) -> dict[str, Any]:
    def _op() -> dict[str, Any]:
      if not session.delete_draft(owner, repo, number, draft_id):
        raise FileNotFoundError(f"Draft comment {draft_id!r} not found")
      return {"removed": True, "pending_drafts": session.get_drafts(owner, repo, number)}

    return store.run(_op)

  @app.post("/api/pr/{owner}/{repo}/{number}/review")
  def api_submit_review(owner: str, repo: str, number: int, req: _SubmitReviewRequest) -> dict[str, Any]:
    def _op() -> dict[str, Any]:
      res = session.submit_review(
        owner, repo, number, event=req.event, body=req.body,
        include_drafts=req.include_drafts, extra_comments=req.extra_comments,
        char_diff_mode=req.diff_mode, normalize=req.normalize, ignore_punct=req.ignore_punct,
      )
      return {"created": res["created"], "pr": _pr_detail_payload(session, res["detail"])}

    return store.run(_op)

  @app.post("/api/pr/{owner}/{repo}/{number}/resolve_thread")
  def api_resolve_thread(owner: str, repo: str, number: int, req: _ResolveThreadRequest) -> dict[str, Any]:
    def _op() -> dict[str, Any]:
      res = session.resolve_thread(
        owner, repo, number, req.thread_id, resolve=req.resolve,
        char_diff_mode=req.diff_mode, normalize=req.normalize, ignore_punct=req.ignore_punct,
      )
      return {"thread": res["thread"], "pr": _pr_detail_payload(session, res["detail"])}

    return store.run(_op)

  @app.post("/api/pr/{owner}/{repo}/{number}/state")
  def api_set_pr_state(owner: str, repo: str, number: int, req: _PrStateRequest) -> dict[str, Any]:
    def _op() -> dict[str, Any]:
      res = session.set_pr_state(
        owner, repo, number, state=req.state,
        char_diff_mode=req.diff_mode, normalize=req.normalize, ignore_punct=req.ignore_punct,
      )
      return {"updated": res["updated"], "pr": _pr_detail_payload(session, res["detail"])}

    return store.run(_op)

  @app.post("/api/pr/{owner}/{repo}/{number}/merge")
  def api_merge_pr(owner: str, repo: str, number: int, req: _MergePrRequest) -> dict[str, Any]:
    def _op() -> dict[str, Any]:
      res = session.merge_pr(
        owner,
        repo,
        number,
        merge_method=req.merge_method,
        commit_title=req.commit_title,
        commit_message=req.commit_message,
        sha=req.sha,
        char_diff_mode=req.diff_mode,
        normalize=req.normalize,
        ignore_punct=req.ignore_punct,
      )
      return {"merged": res["merged"], "pr": _pr_detail_payload(session, res["detail"])}

    return store.run(_op)


def _register_local_routes(app: FastAPI, store: _AppStore) -> None:
  session = store.session

  @app.post("/api/open_in_idea")
  def api_open_in_idea(req: _OpenIdeaRequest) -> dict[str, Any]:
    org_dir = session.client.local_org_dir
    if not org_dir:
      raise HTTPException(status_code=400, detail="Local org directory is not configured")
    try:
      return open_local_file_in_intellij(org_dir, req.repo, req.path, line=req.line)
    except FileNotFoundError as exc:
      raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
      raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
      raise HTTPException(status_code=500, detail=str(exc)) from exc

  @app.post("/api/open_in_goldendict")
  def api_open_in_goldendict(req: _OpenGoldenDictRequest) -> dict[str, Any]:
    try:
      return open_in_goldendict(req.word)
    except ValueError as exc:
      raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
      raise HTTPException(status_code=500, detail=str(exc)) from exc

  @app.post("/api/open_in_browser")
  def api_open_in_browser(req: _OpenBrowserRequest) -> dict[str, Any]:
    try:
      return open_url_in_browser(req.url)
    except ValueError as exc:
      raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
      raise HTTPException(status_code=500, detail=str(exc)) from exc


def create_app(
  *,
  default_owner: str = pr_review.DEFAULT_OWNER,
  default_state: str = "open",
  default_repo: str = "",
  token: str | None = None,
  local_org_dir: str | None = pr_review.DEFAULT_LOCAL_ORG_DIR,
  session: pr_review.PRReviewSession | None = None,
) -> FastAPI:
  """Create the FastAPI application for cross-repo GitHub PR character-level review."""
  if session is None:
    client = pr_review.GitHubClient(token=token, local_org_dir=local_org_dir)
    session = pr_review.PRReviewSession(client=client, default_owner=default_owner)
  store = _AppStore(session)
  app = FastAPI(title="GitHub PR Character-Level Reviewer")
  _register_read_routes(app, store, default_owner=default_owner, default_state=default_state, default_repo=default_repo)
  _register_mutation_routes(app, store)
  _register_local_routes(app, store)
  return app


def _find_free_port(preferred: int = 8770) -> int:
  for port in [preferred, *range(8771, 8800), 0]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
      try:
        s.bind(("127.0.0.1", port))
        return s.getsockname()[1]
      except OSError:
        continue
  return 8770


def _launch_browser(url: str) -> None:
  time.sleep(0.6)
  chrome = shutil.which("google-chrome") or shutil.which("chromium")
  if chrome and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    try:
      subprocess.Popen([chrome, f"--app={url}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
      return
    except Exception:
      pass
  webbrowser.open(url)


def main(argv: Sequence[str] | None = None) -> int:
  p = argparse.ArgumentParser(description="Launch pull_requests_san — GitHub PR Reviewer for Sanskrit scholarship.")
  p.add_argument("--owner", default=pr_review.DEFAULT_OWNER, help="GitHub organization/user (default: vishvAsa).")
  p.add_argument("--state", default="open", choices=["open", "closed", "merged", "all"], help="PR state filter (default: open).")
  p.add_argument("--repo", default="", help="Optional single repo name filter.")
  p.add_argument("--token", default=None, help="GitHub token (defaults to env, gh CLI config, or tokens.toml).")
  p.add_argument("--local-org-dir", default=pr_review.DEFAULT_LOCAL_ORG_DIR, help="Optional local checkout root for opening files in IDEA.")
  p.add_argument("--port", type=int, default=8770, help="Port to bind on 127.0.0.1 (default: 8770 or next free port).")
  p.add_argument("--no-browser", action="store_true", help="Do not auto-open browser window.")
  args = p.parse_args(argv)

  app = create_app(
    default_owner=args.owner,
    default_state=args.state,
    default_repo=args.repo,
    token=args.token,
    local_org_dir=args.local_org_dir,
  )
  port = _find_free_port(args.port)
  url = f"http://127.0.0.1:{port}/"
  print(f"GitHub PR Reviewer running at {url}", flush=True)
  if not args.no_browser:
    threading.Thread(target=_launch_browser, args=(url,), daemon=True).start()
  uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
