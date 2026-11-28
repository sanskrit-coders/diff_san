"""Local file and Git HEAD diff computation built on pull_requests_san.diff."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
from pathlib import Path
import subprocess
import tempfile
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


def _parse_git_status_z(raw: bytes) -> tuple[dict[str, str], dict[str, str]]:
  """Parse ``git status --porcelain -z`` into ``(rel_path -> xy, rel_path -> orig_rel_path)``."""
  rel_status: dict[str, str] = {}
  rename_sources: dict[str, str] = {}
  parts = raw.split(b"\x00")
  i = 0
  while i < len(parts):
    tok = parts[i]
    i += 1
    if not tok or len(tok) < 4:
      continue
    decoded = tok.decode("utf-8", errors="replace")
    xy = decoded[:2]
    rel_p = decoded[3:]
    rel_status[rel_p] = xy
    if (xy[0] in ("R", "C") or xy[1] in ("R", "C")) and i < len(parts):
      orig_p = parts[i].decode("utf-8", errors="replace")
      i += 1
      if orig_p:
        rename_sources[rel_p] = orig_p
  return rel_status, rename_sources


def get_git_status_map(repo_root: str | Path) -> tuple[dict[str, str], list[str], dict[str, str]]:
  """Return ``({abs_path: porcelain_xy}, staged_rel_paths, {rel_path: orig_rel_path})`` for ``repo_root``."""
  root = Path(repo_root).expanduser().resolve()
  st_proc = _run_git_bytes(root, ["status", "--porcelain=v1", "-z", "-uall"])
  if st_proc.returncode != 0:
    msg = (st_proc.stderr or st_proc.stdout).decode("utf-8", errors="replace").strip()
    raise ValueError(msg or f"git status failed in {root}")
  rel_status, rename_sources = _parse_git_status_z(st_proc.stdout)
  abs_status = {os.path.normpath(root / rel_p): xy for rel_p, xy in rel_status.items()}

  cached_proc = _run_git_bytes(root, ["diff", "--cached", "--name-only", "-z"])
  staged: list[str] = []
  if cached_proc.returncode == 0:
    staged = [
      p.decode("utf-8", errors="replace")
      for p in cached_proc.stdout.split(b"\x00")
      if p
    ]
  return abs_status, staged, rename_sources


def commit_files(
  file_paths: Sequence[str | Path],
  commit_message: str = "",
  *,
  push: bool = False,
) -> list[dict[str, Any]]:
  """Safely stage untracked paths if needed, run scoped ``git commit --only``, and optionally push per repo."""
  if not file_paths:
    raise ValueError("No files selected for committing")

  by_repo: dict[Path, list[str]] = {}
  for fp in file_paths:
    p = Path(fp).expanduser()
    abs_f = Path(os.path.normpath(p.resolve() if p.exists() else p.absolute()))
    rr = find_git_root(abs_f)
    if rr is None:
      raise ValueError(f"File is not inside a git repository: {fp}")
    by_repo.setdefault(rr, []).append(abs_f.relative_to(rr).as_posix())

  results: list[dict[str, Any]] = []
  for repo_root, rels in sorted(by_repo.items(), key=lambda kv: str(kv[0])):
    unique_rels = list(dict.fromkeys(rels))
    st_map, pre_staged, rename_sources = get_git_status_map(repo_root)
    dirty_rels: list[str] = []
    untracked_rels: list[str] = []
    pathspec_rels: list[str] = []
    seen_pathspec: set[str] = set()
    for rel in unique_rels:
      abs_f = os.path.normpath(repo_root / rel)
      xy = st_map.get(abs_f, "clean")
      if xy == "clean":
        continue
      dirty_rels.append(rel)
      if rel not in seen_pathspec:
        seen_pathspec.add(rel)
        pathspec_rels.append(rel)
      orig_rel = rename_sources.get(rel)
      if orig_rel and orig_rel not in seen_pathspec:
        seen_pathspec.add(orig_rel)
        pathspec_rels.append(orig_rel)
      if xy == "??":
        untracked_rels.append(rel)

    if not dirty_rels:
      results.append({
        "repo_root": str(repo_root),
        "committed": [],
        "sha": None,
        "pushed": False,
        "note": "No uncommitted changes among selected files.",
      })
      continue

    default_msg = f"Update {dirty_rels[0]}" if len(dirty_rels) == 1 else f"Update {len(dirty_rels)} files"
    msg = (commit_message or "").strip() or default_msg

    if untracked_rels:
      # Use `--intent-to-add` (`git add -N`) when unrelated pre-staged files exist so `git commit --only` keeps them untouched.
      flag = ["-N", "--"] if pre_staged else ["--"]
      for i in range(0, len(untracked_rels), 200):
        add_proc = _run_git_bytes(repo_root, ["add", *flag, *untracked_rels[i : i + 200]])
        if add_proc.returncode != 0:
          err_txt = (add_proc.stderr or add_proc.stdout).decode("utf-8", errors="replace").strip()
          raise ValueError(err_txt or f"git add failed in {repo_root}")

    tmp_fh = tempfile.NamedTemporaryFile("wb", delete=False)
    try:
      tmp_fh.write(b"\x00".join(p.encode("utf-8") for p in pathspec_rels) + b"\x00")
      tmp_fh.close()
      commit_proc = _run_git_bytes(
        repo_root,
        ["commit", "--only", f"--pathspec-from-file={tmp_fh.name}", "--pathspec-file-nul", "-m", msg],
      )
    finally:
      if os.path.exists(tmp_fh.name):
        os.remove(tmp_fh.name)

    commit_out = (commit_proc.stdout + b"\n" + commit_proc.stderr).decode("utf-8", errors="replace").strip()
    if commit_proc.returncode != 0:
      raise ValueError(commit_out or f"git commit failed in {repo_root}")

    sha_proc = _run_git_bytes(repo_root, ["rev-parse", "--short", "HEAD"])
    sha = sha_proc.stdout.decode("utf-8", errors="replace").strip() if sha_proc.returncode == 0 else None
    entry: dict[str, Any] = {
      "repo_root": str(repo_root),
      "committed": dirty_rels,
      "sha": sha,
      "output": commit_out,
      "preserved_pre_staged": len(pre_staged),
      "pushed": False,
    }
    if push:
      push_proc = _run_git_bytes(repo_root, ["push"])
      push_out = (push_proc.stdout + b"\n" + push_proc.stderr).decode("utf-8", errors="replace").strip()
      if push_proc.returncode == 0:
        entry["pushed"] = True
        entry["push_output"] = push_out
      else:
        entry["pushed"] = False
        entry["push_error"] = push_out or f"git push exited with code {push_proc.returncode}"
    results.append(entry)
  return results


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

  is_renamed = bool(old_rel_path and old_rel_path != rel_path)
  left_lbl = (f"{clean_rev}:{blob_rel}" if is_renamed else clean_rev) if head_bytes is not None else f"(absent in {clean_rev})"
  right_lbl = (f"WORKING:{rel_path}" if is_renamed else "WORKING") if work_bytes is not None else "(deleted on disk)"
  return _compute_item_from_bytes(
    item_id=item_id or f"head:{rel_path}",
    title=rel_path,
    mode="git_head",
    status=status,
    left_label=left_lbl,
    right_label=right_lbl,
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
  allow_missing_committed: bool = False,
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
      "commit_files": [],
      "repo_staged": {},
    }

  if spec.mode == "git_head":
    if not spec.paths:
      raise ValueError("At least one file path is required in Git vs revision mode")
    built_items: list[LocalDiffItem] = []
    repo_fallback: str | None = None
    for idx, p in enumerate(spec.paths):
      try:
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
      except FileNotFoundError:
        if not allow_missing_committed:
          raise
        rr = find_git_root(p)
        if rr is None:
          raise
        repo_fallback = repo_fallback or str(rr)
    changed_items = [it for it in built_items if it.status != "unchanged"]
    chosen = next((it for it in changed_items if it.id == active_id), changed_items[0] if changed_items else None)
    chosen_dict = chosen.to_dict(include_hunks=True) if chosen else None
    item_summaries = [
      chosen_dict if (chosen and it.id == chosen.id) else it.to_dict(include_hunks=False)
      for it in changed_items
    ]
    git_cache: dict[str, tuple[dict[str, str], list[str], dict[str, str]]] = {}
    commit_files: list[dict[str, Any]] = []
    repo_staged: dict[str, list[str]] = {}
    seen_commit_paths: set[str] = set()
    for it in built_items:
      if not it.repo_root or not it.rel_path:
        continue
      if it.repo_root not in git_cache:
        git_cache[it.repo_root] = get_git_status_map(it.repo_root)
        repo_staged[it.repo_root] = git_cache[it.repo_root][1]
      st_map, _, rename_sources = git_cache[it.repo_root]
      abs_f = os.path.normpath(Path(it.repo_root) / it.rel_path)
      if abs_f in seen_commit_paths:
        continue
      seen_commit_paths.add(abs_f)
      xy = st_map.get(abs_f, "clean")
      if xy == "clean":
        continue
      commit_files.append({
        "item_id": it.id if it.status != "unchanged" else None,
        "file_path": abs_f,
        "rel_path": it.rel_path,
        "old_rel_path": rename_sources.get(it.rel_path),
        "repo_root": it.repo_root,
        "git_xy": xy,
        "dirty": True,
        "status": it.status,
        "additions": it.additions,
        "deletions": it.deletions,
        "exists_on_disk": Path(abs_f).is_file(),
      })
    commit_files.sort(key=lambda c: c["rel_path"])
    root_out = chosen.repo_root if chosen else (built_items[0].repo_root if built_items else repo_fallback)
    return {
      "spec": spec.to_dict(),
      "repo_root": root_out,
      "items": item_summaries,
      "active_item": chosen_dict,
      "commit_files": commit_files,
      "repo_staged": repo_staged,
    }

  # spec.mode == "git_dir"
  dir_path = Path(spec.paths[0] if spec.paths else Path.cwd()).expanduser().resolve()
  if not dir_path.is_dir():
    raise FileNotFoundError(f"Directory not found: {dir_path}")
  repo_root, entries = discover_git_dir_entries(dir_path, rev=clean_rev)
  if repo_root is None:
    return {
      "spec": spec.to_dict(),
      "repo_root": None,
      "items": [],
      "active_item": None,
      "commit_files": [],
      "repo_staged": {},
    }

  st_map, pre_staged, rename_sources = get_git_status_map(repo_root)
  repo_staged = {str(repo_root): pre_staged}
  commit_files_by_rel: dict[str, dict[str, Any]] = {}

  summary_items: list[dict[str, Any]] = []
  by_id: dict[str, dict[str, Any]] = {}
  for ent in entries:
    rel_p = ent["rel_path"]
    old_rp = rename_sources.get(rel_p) or ent.get("old_rel_path")
    iid = f"head:{rel_p}"
    disk_p = repo_root / rel_p
    abs_f = os.path.normpath(disk_p)
    xy = st_map.get(abs_f, "clean")
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
    commit_files_by_rel[rel_p] = {
      "item_id": iid,
      "file_path": abs_f,
      "rel_path": rel_p,
      "old_rel_path": old_rp,
      "repo_root": str(repo_root),
      "git_xy": xy,
      "dirty": xy != "clean",
      "status": ent["status"],
      "additions": ent["additions"],
      "deletions": ent["deletions"],
      "exists_on_disk": disk_p.is_file(),
    }

  rel_scope = dir_path.relative_to(repo_root).as_posix()
  scope_prefix = "" if rel_scope in ("", ".") else f"{rel_scope}/"
  for abs_f, xy in sorted(st_map.items()):
    rel_p = Path(abs_f).relative_to(repo_root).as_posix()
    if scope_prefix and rel_p != rel_scope and not rel_p.startswith(scope_prefix):
      continue
    if rel_p in commit_files_by_rel:
      continue
    commit_files_by_rel[rel_p] = {
      "item_id": None,
      "file_path": abs_f,
      "rel_path": rel_p,
      "old_rel_path": rename_sources.get(rel_p),
      "repo_root": str(repo_root),
      "git_xy": xy,
      "dirty": xy != "clean",
      "status": "untracked" if xy == "??" else ("deleted" if "D" in xy else "modified"),
      "additions": 0,
      "deletions": 0,
      "exists_on_disk": Path(abs_f).is_file(),
    }

  commit_files = sorted(commit_files_by_rel.values(), key=lambda c: (not c["dirty"], c["rel_path"]))

  if not summary_items:
    return {
      "spec": spec.to_dict(),
      "repo_root": str(repo_root),
      "items": [],
      "active_item": None,
      "commit_files": commit_files,
      "repo_staged": repo_staged,
    }

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
    "commit_files": commit_files,
    "repo_staged": repo_staged,
  }
