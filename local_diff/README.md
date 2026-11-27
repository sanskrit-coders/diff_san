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

- Live web app (pure-JS file/text diff): [sanskrit-coders.github.io/diff_san/local_diff/](https://sanskrit-coders.github.io/diff_san/local_diff/)

# Export a standalone static HTML website (pure JS client-side file/text diffing)
local-diff-san --export-static docs/local_diff/index.html

# Or invoke as a Python module
python -m local_diff_san old_file.md new_file.md
```

## Key Features

- **Single-pane word-flow diff** (default) with intra-word character highlights and **2-pane LHS/RHS mode** (`Alt+S`), powered by `pull_requests_san.diff` on the CLI or pure-JS `DiffSanEngine` in static browser mode.
- **Vedic grapheme (`\X`) & raw codepoint modes** keeping Vedic svara marks (`U+1CD3` `᳓`, etc.) attached to their akṣara.
- **Expandable collapsed context lines** (click any collapsed-lines bar to expand), **Sandhi diff** toggle (highlight interword sandhi changes like `अस्ति इति` ↔ `अस्तीति` in violet), unified **OLD/NEW dashed-pair boxing**, keyboard & mouse line navigation (`Shift+Click` or `↑`/`↓` select line without interfering with click-drag text selection, `PgUp`/`PgDn` scroll page & select first visible line, `Alt+↑`/`Alt+↓` jump prev/next diff line), Unicode normalization (`NFC`/`NFD`/`NFKC`/`NFKD`), `Ignore punct`, customizable **Settings color pickers** (persisted across `pull_requests_san` and `local-diff-san`), **GoldenDict** lookup, and click-to-jump **IntelliJ IDEA** line opening.

## IDE Integrations

Launch `local-diff-san` straight from your editor's file tree, editor tab, or VCS view:

- **JetBrains IDEs:** [`sanskrit-coders/jetbrains_san`](https://github.com/sanskrit-coders/jetbrains_san) ([JetBrains Marketplace](https://plugins.jetbrains.com/plugin/36520-github-san-tools))
- **VS Code / Open VSX:** [`sanskrit-coders/vscode_san`](https://github.com/sanskrit-coders/vscode_san) ([VS Code Marketplace](https://marketplace.visualstudio.com/items?itemName=sanskrit-coders.github-san), [Open VSX](https://open-vsx.org/extension/sanskrit-coders/github-san))
