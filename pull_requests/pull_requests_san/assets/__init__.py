"""Shared CSS, JavaScript, and template injection helpers for diff_san frontends."""

from __future__ import annotations

from pathlib import Path


_ASSETS_DIR = Path(__file__).resolve().parent


def load_shared_diff_css() -> str:
  """Return the shared CSS stylesheet for Sanskrit diff viewers."""
  return (_ASSETS_DIR / "diff_viewer.css").read_text(encoding="utf-8")


def load_shared_diff_js() -> str:
  """Return the shared JavaScript runtime (diff segments, settings color pickers, font & GoldenDict)."""
  return (_ASSETS_DIR / "diff_viewer.js").read_text(encoding="utf-8")


def load_shared_diff_engine_js() -> str:
  """Return the pure-JS Sanskrit/Vedic diff generation engine."""
  return (_ASSETS_DIR / "diff_engine.js").read_text(encoding="utf-8")


def load_pr_static_adapter_js() -> str:
  """Return the static browser GitHub API adapter for pull_requests_san."""
  return (_ASSETS_DIR / "pr_static_adapter.js").read_text(encoding="utf-8")


def render_html_with_shared_assets(template_html: str) -> str:
  """Inject shared CSS and JS assets into an HTML template string."""
  return (
    template_html
    .replace("/* {{SHARED_DIFF_CSS}} */", load_shared_diff_css())
    .replace("// {{SHARED_DIFF_ENGINE_JS}}", load_shared_diff_engine_js())
    .replace("// {{SHARED_DIFF_JS}}", load_shared_diff_js())
    .replace("// {{PR_STATIC_ADAPTER_JS}}", load_pr_static_adapter_js())
  )
