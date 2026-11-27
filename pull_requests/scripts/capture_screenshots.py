"""Generate repeatable showcase screenshots for the pull_requests_san READMEs.

Requires ``playwright`` (and Chromium) in the Python environment running the script:

  ~/gitland/python_venv/bin/python pull_requests/scripts/capture_screenshots.py
"""

from __future__ import annotations

from pathlib import Path
import socket
import threading
import time

try:
  from playwright.sync_api import sync_playwright
except ImportError as exc:  # pragma: no cover - optional docs helper
  raise SystemExit(
    "playwright is required to regenerate screenshots (`pip install playwright && playwright install chromium`)."
  ) from exc

import uvicorn

from pull_requests_san import (
  PRReviewSession,
  PullRequestCommit,
  PullRequestDetail,
  PullRequestFileDiff,
  PullRequestSummary,
  ReviewComment,
  ReviewThread,
  extract_three_way_conflicts,
  parse_patch_hunks,
  pr_review_gui,
)


OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "screenshots"


class ShowcaseClient:
  """Deterministic mock GitHub client for Sanskrit/Vedic PR screenshot capture."""

  def __init__(self) -> None:
    self.token = "tok_demo"
    self.local_org_dir = "/home/vishvAsa"
    self.pr1 = PullRequestSummary(
      owner="vishvAsa",
      repo="bhAgavatam",
      repo_full_name="vishvAsa/bhAgavatam",
      number=192,
      title="वीरराघव-टीका-परिष्कारः (svara & footnote cleanup)",
      url="https://github.com/vishvAsa/bhAgavatam/pull/192",
      state="open",
      is_draft=False,
      author="mrunaldatar",
      created_at="2026-11-24T06:10:19Z",
      updated_at="2026-11-27T08:15:00Z",
      base_ref="master",
      base_sha="base192",
      head_ref="parishkara",
      head_sha="head192",
      head_repo_full_name="mrunaldatar/bhAgavatam",
      additions=8,
      deletions=9,
      changed_files=2,
      review_decision="REVIEW_REQUIRED",
      merged=False,
      mergeable=True,
      mergeable_state="clean",
      comment_count=1,
      thread_count=2,
    )
    self.pr2 = PullRequestSummary(
      owner="vishvAsa",
      repo="vedAH_sAma",
      repo_full_name="vishvAsa/vedAH_sAma",
      number=54,
      title="आरण्यक-स्वराः (U+1CD3 udātta alignment)",
      url="https://github.com/vishvAsa/vedAH_sAma/pull/54",
      state="open",
      is_draft=False,
      author="sarvesh_b",
      created_at="2026-11-25T11:20:00Z",
      updated_at="2026-11-27T07:50:00Z",
      base_ref="master",
      base_sha="base54",
      head_ref="svara-fix",
      head_sha="head54",
      head_repo_full_name="sarvesh_b/vedAH_sAma",
      additions=4,
      deletions=4,
      changed_files=1,
      review_decision="CHANGES_REQUESTED",
      merged=False,
      mergeable=False,
      mergeable_state="dirty",
      comment_count=0,
      thread_count=1,
    )
    self.pr3 = PullRequestSummary(
      owner="vishvAsa",
      repo="mahAbhAratam",
      repo_full_name="vishvAsa/mahAbhAratam",
      number=418,
      title="उद्योगपर्व सन्धि-शोधनम्",
      url="https://github.com/vishvAsa/mahAbhAratam/pull/418",
      state="open",
      is_draft=True,
      author="ananya_k",
      created_at="2026-11-26T04:00:00Z",
      updated_at="2026-11-26T18:30:00Z",
      base_ref="master",
      base_sha="base418",
      head_ref="sandhi",
      head_sha="head418",
      head_repo_full_name="ananya_k/mahAbhAratam",
      additions=3,
      deletions=2,
      changed_files=1,
      merged=False,
      mergeable=True,
      mergeable_state="clean",
      comment_count=0,
      thread_count=0,
    )

  def get_authenticated_user(self) -> dict[str, str]:
    return {"login": "vvasuki", "name": "Vishvas Vasuki"}

  def list_org_pull_requests(self, owner: str, state: str = "open", repo: str | None = None, **kwargs):
    prs = [self.pr1, self.pr2, self.pr3]
    if repo:
      prs = [p for p in prs if p.repo == repo]
    return prs

  def get_pull_request_detail(
    self,
    owner: str,
    repo: str,
    number: int,
    *,
    char_diff_mode: str = "grapheme",
    normalize: str = "none",
    ignore_punct: bool = False,
  ) -> PullRequestDetail:
    if number == 192:
      patch1 = (
        "@@ -335,10 +339,8 @@\n"
        " </details>\n"
        " \n"
        "-<details><summary>वीरराघवः [यान्त्रिकम्]</summary>\n"
        "-\n"
        "-**आविध्यति** । स सुरः शूलम् [^v5]आविध्याऽऽलोल्य[^v5] तरसा बलेन गरुडाय\n"
        "+<details><summary>वीरराघवः</summary>\n"
        " \n"
        "-[^v5]: K उद्धृत्य\n"
        "+**आविध्यति** । स सुरः शूलम् आविध्याऽऽलोल्य तरसा बलेन गरुडाय\n"
        " \n"
        "-[^v6]: K,T,W omit\n"
        "+[^v5]: K,T,W omit\n"
        " \n"
        " </details>"
      )
      patch2 = (
        "@@ -12,3 +12,3 @@\n"
        " ## मूलम्\n"
        "-प्रणयो ऽस्येति धा० भ्वादिः ।\n"
        "+प्रणयोऽस्येति धा॰ भ्वादिः ।\n"
        " समाप्तम् ॥"
      )
      return PullRequestDetail(
        summary=self.pr1,
        body="Cleaned spurious automated tags, realigned commentary across line wraps, and renumbered footnotes.",
        commits_count=2,
        merged=False,
        mergeable=True,
        mergeable_state="clean",
        rebaseable=True,
        behind_by=0,
        ahead_by=2,
        conflicts=[],
        char_diff_mode=char_diff_mode,
        normalize=normalize,
        ignore_punct=ignore_punct,
        commits=[
          PullRequestCommit(
            sha="a111111111111111111111111111111111111111",
            short_sha="a111111",
            title="वीरराघव-टीका-परिष्कारः (tag & footnote cleanup)",
            message="वीरराघव-टीका-परिष्कारः (tag & footnote cleanup)",
            author="mrunaldatar",
            committed_at="2026-11-26T18:40:00Z",
            parents=["base192"],
          ),
          PullRequestCommit(
            sha="b222222222222222222222222222222222222222",
            short_sha="b222222",
            title="अवग्रह-धातु-शोधनम्",
            message="अवग्रह-धातु-शोधनम्",
            author="mrunaldatar",
            committed_at="2026-11-27T08:15:00Z",
            parents=["a111111111111111111111111111111111111111"],
          ),
        ],
        files=[
          PullRequestFileDiff(
            filename="10/08_05_vIrarAghava.md",
            status="modified",
            additions=4,
            deletions=5,
            changes=9,
            patch=patch1,
            local_file_path="/home/vishvAsa/bhAgavatam/10/08_05_vIrarAghava.md",
            hunks=parse_patch_hunks(
              patch1,
              char_diff_mode=char_diff_mode,
              normalize=normalize,
              ignore_punct=ignore_punct,
            ),
          ),
          PullRequestFileDiff(
            filename="10/09_avagraha.md",
            status="modified",
            additions=1,
            deletions=1,
            changes=2,
            patch=patch2,
            hunks=parse_patch_hunks(
              patch2,
              char_diff_mode=char_diff_mode,
              normalize=normalize,
              ignore_punct=ignore_punct,
            ),
          ),
        ],
        issue_comments=[
          ReviewComment(
            id=201,
            author="mrunaldatar",
            body="साधु पठितम् — पादटिप्पणी-क्रमः शोधितः।",
            created_at="2026-11-27T06:00:00Z",
          )
        ],
        reviews=[],
        review_threads=[
          ReviewThread(
            id="PRRT_192_resolved",
            path="10/08_05_vIrarAghava.md",
            line=337,
            original_line=337,
            side="LEFT",
            is_resolved=True,
            is_outdated=True,
            comments=[
              ReviewComment(
                id=1000,
                author="vvasuki",
                body="[यान्त्रिकम्] इत्यंशोऽपसारणीयः — साधु अपसारितः।",
                created_at="2026-11-27T05:15:00Z",
              )
            ],
          ),
          ReviewThread(
            id="PRRT_192_open",
            path="10/08_05_vIrarAghava.md",
            line=344,
            original_line=344,
            side="RIGHT",
            is_resolved=False,
            is_outdated=False,
            comments=[
              ReviewComment(
                id=1001,
                author="vvasuki",
                body="अत्र [^v5] टिप्पणी समुचिता, पाठभेदोऽपि सुस्पष्टः।",
                created_at="2026-11-27T07:30:00Z",
              )
            ],
          ),
        ],
      )

    patch_vedic = (
      "@@ -21,4 +21,4 @@\n"
      " **मन्त्रः**\n"
      "-इमा᳓म् अ᳓रण्या ते᳓जसा प्रा᳓णो धा०\n"
      "+इमा᳓म् अर᳓ण्या ते᳓जसा प्राणो᳓ धा॰\n"
      " इति तृतीयः ॥"
    )
    anc = "line1\nप्रणयो ऽस्येति मन्त्राः\nline3\nline4\nline5\n"
    base_tip = "line1\nअसुहृद्भिर् प्रणयो ऽस्येति मन्त्राः\nline3\nline4\nline5\n"
    head_txt = "line1\nदुर्हृद्भिर् प्रणयोऽस्येति मन्त्राः\nline3\nline4\nline5\n"
    conf_hunks = extract_three_way_conflicts(
      base_tip,
      anc,
      head_txt,
      char_diff_mode=char_diff_mode,
    )
    conflicts = [
      {
        "filename": "01_Aranyakam/03_sUkta.md",
        "reason": "content_conflict",
        "hunks": conf_hunks,
      }
    ]
    return PullRequestDetail(
      summary=self.pr2,
      body="Adjusted udātta accent marks on अर᳓ण्या and प्राणो᳓. Rebase needed after upstream master edits.",
      commits_count=3,
      merged=False,
      mergeable=False,
      mergeable_state="dirty",
      rebaseable=False,
      behind_by=4,
      ahead_by=2,
      conflicts=conflicts,
      char_diff_mode=char_diff_mode,
      normalize=normalize,
      ignore_punct=ignore_punct,
      files=[
        PullRequestFileDiff(
          filename="01_Aranyakam/03_sUkta.md",
          status="modified",
          additions=2,
          deletions=2,
          changes=4,
          patch=patch_vedic,
          hunks=parse_patch_hunks(
            patch_vedic,
            char_diff_mode=char_diff_mode,
            normalize=normalize,
            ignore_punct=ignore_punct,
          ),
        )
      ],
      issue_comments=[],
      reviews=[],
      review_threads=[
        ReviewThread(
          id="PRRT_54",
          path="01_Aranyakam/03_sUkta.md",
          line=22,
          original_line=22,
          side="RIGHT",
          is_resolved=False,
          is_outdated=False,
          comments=[
            ReviewComment(
              id=2001,
              author="vvasuki",
              body="अत्र उदात्त-स्वरस्थानं (U+1CD3) समीचीनम्।",
              created_at="2026-11-27T08:05:00Z",
            )
          ],
        )
      ],
    )


