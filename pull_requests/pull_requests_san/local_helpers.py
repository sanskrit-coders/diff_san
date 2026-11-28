"""Shared local desktop integration and server helpers for Sanskrit diff tools."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Callable, Sequence
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


def resolve_vscode_binary() -> str | None:
  """Locate an executable VS Code CLI launcher on the local machine."""
  candidates: list[Path] = []
  for name in ("code", "code-insiders", "codium"):
    w = shutil.which(name)
    if w:
      candidates.append(Path(w))
  for p_str in ("/usr/bin/code", "/usr/local/bin/code", "/snap/bin/code"):
    candidates.append(Path(p_str))
  candidates.append(Path.home() / ".local/bin/code")
  for cand in candidates:
    try:
      resolved = cand.resolve(strict=True)
    except OSError:
      continue
    if resolved.is_file() and os.access(resolved, os.X_OK):
      return str(cand if os.access(cand, os.X_OK) else resolved)
  return None


def open_path_in_vscode(
  target_path: str | Path,
  *,
  line: int | None = None,
  code_resolver: Callable[[], str | None] | None = None,
  popen_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
  """Open an existing file on disk in VS Code at an optional 1-based line number."""
  target = Path(target_path).expanduser().resolve()
  if not target.is_file():
    raise FileNotFoundError(f"Local file not found: {target}")
  code_bin = (code_resolver or resolve_vscode_binary)()
  if not code_bin:
    raise RuntimeError("VS Code CLI binary ('code') not found")
  cmd = [code_bin]
  if line is not None and line >= 1:
    cmd.extend(["--goto", f"{target}:{line}"])
  else:
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


_PYTHON_EXE_RE = re.compile(r"^(?:python|pypy)(?:\d+(?:\.\d+)*)?$", re.I)
_PYTHON_TWO_ARG_OPTS = frozenset({"-W", "-X", "--check-hash-based-pycs"})


def is_matching_server_cmdline(
  argv: Sequence[str],
  *,
  entry_names: Sequence[str],
  module_names: Sequence[str],
  script_suffixes: Sequence[str],
) -> bool:
  """Return True if ``argv`` launches the matching web server (ignoring ``--export-static``, ``--help``, or ``-c``)."""
  args = [str(a) for a in argv if str(a)]
  if not args:
    return False
  for tok in args[1:]:
    if tok in ("-h", "--help", "--export-static") or tok.startswith("--export-static="):
      return False

  exe_name = Path(args[0]).name
  if _PYTHON_EXE_RE.fullmatch(exe_name):
    i = 1
    while i < len(args):
      tok = args[i]
      if tok == "-m":
        return i + 1 < len(args) and args[i + 1] in module_names
      if tok.startswith("-m") and len(tok) > 2:
        return tok[2:] in module_names
      if tok == "-c" or tok.startswith("-c"):
        return False
      if tok in _PYTHON_TWO_ARG_OPTS:
        i += 2
        continue
      if tok.startswith("-"):
        i += 1
        continue
      script_norm = tok.replace("\\", "/")
      script_base = Path(tok).name
      return script_base in entry_names or any(
        script_norm == suf or script_norm.endswith("/" + suf) for suf in script_suffixes
      )
    return False

  return exe_name in entry_names


def _ancestor_pids(pid: int | None = None) -> set[int]:
  cur = int(pid or os.getpid())
  seen = {cur}
  proc_root = Path("/proc")
  if not proc_root.is_dir():
    try:
      ppid = os.getppid()
      if ppid > 1:
        seen.add(ppid)
    except Exception:
      pass
    return seen
  while cur > 1:
    try:
      stat_txt = (proc_root / str(cur) / "stat").read_text(encoding="utf-8", errors="replace")
      rparen = stat_txt.rfind(")")
      if rparen < 0:
        break
      rest = stat_txt[rparen + 2 :].split()
      ppid = int(rest[1]) if len(rest) > 1 else 0
    except Exception:
      break
    if ppid <= 1 or ppid in seen:
      break
    seen.add(ppid)
    cur = ppid
  return seen


def iter_process_cmdlines() -> list[tuple[int, list[str]]]:
  """Enumerate ``(pid, argv)`` pairs for current-user processes via ``/proc`` (or ``ps`` on non-/proc POSIX)."""
  uid = os.getuid() if hasattr(os, "getuid") else None
  proc_root = Path("/proc")
  if proc_root.is_dir():
    entries: list[tuple[int, list[str]]] = []
    for child in proc_root.iterdir():
      if not child.name.isdigit():
        continue
      pid = int(child.name)
      try:
        if uid is not None and child.stat().st_uid != uid:
          continue
        raw = (child / "cmdline").read_bytes()
      except OSError:
        continue
      if not raw:
        continue
      argv = [p.decode("utf-8", errors="replace") for p in raw.split(b"\x00") if p]
      if argv:
        entries.append((pid, argv))
    return entries

  if sys.platform == "win32":
    return []
  cmd = ["ps"]
  if uid is not None:
    cmd.extend(["-u", str(uid)])
  cmd.extend(["-o", "pid=,args="])
  proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
  if proc.returncode != 0:
    return []
  out: list[tuple[int, list[str]]] = []
  for ln in proc.stdout.splitlines():
    stripped = ln.strip()
    if not stripped:
      continue
    parts = stripped.split(None, 1)
    if len(parts) != 2 or not parts[0].isdigit():
      continue
    try:
      argv = shlex.split(parts[1])
    except ValueError:
      argv = parts[1].split()
    out.append((int(parts[0]), argv))
  return out


def _is_pid_running(pid: int) -> bool:
  stat_path = Path(f"/proc/{pid}/stat")
  if stat_path.parent.parent.is_dir():
    try:
      stat_txt = stat_path.read_text(encoding="utf-8", errors="replace")
      rparen = stat_txt.rfind(")")
      if rparen >= 0:
        state_tok = stat_txt[rparen + 2 :].lstrip().split(None, 1)[0]
        if state_tok == "Z":
          return False
    except OSError:
      return False
  try:
    os.kill(pid, 0)
    return True
  except (ProcessLookupError, PermissionError, OSError):
    return False


def kill_other_server_instances(
  *,
  entry_names: Sequence[str],
  module_names: Sequence[str],
  script_suffixes: Sequence[str],
  label: str = "",
  timeout_seconds: float = 2.5,
  iter_cmdlines_fn: Callable[[], list[tuple[int, list[str]]]] | None = None,
  kill_fn: Callable[[int, int], None] | None = None,
  is_running_fn: Callable[[int], bool] | None = None,
) -> list[int]:
  """Terminate other running server instances matching ``entry_names`` / ``module_names`` / ``script_suffixes``."""
  if sys.platform == "win32" and iter_cmdlines_fn is None:
    return []
  excluded = _ancestor_pids()
  lister = iter_cmdlines_fn or iter_process_cmdlines
  targets = [
    pid
    for pid, argv in lister()
    if pid not in excluded
    and is_matching_server_cmdline(
      argv,
      entry_names=entry_names,
      module_names=module_names,
      script_suffixes=script_suffixes,
    )
  ]
  if not targets:
    return []

  killer = kill_fn or os.kill
  checker = is_running_fn or _is_pid_running
  signaled: list[int] = []
  for pid in targets:
    try:
      killer(pid, signal.SIGTERM)
      signaled.append(pid)
    except ProcessLookupError:
      pass
    except OSError:
      pass

  if not signaled:
    return []

  deadline = time.monotonic() + max(0.0, timeout_seconds)
  alive = list(signaled)
  while alive and time.monotonic() < deadline:
    time.sleep(0.05)
    alive = [pid for pid in alive if checker(pid)]

  if alive and hasattr(signal, "SIGKILL"):
    for pid in alive:
      try:
        killer(pid, signal.SIGKILL)
      except ProcessLookupError:
        pass
      except OSError:
        pass
    kill_deadline = time.monotonic() + 0.5
    while alive and time.monotonic() < kill_deadline:
      time.sleep(0.05)
      alive = [pid for pid in alive if checker(pid)]

  killed = sorted(set(signaled))
  if killed and label:
    print(f"Stopped {len(killed)} previous {label} server(s): {', '.join(str(p) for p in killed)}", flush=True)
  return killed
