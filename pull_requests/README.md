# pull_requests_san

Single-pane word-flow and character/grapheme-level GitHub Pull Request reviewer tailored for Sanskrit and Indic repositories (`vishvAsa` by default, or any `--owner` / `--repo`).

## Install

```bash
pip install pull_requests_san
```

## Usage

```bash
# Review all open PRs in vishvAsa repos (or specify any GitHub user/org)
pull_requests_san --owner vishvAsa --state open

# Review a specific repo or use module invocation
python -m pull_requests_san --owner vishvAsa --repo mahAbhAratam
```

GitHub authentication is auto-resolved in order from `--token`, `GITHUB_TOKEN`/`GH_TOKEN`, `~/.config/gh/hosts.yml`, `gh auth token`, or optional `tokens.toml` (`curation_utils.creds` / TOML fallback).

## Key Features

- **Single-pane word-flow diff** (default) matching changed space-separated strings inline without requiring strict line-by-line alignment (`Alt+P` toggles 2-pane LHS/RHS).
- **Vedic grapheme & codepoint modes** keeping trailing Vedic svaras (including `U+1CD3` `᳓`) attached to their akṣara.
- **Unicode normalization (`NFC`/`NFD`/`NFKC`) & `Ignore punct`** (`।`, `॥`, quotes, commas) adapted from `bhashika-org-in/wordiff`.
- **Full GitHub review & PR management**: inline line/range comments, thread replies, draft review batching, approve / request changes, resolve threads, mergeability & 3-way conflict inspection, open PR in main browser (`Alt+B`), merge PR (`Alt+Shift+M`), close/reopen PR (`Alt+Shift+C`).
