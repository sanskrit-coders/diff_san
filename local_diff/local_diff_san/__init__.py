"""Sanskrit/Indic local file and Git HEAD single-pane & character-level diff viewer."""

from local_diff_san.core import (
  DEFAULT_REV,
  LocalDiffItem,
  LocalDiffSpec,
  build_diff_workspace,
  compare_file_with_head,
  compare_two_files,
  discover_git_dir_entries,
  find_git_root,
  read_git_blob,
  resolve_spec_from_paths,
)

__all__ = [
  "DEFAULT_REV",
  "LocalDiffItem",
  "LocalDiffSpec",
  "build_diff_workspace",
  "compare_file_with_head",
  "compare_two_files",
  "discover_git_dir_entries",
  "find_git_root",
  "read_git_blob",
  "resolve_spec_from_paths",
]
