"""GitHub REST + GraphQL client for discovering org PRs, fetching diffs, and submitting reviews."""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote

import requests

from pull_requests_san.auth import DEFAULT_OWNER, normalize_owner, resolve_github_token
from pull_requests_san.diff import (
  VALID_DIFF_MODES,
  VALID_NORMALIZATIONS,
  build_full_file_hunk,
  build_unified_patch_from_texts,
  extract_three_way_conflicts,
  parse_full_diff_by_file,
  parse_patch_hunks,
)
from pull_requests_san.models import (
  DiffHunk,
  PullRequestCommit,
  PullRequestDetail,
  PullRequestFileDiff,
  PullRequestSummary,
  ReviewComment,
  ReviewThread,
)


logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_LOCAL_ORG_DIR = os.path.expanduser("~/gitland/vishvAsa")
VALID_REVIEW_EVENTS = ("COMMENT", "APPROVE", "REQUEST_CHANGES")
VALID_MERGE_METHODS = ("merge", "squash", "rebase")
DEFAULT_OPEN_PR_LIMIT = 100
DEFAULT_PAST_PR_LIMIT = 10


def default_pr_list_limit(state: str = "open") -> int:
  """Return default PR list limit: 100 for ``open``, 10 for ``closed`` / ``merged`` / ``all``."""
  state_norm = (state or "open").strip().lower()
  return DEFAULT_OPEN_PR_LIMIT if state_norm == "open" else DEFAULT_PAST_PR_LIMIT

_NEXT_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="next"')
_HEX_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

_PR_SEARCH_GQL = """
query($q: String!, $first: Int!, $after: String) {
  search(query: $q, type: ISSUE, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title url state isDraft merged mergeable mergeStateStatus createdAt updatedAt
        additions deletions changedFiles reviewDecision
        baseRefName baseRefOid headRefName headRefOid
        headRepository { nameWithOwner }
        author { login }
        repository { name nameWithOwner }
        comments { totalCount }
        reviewThreads(first: 100) {
          totalCount
          nodes {
            isResolved
            comments { totalCount }
          }
        }
      }
    }
  }
}
"""

_PR_THREADS_GQL = """
query($owner: String!, $repo: String!, $number: Int!, $threadsAfter: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      merged mergeable mergeStateStatus reviewDecision
      reviews(last: 100) {
        nodes {
          databaseId state body submittedAt url
          author { login }
        }
      }
      reviewThreads(first: 100, after: $threadsAfter) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated path line originalLine diffSide
          comments(first: 100) {
            nodes {
              databaseId body createdAt updatedAt url
              author { login }
              replyTo { databaseId }
            }
          }
        }
      }
    }
  }
}
"""


class GitHubAPIError(RuntimeError):
  """Raised when the GitHub API returns an error or invalid response."""

  def __init__(self, message: str, *, status_code: int = 0, details: Any = None) -> None:
    super().__init__(message)
    self.status_code = status_code
    self.details = details


def _safe_segment(val: str, label: str) -> str:
  cleaned = (val or "").strip()
  if not cleaned or "/" in cleaned or "\\" in cleaned or cleaned in (".", ".."):
    raise ValueError(f"Invalid {label}: {val!r}")
  return quote(cleaned, safe="")


def _split_repo_full_name(full_name: str, fallback_owner: str, fallback_repo: str) -> tuple[str, str]:
  if full_name and "/" in full_name:
    o, r = full_name.split("/", 1)
    if o and r:
      return o, r
  return fallback_owner, fallback_repo


def _resolve_local_repo_file(local_org_dir: str | Path | None, owner: str, repo: str, filename: str) -> str | None:
  if not local_org_dir:
    return None
  root = Path(local_org_dir).expanduser().resolve()
  if owner.lower() not in (root.name.lower(), DEFAULT_OWNER.lower()):
    return None
  if not repo or "/" in repo or "\\" in repo or repo in (".", "..") or not filename:
    return None
  if Path(filename).is_absolute():
    return None
  repo_root = (root / repo).resolve()
  if not repo_root.is_relative_to(root):
    return None
  cand = (repo_root / filename).resolve()
  if cand.is_relative_to(repo_root) and cand.is_file():
    return str(cand)
  return None


def _parse_graphql_thread_node(th: dict[str, Any]) -> ReviewThread:
  comments = [
    ReviewComment(
      id=int(c.get("databaseId") or 0),
      author=((c.get("author") or {}).get("login")) or "ghost",
      body=c.get("body") or "",
      created_at=c.get("createdAt") or "",
      updated_at=c.get("updatedAt") or "",
      url=c.get("url") or "",
      in_reply_to_id=((c.get("replyTo") or {}).get("databaseId")),
    )
    for c in (((th.get("comments") or {}).get("nodes")) or [])
    if isinstance(c, dict)
  ]
  return ReviewThread(
    id=th.get("id") or "",
    path=th.get("path") or "",
    line=th.get("line"),
    original_line=th.get("originalLine"),
    side=(th.get("diffSide") or "RIGHT").upper(),
    is_resolved=bool(th.get("isResolved")),
    is_outdated=bool(th.get("isOutdated")),
    comments=comments,
  )


def _parse_gql_mergeable(val: Any) -> bool | None:
  if val is None or isinstance(val, bool):
    return val
  up = str(val).strip().upper()
  if up == "MERGEABLE":
    return True
  if up == "CONFLICTING":
    return False
  return None


def _normalize_mergeable_state(val: Any) -> str:
  return str(val).strip().lower() if val else ""


def count_unresolved_comments_in_threads(threads: Sequence[ReviewThread]) -> int:
  """Return total comments inside unresolved inline review threads."""
  return sum(len(t.comments) for t in threads if not t.is_resolved)


def _parse_graphql_pr_summary(node: dict[str, Any], clean_owner: str) -> PullRequestSummary:
  repo_info = node.get("repository") or {}
  repo_full = repo_info.get("nameWithOwner") or ""
  repo_owner = repo_full.split("/")[0] if "/" in repo_full else clean_owner
  repo_name = repo_info.get("name") or (repo_full.split("/")[-1] if "/" in repo_full else "")
  head_repo = (node.get("headRepository") or {}).get("nameWithOwner") or repo_full
  state_norm = (node.get("state") or "OPEN").lower()
  merged_flag = bool(node.get("merged")) or state_norm == "merged"
  rt_obj = node.get("reviewThreads") or {}
  rt_nodes = rt_obj.get("nodes")
  if isinstance(rt_nodes, list):
    unresolved_count = sum(
      int(((th.get("comments") or {}).get("totalCount")) or 0)
      for th in rt_nodes
      if isinstance(th, dict) and not th.get("isResolved")
    )
  else:
    unresolved_count = int(rt_obj.get("totalCount") or 0)
  return PullRequestSummary(
    owner=repo_owner,
    repo=repo_name,
    repo_full_name=repo_full or f"{repo_owner}/{repo_name}",
    number=int(node["number"]),
    title=node.get("title") or "",
    url=node.get("url") or "",
    state=state_norm,
    is_draft=bool(node.get("isDraft")),
    author=((node.get("author") or {}).get("login")) or "ghost",
    created_at=node.get("createdAt") or "",
    updated_at=node.get("updatedAt") or "",
    base_ref=node.get("baseRefName") or "",
    base_sha=node.get("baseRefOid") or "",
    head_ref=node.get("headRefName") or "",
    head_sha=node.get("headRefOid") or "",
    head_repo_full_name=head_repo,
    additions=int(node.get("additions") or 0),
    deletions=int(node.get("deletions") or 0),
    changed_files=int(node.get("changedFiles") or 0),
    review_decision=node.get("reviewDecision"),
    merged=merged_flag,
    mergeable=_parse_gql_mergeable(node.get("mergeable")),
    mergeable_state=_normalize_mergeable_state(node.get("mergeStateStatus")),
    comment_count=int(((node.get("comments") or {}).get("totalCount")) or 0),
    thread_count=int(rt_obj.get("totalCount") or 0),
    unresolved_comment_count=unresolved_count,
  )


