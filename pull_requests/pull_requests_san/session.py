"""In-memory PR review session caching, refresh, and draft batching."""

from __future__ import annotations

from typing import Any

from pull_requests_san.auth import DEFAULT_OWNER, normalize_owner
from pull_requests_san.client import GitHubClient, _validate_comment_line_args
from pull_requests_san.models import PullRequestDetail, PullRequestFileDiff, PullRequestSummary


class PRReviewSession:
  """In-memory session cache and draft-review coordinator around ``GitHubClient``."""

  def __init__(self, client: GitHubClient | None = None, *, default_owner: str = DEFAULT_OWNER) -> None:
    self.client = client or GitHubClient()
    self.default_owner = normalize_owner(default_owner)
    self.pr_list_cache: dict[tuple[str, str, str, int], list[PullRequestSummary]] = {}
    self.pr_detail_cache: dict[tuple[str, str, int], PullRequestDetail] = {}
    self.commit_cache: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    self.pending_drafts: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    self._next_draft_id = 1

  def list_prs(
    self,
    *,
    owner: str | None = None,
    state: str = "open",
    repo: str = "",
    refresh: bool = False,
    limit: int = 100,
  ) -> list[PullRequestSummary]:
    norm_owner = normalize_owner(owner or self.default_owner)
    norm_state = (state or "open").strip().lower()
    norm_repo = (repo or "").strip().strip("/")
    key = (norm_owner, norm_state, norm_repo, int(limit))
    if refresh or key not in self.pr_list_cache:
      self.pr_list_cache[key] = self.client.list_org_pull_requests(
        norm_owner, state=norm_state, repo=norm_repo or None, limit=int(limit)
      )
    return self.pr_list_cache[key]

  def get_pr(
    self,
    owner: str,
    repo: str,
    number: int,
    *,
    refresh: bool = False,
    char_diff_mode: str = "grapheme",
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> PullRequestDetail:
    key = (owner, repo, int(number))
    norm_clean = (normalize or "none").strip().lower()
    ign_punct = bool(ignore_punct)
    prev_decision = self.pr_detail_cache[key].summary.review_decision if key in self.pr_detail_cache else None
    if refresh or key not in self.pr_detail_cache:
      for ck in [k for k in self.commit_cache if k[:3] == key]:
        self.commit_cache.pop(ck, None)
      detail = self.client.get_pull_request_detail(
        owner,
        repo,
        int(number),
        char_diff_mode=char_diff_mode,
        normalize=norm_clean,
        ignore_punct=ign_punct,
      )
      if detail.summary.review_decision is None:
        detail.summary.review_decision = prev_decision or self._lookup_cached_review_decision(owner, repo, int(number))
      self.pr_detail_cache[key] = detail
    else:
      detail = self.pr_detail_cache[key]
      if detail.char_diff_mode != char_diff_mode or detail.normalize != norm_clean or detail.ignore_punct != ign_punct:
        detail.recompute_diff_mode(char_diff_mode, normalize=norm_clean, ignore_punct=ign_punct)
    return detail

  def _lookup_cached_review_decision(self, owner: str, repo: str, number: int) -> str | None:
    for lst in self.pr_list_cache.values():
      for item in lst:
        if item.owner == owner and item.repo == repo and item.number == number:
          return item.review_decision
    return None

  def refresh_all(
    self,
    *,
    owner: str | None = None,
    state: str = "open",
    repo: str = "",
    active_pr: tuple[str, str, int] | None = None,
    char_diff_mode: str = "grapheme",
    normalize: str = "none",
    ignore_punct: bool = False,
    limit: int = 100,
  ) -> dict[str, Any]:
    """Invalidate cached PR data and fetch fresh listings (plus active PR if specified)."""
    self.pr_list_cache.clear()
    self.pr_detail_cache.clear()
    self.commit_cache.clear()
    prs = self.list_prs(owner=owner, state=state, repo=repo, refresh=True, limit=limit)
    active_detail = None
    if active_pr is not None:
      a_owner, a_repo, a_num = active_pr
      active_detail = self.get_pr(
        a_owner,
        a_repo,
        int(a_num),
        refresh=True,
        char_diff_mode=char_diff_mode,
        normalize=normalize,
        ignore_punct=ignore_punct,
      )
    return {"prs": prs, "active_detail": active_detail}

  def get_changelist(
    self,
    owner: str,
    repo: str,
    number: int,
    sha: str,
    *,
    refresh: bool = False,
    char_diff_mode: str = "grapheme",
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> dict[str, Any]:
    clean_sha = (sha or "").strip()
    norm_clean = (normalize or "none").strip().lower()
    ign_punct = bool(ignore_punct)
    detail = self.get_pr(
      owner,
      repo,
      number,
      char_diff_mode=char_diff_mode,
      normalize=norm_clean,
      ignore_punct=ign_punct,
    )
    if not clean_sha:
      return {
        "sha": "",
        "parent_sha": detail.summary.base_sha,
        "commit": None,
        "char_diff_mode": detail.char_diff_mode,
        "normalize": detail.normalize,
        "ignore_punct": detail.ignore_punct,
        "files": detail.files,
      }
    ck = (owner, repo, int(number), clean_sha)
    if refresh or ck not in self.commit_cache:
      self.commit_cache[ck] = self.client.get_commit_files(
        owner,
        repo,
        clean_sha,
        head_repo_full_name=detail.summary.head_repo_full_name,
        char_diff_mode=char_diff_mode,
        normalize=norm_clean,
        ignore_punct=ign_punct,
      )
    else:
      entry = self.commit_cache[ck]
      if (
        entry.get("char_diff_mode") != char_diff_mode
        or entry.get("normalize") != norm_clean
        or entry.get("ignore_punct") != ign_punct
      ):
        for f in entry.get("files") or []:
          f.recompute_diff_mode(char_diff_mode, normalize=norm_clean, ignore_punct=ign_punct)
        entry["char_diff_mode"] = char_diff_mode
        entry["normalize"] = norm_clean
        entry["ignore_punct"] = ign_punct
    return self.commit_cache[ck]

  def get_full_file(
    self,
    owner: str,
    repo: str,
    number: int,
    filename: str,
    *,
    commit_sha: str = "",
    char_diff_mode: str = "grapheme",
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> PullRequestFileDiff:
    clean_sha = (commit_sha or "").strip()
    if clean_sha:
      cl = self.get_changelist(
        owner,
        repo,
        number,
        clean_sha,
        char_diff_mode=char_diff_mode,
        normalize=normalize,
        ignore_punct=ignore_punct,
      )
      detail = self.get_pr(owner, repo, number, char_diff_mode=char_diff_mode, normalize=normalize, ignore_punct=ignore_punct)
      for idx, f in enumerate(cl["files"]):
        if f.filename == filename:
          full_f = self.client.get_file_full_diff(
            owner,
            repo,
            f,
            base_sha=cl.get("parent_sha") or "",
            head_sha=cl.get("sha") or clean_sha,
            head_repo_full_name=detail.summary.head_repo_full_name,
            char_diff_mode=char_diff_mode,
            normalize=normalize,
            ignore_punct=ignore_punct,
          )
          cl["files"][idx] = full_f
          return full_f
      raise FileNotFoundError(f"File {filename!r} not found in changelist {clean_sha[:7]} of {owner}/{repo}#{number}")

    detail = self.get_pr(owner, repo, number, char_diff_mode=char_diff_mode, normalize=normalize, ignore_punct=ignore_punct)
    for idx, f in enumerate(detail.files):
      if f.filename == filename:
        full_f = self.client.get_file_full_diff(
          owner,
          repo,
          f,
          base_sha=detail.summary.base_sha,
          head_sha=detail.summary.head_sha,
          head_repo_full_name=detail.summary.head_repo_full_name,
          char_diff_mode=char_diff_mode,
          normalize=normalize,
          ignore_punct=ignore_punct,
        )
        detail.files[idx] = full_f
        return full_f
    raise FileNotFoundError(f"File {filename!r} not found in {owner}/{repo}#{number}")

  def get_drafts(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    return list(self.pending_drafts.get((owner, repo, int(number)), []))

  def add_draft(
    self,
    owner: str,
    repo: str,
    number: int,
    *,
    path: str,
    line: int,
    side: str = "RIGHT",
    body: str,
    start_line: int | None = None,
    start_side: str | None = None,
  ) -> dict[str, Any]:
    text = (body or "").strip()
    if not text:
      raise ValueError("Draft comment body cannot be empty")
    if not (path or "").strip():
      raise ValueError("Draft file path cannot be empty")
    line_int, side_norm, start_int, start_side_norm = _validate_comment_line_args(line, side, start_line, start_side)
    draft = {
      "draft_id": f"draft_{self._next_draft_id}",
      "path": path,
      "line": line_int,
      "side": side_norm,
      "body": text,
      "start_line": start_int,
      "start_side": start_side_norm,
    }
    self._next_draft_id += 1
    self.pending_drafts.setdefault((owner, repo, int(number)), []).append(draft)
    return draft

  def delete_draft(self, owner: str, repo: str, number: int, draft_id: str) -> bool:
    key = (owner, repo, int(number))
    drafts = self.pending_drafts.get(key, [])
    new_drafts = [d for d in drafts if d["draft_id"] != draft_id]
    if len(new_drafts) == len(drafts):
      return False
    self.pending_drafts[key] = new_drafts
    return True

  def update_draft(self, owner: str, repo: str, number: int, draft_id: str, body: str) -> dict[str, Any]:
    text = (body or "").strip()
    if not text:
      raise ValueError("Draft comment body cannot be empty")
    key = (owner, repo, int(number))
    for d in self.pending_drafts.get(key, []):
      if d["draft_id"] == draft_id:
        d["body"] = text
        return dict(d)
    raise FileNotFoundError(f"Draft comment {draft_id!r} not found")

  def _refresh_after_mutation(
    self,
    owner: str,
    repo: str,
    number: int,
    char_diff_mode: str,
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> PullRequestDetail:
    detail = self.get_pr(
      owner,
      repo,
      number,
      refresh=True,
      char_diff_mode=char_diff_mode,
      normalize=normalize,
      ignore_punct=ignore_punct,
    )
    s = detail.summary
    for lst in self.pr_list_cache.values():
      for i, item in enumerate(lst):
        if item.owner == s.owner and item.repo == s.repo and item.number == s.number:
          if s.review_decision is None and item.review_decision is not None:
            s.review_decision = item.review_decision
          lst[i] = s
    return detail

  def submit_issue_comment(
    self,
    owner: str,
    repo: str,
    number: int,
    body: str,
    *,
    char_diff_mode: str = "grapheme",
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> dict[str, Any]:
    created = self.client.create_issue_comment(owner, repo, number, body)
    return {"created": created, "detail": self._refresh_after_mutation(owner, repo, number, char_diff_mode, normalize, ignore_punct)}

  def submit_inline_comment(
    self,
    owner: str,
    repo: str,
    number: int,
    *,
    path: str,
    line: int,
    side: str = "RIGHT",
    body: str,
    start_line: int | None = None,
    start_side: str | None = None,
    commit_id: str | None = None,
    char_diff_mode: str = "grapheme",
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> dict[str, Any]:
    if not commit_id and (owner, repo, int(number)) in self.pr_detail_cache:
      commit_id = self.pr_detail_cache[(owner, repo, int(number))].summary.head_sha
    created = self.client.create_inline_comment(
      owner,
      repo,
      number,
      path=path,
      line=line,
      side=side,
      body=body,
      commit_id=commit_id,
      start_line=start_line,
      start_side=start_side,
    )
    return {"created": created, "detail": self._refresh_after_mutation(owner, repo, number, char_diff_mode, normalize, ignore_punct)}

  def reply_to_comment(
    self,
    owner: str,
    repo: str,
    number: int,
    comment_id: int,
    body: str,
    *,
    char_diff_mode: str = "grapheme",
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> dict[str, Any]:
    created = self.client.reply_to_review_comment(owner, repo, number, comment_id, body)
    return {"created": created, "detail": self._refresh_after_mutation(owner, repo, number, char_diff_mode, normalize, ignore_punct)}

  def edit_review_comment(
    self,
    owner: str,
    repo: str,
    number: int,
    comment_id: int,
    body: str,
    *,
    char_diff_mode: str = "grapheme",
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> dict[str, Any]:
    updated = self.client.update_review_comment(owner, repo, comment_id, body)
    return {"updated": updated, "detail": self._refresh_after_mutation(owner, repo, number, char_diff_mode, normalize, ignore_punct)}

  def edit_issue_comment(
    self,
    owner: str,
    repo: str,
    number: int,
    comment_id: int,
    body: str,
    *,
    char_diff_mode: str = "grapheme",
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> dict[str, Any]:
    updated = self.client.update_issue_comment(owner, repo, comment_id, body)
    return {"updated": updated, "detail": self._refresh_after_mutation(owner, repo, number, char_diff_mode, normalize, ignore_punct)}

  def edit_review_summary(
    self,
    owner: str,
    repo: str,
    number: int,
    review_id: int,
    body: str,
    *,
    char_diff_mode: str = "grapheme",
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> dict[str, Any]:
    updated = self.client.update_review_summary(owner, repo, number, review_id, body)
    return {"updated": updated, "detail": self._refresh_after_mutation(owner, repo, number, char_diff_mode, normalize, ignore_punct)}

  def submit_review(
    self,
    owner: str,
    repo: str,
    number: int,
    *,
    event: str = "COMMENT",
    body: str = "",
    include_drafts: bool = True,
    extra_comments: list[dict[str, Any]] | None = None,
    char_diff_mode: str = "grapheme",
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> dict[str, Any]:
    key = (owner, repo, int(number))
    drafts = list(self.pending_drafts.get(key, [])) if include_drafts else []
    combined_comments = [*drafts, *(extra_comments or [])]
    commit_id = self.pr_detail_cache[key].summary.head_sha if key in self.pr_detail_cache else None
    created = self.client.submit_review(
      owner,
      repo,
      number,
      event=event,
      body=body,
      comments=combined_comments,
      commit_id=commit_id,
    )
    if include_drafts:
      self.pending_drafts[key] = []
    return {"created": created, "detail": self._refresh_after_mutation(owner, repo, number, char_diff_mode, normalize, ignore_punct)}

  def resolve_thread(
    self,
    owner: str,
    repo: str,
    number: int,
    thread_id: str,
    *,
    resolve: bool = True,
    char_diff_mode: str = "grapheme",
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> dict[str, Any]:
    updated = self.client.resolve_review_thread(thread_id, resolve=resolve)
    return {"thread": updated, "detail": self._refresh_after_mutation(owner, repo, number, char_diff_mode, normalize, ignore_punct)}

  def _prune_or_update_list_caches(self, detail: PullRequestDetail) -> None:
    s = detail.summary
    new_state = s.state.lower()
    is_merged = detail.merged or s.merged
    for cache_key, lst in list(self.pr_list_cache.items()):
      list_state = cache_key[1]
      should_remove = (
        (list_state == "open" and new_state != "open")
        or (list_state == "closed" and new_state != "closed")
        or (list_state == "merged" and not is_merged)
      )
      if should_remove:
        self.pr_list_cache[cache_key] = [
          p for p in lst if not (p.owner == s.owner and p.repo == s.repo and p.number == s.number)
        ]

  def set_pr_state(
    self,
    owner: str,
    repo: str,
    number: int,
    *,
    state: str = "closed",
    char_diff_mode: str = "grapheme",
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> dict[str, Any]:
    updated = self.client.update_pull_request_state(owner, repo, number, state=state)
    detail = self._refresh_after_mutation(owner, repo, number, char_diff_mode, normalize, ignore_punct)
    self._prune_or_update_list_caches(detail)
    return {"updated": updated, "detail": detail}

  def merge_pr(
    self,
    owner: str,
    repo: str,
    number: int,
    *,
    merge_method: str = "merge",
    commit_title: str = "",
    commit_message: str = "",
    sha: str | None = None,
    char_diff_mode: str = "grapheme",
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> dict[str, Any]:
    key = (owner, repo, int(number))
    head_sha = sha or (self.pr_detail_cache[key].summary.head_sha if key in self.pr_detail_cache else None)
    merged = self.client.merge_pull_request(
      owner,
      repo,
      number,
      merge_method=merge_method,
      commit_title=commit_title,
      commit_message=commit_message,
      sha=head_sha,
    )
    detail = self._refresh_after_mutation(owner, repo, number, char_diff_mode, normalize, ignore_punct)
    self._prune_or_update_list_caches(detail)
    return {"merged": merged, "detail": detail}
