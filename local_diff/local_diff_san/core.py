"""Local file and Git HEAD diff computation built on pull_requests_san.diff."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import subprocess
from typing import Any, Sequence

from pull_requests_san.diff import (
  DiffHunk,
  VALID_DIFF_MODES,
  VALID_NORMALIZATIONS,
  build_full_file_hunk,
  build_unified_patch_from_texts,
)


DEFAULT_REV = "HEAD"


@dataclass
class LocalDiffSpec:
  """Target specification describing which local files or git diffs to view."""

  mode: str = "git_dir"  # "file_pair" | "git_head" | "git_dir"
  left_path: str | None = None
  right_path: str | None = None
  paths: list[str] = field(default_factory=list)
  rev: str = DEFAULT_REV

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)


@dataclass
class LocalDiffItem:
  """Single compared item (pair of local files or local file vs Git revision)."""

  id: str
  title: str
  mode: str
  status: str
  left_label: str
  right_label: str
  left_path: str | None = None
  right_path: str | None = None
  repo_root: str | None = None
  rel_path: str | None = None
  rev: str = DEFAULT_REV
  additions: int = 0
  deletions: int = 0
  is_binary: bool = False
  patch: str = ""
  hunks: list[DiffHunk] = field(default_factory=list)

  def to_dict(self, *, include_hunks: bool = True) -> dict[str, Any]:
    out = {
      "id": self.id,
      "title": self.title,
      "mode": self.mode,
      "status": self.status,
      "left_label": self.left_label,
      "right_label": self.right_label,
      "left_path": self.left_path,
      "right_path": self.right_path,
      "repo_root": self.repo_root,
      "rel_path": self.rel_path,
      "rev": self.rev,
      "additions": self.additions,
      "deletions": self.deletions,
      "is_binary": self.is_binary,
      "patch": self.patch if include_hunks else "",
      "hunks": [h.to_dict() for h in self.hunks] if include_hunks else [],
    }
    return out


def _validate_diff_options(char_diff_mode: str, normalize: str) -> tuple[str, str]:
  mode = (char_diff_mode or "word").strip().lower()
  if mode not in VALID_DIFF_MODES:
    raise ValueError(f"Invalid diff_mode {char_diff_mode!r}; expected one of {VALID_DIFF_MODES}")
  norm = (normalize or "none").strip().lower()
  if norm not in VALID_NORMALIZATIONS:
    raise ValueError(f"Invalid normalize {normalize!r}; expected one of {VALID_NORMALIZATIONS}")
  return mode, norm


def _run_git_bytes(repo_or_dir: Path, args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
  return subprocess.run(
    ["git", "-c", "core.quotePath=false", "-C", str(repo_or_dir), *args],
    capture_output=True,
    check=False,
  )


def find_git_root(path: str | Path) -> Path | None:
  """Locate the enclosing Git repository root for ``path`` (works even if leaf does not exist)."""
  cur = Path(path).expanduser()
  try:
    cur = cur.resolve()
  except OSError:
    cur = cur.absolute()
  while not cur.is_dir():
    parent = cur.parent
    if parent == cur:
      return None
    cur = parent
  proc = _run_git_bytes(cur, ["rev-parse", "--show-toplevel"])
  if proc.returncode != 0:
    return None
  root_str = proc.stdout.decode("utf-8", errors="replace").strip()
  return Path(root_str).resolve() if root_str else None


def _git_has_rev(repo_root: Path, rev: str) -> bool:
  proc = _run_git_bytes(repo_root, ["rev-parse", "--verify", "--quiet", f"{rev}^{{tree}}"])
  return proc.returncode == 0


def read_git_blob(repo_root: Path, rel_path: str, rev: str = DEFAULT_REV) -> bytes | None:
  """Read raw bytes of ``rel_path`` at ``rev`` inside ``repo_root``, or None if absent."""
  clean_rel = Path(rel_path).as_posix().lstrip("/")
  proc = _run_git_bytes(repo_root, ["show", f"{rev}:{clean_rel}"])
  if proc.returncode != 0:
    return None
  return proc.stdout


def _is_tracked_in_index(repo_root: Path, rel_path: str) -> bool:
  clean_rel = Path(rel_path).as_posix().lstrip("/")
  proc = _run_git_bytes(repo_root, ["ls-files", "--error-unmatch", "--", clean_rel])
  return proc.returncode == 0


def _decode_text_or_binary(raw: bytes | None) -> tuple[str | None, bool]:
  if raw is None:
    return None, False
  if b"\x00" in raw[:8192]:
    return None, True
  return raw.decode("utf-8", errors="replace"), False


def _count_patch_stats(patch: str) -> tuple[int, int]:
  adds = 0
  dels = 0
  for ln in (patch or "").splitlines():
    if ln.startswith("+++") or ln.startswith("---"):
      continue
    if ln.startswith("+"):
      adds += 1
    elif ln.startswith("-"):
      dels += 1
  return adds, dels


def _compute_item_from_bytes(
  *,
  item_id: str,
  title: str,
  mode: str,
  status: str,
  left_label: str,
  right_label: str,
  left_bytes: bytes | None,
  right_bytes: bytes | None,
  left_path: str | None = None,
  right_path: str | None = None,
  repo_root: str | None = None,
  rel_path: str | None = None,
  rev: str = DEFAULT_REV,
  display_name: str = "file",
  previous_name: str | None = None,
  char_diff_mode: str = "word",
  normalize: str = "none",
  ignore_punct: bool = False,
) -> LocalDiffItem:
  left_text, left_bin = _decode_text_or_binary(left_bytes)
  right_text, right_bin = _decode_text_or_binary(right_bytes)
  is_binary = left_bin or right_bin
  if is_binary:
    return LocalDiffItem(
      id=item_id,
      title=title,
      mode=mode,
      status=status,
      left_label=left_label,
      right_label=right_label,
      left_path=left_path,
      right_path=right_path,
      repo_root=repo_root,
      rel_path=rel_path,
      rev=rev,
      is_binary=True,
    )

  patch = build_unified_patch_from_texts(
    left_text,
    right_text,
    display_name,
    previous_filename=previous_name,
  )
  adds, dels = _count_patch_stats(patch)
  hunk = build_full_file_hunk(
    left_text,
    right_text,
    existing_patch=patch or None,
    char_diff_mode=char_diff_mode,
    normalize=normalize,
    ignore_punct=ignore_punct,
  )
  return LocalDiffItem(
    id=item_id,
    title=title,
    mode=mode,
    status=status,
    left_label=left_label,
    right_label=right_label,
    left_path=left_path,
    right_path=right_path,
    repo_root=repo_root,
    rel_path=rel_path,
    rev=rev,
    additions=adds,
    deletions=dels,
    is_binary=False,
    patch=patch,
    hunks=[hunk],
  )


def compare_two_files(
  left_path: str | Path,
  right_path: str | Path,
  *,
  char_diff_mode: str = "word",
  normalize: str = "none",
  ignore_punct: bool = False,
) -> LocalDiffItem:
  """Compare two local files on disk using ``pull_requests_san.diff``."""
  diff_mode, norm_form = _validate_diff_options(char_diff_mode, normalize)
  lp = Path(left_path).expanduser().resolve()
  rp = Path(right_path).expanduser().resolve()
  if not lp.is_file():
    raise FileNotFoundError(f"Left file not found: {lp}")
  if not rp.is_file():
    raise FileNotFoundError(f"Right file not found: {rp}")
  l_bytes = lp.read_bytes()
  r_bytes = rp.read_bytes()
  status = "unchanged" if l_bytes == r_bytes else "modified"
  title = f"{lp.name} ↔ {rp.name}" if lp.name != rp.name else f"{lp} ↔ {rp}"
  return _compute_item_from_bytes(
    item_id="pair:0",
    title=title,
    mode="file_pair",
    status=status,
    left_label=str(lp),
    right_label=str(rp),
    left_bytes=l_bytes,
    right_bytes=r_bytes,
    left_path=str(lp),
    right_path=str(rp),
    rev="",
    display_name=rp.name,
    previous_name=lp.name,
    char_diff_mode=diff_mode,
    normalize=norm_form,
    ignore_punct=bool(ignore_punct),
  )


def compare_file_with_head(
  file_path: str | Path,
  *,
  rev: str = DEFAULT_REV,
  old_rel_path: str | None = None,
  item_id: str | None = None,
  char_diff_mode: str = "word",
  normalize: str = "none",
  ignore_punct: bool = False,
) -> LocalDiffItem:
  """Compare a local file on disk against its committed version at ``rev`` (default ``HEAD``)."""
  diff_mode, norm_form = _validate_diff_options(char_diff_mode, normalize)
  clean_rev = (rev or DEFAULT_REV).strip() or DEFAULT_REV
  target = Path(file_path).expanduser()
  try:
    target = target.resolve()
  except OSError:
    target = target.absolute()
  if target.is_dir():
    raise ValueError(f"Expected file path, got directory: {target}")

  repo_root = find_git_root(target)
  if repo_root is None:
    raise ValueError(f"Not inside a git repository: {target}")

  try:
    rel_path = target.relative_to(repo_root).as_posix()
  except ValueError as exc:
    raise ValueError(f"File {target} is outside git repository {repo_root}") from exc

  blob_rel = old_rel_path or rel_path
  has_rev = _git_has_rev(repo_root, clean_rev)
  head_bytes = read_git_blob(repo_root, blob_rel, rev=clean_rev) if has_rev else None
  work_bytes = target.read_bytes() if target.is_file() else None

  if head_bytes is None and work_bytes is None:
    raise FileNotFoundError(f"File not found on disk or at {clean_rev}: {target}")

  if old_rel_path and old_rel_path != rel_path:
    status = "renamed"
  elif head_bytes is None and work_bytes is not None:
    status = "added" if _is_tracked_in_index(repo_root, rel_path) else "untracked"
  elif head_bytes is not None and work_bytes is None:
    status = "deleted"
  elif head_bytes == work_bytes:
    status = "unchanged"
  else:
    status = "modified"

  return _compute_item_from_bytes(
    item_id=item_id or f"head:{rel_path}",
    title=rel_path,
    mode="git_head",
    status=status,
    left_label=f"{clean_rev}:{blob_rel}" if head_bytes is not None else f"(absent in {clean_rev})",
    right_label=f"WORKING:{rel_path}" if work_bytes is not None else f"(deleted on disk)",
    left_bytes=head_bytes,
    right_bytes=work_bytes,
    left_path=None,
    right_path=str(target) if work_bytes is not None else None,
    repo_root=str(repo_root),
    rel_path=rel_path,
    rev=clean_rev,
    display_name=rel_path,
    previous_name=old_rel_path,
    char_diff_mode=diff_mode,
    normalize=norm_form,
    ignore_punct=bool(ignore_punct),
  )


def _parse_numstat_z(raw: bytes) -> dict[str, tuple[int, int]]:
  stats: dict[str, tuple[int, int]] = {}
  parts = raw.split(b"\x00")
  i = 0
  while i < len(parts):
    tok = parts[i]
    i += 1
    if not tok:
      continue
    fields = tok.decode("utf-8", errors="replace").split("\t", 2)
    if len(fields) != 3:
      continue
    add_s, del_s, path_s = fields
    adds = int(add_s) if add_s.isdigit() else 0
    dels = int(del_s) if del_s.isdigit() else 0
    if path_s == "" and i + 1 < len(parts):
      # Rename entry in numstat -z: empty path, then old_path\0, new_path\0
      _old_p = parts[i].decode("utf-8", errors="replace")
      new_p = parts[i + 1].decode("utf-8", errors="replace")
      i += 2
      stats[new_p] = (adds, dels)
    else:
      stats[path_s] = (adds, dels)
  return stats


def _parse_name_status_z(raw: bytes) -> list[dict[str, Any]]:
  entries: list[dict[str, Any]] = []
  parts = [p.decode("utf-8", errors="replace") for p in raw.split(b"\x00")]
  i = 0
  status_names = {
    "M": "modified",
    "A": "added",
    "D": "deleted",
    "T": "typechange",
    "U": "unmerged",
  }
  while i < len(parts):
    st_code = parts[i]
    i += 1
    if not st_code:
      continue
    kind = st_code[0]
    if kind in ("R", "C"):
      if i + 1 >= len(parts):
        break
      old_p = parts[i]
      new_p = parts[i + 1]
      i += 2
      entries.append({"rel_path": new_p, "old_rel_path": old_p, "status": "renamed" if kind == "R" else "copied"})
    else:
      if i >= len(parts):
        break
      rel_p = parts[i]
      i += 1
      entries.append({"rel_path": rel_p, "old_rel_path": None, "status": status_names.get(kind, "modified")})
  return entries


def discover_git_dir_entries(target_dir: str | Path, *, rev: str = DEFAULT_REV) -> tuple[Path | None, list[dict[str, Any]]]:
  """Discover modified/added/deleted/untracked files under ``target_dir`` relative to ``rev``."""
  clean_rev = (rev or DEFAULT_REV).strip() or DEFAULT_REV
  target = Path(target_dir).expanduser().resolve()
  repo_root = find_git_root(target)
  if repo_root is None:
    return None, []

  rel_scope = target.relative_to(repo_root).as_posix()
  scope_args = ["--", rel_scope] if rel_scope and rel_scope != "." else []

  entries: list[dict[str, Any]] = []
  seen: set[str] = set()
  if _git_has_rev(repo_root, clean_rev):
    ns_proc = _run_git_bytes(repo_root, ["diff", "--name-status", "-z", clean_rev, *scope_args])
    num_proc = _run_git_bytes(repo_root, ["diff", "--numstat", "-z", clean_rev, *scope_args])
    numstat = _parse_numstat_z(num_proc.stdout) if num_proc.returncode == 0 else {}
    if ns_proc.returncode == 0:
      for ent in _parse_name_status_z(ns_proc.stdout):
        rp = ent["rel_path"]
        seen.add(rp)
        adds, dels = numstat.get(rp, (0, 0))
        entries.append({**ent, "additions": adds, "deletions": dels})

  ut_proc = _run_git_bytes(repo_root, ["ls-files", "--others", "--exclude-standard", "-z", *scope_args])
  if ut_proc.returncode == 0:
    for raw_p in ut_proc.stdout.split(b"\x00"):
      rp = raw_p.decode("utf-8", errors="replace")
      if not rp or rp in seen:
        continue
      seen.add(rp)
      disk_file = repo_root / rp
      line_cnt = 0
      if disk_file.is_file():
        try:
          b = disk_file.read_bytes()
          if b"\x00" not in b[:8192]:
            line_cnt = len(b.decode("utf-8", errors="replace").splitlines())
        except OSError:
          pass
      entries.append({"rel_path": rp, "old_rel_path": None, "status": "untracked", "additions": line_cnt, "deletions": 0})

  entries.sort(key=lambda e: e["rel_path"])
  return repo_root, entries


def resolve_spec_from_paths(
  paths: Sequence[str] = (),
  *,
  rev: str = DEFAULT_REV,
  force_head: bool = False,
) -> LocalDiffSpec:
  """Build a ``LocalDiffSpec`` from CLI path arguments."""
  clean_rev = (rev or DEFAULT_REV).strip() or DEFAULT_REV
  raw_paths = [str(p) for p in paths if str(p).strip()]
  if len(raw_paths) == 0:
    return LocalDiffSpec(mode="git_dir", paths=[str(Path.cwd().resolve())], rev=clean_rev)
  if len(raw_paths) == 2 and not force_head:
    p0 = Path(raw_paths[0]).expanduser().resolve()
    p1 = Path(raw_paths[1]).expanduser().resolve()
    if not p0.is_dir() and not p1.is_dir():
      return LocalDiffSpec(mode="file_pair", left_path=str(p0), right_path=str(p1), rev=clean_rev)
  if len(raw_paths) == 1:
    p0 = Path(raw_paths[0]).expanduser()
    resolved = p0.resolve() if p0.exists() else p0.absolute()
    if resolved.is_dir():
      return LocalDiffSpec(mode="git_dir", paths=[str(resolved)], rev=clean_rev)
    return LocalDiffSpec(mode="git_head", paths=[str(resolved)], rev=clean_rev)
  resolved_list = [str((Path(x).expanduser().resolve() if Path(x).expanduser().exists() else Path(x).expanduser().absolute())) for x in raw_paths]
  return LocalDiffSpec(mode="git_head", paths=resolved_list, rev=clean_rev)


def build_diff_workspace(
  spec: LocalDiffSpec,
  *,
  active_id: str | None = None,
  char_diff_mode: str = "word",
  normalize: str = "none",
  ignore_punct: bool = False,
) -> dict[str, Any]:
  """Compute sidebar items and the active file diff payload for ``spec``."""
  diff_mode, norm_form = _validate_diff_options(char_diff_mode, normalize)
  clean_rev = (spec.rev or DEFAULT_REV).strip() or DEFAULT_REV

  if spec.mode == "file_pair":
    if not spec.left_path or not spec.right_path:
      raise ValueError("Both left_path and right_path are required in 2-file mode")
    item = compare_two_files(
      spec.left_path,
      spec.right_path,
      char_diff_mode=diff_mode,
      normalize=norm_form,
      ignore_punct=bool(ignore_punct),
    )
    payload = item.to_dict(include_hunks=True)
    return {
      "spec": spec.to_dict(),
      "repo_root": None,
      "items": [payload],
      "active_item": payload,
    }

  if spec.mode == "git_head":
    if not spec.paths:
      raise ValueError("At least one file path is required in Git vs revision mode")
    built_items: list[LocalDiffItem] = []
    for idx, p in enumerate(spec.paths):
      built_items.append(
        compare_file_with_head(
          p,
          rev=clean_rev,
          item_id=f"head:{idx}:{Path(p).name}",
          char_diff_mode=diff_mode,
          normalize=norm_form,
          ignore_punct=bool(ignore_punct),
        )
      )
    chosen = next((it for it in built_items if it.id == active_id), built_items[0])
    chosen_dict = chosen.to_dict(include_hunks=True)
    item_summaries = [
      chosen_dict if it.id == chosen.id else it.to_dict(include_hunks=False)
      for it in built_items
    ]
    return {
      "spec": spec.to_dict(),
      "repo_root": chosen.repo_root,
      "items": item_summaries,
      "active_item": chosen_dict,
    }

  # spec.mode == "git_dir"
  dir_path = Path(spec.paths[0] if spec.paths else Path.cwd()).expanduser().resolve()
  if not dir_path.is_dir():
    raise FileNotFoundError(f"Directory not found: {dir_path}")
  repo_root, entries = discover_git_dir_entries(dir_path, rev=clean_rev)
  if repo_root is None or not entries:
    return {
      "spec": spec.to_dict(),
      "repo_root": str(repo_root) if repo_root else None,
      "items": [],
      "active_item": None,
    }

  summary_items: list[dict[str, Any]] = []
  by_id: dict[str, dict[str, Any]] = {}
  for ent in entries:
    rel_p = ent["rel_path"]
    old_rp = ent.get("old_rel_path")
    iid = f"head:{rel_p}"
    disk_p = repo_root / rel_p
    summary = LocalDiffItem(
      id=iid,
      title=rel_p,
      mode="git_head",
      status=ent["status"],
      left_label=f"{clean_rev}:{old_rp or rel_p}" if ent["status"] != "untracked" else f"(absent in {clean_rev})",
      right_label=f"WORKING:{rel_p}" if disk_p.is_file() else "(deleted on disk)",
      left_path=None,
      right_path=str(disk_p) if disk_p.is_file() else None,
      repo_root=str(repo_root),
      rel_path=rel_p,
      rev=clean_rev,
      additions=ent["additions"],
      deletions=ent["deletions"],
    ).to_dict(include_hunks=False)
    summary_items.append(summary)
    by_id[iid] = ent

  target_id = active_id if (active_id and active_id in by_id) else summary_items[0]["id"]
  chosen_ent = by_id[target_id]
  active_obj = compare_file_with_head(
    repo_root / chosen_ent["rel_path"],
    rev=clean_rev,
    old_rel_path=chosen_ent.get("old_rel_path"),
    item_id=target_id,
    char_diff_mode=diff_mode,
    normalize=norm_form,
    ignore_punct=bool(ignore_punct),
  ).to_dict(include_hunks=True)
  items_out = [active_obj if s["id"] == target_id else s for s in summary_items]
  return {
    "spec": spec.to_dict(),
    "repo_root": str(repo_root),
    "items": items_out,
    "active_item": active_obj,
  }
