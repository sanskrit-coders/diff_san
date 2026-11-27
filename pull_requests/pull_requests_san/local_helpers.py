"""Shared local desktop integration and server helpers for Sanskrit diff tools."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
from typing import Any, Callable
from urllib.parse import urlparse
import webbrowser

from pydantic import BaseModel


_ACCENT_STRIP_RE = re.compile(r"[\u0951-\u0954\u1cd0-\u1cff\u0300-\u036f]")


class OpenGoldenDictRequest(BaseModel):
  word: str


class OpenPathIdeaRequest(BaseModel):
  path: str
  line: int | None = None


def resolve_idea_binary() -> str | None:
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


def open_path_in_intellij(
  target_path: str | Path,
  *,
  line: int | None = None,
  idea_resolver: Callable[[], str | None] | None = None,
  popen_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
  """Open an existing file on disk in IntelliJ IDEA at an optional 1-based line number."""
  target = Path(target_path).expanduser().resolve()
  if not target.is_file():
    raise FileNotFoundError(f"Local file not found: {target}")
  idea_bin = (idea_resolver or resolve_idea_binary)()
  if not idea_bin:
    raise RuntimeError("IntelliJ IDEA CLI binary ('idea') not found")
  cmd = [idea_bin]
  if line is not None and line >= 1:
    cmd.extend(["--line", str(line)])
  cmd.append(str(target))
  (popen_fn or subprocess.Popen)(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  return {"ok": True, "file_path": str(target), "line": line, "command": cmd}


def open_local_file_in_intellij(
  local_org_dir: str | Path,
  repo: str,
  rel_path: str,
  *,
  line: int | None = None,
  idea_resolver: Callable[[], str | None] | None = None,
  popen_fn: Callable[..., Any] | None = None,
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
  return open_path_in_intellij(
    target,
    line=line,
    idea_resolver=idea_resolver,
    popen_fn=popen_fn,
  )


def resolve_goldendict_binary() -> str | None:
  """Locate an executable GoldenDict binary on PATH."""
  for name in ("goldendict", "goldendict-ng"):
    path = shutil.which(name)
    if path:
      return path
  return None


def open_in_goldendict(
  word: str,
  *,
  gd_resolver: Callable[[], str | None] | None = None,
  popen_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
  """Look up unaccented ``word`` in GoldenDict."""
  query = _ACCENT_STRIP_RE.sub("", (word or "")).strip()
  if not query:
    raise ValueError("Empty lookup word")
  gd_bin = (gd_resolver or resolve_goldendict_binary)()
  if not gd_bin:
    raise RuntimeError("GoldenDict CLI binary ('goldendict') not found")
  cmd = [gd_bin, query]
  (popen_fn or subprocess.Popen)(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  return {"ok": True, "word": query, "command": cmd}


def open_url_in_browser(
  url: str,
  *,
  open_tab_fn: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
  """Open a validated https://github.com/ URL in the user's main web browser."""
  cleaned = (url or "").strip()
  parsed = urlparse(cleaned)
  if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or parsed.username or parsed.password:
    raise ValueError(f"Only https://github.com/ URLs are allowed, got {url!r}")
  opened = (open_tab_fn or webbrowser.open_new_tab)(cleaned)
  return {"ok": bool(opened), "url": cleaned}


def find_free_port(preferred: int = 8770) -> int:
  """Return ``preferred`` if bindable on localhost, otherwise the next available port."""
  for port in [preferred, *range(preferred + 1, preferred + 30), 0]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
      try:
        s.bind(("127.0.0.1", port))
        return s.getsockname()[1]
      except OSError:
        continue
  return preferred


def launch_browser(url: str) -> None:
  """Open ``url`` in a Chromium app window if available, else the default browser."""
  time.sleep(0.6)
  chrome = shutil.which("google-chrome") or shutil.which("chromium")
  if chrome and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    try:
      subprocess.Popen([chrome, f"--app={url}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
      return
    except Exception:
      pass
  webbrowser.open(url)
