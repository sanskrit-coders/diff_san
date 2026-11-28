"""FastAPI web GUI for reviewing GitHub pull requests across Sanskrit repositories.

Run from the command line::

  pull_requests_san --owner vishvAsa
"""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
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

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
import requests
import uvicorn

from pull_requests_san import local_helpers, pr_review
from pull_requests_san.assets import render_html_with_shared_assets


_T = TypeVar("_T")
_TEMPLATE_PATH = Path(__file__).with_name("templates") / "pr_review.html"


def load_html_template(*, static_mode: bool = False) -> str:
  """Read the standalone HTML/CSS/JS review application template with shared diff assets injected."""
  raw = _TEMPLATE_PATH.read_text(encoding="utf-8")
  flag = "true" if static_mode else "false"
  raw = raw.replace("/* {{PR_REVIEW_STATIC_FLAG}} */ false", flag)
  if not static_mode:
    raw = raw.replace("// {{PR_STATIC_ADAPTER_JS}}", "")
  return render_html_with_shared_assets(raw)


def render_static_html() -> str:
  """Render a self-contained static GitHub PR viewer HTML page powered by GitHub REST API + pure JS diffing."""
  return load_html_template(static_mode=True)


def export_static_html(output_path: str | Path) -> Path:
  """Write the self-contained static PR viewer HTML bundle to ``output_path``."""
  dest = Path(output_path).expanduser().resolve()
  dest.parent.mkdir(parents=True, exist_ok=True)
  dest.write_text(render_static_html(), encoding="utf-8")
  return dest


class _DiffOptionsModel(BaseModel):
  diff_mode: str = "grapheme"
  normalize: str = "none"
  ignore_punct: bool = False


class _RefreshRequest(_DiffOptionsModel):
  owner: str = pr_review.DEFAULT_OWNER
  state: str = "pend_comm"
  repo: str = ""
  limit: int | None = None
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


class _EditCommentBodyRequest(_DiffOptionsModel):
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
  return local_helpers.resolve_idea_binary()


def open_local_file_in_intellij(
  local_org_dir: str | Path,
  repo: str,
  rel_path: str,
  *,
  line: int | None = None,
) -> dict[str, Any]:
  """Open a locally checked-out file under ``local_org_dir/repo/rel_path`` in IntelliJ IDEA."""
  return local_helpers.open_local_file_in_intellij(
    local_org_dir,
    repo,
    rel_path,
    line=line,
    idea_resolver=_resolve_idea_binary,
    popen_fn=subprocess.Popen,
  )


def _resolve_goldendict_binary() -> str | None:
  return local_helpers.resolve_goldendict_binary()


def open_in_goldendict(word: str) -> dict[str, Any]:
  """Look up unaccented ``word`` in GoldenDict."""
  return local_helpers.open_in_goldendict(
    word,
    gd_resolver=_resolve_goldendict_binary,
    popen_fn=subprocess.Popen,
  )


def open_url_in_browser(url: str) -> dict[str, Any]:
  """Open a validated GitHub PR/repo URL in the user's main web browser."""
  return local_helpers.open_url_in_browser(url, open_tab_fn=webbrowser.open_new_tab)