def _validate_comment_line_args(line: int, side: str, start_line: int | None = None, start_side: str | None = None) -> tuple[int, str, int | None, str | None]:
  line_int = int(line)
  if line_int < 1:
    raise ValueError(f"Line number must be >= 1, got {line!r}")
  side_norm = (side or "RIGHT").upper()
  if side_norm not in ("LEFT", "RIGHT"):
    raise ValueError(f"Invalid diff side {side!r}; expected 'LEFT' or 'RIGHT'")
  if start_line is None:
    return line_int, side_norm, None, None
  start_int = int(start_line)
  if start_int < 1 or start_int >= line_int:
    raise ValueError(f"start_line ({start_int}) must be >= 1 and < line ({line_int})")
  start_side_norm = (start_side or side_norm).upper()
  if start_side_norm not in ("LEFT", "RIGHT"):
    raise ValueError(f"Invalid start_side {start_side!r}; expected 'LEFT' or 'RIGHT'")
  return line_int, side_norm, start_int, start_side_norm


class GitHubClient:
  """GitHub REST + GraphQL client for discovering org PRs, fetching diffs, and posting reviews."""

  def __init__(
    self,
    token: str | None = None,
    *,
    api_base: str = DEFAULT_API_BASE,
    timeout: int = 30,
    session: requests.Session | None = None,
    cred_path: str | Path | None = None,
    local_org_dir: str | Path | None = DEFAULT_LOCAL_ORG_DIR,
  ) -> None:
    self.token = resolve_github_token(token, cred_path=cred_path)
    self.api_base = api_base.rstrip("/")
    self.timeout = timeout
    self.session = session or requests.Session()
    self.local_org_dir = str(local_org_dir) if local_org_dir else None
    self._auth_user_cached: dict[str, Any] | None = None
    self._auth_user_checked = False

  def _headers(self, accept: str | None = None) -> dict[str, str]:
    headers = {
      "Accept": accept or "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "pull_requests_san-github-pr-review",
    }
    if self.token:
      headers["Authorization"] = f"Bearer {self.token}"
    return headers

  def _raise_for_response(self, resp: requests.Response) -> None:
    if resp.status_code < 400:
      return
    msg = f"GitHub API HTTP {resp.status_code}"
    details: Any = None
    try:
      details = resp.json()
      if isinstance(details, dict) and details.get("message"):
        msg = f"{msg}: {details['message']}"
        if details.get("errors"):
          msg = f"{msg} ({details['errors']})"
    except ValueError:
      if resp.text:
        msg = f"{msg}: {resp.text[:300]}"
    raise GitHubAPIError(msg, status_code=resp.status_code, details=details)

  def rest_get(
    self,
    path_or_url: str,
    *,
    params: dict[str, Any] | None = None,
    accept: str | None = None,
  ) -> requests.Response:
    if path_or_url.startswith("http"):
      if not path_or_url.startswith(self.api_base + "/"):
        raise GitHubAPIError(f"Refusing cross-origin API request to {path_or_url!r}", status_code=400)
      url = path_or_url
    else:
      url = f"{self.api_base}/{path_or_url.lstrip('/')}"
    resp = self.session.get(url, headers=self._headers(accept=accept), params=params, timeout=self.timeout)
    self._raise_for_response(resp)
    return resp

  def rest_get_paginated(
    self,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    max_pages: int = 20,
  ) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    next_url: str | None = path
    cur_params = dict(params or {})
    cur_params.setdefault("per_page", 100)
    for page_idx in range(max_pages):
      resp = self.rest_get(next_url, params=cur_params if page_idx == 0 else None)
      payload = resp.json()
      if isinstance(payload, list):
        items.extend(payload)
      elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
        items.extend(payload["items"])
      else:
        break
      m = _NEXT_LINK_RE.search(resp.headers.get("Link") or "")
      if not m:
        break
      cand = m.group(1)
      if not cand.startswith(self.api_base + "/"):
        logger.warning("Ignoring non-API pagination Link target: %s", cand)
        break
      next_url = cand
    return items

  def _rest_mutate(self, method: str, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
    if not self.token:
      raise GitHubAPIError("GitHub authentication token is required to modify PRs, comments, or reviews.", status_code=401)
    url = f"{self.api_base}/{path.lstrip('/')}"
    resp = self.session.request(method, url, headers=self._headers(), json=json_body, timeout=self.timeout)
    self._raise_for_response(resp)
    return resp.json() if resp.text else {}

  def rest_post(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
    return self._rest_mutate("POST", path, json_body)

  def rest_patch(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
    return self._rest_mutate("PATCH", path, json_body)

  def rest_put(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
    return self._rest_mutate("PUT", path, json_body)

  def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    if not self.token:
      raise GitHubAPIError("GitHub authentication token is required for GraphQL API.", status_code=401)
    url = f"{self.api_base}/graphql"
    resp = self.session.post(
      url,
      headers=self._headers(),
      json={"query": query, "variables": variables or {}},
      timeout=self.timeout,
    )
    self._raise_for_response(resp)
    payload = resp.json()
    if payload.get("errors"):
      raise GitHubAPIError(f"GitHub GraphQL error: {payload['errors']}", status_code=resp.status_code, details=payload["errors"])
    return payload.get("data") or {}

  def get_authenticated_user(self) -> dict[str, Any] | None:
    """Return the authenticated GitHub user profile, or ``None`` if unauthenticated."""
    if self._auth_user_checked:
      return self._auth_user_cached
    self._auth_user_checked = True
    if not self.token:
      return None
    try:
      data = self.rest_get("/user").json()
      self._auth_user_cached = {"login": data.get("login"), "name": data.get("name"), "avatar_url": data.get("avatar_url")}
    except GitHubAPIError:
      self._auth_user_cached = None
    return self._auth_user_cached

  def list_org_pull_requests(
    self,
    owner: str = DEFAULT_OWNER,
    *,
    state: str = "open",
    repo: str | None = None,
    limit: int | None = None,
  ) -> list[PullRequestSummary]:
    """List pull requests across all repositories owned by ``owner`` (e.g. ``vishvAsa``)."""
    clean_owner = normalize_owner(owner)
    state_norm = (state or "open").strip().lower()
    state_terms = {"open": "is:open", "closed": "is:closed", "merged": "is:merged", "all": ""}
    if state_norm not in state_terms:
      raise ValueError(f"Unsupported PR state {state!r}; expected one of {tuple(state_terms)}")

    resolved_limit = int(limit) if limit is not None else default_pr_list_limit(state_norm)
    clean_repo = re.sub(r"\s+", "", repo or "").strip("/").split("/")[-1]
    scope_term = f"repo:{clean_owner}/{clean_repo}" if clean_repo else f"user:{clean_owner}"
    query_parts = [scope_term, "is:pr"]
    if state_terms[state_norm]:
      query_parts.append(state_terms[state_norm])
    query_parts.append("sort:updated-desc")
    search_q = " ".join(query_parts)

    if self.token:
      return self._list_prs_graphql(clean_owner, search_q, limit=resolved_limit)
    return self._list_prs_rest_search(clean_owner, search_q, limit=resolved_limit)

  def _list_prs_graphql(self, clean_owner: str, search_q: str, *, limit: int) -> list[PullRequestSummary]:
    results: list[PullRequestSummary] = []
    after: str | None = None
    while len(results) < limit:
      batch = min(100, limit - len(results))
      data = self.graphql(_PR_SEARCH_GQL, {"q": search_q, "first": batch, "after": after})
      search_block = data.get("search") or {}
      nodes = search_block.get("nodes") or []
      for node in nodes:
        if isinstance(node, dict) and "number" in node:
          results.append(_parse_graphql_pr_summary(node, clean_owner))
      page_info = search_block.get("pageInfo") or {}
      if not page_info.get("hasNextPage") or not nodes:
        break
      after = page_info.get("endCursor")
    return results[:limit]

  def _list_prs_rest_search(self, clean_owner: str, search_q: str, *, limit: int) -> list[PullRequestSummary]:
    items = self.rest_get_paginated(
      "/search/issues",
      params={"q": search_q, "per_page": min(100, limit)},
      max_pages=max(1, (limit + 99) // 100),
    )[:limit]
    results: list[PullRequestSummary] = []
    for item in items:
      parts = (item.get("repository_url") or "").rstrip("/").split("/")
      repo_owner = parts[-2] if len(parts) >= 2 else clean_owner
      repo_name = parts[-1] if parts else ""
      results.append(
        PullRequestSummary(
          owner=repo_owner,
          repo=repo_name,
          repo_full_name=f"{repo_owner}/{repo_name}",
          number=int(item["number"]),
          title=item.get("title") or "",
          url=item.get("html_url") or "",
          state=(item.get("state") or "open").lower(),
          is_draft=bool(item.get("draft")),
          author=((item.get("user") or {}).get("login")) or "ghost",
          created_at=item.get("created_at") or "",
          updated_at=item.get("updated_at") or "",
          merged=bool((item.get("pull_request") or {}).get("merged_at")),
          comment_count=int(item.get("comments") or 0),
        )
      )
    return results

  def _fetch_file_text_at_ref(
    self,
    owner: str,
    repo: str,
    path: str,
    ref: str,
    *,
    blob_sha: str | None = None,
    fallback_owner_repo: tuple[str, str] | None = None,
  ) -> str | None:
    o_seg, r_seg = _safe_segment(owner, "owner"), _safe_segment(repo, "repo")
    if blob_sha:
      blob_seg = _safe_segment(blob_sha, "blob_sha")
      blob_targets = [(o_seg, r_seg)]
      if fallback_owner_repo:
        blob_targets.append((_safe_segment(fallback_owner_repo[0], "owner"), _safe_segment(fallback_owner_repo[1], "repo")))
      for bo, br in blob_targets:
        try:
          blob = self.rest_get(f"/repos/{bo}/{br}/git/blobs/{blob_seg}").json()
          if blob.get("encoding") == "base64" and isinstance(blob.get("content"), str):
            return base64.b64decode(blob["content"]).decode("utf-8", errors="replace")
        except GitHubAPIError:
          continue

    quoted_path = quote(path.lstrip("/"), safe="/")
    targets = [(o_seg, r_seg)]
    if fallback_owner_repo:
      targets.append((_safe_segment(fallback_owner_repo[0], "owner"), _safe_segment(fallback_owner_repo[1], "repo")))
    for to, tr in targets:
      try:
        resp = self.rest_get(
          f"/repos/{to}/{tr}/contents/{quoted_path}",
          params={"ref": ref},
          accept="application/vnd.github.raw",
        )
        return resp.text
      except GitHubAPIError as exc:
        if exc.status_code == 404:
          continue
        raise
    return None

  def _fetch_reviews_threads_and_merge(
    self,
    owner: str,
    repo: str,
    number: int,
  ) -> tuple[list[dict[str, Any]], list[ReviewThread], str | None, dict[str, Any]]:
    if self.token:
      try:
        return self._fetch_reviews_and_threads_graphql(owner, repo, number)
      except GitHubAPIError as exc:
        logger.warning("GraphQL reviewThread fetch failed for %s/%s#%s (%s); falling back to REST", owner, repo, number, exc)
    reviews, threads = self._fetch_reviews_and_threads_rest(owner, repo, number)
    return reviews, threads, None, {}

  def _fetch_reviews_and_threads(
    self,
    owner: str,
    repo: str,
    number: int,
  ) -> tuple[list[dict[str, Any]], list[ReviewThread], str | None]:
    reviews, threads, review_decision, _ = self._fetch_reviews_threads_and_merge(owner, repo, number)
    return reviews, threads, review_decision

  def _fetch_reviews_and_threads_graphql(
    self,
    owner: str,
    repo: str,
    number: int,
  ) -> tuple[list[dict[str, Any]], list[ReviewThread], str | None, dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    threads: list[ReviewThread] = []
    review_decision: str | None = None
    merge_meta: dict[str, Any] = {}
    after: str | None = None
    first_page = True
    while True:
      data = self.graphql(_PR_THREADS_GQL, {"owner": owner, "repo": repo, "number": number, "threadsAfter": after})
      pr_node = ((data.get("repository") or {}).get("pullRequest")) or {}
      if first_page:
        review_decision = pr_node.get("reviewDecision")
        merge_meta = {
          "merged": pr_node.get("merged"),
          "mergeable": _parse_gql_mergeable(pr_node.get("mergeable")),
          "mergeable_state": _normalize_mergeable_state(pr_node.get("mergeStateStatus")),
        }
        for rev in ((pr_node.get("reviews") or {}).get("nodes")) or []:
          if isinstance(rev, dict):
            reviews.append(
              {
                "id": rev.get("databaseId"),
                "author": ((rev.get("author") or {}).get("login")) or "ghost",
                "state": rev.get("state") or "",
                "body": rev.get("body") or "",
                "submitted_at": rev.get("submittedAt") or "",
                "url": rev.get("url") or "",
              }
            )
        first_page = False
      rt_block = pr_node.get("reviewThreads") or {}
      for th in rt_block.get("nodes") or []:
        if isinstance(th, dict):
          threads.append(_parse_graphql_thread_node(th))
      page_info = rt_block.get("pageInfo") or {}
      if not page_info.get("hasNextPage"):
        break
      after = page_info.get("endCursor")
    return reviews, threads, review_decision, merge_meta

  def _fetch_reviews_and_threads_rest(
    self,
    owner: str,
    repo: str,
    number: int,
  ) -> tuple[list[dict[str, Any]], list[ReviewThread]]:
    o_seg, r_seg = _safe_segment(owner, "owner"), _safe_segment(repo, "repo")
    raw_reviews = self.rest_get_paginated(f"/repos/{o_seg}/{r_seg}/pulls/{int(number)}/reviews")
    reviews = [
      {
        "id": r.get("id"),
        "author": ((r.get("user") or {}).get("login")) or "ghost",
        "state": r.get("state") or "",
        "body": r.get("body") or "",
        "submitted_at": r.get("submitted_at") or "",
        "url": r.get("html_url") or "",
      }
      for r in raw_reviews
    ]
    raw_comments = self.rest_get_paginated(f"/repos/{o_seg}/{r_seg}/pulls/{int(number)}/comments")
    by_root: dict[int, list[dict[str, Any]]] = {}
    for c in raw_comments:
      by_root.setdefault(int(c.get("in_reply_to_id") or c["id"]), []).append(c)

    threads: list[ReviewThread] = []
    for root_id, group in by_root.items():
      group_sorted = sorted(group, key=lambda x: (x.get("created_at") or "", int(x.get("id") or 0)))
      first = group_sorted[0]
      threads.append(
        ReviewThread(
          id=str(root_id),
          path=first.get("path") or "",
          line=first.get("line"),
          original_line=first.get("original_line"),
          side=(first.get("side") or "RIGHT").upper(),
          is_resolved=False,
          is_outdated=first.get("line") is None and first.get("original_line") is not None,
          comments=[
            ReviewComment(
              id=int(c.get("id") or 0),
              author=((c.get("user") or {}).get("login")) or "ghost",
              body=c.get("body") or "",
              created_at=c.get("created_at") or "",
              updated_at=c.get("updated_at") or "",
              url=c.get("html_url") or "",
              in_reply_to_id=c.get("in_reply_to_id"),
            )
            for c in group_sorted
          ],
        )
      )
    return reviews, threads

  def _load_fallback_patches(self, owner: str, repo: str, number: int, files_raw: list[dict[str, Any]]) -> dict[str, str]:
    needs_fallback = any(
      f.get("patch") is None and f.get("changes", 0) > 0
      for f in files_raw
    )
    if not needs_fallback:
      return {}
    o_seg, r_seg = _safe_segment(owner, "owner"), _safe_segment(repo, "repo")
    try:
      resp = self.rest_get(f"/repos/{o_seg}/{r_seg}/pulls/{int(number)}", accept="application/vnd.github.v3.diff")
      return parse_full_diff_by_file(resp.text)
    except GitHubAPIError:
      return {}

  def _fetch_pair_texts(
    self,
    owner: str,
    repo: str,
    *,
    filename: str,
    previous_filename: str | None,
    status: str,
    base_sha: str,
    head_sha: str,
    head_repo_full_name: str,
    blob_sha: str | None,
  ) -> tuple[str | None, str | None]:
    head_owner, head_repo = _split_repo_full_name(head_repo_full_name, owner, repo)
    base_txt = (
      None
      if status == "added"
      else self._fetch_file_text_at_ref(owner, repo, previous_filename or filename, base_sha)
    )
    head_txt = (
      None
      if status == "removed"
      else self._fetch_file_text_at_ref(
        head_owner,
        head_repo,
        filename,
        head_sha,
        blob_sha=blob_sha,
        fallback_owner_repo=(owner, repo) if (head_owner, head_repo) != (owner, repo) else None,
      )
    )
    return base_txt, head_txt

  def _build_file_diff(
    self,
    owner: str,
    repo: str,
    file_raw: dict[str, Any],
    *,
    base_sha: str,
    head_sha: str,
    head_repo_full_name: str,
    fallback_patches: dict[str, str],
    char_diff_mode: str,
    include_full_files: bool,
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> PullRequestFileDiff:
    filename = file_raw.get("filename") or ""
    prev_filename = file_raw.get("previous_filename")
    status = file_raw.get("status") or "modified"
    patch = file_raw.get("patch") or fallback_patches.get(filename) or (
      fallback_patches.get(prev_filename) if prev_filename else None
    )
    is_binary = False
    is_full = False
    hunks: list[DiffHunk] = []

    if include_full_files:
      base_txt, head_txt = self._fetch_pair_texts(
        owner, repo, filename=filename, previous_filename=prev_filename, status=status,
        base_sha=base_sha, head_sha=head_sha, head_repo_full_name=head_repo_full_name, blob_sha=file_raw.get("sha"),
      )
      if base_txt is not None or head_txt is not None:
        hunks = [
          build_full_file_hunk(
            base_txt,
            head_txt,
            existing_patch=patch,
            char_diff_mode=char_diff_mode,
            normalize=normalize,
            ignore_punct=ignore_punct,
          )
        ]
        is_full = True

    if not is_full:
      if patch is not None:
        hunks = parse_patch_hunks(
          patch,
          char_diff_mode=char_diff_mode,
          normalize=normalize,
          ignore_punct=ignore_punct,
        )
      elif status in ("added", "modified", "removed", "renamed") and (file_raw.get("changes", 0) > 0):
        base_txt, head_txt = self._fetch_pair_texts(
          owner, repo, filename=filename, previous_filename=prev_filename, status=status,
          base_sha=base_sha, head_sha=head_sha, head_repo_full_name=head_repo_full_name, blob_sha=file_raw.get("sha"),
        )
        if base_txt is not None or head_txt is not None:
          patch = build_unified_patch_from_texts(base_txt, head_txt, filename, previous_filename=prev_filename)
          hunks = parse_patch_hunks(
            patch,
            char_diff_mode=char_diff_mode,
            normalize=normalize,
            ignore_punct=ignore_punct,
          )
        else:
          is_binary = True
      elif file_raw.get("changes", 0) == 0 and status != "renamed":
        is_binary = True

    return PullRequestFileDiff(
      filename=filename,
      status=status,
      additions=int(file_raw.get("additions") or 0),
      deletions=int(file_raw.get("deletions") or 0),
      changes=int(file_raw.get("changes") or 0),
      previous_filename=prev_filename,
      sha=file_raw.get("sha") or "",
      patch=patch,
      is_binary=is_binary,
      is_full_file=is_full,
      local_file_path=_resolve_local_repo_file(self.local_org_dir, owner, repo, filename),
      hunks=hunks,
    )

  def _fetch_blobs_batch_graphql(self, owner: str, repo: str, oids: list[str]) -> dict[str, str]:
    """Fetch multiple non-binary UTF-8 blob texts from ``owner/repo`` in compact GraphQL batches."""
    if not self.token:
      return {}
    valid_oids = [oid for oid in dict.fromkeys(oids) if oid and _HEX_SHA_RE.match(oid)]
    if not valid_oids:
      return {}
    texts: dict[str, str] = {}
    for start in range(0, len(valid_oids), 25):
      chunk = valid_oids[start : start + 25]
      fields = " ".join(
        f'b{i}: object(oid: "{oid}") {{ ... on Blob {{ isBinary text }} }}'
        for i, oid in enumerate(chunk)
      )
      query = f"query($owner: String!, $repo: String!) {{ repository(owner: $owner, name: $repo) {{ {fields} }} }}"
      try:
        data = self.graphql(query, {"owner": owner, "repo": repo})
      except GitHubAPIError:
        break
      repo_node = data.get("repository") or {}
      for i, oid in enumerate(chunk):
        blob = repo_node.get(f"b{i}") or {}
        if isinstance(blob, dict) and not blob.get("isBinary") and isinstance(blob.get("text"), str):
          texts[oid] = blob["text"]
    return texts

  def _fetch_tree_blob_map(self, owner: str, repo: str, sha: str) -> dict[str, str] | None:
    o_seg, r_seg = _safe_segment(owner, "owner"), _safe_segment(repo, "repo")
    sha_seg = _safe_segment(sha, "sha")
    try:
      data = self.rest_get(f"/repos/{o_seg}/{r_seg}/git/trees/{sha_seg}", params={"recursive": "1"}).json()
    except GitHubAPIError:
      return None
    if data.get("truncated"):
      return None
    tree = data.get("tree")
    if not isinstance(tree, list):
      return None
    return {
      e["path"]: e.get("sha") or ""
      for e in tree
      if isinstance(e, dict) and e.get("type") == "blob" and e.get("path")
    }

  def _detect_merge_conflicts(
    self,
    owner: str,
    repo: str,
    *,
    state: str,
    base_ref: str,
    base_sha: str,
    head_sha: str,
    head_repo_full_name: str,
    mergeable: bool | None,
    mergeable_state: str,
    file_diffs: list[PullRequestFileDiff],
    char_diff_mode: str,
  ) -> tuple[int | None, int | None, list[dict[str, Any]]]:
    if state != "open" or not base_ref or not head_sha:
      return None, None, []
    should_compare = (mergeable is False) or (mergeable_state in ("dirty", "behind", "blocked", "unknown"))
    if not should_compare:
      return None, None, []

    o_seg, r_seg = _safe_segment(owner, "owner"), _safe_segment(repo, "repo")
    try:
      comp = self.rest_get(
        f"/repos/{o_seg}/{r_seg}/compare/{quote(base_ref, safe='/')}...{quote(head_sha, safe='')}"
      ).json()
    except GitHubAPIError:
      return None, None, []

    ahead_by = comp.get("ahead_by")
    behind_by = comp.get("behind_by")
    base_tip_sha = ((comp.get("base_commit") or {}).get("sha")) or ""
    merge_base_sha = ((comp.get("merge_base_commit") or {}).get("sha")) or base_sha
    if not (mergeable is False or mergeable_state == "dirty"):
      return ahead_by, behind_by, []
    if not base_tip_sha or not merge_base_sha or base_tip_sha == merge_base_sha or not file_diffs:
      return ahead_by, behind_by, []

    mb_map = self._fetch_tree_blob_map(owner, repo, merge_base_sha)
    bt_map = self._fetch_tree_blob_map(owner, repo, base_tip_sha) if mb_map is not None else None

    base_compare_files: dict[str, dict[str, Any]] = {}
    if mb_map is None or bt_map is None:
      try:
        comp_base = self.rest_get(
          f"/repos/{o_seg}/{r_seg}/compare/{quote(merge_base_sha, safe='')}...{quote(base_tip_sha, safe='')}"
        ).json()
        for cf in comp_base.get("files") or []:
          if isinstance(cf, dict) and cf.get("filename"):
            base_compare_files[cf["filename"]] = cf
          if isinstance(cf, dict) and cf.get("previous_filename"):
            base_compare_files[cf["previous_filename"]] = cf
      except GitHubAPIError:
        return ahead_by, behind_by, []

    candidates: list[tuple[PullRequestFileDiff, str | None, str | None, str]] = []
    for f in file_diffs:
      if mb_map is not None and bt_map is not None:
        key_path = f.previous_filename or f.filename
        mb_blob = mb_map.get(key_path)
        bt_blob = bt_map.get(f.filename)
        if f.previous_filename and mb_blob != bt_map.get(f.previous_filename):
          base_status = "removed" if f.previous_filename not in bt_map else "modified"
          candidates.append((f, mb_blob, bt_map.get(f.previous_filename), base_status))
        elif mb_map.get(f.filename) != bt_blob:
          base_status = "added" if f.filename not in mb_map else ("removed" if bt_blob is None else "modified")
          candidates.append((f, mb_map.get(f.filename), bt_blob, base_status))
      else:
        cf = base_compare_files.get(f.filename) or (base_compare_files.get(f.previous_filename) if f.previous_filename else None)
        if cf is not None:
          candidates.append((f, None, cf.get("sha"), cf.get("status") or "modified"))

    if not candidates:
      return ahead_by, behind_by, []

    needed_oids: list[str] = []
    for f, mb_blob, bt_blob, base_status in candidates[:25]:
      if mb_blob:
        needed_oids.append(mb_blob)
      if bt_blob:
        needed_oids.append(bt_blob)
      if f.sha and f.status != "removed":
        needed_oids.append(f.sha)
    blob_cache = self._fetch_blobs_batch_graphql(owner, repo, needed_oids)
    head_owner, head_repo = _split_repo_full_name(head_repo_full_name, owner, repo)
    fallback_or = (owner, repo) if (head_owner, head_repo) != (owner, repo) else None

    conflicts: list[dict[str, Any]] = []
    fallback_overlaps: list[dict[str, Any]] = []
    for f, mb_blob, bt_blob, base_status in candidates[:25]:
      if f.status == "removed" and base_status != "removed":
        conflicts.append({"filename": f.filename, "reason": "delete_modify", "base_status": base_status, "pr_status": f.status, "hunks": []})
        continue
      if f.status != "added" and base_status == "removed":
        conflicts.append({"filename": f.filename, "reason": "modify_delete", "base_status": base_status, "pr_status": f.status, "hunks": []})
        continue

      bt_txt = blob_cache.get(bt_blob or "") if bt_blob else None
      if bt_txt is None:
        bt_txt = self._fetch_file_text_at_ref(owner, repo, f.filename, base_tip_sha, blob_sha=bt_blob)
      if f.status == "added":
        anc_txt: str | None = ""
      else:
        anc_txt = blob_cache.get(mb_blob or "") if mb_blob else None
        if anc_txt is None:
          anc_txt = self._fetch_file_text_at_ref(owner, repo, f.previous_filename or f.filename, merge_base_sha, blob_sha=mb_blob)
      hd_txt = blob_cache.get(f.sha) if f.sha else None
      if hd_txt is None:
        hd_txt = self._fetch_file_text_at_ref(head_owner, head_repo, f.filename, head_sha, blob_sha=f.sha or None, fallback_owner_repo=fallback_or)

      if bt_txt is None or anc_txt is None or hd_txt is None:
        fallback_overlaps.append({"filename": f.filename, "reason": "both_modified", "base_status": base_status, "pr_status": f.status, "hunks": []})
        continue

      hunks = extract_three_way_conflicts(bt_txt, anc_txt, hd_txt, char_diff_mode=char_diff_mode)
      if hunks:
        reason = "both_added" if f.status == "added" else "content_conflict"
        conflicts.append({"filename": f.filename, "reason": reason, "base_status": base_status, "pr_status": f.status, "hunks": hunks})
      else:
        fallback_overlaps.append({"filename": f.filename, "reason": "both_modified", "base_status": base_status, "pr_status": f.status, "hunks": []})

    if not conflicts and fallback_overlaps:
      conflicts = fallback_overlaps
    return ahead_by, behind_by, conflicts

  def get_pull_request_detail(
    self,
    owner: str,
    repo: str,
    number: int,
    *,
    char_diff_mode: str = "grapheme",
    include_full_files: bool = False,
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> PullRequestDetail:
    """Fetch complete PR metadata, file diffs, comments, and review threads."""
    if char_diff_mode not in VALID_DIFF_MODES:
      raise ValueError(f"Unsupported diff mode {char_diff_mode!r}")
    norm_clean = (normalize or "none").strip().lower()
    if norm_clean not in VALID_NORMALIZATIONS:
      raise ValueError(f"Unsupported normalization {normalize!r}")
    o_seg, r_seg = _safe_segment(owner, "owner"), _safe_segment(repo, "repo")
    pr_raw = self.rest_get(f"/repos/{o_seg}/{r_seg}/pulls/{int(number)}").json()
    base_info = pr_raw.get("base") or {}
    head_info = pr_raw.get("head") or {}
    base_sha = base_info.get("sha") or ""
    head_sha = head_info.get("sha") or ""
    head_repo_full = ((head_info.get("repo") or {}).get("full_name")) or f"{owner}/{repo}"

    files_raw = self.rest_get_paginated(f"/repos/{o_seg}/{r_seg}/pulls/{int(number)}/files")
    fallback_patches = self._load_fallback_patches(owner, repo, number, files_raw)
    file_diffs = [
      self._build_file_diff(
        owner,
        repo,
        f,
        base_sha=base_sha,
        head_sha=head_sha,
        head_repo_full_name=head_repo_full,
        fallback_patches=fallback_patches,
        char_diff_mode=char_diff_mode,
        include_full_files=include_full_files,
        normalize=norm_clean,
        ignore_punct=ignore_punct,
      )
      for f in files_raw
    ]

    raw_issue_comments = self.rest_get_paginated(f"/repos/{o_seg}/{r_seg}/issues/{int(number)}/comments")
    issue_comments = [
      ReviewComment(
        id=int(c.get("id") or 0),
        author=((c.get("user") or {}).get("login")) or "ghost",
        body=c.get("body") or "",
        created_at=c.get("created_at") or "",
        updated_at=c.get("updated_at") or "",
        url=c.get("html_url") or "",
      )
      for c in raw_issue_comments
    ]
    commits = self._fetch_pull_request_commits(owner, repo, number)
    reviews, threads, review_decision, gql_merge = self._fetch_reviews_threads_and_merge(owner, repo, number)
    pr_state = (pr_raw.get("state") or "open").lower()
    merged_flag = bool(pr_raw.get("merged")) or bool(pr_raw.get("merged_at")) or bool(gql_merge.get("merged")) or pr_state == "merged"
    mergeable_val = pr_raw.get("mergeable")
    if mergeable_val is None and pr_state == "open":
      mergeable_val = gql_merge.get("mergeable")
    mergeable_state_val = _normalize_mergeable_state(pr_raw.get("mergeable_state"))
    if (not mergeable_state_val or mergeable_state_val == "unknown") and gql_merge.get("mergeable_state"):
      mergeable_state_val = gql_merge["mergeable_state"]

    base_ref = base_info.get("ref") or ""
    ahead_by, behind_by, conflicts = self._detect_merge_conflicts(
      owner,
      repo,
      state=pr_state,
      base_ref=base_ref,
      base_sha=base_sha,
      head_sha=head_sha,
      head_repo_full_name=head_repo_full,
      mergeable=mergeable_val,
      mergeable_state=mergeable_state_val,
      file_diffs=file_diffs,
      char_diff_mode=char_diff_mode,
    )
    summary = PullRequestSummary(
      owner=owner,
      repo=repo,
      repo_full_name=f"{owner}/{repo}",
      number=int(pr_raw.get("number") or number),
      title=pr_raw.get("title") or "",
      url=pr_raw.get("html_url") or "",
      state=pr_state,
      is_draft=bool(pr_raw.get("draft")),
      author=((pr_raw.get("user") or {}).get("login")) or "ghost",
      created_at=pr_raw.get("created_at") or "",
      updated_at=pr_raw.get("updated_at") or "",
      base_ref=base_ref,
      base_sha=base_sha,
      head_ref=head_info.get("ref") or "",
      head_sha=head_sha,
      head_repo_full_name=head_repo_full,
      additions=int(pr_raw.get("additions") or 0),
      deletions=int(pr_raw.get("deletions") or 0),
      changed_files=int(pr_raw.get("changed_files") or len(file_diffs)),
      review_decision=review_decision,
      merged=merged_flag,
      mergeable=mergeable_val,
      mergeable_state=mergeable_state_val,
      comment_count=len(issue_comments),
      thread_count=len(threads),
      unresolved_comment_count=count_unresolved_comments_in_threads(threads),
    )
    return PullRequestDetail(
      summary=summary,
      body=pr_raw.get("body") or "",
      commits_count=int(pr_raw.get("commits") or 0),
      merged=merged_flag,
      mergeable=mergeable_val,
      mergeable_state=mergeable_state_val,
      rebaseable=pr_raw.get("rebaseable"),
      behind_by=behind_by,
      ahead_by=ahead_by,
      conflicts=conflicts,
      char_diff_mode=char_diff_mode,
      normalize=norm_clean,
      ignore_punct=ignore_punct,
      commits=commits,
      files=file_diffs,
      issue_comments=issue_comments,
      reviews=reviews,
      review_threads=threads,
    )

  @staticmethod
  def _commit_from_raw(c: dict[str, Any]) -> PullRequestCommit:
    sha = str(c.get("sha") or "").strip()
    commit_obj = c.get("commit") or {}
    msg = str(commit_obj.get("message") or "")
    title = msg.splitlines()[0].strip() if msg.strip() else sha[:7]
    author_info = commit_obj.get("author") or {}
    committer_info = commit_obj.get("committer") or {}
    login = ((c.get("author") or {}).get("login")) or author_info.get("name") or committer_info.get("name") or "ghost"
    committed_at = str(author_info.get("date") or committer_info.get("date") or "")
    parents = [str(p.get("sha") or "") for p in (c.get("parents") or []) if isinstance(p, dict) and p.get("sha")]
    return PullRequestCommit(
      sha=sha,
      short_sha=sha[:7],
      title=title,
      message=msg,
      author=str(login),
      committed_at=committed_at,
      url=str(c.get("html_url") or ""),
      parents=parents,
    )

  def _fetch_pull_request_commits(self, owner: str, repo: str, number: int) -> list[PullRequestCommit]:
    o_seg, r_seg = _safe_segment(owner, "owner"), _safe_segment(repo, "repo")
    try:
      raw_commits = self.rest_get_paginated(f"/repos/{o_seg}/{r_seg}/pulls/{int(number)}/commits")
    except GitHubAPIError:
      return []
    return [self._commit_from_raw(c) for c in raw_commits if isinstance(c, dict) and c.get("sha")]

  def get_commit_files(
    self,
    owner: str,
    repo: str,
    sha: str,
    *,
    head_repo_full_name: str = "",
    char_diff_mode: str = "grapheme",
    include_full_files: bool = False,
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> dict[str, Any]:
    """Fetch changed files and parsed diffs for a single PR commit / changelist."""
    clean_sha = (sha or "").strip()
    if not re.match(r"^[0-9a-fA-F]{4,64}$", clean_sha):
      raise ValueError(f"Invalid commit SHA {sha!r}")
    if char_diff_mode not in VALID_DIFF_MODES:
      raise ValueError(f"Unsupported diff mode {char_diff_mode!r}")
    norm_clean = (normalize or "none").strip().lower()
    if norm_clean not in VALID_NORMALIZATIONS:
      raise ValueError(f"Unsupported normalization {normalize!r}")

    o_seg, r_seg = _safe_segment(owner, "owner"), _safe_segment(repo, "repo")
    commit_raw: dict[str, Any] | None = None
    try:
      commit_raw = self.rest_get(f"/repos/{o_seg}/{r_seg}/commits/{clean_sha}").json()
    except GitHubAPIError as exc:
      head_owner, head_repo = _split_repo_full_name(head_repo_full_name, owner, repo)
      if exc.status_code == 404 and (head_owner, head_repo) != (owner, repo):
        ho_seg, hr_seg = _safe_segment(head_owner, "head_owner"), _safe_segment(head_repo, "head_repo")
        commit_raw = self.rest_get(f"/repos/{ho_seg}/{hr_seg}/commits/{clean_sha}").json()
      else:
        raise

    commit_obj = self._commit_from_raw(commit_raw or {"sha": clean_sha})
    parent_sha = commit_obj.parents[0] if commit_obj.parents else ""
    files_raw = (commit_raw or {}).get("files") or []
    effective_head_repo = head_repo_full_name or f"{owner}/{repo}"
    file_diffs = [
      self._build_file_diff(
        owner,
        repo,
        f,
        base_sha=parent_sha,
        head_sha=commit_obj.sha or clean_sha,
        head_repo_full_name=effective_head_repo,
        fallback_patches={},
        char_diff_mode=char_diff_mode,
        include_full_files=include_full_files,
        normalize=norm_clean,
        ignore_punct=ignore_punct,
      )
      for f in files_raw
      if isinstance(f, dict)
    ]
    return {
      "sha": commit_obj.sha or clean_sha,
      "parent_sha": parent_sha,
      "commit": commit_obj,
      "char_diff_mode": char_diff_mode,
      "normalize": norm_clean,
      "ignore_punct": ignore_punct,
      "files": file_diffs,
    }

  def get_file_full_diff(
    self,
    owner: str,
    repo: str,
    file_diff: PullRequestFileDiff,
    *,
    base_sha: str,
    head_sha: str,
    head_repo_full_name: str = "",
    char_diff_mode: str = "grapheme",
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> PullRequestFileDiff:
    """Fetch full base and head contents for ``file_diff`` and return a full-file diff copy."""
    base_txt, head_txt = self._fetch_pair_texts(
      owner,
      repo,
      filename=file_diff.filename,
      previous_filename=file_diff.previous_filename,
      status=file_diff.status,
      base_sha=base_sha,
      head_sha=head_sha,
      head_repo_full_name=head_repo_full_name or f"{owner}/{repo}",
      blob_sha=file_diff.sha or None,
    )
    full_hunk = build_full_file_hunk(
      base_txt,
      head_txt,
      existing_patch=file_diff.patch,
      char_diff_mode=char_diff_mode,
      normalize=normalize,
      ignore_punct=ignore_punct,
    )
    return PullRequestFileDiff(
      filename=file_diff.filename,
      status=file_diff.status,
      additions=file_diff.additions,
      deletions=file_diff.deletions,
      changes=file_diff.changes,
      previous_filename=file_diff.previous_filename,
      sha=file_diff.sha,
      patch=file_diff.patch,
      is_binary=file_diff.is_binary,
      is_full_file=True,
      local_file_path=file_diff.local_file_path,
      hunks=[full_hunk],
    )

  def create_issue_comment(self, owner: str, repo: str, number: int, body: str) -> dict[str, Any]:
    """Post a top-level PR conversation comment."""
    text = (body or "").strip()
    if not text:
      raise ValueError("Comment body cannot be empty")
    o_seg, r_seg = _safe_segment(owner, "owner"), _safe_segment(repo, "repo")
    return self.rest_post(f"/repos/{o_seg}/{r_seg}/issues/{int(number)}/comments", {"body": text})

  def create_inline_comment(
    self,
    owner: str,
    repo: str,
    number: int,
    *,
    path: str,
    line: int,
    side: str = "RIGHT",
    body: str,
    commit_id: str | None = None,
    start_line: int | None = None,
    start_side: str | None = None,
  ) -> dict[str, Any]:
    """Post an inline review comment anchored to ``(path, line, side)`` on the PR."""
    text = (body or "").strip()
    if not text:
      raise ValueError("Comment body cannot be empty")
    if not (path or "").strip():
      raise ValueError("Comment file path cannot be empty")
    line_int, side_norm, start_int, start_side_norm = _validate_comment_line_args(line, side, start_line, start_side)
    o_seg, r_seg = _safe_segment(owner, "owner"), _safe_segment(repo, "repo")
    if not commit_id:
      pr_raw = self.rest_get(f"/repos/{o_seg}/{r_seg}/pulls/{int(number)}").json()
      commit_id = ((pr_raw.get("head") or {}).get("sha")) or ""
    payload: dict[str, Any] = {
      "body": text,
      "commit_id": commit_id,
      "path": path,
      "line": line_int,
      "side": side_norm,
    }
    if start_int is not None:
      payload["start_line"] = start_int
      payload["start_side"] = start_side_norm
    return self.rest_post(f"/repos/{o_seg}/{r_seg}/pulls/{int(number)}/comments", payload)

  def reply_to_review_comment(self, owner: str, repo: str, number: int, comment_id: int, body: str) -> dict[str, Any]:
    """Reply to an existing inline PR review comment thread."""
    text = (body or "").strip()
    if not text:
      raise ValueError("Reply body cannot be empty")
    o_seg, r_seg = _safe_segment(owner, "owner"), _safe_segment(repo, "repo")
    return self.rest_post(
      f"/repos/{o_seg}/{r_seg}/pulls/{int(number)}/comments/{int(comment_id)}/replies",
      {"body": text},
    )

  def submit_review(
    self,
    owner: str,
    repo: str,
    number: int,
    *,
    event: str = "COMMENT",
    body: str = "",
    comments: list[dict[str, Any]] | None = None,
    commit_id: str | None = None,
  ) -> dict[str, Any]:
    """Submit a PR review (``COMMENT``, ``APPROVE``, or ``REQUEST_CHANGES``) with optional batched comments."""
    ev = (event or "COMMENT").upper()
    if ev not in VALID_REVIEW_EVENTS:
      raise ValueError(f"Invalid review event {event!r}; expected one of {VALID_REVIEW_EVENTS}")
    text = (body or "").strip()
    formatted_comments: list[dict[str, Any]] = []
    for c in comments or []:
      if not isinstance(c, dict) or not (c.get("path") or "").strip() or c.get("line") is None:
        raise ValueError("Each review comment requires 'path' and 'line'")
      c_body = (c.get("body") or "").strip()
      if not c_body:
        continue
      c_line, c_side, c_start, c_start_side = _validate_comment_line_args(
        c["line"], c.get("side") or "RIGHT", c.get("start_line"), c.get("start_side")
      )
      entry: dict[str, Any] = {"path": c["path"], "line": c_line, "side": c_side, "body": c_body}
      if c_start is not None:
        entry["start_line"] = c_start
        entry["start_side"] = c_start_side
      formatted_comments.append(entry)
    if ev in ("COMMENT", "REQUEST_CHANGES") and not text and not formatted_comments:
      raise ValueError("Review body or at least one inline comment is required for COMMENT / REQUEST_CHANGES")
    payload: dict[str, Any] = {"event": ev}
    if text:
      payload["body"] = text
    if commit_id:
      payload["commit_id"] = commit_id
    if formatted_comments:
      payload["comments"] = formatted_comments
    o_seg, r_seg = _safe_segment(owner, "owner"), _safe_segment(repo, "repo")
    return self.rest_post(f"/repos/{o_seg}/{r_seg}/pulls/{int(number)}/reviews", payload)

  def update_review_comment(self, owner: str, repo: str, comment_id: int, body: str) -> dict[str, Any]:
    """Edit the body of an existing inline PR review comment."""
    text = (body or "").strip()
    if not text:
      raise ValueError("Comment body cannot be empty")
    o_seg, r_seg = _safe_segment(owner, "owner"), _safe_segment(repo, "repo")
    return self.rest_patch(f"/repos/{o_seg}/{r_seg}/pulls/comments/{int(comment_id)}", {"body": text})

  def update_issue_comment(self, owner: str, repo: str, comment_id: int, body: str) -> dict[str, Any]:
    """Edit the body of an existing PR conversation comment."""
    text = (body or "").strip()
    if not text:
      raise ValueError("Comment body cannot be empty")
    o_seg, r_seg = _safe_segment(owner, "owner"), _safe_segment(repo, "repo")
    return self.rest_patch(f"/repos/{o_seg}/{r_seg}/issues/comments/{int(comment_id)}", {"body": text})

  def update_review_summary(self, owner: str, repo: str, number: int, review_id: int, body: str) -> dict[str, Any]:
    """Edit the summary body of an already submitted PR review."""
    text = (body or "").strip()
    if not text:
      raise ValueError("Review summary body cannot be empty")
    o_seg, r_seg = _safe_segment(owner, "owner"), _safe_segment(repo, "repo")
    return self.rest_put(f"/repos/{o_seg}/{r_seg}/pulls/{int(number)}/reviews/{int(review_id)}", {"body": text})

  def resolve_review_thread(self, thread_id: str, *, resolve: bool = True) -> dict[str, Any]:
    """Resolve or unresolve a GraphQL PR review thread."""
    if not thread_id or thread_id.isdigit():
      raise ValueError("GraphQL thread node ID is required to resolve/unresolve a thread")
    mutation_name = "resolveReviewThread" if resolve else "unresolveReviewThread"
    gql = f"""
    mutation($threadId: ID!) {{
      {mutation_name}(input: {{ threadId: $threadId }}) {{
        thread {{ id isResolved }}
      }}
    }}
    """
    data = self.graphql(gql, {"threadId": thread_id})
    return ((data.get(mutation_name) or {}).get("thread")) or {}

  def update_pull_request_state(self, owner: str, repo: str, number: int, *, state: str = "closed") -> dict[str, Any]:
    """Update pull request state (``closed`` or ``open``)."""
    state_norm = (state or "").strip().lower()
    if state_norm not in ("closed", "open"):
      raise ValueError(f"Unsupported PR target state {state!r}; expected 'closed' or 'open'")
    o_seg, r_seg = _safe_segment(owner, "owner"), _safe_segment(repo, "repo")
    return self.rest_patch(f"/repos/{o_seg}/{r_seg}/pulls/{int(number)}", {"state": state_norm})

  def merge_pull_request(
    self,
    owner: str,
    repo: str,
    number: int,
    *,
    merge_method: str = "merge",
    commit_title: str = "",
    commit_message: str = "",
    sha: str | None = None,
  ) -> dict[str, Any]:
    """Merge a pull request using ``merge``, ``squash``, or ``rebase``."""
    method_norm = (merge_method or "merge").strip().lower()
    if method_norm not in VALID_MERGE_METHODS:
      raise ValueError(f"Unsupported merge method {merge_method!r}; expected one of {VALID_MERGE_METHODS}")
    payload: dict[str, Any] = {"merge_method": method_norm}
    if (commit_title or "").strip():
      payload["commit_title"] = commit_title.strip()
    if (commit_message or "").strip():
      payload["commit_message"] = commit_message.strip()
    if sha:
      payload["sha"] = sha
    o_seg, r_seg = _safe_segment(owner, "owner"), _safe_segment(repo, "repo")
    return self.rest_put(f"/repos/{o_seg}/{r_seg}/pulls/{int(number)}/merge", payload)
