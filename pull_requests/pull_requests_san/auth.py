"""GitHub owner normalization and API token resolution helpers."""

from __future__ import annotations

import inspect
import os
from pathlib import Path
import re
import shutil
import subprocess

try:
  import tomllib
except ImportError:  # Python 3.10 compatibility
  tomllib = None  # type: ignore[assignment]


DEFAULT_OWNER = "vishvAsa"


def _default_tokens_toml_path() -> Path | None:
  try:
    from curation_utils import creds

    default_path = inspect.signature(creds.get_toml_value).parameters["path"].default
    return Path(default_path) if default_path else None
  except Exception:
    return None


def normalize_owner(owner: str) -> str:
  """Normalize an owner string or GitHub org URL (e.g. ``https://github.com/vishvAsa/``) to its login."""
  raw = (owner or "").strip()
  if not raw:
    return DEFAULT_OWNER
  m = re.match(r"^https?://(?:www\.)?github\.com/([^/?#]+)", raw, re.IGNORECASE)
  if m:
    return m.group(1).strip()
  return raw.strip("/").split("/")[0]


def _read_gh_hosts_token(hosts_path: str | Path | None = None) -> str | None:
  path = Path(hosts_path or os.path.expanduser("~/.config/gh/hosts.yml"))
  if not path.is_file():
    return None
  try:
    text = path.read_text(encoding="utf-8")
  except OSError:
    return None
  try:
    import yaml  # type: ignore

    data = yaml.safe_load(text) or {}
    if isinstance(data, dict):
      gh_entry = data.get("github.com")
      if isinstance(gh_entry, dict):
        tok = gh_entry.get("oauth_token")
        if isinstance(tok, str) and tok.strip():
          return tok.strip()
  except Exception:
    pass
  in_github = False
  for line in text.splitlines():
    if not line.strip() or line.lstrip().startswith("#"):
      continue
    if not line.startswith(" "):
      in_github = line.strip().startswith("github.com:")
      continue
    if in_github:
      m = re.match(r"^\s+oauth_token:\s*(.+?)\s*$", line)
      if m:
        val = m.group(1).strip().strip("'\"")
        if val:
          return val
  return None


def resolve_github_token(
  token: str | None = None,
  *,
  cred_path: str | Path | None = None,
  allow_gh_cli: bool = True,
) -> str | None:
  """Resolve a GitHub API token.

  When ``token`` is explicitly provided as ``""`` (empty string), returns ``None``
  to allow opting out of ambient credentials (e.g. in tests). When ``token is None``,
  checks env vars, gh CLI config, ``gh auth token``, and ``curation_utils.creds``.
  """
  if token is not None:
    return token.strip() or None
  for env_name in ("GITHUB_TOKEN", "GH_TOKEN"):
    val = os.environ.get(env_name)
    if val and val.strip():
      return val.strip()
  hosts_tok = _read_gh_hosts_token()
  if hosts_tok:
    return hosts_tok
  if allow_gh_cli and shutil.which("gh"):
    try:
      out = subprocess.check_output(
        ["gh", "auth", "token"],
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
      ).strip()
      if out:
        return out
    except (subprocess.SubprocessError, OSError):
      pass
  try:
    from curation_utils import creds

    toml_path = Path(cred_path) if cred_path else _default_tokens_toml_path()
    if toml_path and toml_path.is_file():
      for key in ("github.general_kv", "github.checkin"):
        val = creds.get_toml_value(key=key, path=toml_path)
        if isinstance(val, dict):
          val = val.get("key") or val.get("token")
        if isinstance(val, str) and val.strip():
          return val.strip()
  except Exception:
    pass
  if tomllib is not None:
    toml_path = Path(cred_path) if cred_path else Path(os.path.expanduser("~/.config/curation_utils/tokens.toml"))
    if toml_path.is_file():
      try:
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        gh = data.get("github")
        if isinstance(gh, dict):
          for k in ("general_kv", "checkin"):
            val = gh.get(k)
            if isinstance(val, dict):
              val = val.get("key") or val.get("token")
            if isinstance(val, str) and val.strip():
              return val.strip()
      except Exception:
        pass
  return None