def _free_port() -> int:
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.bind(("127.0.0.1", 0))
    return int(s.getsockname()[1])


def main() -> None:
  OUT_DIR.mkdir(parents=True, exist_ok=True)
  client = ShowcaseClient()
  session = PRReviewSession(client=client, default_owner="vishvAsa")
  app = pr_review_gui.create_app(session=session)
  port = _free_port()
  config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
  server = uvicorn.Server(config)
  thread = threading.Thread(target=server.run, daemon=True)
  thread.start()
  time.sleep(0.5)

  base_url = f"http://127.0.0.1:{port}"
  try:
    with sync_playwright() as p:
      browser = p.chromium.launch()
      page = browser.new_page(viewport={"width": 1440, "height": 960}, device_scale_factor=2)
      page.set_default_timeout(8000)
      page.goto(base_url, wait_until="networkidle")
      page.wait_for_selector(".diff-table-single")
      page.evaluate("document.fonts.ready")
      page.wait_for_timeout(300)

      # Screenshot 1: PR overview header, mergeability, conversation, review box
      page.screenshot(path=str(OUT_DIR / "pr_review_overview.png"))

      # Screenshot 2: Single-pane word-flow Devanagari diff + collapsed resolved & open inline threads
      page.evaluate("""() => {
        const fc = document.getElementById('fileCard_0');
        if (fc) fc.scrollIntoView({ behavior: 'instant', block: 'start' });
      }""")
      page.wait_for_timeout(150)
      page.screenshot(path=str(OUT_DIR / "single_pane_word.png"))

      # Screenshot 3: 2-pane Vedic grapheme mode + 3-way conflict banner on PR #54
      page.select_option("#diffModeSelect", "grapheme")
      page.uncheck("#singlePaneChk")
      page.click(".pr-item:nth-child(2)")
      page.wait_for_selector("#mergeabilityBox.dirty")
      page.wait_for_selector(".conflict-item")
      page.wait_for_selector(".lhs-cell")
      page.evaluate("document.fonts.ready")
      page.evaluate("""() => {
        const mb = document.getElementById('mergeabilityBox');
        if (mb) mb.scrollIntoView({ behavior: 'instant', block: 'start' });
      }""")
      page.wait_for_timeout(300)
      page.screenshot(path=str(OUT_DIR / "two_pane_grapheme_conflict.png"))
      browser.close()
  finally:
    server.should_exit = True
    thread.join(timeout=2)


if __name__ == "__main__":
  main()
