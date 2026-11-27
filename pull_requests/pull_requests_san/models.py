"""Dataclass models for pull requests, side-by-side diffs, and review threads."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class AlignedDiffRow:
  """One aligned row in a side-by-side LHS/RHS diff table."""

  kind: str  # "context" | "modify" | "del" | "add"
  left_num: int | None
  right_num: int | None
  left_text: str | None
  right_text: str | None
  lhs_segments: list[dict[str, str]] = field(default_factory=list)
  rhs_segments: list[dict[str, str]] = field(default_factory=list)
  in_diff_hunk: bool = True

  def recompute_segments(
    self,
    mode: str = "grapheme",
    *,
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> None:
    from pull_requests_san.diff import apply_normalization, compute_char_diff, strip_punctuation

    if self.left_text is not None and self.right_text is not None:
      l_cmp = apply_normalization(self.left_text, normalize)
      r_cmp = apply_normalization(self.right_text, normalize)
      if ignore_punct:
        l_cmp = strip_punctuation(l_cmp)
        r_cmp = strip_punctuation(r_cmp)
      if l_cmp == r_cmp:
        self.kind = "context"
        txt_l = apply_normalization(self.left_text, normalize)
        txt_r = apply_normalization(self.right_text, normalize)
        self.lhs_segments = [{"op": "eq", "text": txt_l}] if txt_l else []
        self.rhs_segments = [{"op": "eq", "text": txt_r}] if txt_r else []
      else:
        self.kind = "modify"
        self.lhs_segments, self.rhs_segments = compute_char_diff(
          self.left_text,
          self.right_text,
          mode=mode,
          normalize=normalize,
          ignore_punct=ignore_punct,
        )
    elif self.kind == "del":
      txt = apply_normalization(self.left_text or "", normalize)
      self.lhs_segments = [{"op": "del", "text": txt}] if txt else []
      self.rhs_segments = []
    elif self.kind == "add":
      txt = apply_normalization(self.right_text or "", normalize)
      self.lhs_segments = []
      self.rhs_segments = [{"op": "ins", "text": txt}] if txt else []

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)


@dataclass
class DiffHunk:
  """One unified-diff hunk converted into aligned LHS/RHS rows."""

  header: str
  old_start: int
  old_count: int
  new_start: int
  new_count: int
  context: str = ""
  rows: list[AlignedDiffRow] = field(default_factory=list)
  inline_rows: list[dict[str, Any]] = field(default_factory=list)

  def to_dict(self) -> dict[str, Any]:
    return {
      "header": self.header,
      "old_start": self.old_start,
      "old_count": self.old_count,
      "new_start": self.new_start,
      "new_count": self.new_count,
      "context": self.context,
      "rows": [r.to_dict() for r in self.rows],
      "inline_rows": list(self.inline_rows),
    }


@dataclass
class PullRequestSummary:
  """Lightweight pull request metadata returned in multi-repo listings."""

  owner: str
  repo: str
  repo_full_name: str
  number: int
  title: str
  url: str
  state: str
  is_draft: bool
  author: str
  created_at: str
  updated_at: str
  base_ref: str = ""
  base_sha: str = ""
  head_ref: str = ""
  head_sha: str = ""
  head_repo_full_name: str = ""
  additions: int = 0
  deletions: int = 0
  changed_files: int = 0
  review_decision: str | None = None
  merged: bool = False
  mergeable: bool | None = None
  mergeable_state: str = ""
  comment_count: int = 0
  thread_count: int = 0

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)


@dataclass
class ReviewComment:
  """One PR conversation comment or inline review thread comment."""

  id: int
  author: str
  body: str
  created_at: str
  updated_at: str = ""
  url: str = ""
  in_reply_to_id: int | None = None

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)


@dataclass
class PullRequestCommit:
  """One commit / changelist inside a pull request."""

  sha: str
  short_sha: str
  title: str
  message: str
  author: str
  committed_at: str
  url: str = ""
  parents: list[str] = field(default_factory=list)

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)


@dataclass
class ReviewThread:
  """One inline PR review thread anchored to a file and line."""

  id: str
  path: str
  line: int | None
  original_line: int | None
  side: str
  is_resolved: bool = False
  is_outdated: bool = False
  comments: list[ReviewComment] = field(default_factory=list)

  def to_dict(self) -> dict[str, Any]:
    return {
      "id": self.id,
      "path": self.path,
      "line": self.line,
      "original_line": self.original_line,
      "side": self.side,
      "is_resolved": self.is_resolved,
      "is_outdated": self.is_outdated,
      "comments": [c.to_dict() for c in self.comments],
    }


@dataclass
class PullRequestFileDiff:
  """One changed file inside a PR along with parsed LHS/RHS diff hunks."""

  filename: str
  status: str
  additions: int
  deletions: int
  changes: int
  previous_filename: str | None = None
  sha: str = ""
  patch: str | None = None
  is_binary: bool = False
  is_full_file: bool = False
  local_file_path: str | None = None
  hunks: list[DiffHunk] = field(default_factory=list)

  def recompute_diff_mode(
    self,
    mode: str,
    *,
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> None:
    from pull_requests_san.diff import build_inline_flow_rows

    for h in self.hunks:
      for r in h.rows:
        r.recompute_segments(mode=mode, normalize=normalize, ignore_punct=ignore_punct)
      h.inline_rows = build_inline_flow_rows(
        h.rows,
        char_diff_mode=mode,
        normalize=normalize,
        ignore_punct=ignore_punct,
      )

  def to_dict(self) -> dict[str, Any]:
    return {
      "filename": self.filename,
      "status": self.status,
      "additions": self.additions,
      "deletions": self.deletions,
      "changes": self.changes,
      "previous_filename": self.previous_filename,
      "sha": self.sha,
      "has_patch": bool(self.patch),
      "is_binary": self.is_binary,
      "is_full_file": self.is_full_file,
      "local_file_path": self.local_file_path,
      "hunks": [h.to_dict() for h in self.hunks],
    }


@dataclass
class PullRequestDetail:
  """Complete PR view including metadata, file diffs, comments, reviews, and threads."""

  summary: PullRequestSummary
  body: str
  commits_count: int = 0
  merged: bool = False
  mergeable: bool | None = None
  mergeable_state: str = ""
  rebaseable: bool | None = None
  behind_by: int | None = None
  ahead_by: int | None = None
  conflicts: list[dict[str, Any]] = field(default_factory=list)
  char_diff_mode: str = "grapheme"
  normalize: str = "none"
  ignore_punct: bool = False
  commits: list[PullRequestCommit] = field(default_factory=list)
  files: list[PullRequestFileDiff] = field(default_factory=list)
  issue_comments: list[ReviewComment] = field(default_factory=list)
  reviews: list[dict[str, Any]] = field(default_factory=list)
  review_threads: list[ReviewThread] = field(default_factory=list)

  def recompute_diff_mode(
    self,
    mode: str,
    *,
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> None:
    from pull_requests_san.diff import build_inline_flow_rows, compute_char_diff

    self.char_diff_mode = mode
    self.normalize = normalize
    self.ignore_punct = ignore_punct
    for f in self.files:
      f.recompute_diff_mode(mode, normalize=normalize, ignore_punct=ignore_punct)
    for conf in self.conflicts:
      for hunk in conf.get("hunks") or []:
        row_objs: list[AlignedDiffRow] = []
        for row in hunk.get("rows") or []:
          if row.get("left_text") is not None and row.get("right_text") is not None:
            tmp_row = AlignedDiffRow(
              kind=row.get("kind") or "modify",
              left_num=row.get("left_num"),
              right_num=row.get("right_num"),
              left_text=row.get("left_text"),
              right_text=row.get("right_text"),
              in_diff_hunk=bool(row.get("in_diff_hunk", True)),
            )
            tmp_row.recompute_segments(mode=mode, normalize=normalize, ignore_punct=ignore_punct)
            row["kind"] = tmp_row.kind
            row["lhs_segments"] = tmp_row.lhs_segments
            row["rhs_segments"] = tmp_row.rhs_segments
            row_objs.append(tmp_row)
          else:
            row_objs.append(
              AlignedDiffRow(
                kind=row.get("kind") or "modify",
                left_num=row.get("left_num"),
                right_num=row.get("right_num"),
                left_text=row.get("left_text"),
                right_text=row.get("right_text"),
                lhs_segments=row.get("lhs_segments") or [],
                rhs_segments=row.get("rhs_segments") or [],
                in_diff_hunk=bool(row.get("in_diff_hunk", True)),
              )
            )
        hunk["inline_rows"] = build_inline_flow_rows(
          row_objs,
          char_diff_mode=mode,
          normalize=normalize,
          ignore_punct=ignore_punct,
        )

  def to_dict(self) -> dict[str, Any]:
    return {
      "summary": self.summary.to_dict(),
      "body": self.body,
      "commits_count": self.commits_count,
      "merged": self.merged,
      "mergeable": self.mergeable,
      "mergeable_state": self.mergeable_state,
      "rebaseable": self.rebaseable,
      "behind_by": self.behind_by,
      "ahead_by": self.ahead_by,
      "conflicts": list(self.conflicts),
      "char_diff_mode": self.char_diff_mode,
      "normalize": self.normalize,
      "ignore_punct": self.ignore_punct,
      "commits": [c.to_dict() for c in self.commits],
      "files": [f.to_dict() for f in self.files],
      "issue_comments": [c.to_dict() for c in self.issue_comments],
      "reviews": list(self.reviews),
      "review_threads": [t.to_dict() for t in self.review_threads],
    }