class _AppStore:
  def __init__(self, session: pr_review.PRReviewSession) -> None:
    self.lock = threading.RLock()
    self.session = session
    self.state_version = 1
    self.active_sse_clients = 0
    self.shutdown_event = threading.Event()

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
      "app": "pull_requests_san",
      "has_active_ui": store.active_sse_clients > 0,
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

  @app.get("/api/events")
  async def api_events(request: Request) -> StreamingResponse:
    def _inc() -> None:
      with store.lock:
        store.active_sse_clients += 1

    def _dec() -> None:
      with store.lock:
        store.active_sse_clients = max(0, store.active_sse_clients - 1)

    return StreamingResponse(
      local_helpers.iter_version_sse_events(
        request,
        get_version=lambda: store.state_version,
        shutdown_event=store.shutdown_event,
        on_connect=_inc,
        on_disconnect=_dec,
      ),
      media_type="text/event-stream",
      headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

  @app.get("/api/prs")
  def api_prs(
    owner: str = Query(default_owner),
    state: str = Query(default_state),
    repo: str = Query(default_repo),
    refresh: bool = Query(False),
    limit: int | None = Query(None, ge=1, le=500),
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
        limit=req.limit,
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

  @app.patch("/api/pr/{owner}/{repo}/{number}/draft_comment/{draft_id}")
  def api_update_draft(owner: str, repo: str, number: int, draft_id: str, req: _EditCommentBodyRequest) -> dict[str, Any]:
    def _op() -> dict[str, Any]:
      draft = session.update_draft(owner, repo, number, draft_id, req.body)
      return {"draft": draft, "pending_drafts": session.get_drafts(owner, repo, number)}

    return store.run(_op)

  @app.patch("/api/pr/{owner}/{repo}/{number}/review_comment/{comment_id}")
  def api_edit_review_comment(owner: str, repo: str, number: int, comment_id: int, req: _EditCommentBodyRequest) -> dict[str, Any]:
    def _op() -> dict[str, Any]:
      res = session.edit_review_comment(
        owner, repo, number, comment_id, req.body,
        char_diff_mode=req.diff_mode, normalize=req.normalize, ignore_punct=req.ignore_punct,
      )
      return {"updated": res["updated"], "pr": _pr_detail_payload(session, res["detail"])}

    return store.run(_op)

  @app.patch("/api/pr/{owner}/{repo}/{number}/issue_comment/{comment_id}")
  def api_edit_issue_comment(owner: str, repo: str, number: int, comment_id: int, req: _EditCommentBodyRequest) -> dict[str, Any]:
    def _op() -> dict[str, Any]:
      res = session.edit_issue_comment(
        owner, repo, number, comment_id, req.body,
        char_diff_mode=req.diff_mode, normalize=req.normalize, ignore_punct=req.ignore_punct,
      )
      return {"updated": res["updated"], "pr": _pr_detail_payload(session, res["detail"])}

    return store.run(_op)

  @app.put("/api/pr/{owner}/{repo}/{number}/review_summary/{review_id}")
  def api_edit_review_summary(owner: str, repo: str, number: int, review_id: int, req: _EditCommentBodyRequest) -> dict[str, Any]:
    def _op() -> dict[str, Any]:
      res = session.edit_review_summary(
        owner, repo, number, review_id, req.body,
        char_diff_mode=req.diff_mode, normalize=req.normalize, ignore_punct=req.ignore_punct,
      )
      return {"updated": res["updated"], "pr": _pr_detail_payload(session, res["detail"])}

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
  default_state: str = "pend_comm",
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

  @asynccontextmanager
  async def _lifespan(_app: FastAPI):
    try:
      yield
    finally:
      store.shutdown_event.set()

  app = FastAPI(title="GitHub PR Character-Level Reviewer", lifespan=_lifespan)
  app.state.store = store
  _register_read_routes(app, store, default_owner=default_owner, default_state=default_state, default_repo=default_repo)
  _register_mutation_routes(app, store)
  _register_local_routes(app, store)
  return app


def _find_free_port(preferred: int = 8770) -> int:
  return local_helpers.find_free_port(preferred)


def _launch_browser(url: str) -> None:
  local_helpers.launch_browser(url)


def _kill_other_server_instances() -> list[int]:
  return local_helpers.kill_other_server_instances(
    entry_names=("pull_requests_san",),
    module_names=("pull_requests_san", "pull_requests_san.__main__", "pull_requests_san.pr_review_gui"),
    script_suffixes=("pull_requests_san/__main__.py", "pull_requests_san/pr_review_gui.py"),
    label="pull_requests_san",
  )


def main(argv: Sequence[str] | None = None) -> int:
  p = argparse.ArgumentParser(description="Launch pull_requests_san — GitHub PR Reviewer for Sanskrit scholarship.")
  p.add_argument("--owner", default=pr_review.DEFAULT_OWNER, help="GitHub organization/user (default: vishvAsa).")
  p.add_argument("--state", default="pend_comm", choices=["pend_comm", "open", "merged", "all"], help="PR state filter (default: pend_comm).")
  p.add_argument("--repo", default="", help="Optional single repo name filter.")
  p.add_argument("--token", default=None, help="GitHub token (defaults to env, gh CLI config, or tokens.toml).")
  p.add_argument("--local-org-dir", default=pr_review.DEFAULT_LOCAL_ORG_DIR, help="Optional local checkout root for opening files in IDEA.")
  p.add_argument("--port", type=int, default=8770, help="Port to bind on 127.0.0.1 (default: 8770 or next free port).")
  p.add_argument("--no-browser", action="store_true", help="Do not auto-open browser window.")
  p.add_argument("--restart", action="store_true", help="Kill any existing pull_requests_san server and start a fresh instance.")
  p.add_argument("--export-static", default="", help="Write a self-contained pure-JS static PR viewer HTML page to FILE and exit.")
  args = p.parse_args(argv)

  if args.export_static:
    out = export_static_html(args.export_static)
    print(f"Exported standalone static pull_requests_san page to {out}", flush=True)
    return 0

  if not args.restart:
    existing = local_helpers.probe_running_server("pull_requests_san", default_port=args.port, meta_path="/api/meta")
    if existing:
      url = existing["url"]
      print(f"GitHub PR Reviewer running at {url} (reused existing server)", flush=True)
      if not args.no_browser and not existing["meta"].get("has_active_ui"):
        _launch_browser(url)
      return 0

  app = create_app(
    default_owner=args.owner,
    default_state=args.state,
    default_repo=args.repo,
    token=args.token,
    local_org_dir=args.local_org_dir,
  )
  _kill_other_server_instances()
  port = _find_free_port(args.port)
  url = f"http://127.0.0.1:{port}/"
  local_helpers.register_running_server("pull_requests_san", pid=os.getpid(), port=port, url=url)
  print(f"GitHub PR Reviewer running at {url}", flush=True)
  if not args.no_browser:
    threading.Thread(target=_launch_browser, args=(url,), daemon=True).start()
  try:
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
  finally:
    local_helpers.unregister_running_server("pull_requests_san", pid=os.getpid())
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
