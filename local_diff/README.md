# local-diff-san

Single-pane word-flow, intra-word character/Vedic-grapheme, and 2-pane local diff viewer built on [`pull_requests_san`](../pull_requests/README.md) for Sanskrit and Indic scholarship.

## Install

```bash
pip install local-diff-san
```

## Usage

```bash
# Compare a local file against Git HEAD (or another revision via --rev)
local-diff-san path/to/file.md
local-diff-san path/to/file.md --rev HEAD~1

# Compare two given local files side-by-side or in single-pane word flow
local-diff-san old_file.md new_file.md

# Browse all changed files in the current git repository (or a directory) vs HEAD
local-diff-san
local-diff-san path/to/dir

# Export a standalone static HTML website (pure JS client-side file/text diffing)
local-diff-san --export-static docs/index.html

# Or invoke as a Python module
python -m local_diff_san old_file.md new_file.md
```

## Key Features

- **Single-pane word-flow diff** (default) with intra-word character highlights and **2-pane LHS/RHS mode** (`Alt+S`), powered by `pull_requests_san.diff` on the CLI or pure-JS `DiffSanEngine` in static browser mode.
- **Vedic grapheme (`\X`) & raw codepoint modes** keeping Vedic svara marks (`U+1CD3` `᳓`, etc.) attached to their akṣara.
- **Expandable collapsed context lines** (click any collapsed-lines bar to expand), **Sandhi diff** toggle (highlight interword sandhi changes like `अस्ति इति` ↔ `अस्तीति` in violet), keyboard line navigation (`↑`/`↓` select line, `Alt+↑`/`Alt+↓` jump prev/next diff line), Unicode normalization (`NFC`/`NFD`/`NFKC`/`NFKD`), `Ignore punct`, customizable **Settings color pickers** (persisted across `pull_requests_san` and `local-diff-san`), **GoldenDict** lookup, and click-to-jump **IntelliJ IDEA** line opening.
