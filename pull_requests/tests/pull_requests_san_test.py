from __future__ import annotations

import base64
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

import pytest

from pull_requests_san.diff import compute_stream_diff
from pull_requests_san import (
  GitHubAPIError,
  GitHubClient,
  PRReviewSession,
  apply_normalization,
  build_full_file_hunk,
  compute_char_diff,
  extract_three_way_conflicts,
  normalize_owner,
  parse_full_diff_by_file,
  parse_patch_hunks,
  resolve_github_token,
  strip_punctuation,
)


class _FakeResponse:
  def __init__(self, status_code: int = 200, *, json_data: Any = None, text: str | None = None, headers: dict[str, str] | None = None) -> None:
    self.status_code = status_code
    self._json_data = json_data
    if text is not None:
      self.text = text
    elif json_data is not None:
      self.text = json.dumps(json_data)
    else:
      self.text = ""
    self.headers = headers or {}

  def json(self) -> Any:
    if self._json_data is not None:
      return self._json_data
    return json.loads(self.text)


class _FakeSession:
  def __init__(self) -> None:
    self.calls: list[dict[str, Any]] = []
    self.pr_title = "changes- ५४७,५४८"
    self.pr_state = "open"
    self.pr_merged = False
    self.pr_mergeable: bool | None = True
    self.pr_mergeable_state = "clean"
    self.head_sha = "headsha111"
    self.base_sha = "basesha000"
    self.base_tip_sha = "basetip999"
    self.patch_in_files = True
    self.threads: list[dict[str, Any]] = [
      {
        "id": "PRRT_abc1",
        "isResolved": False,
        "isOutdated": False,
        "path": "01.md",
        "line": 3,
        "originalLine": 3,
        "diffSide": "RIGHT",
        "comments": {
          "nodes": [
            {
              "databaseId": 1001,
              "body": "सन्धिः?",
              "createdAt": "2026-11-26T01:00:00Z",
              "updatedAt": "2026-11-26T01:00:00Z",
              "url": "https://github.com/vishvAsa/mahAbhAratam/pull/192#discussion_r1001",
              "author": {"login": "vvasuki"},
              "replyTo": None,
            }
          ]
        },
      }
    ]
    self.issue_comments: list[dict[str, Any]] = []
    self.reviews: list[dict[str, Any]] = []

  def get(self, url: str, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None, timeout: int = 30) -> _FakeResponse:
    accept = (headers or {}).get("Accept", "")
    self.calls.append({"method": "GET", "url": url, "params": params, "accept": accept})

    if url.endswith("/user"):
      return _FakeResponse(json_data={"login": "vvasuki", "name": "Vishvas", "avatar_url": "https://example.org/a.png"})

    if "/search/issues" in url:
      return _FakeResponse(
        json_data={
          "items": [
            {
              "number": 192,
              "title": self.pr_title,
              "html_url": "https://github.com/vishvAsa/mahAbhAratam/pull/192",
              "repository_url": "https://api.github.com/repos/vishvAsa/mahAbhAratam",
              "state": "open",
              "draft": False,
              "user": {"login": "mrunaldatar"},
              "created_at": "2026-11-16T06:10:19Z",
              "updated_at": "2026-11-26T09:39:07Z",
              "comments": len(self.issue_comments),
            }
          ]
        }
      )

    if url.endswith("/pulls/192") and accept == "application/vnd.github.v3.diff":
      return _FakeResponse(
        text=(
          "diff --git a/01.md b/01.md\n"
          "index 111..222 100644\n"
          "--- a/01.md\n"
          "+++ b/01.md\n"
          "@@ -1,4 +1,4 @@\n"
          " title\n"
          "-स एव [[दुर्हद्भिर्|असुहृद्भिर्]] प्रणयो ऽस्येति\n"
          "+स एव दुर्हृद्भिर् प्रणयोऽस्येति\n"
          " धा॰ भ्वादिः ।\n"
        )
      )

    if url.endswith("/pulls/192"):
      return _FakeResponse(
        json_data={
          "number": 192,
          "title": self.pr_title,
          "html_url": "https://github.com/vishvAsa/mahAbhAratam/pull/192",
          "state": self.pr_state,
          "draft": False,
          "merged": self.pr_merged,
          "body": "PR description",
          "commits": 2,
          "mergeable": self.pr_mergeable,
          "mergeable_state": self.pr_mergeable_state,
          "rebaseable": self.pr_mergeable is not False,
          "additions": 1,
          "deletions": 1,
          "changed_files": 1,
          "user": {"login": "mrunaldatar"},
          "base": {"ref": "content", "sha": self.base_sha},
          "head": {"ref": "content", "sha": self.head_sha, "repo": {"full_name": "mrunaldatar/mahAbhAratam"}},
          "created_at": "2026-11-16T06:10:19Z",
          "updated_at": "2026-11-26T09:39:07Z",
        }
      )

    if url.endswith("/pulls/192/files"):
      patch = (
        "@@ -1,4 +1,4 @@\n"
        " title\n"
        "-स एव [[दुर्हद्भिर्|असुहृद्भिर्]] प्रणयो ऽस्येति\n"
        "+स एव दुर्हृद्भिर् प्रणयोऽस्येति\n"
        " धा॰ भ्वादिः ।\n"
      ) if self.patch_in_files else None
      return _FakeResponse(
        json_data=[
          {
            "filename": "01.md",
            "status": "modified",
            "additions": 1,
            "deletions": 1,
            "changes": 2,
            "sha": "blobhead01",
            "patch": patch,
          }
        ]
      )

    if "/git/blobs/blobhead01" in url:
      txt = "title\nस एव दुर्हृद्भिर् प्रणयोऽस्येति\nधा॰ भ्वादिः ।\nfooter\n"
      return _FakeResponse(json_data={"encoding": "base64", "content": base64.b64encode(txt.encode("utf-8")).decode("ascii")})

    if "/git/blobs/mb_blob" in url:
      txt = "title\nस एव [[दुर्हद्भिर्|असुहृद्भिर्]] प्रणयो ऽस्येति\nधा॰ भ्वादिः ।\nfooter\n"
      return _FakeResponse(json_data={"encoding": "base64", "content": base64.b64encode(txt.encode("utf-8")).decode("ascii")})

    if "/git/blobs/bt_blob" in url:
      txt = "title\nस एव असुहृद्भिर् प्रणयो ऽस्येति\nधा॰ भ्वादिः ।\nfooter\n"
      return _FakeResponse(json_data={"encoding": "base64", "content": base64.b64encode(txt.encode("utf-8")).decode("ascii")})

    if "/compare/content..." in url:
      return _FakeResponse(
        json_data={
          "ahead_by": 2,
          "behind_by": 4,
          "base_commit": {"sha": self.base_tip_sha},
          "merge_base_commit": {"sha": self.base_sha},
        }
      )

    if f"/git/trees/{self.base_sha}" in url:
      return _FakeResponse(json_data={"truncated": False, "tree": [{"path": "01.md", "type": "blob", "sha": "mb_blob"}]})

    if f"/git/trees/{self.base_tip_sha}" in url:
      return _FakeResponse(json_data={"truncated": False, "tree": [{"path": "01.md", "type": "blob", "sha": "bt_blob"}]})

    if "/contents/01.md" in url:
      txt = "title\nस एव [[दुर्हद्भिर्|असुहृद्भिर्]] प्रणयो ऽस्येति\nधा॰ भ्वादिः ।\nfooter\n"
      if "/mrunaldatar/" in url:
        txt = "title\nस एव दुर्हृद्भिर् प्रणयोऽस्येति\nधा॰ भ्वादिः ।\nfooter\n"
      return _FakeResponse(text=txt)

    if url.endswith("/issues/192/comments"):
      return _FakeResponse(json_data=list(self.issue_comments))

    if url.endswith("/pulls/192/reviews"):
      return _FakeResponse(json_data=list(self.reviews))

    if url.endswith("/pulls/192/commits"):
      return _FakeResponse(
        json_data=[
          {
            "sha": "1111111111111111111111111111111111111111",
            "html_url": "https://github.com/vishvAsa/mahAbhAratam/pull/192/commits/1111111111111111111111111111111111111111",
            "commit": {
              "message": "Initial cleanup\n\nDetails",
              "author": {"name": "Mrunal", "date": "2026-11-26T06:10:00Z"},
            },
            "author": {"login": "mrunaldatar"},
            "parents": [{"sha": self.base_sha}],
          },
          {
            "sha": "2222222222222222222222222222222222222222",
            "html_url": "https://github.com/vishvAsa/mahAbhAratam/pull/192/commits/2222222222222222222222222222222222222222",
            "commit": {
              "message": "Fix avagraha",
              "author": {"name": "Mrunal", "date": "2026-11-26T09:35:00Z"},
            },
            "author": {"login": "mrunaldatar"},
            "parents": [{"sha": "1111111111111111111111111111111111111111"}],
          },
        ]
      )

    if url.endswith("/commits/1111111111111111111111111111111111111111"):
      return _FakeResponse(
        json_data={
          "sha": "1111111111111111111111111111111111111111",
          "html_url": "https://github.com/vishvAsa/mahAbhAratam/commit/1111111111111111111111111111111111111111",
          "commit": {"message": "Initial cleanup", "author": {"date": "2026-11-26T06:10:00Z"}},
          "author": {"login": "mrunaldatar"},
          "parents": [{"sha": self.base_sha}],
          "files": [
            {
              "filename": "01.md",
              "status": "modified",
              "additions": 1,
              "deletions": 1,
              "changes": 2,
              "sha": "blobhead01",
              "patch": "@@ -1,3 +1,3 @@\n title\n-स एव [[दुर्हद्भिर्|असुहृद्भिर्]]\n+स एव दुर्हृद्भिर्\n",
            }
          ],
        }
      )

    if url.endswith("/pulls/192/comments"):
      flat = []
      for th in self.threads:
        for c in th["comments"]["nodes"]:
          flat.append(
            {
              "id": c["databaseId"],
              "path": th["path"],
              "line": th["line"],
              "original_line": th["originalLine"],
              "side": th["diffSide"],
              "user": c["author"],
              "body": c["body"],
              "created_at": c["createdAt"],
              "updated_at": c["updatedAt"],
              "html_url": c["url"],
              "in_reply_to_id": (c.get("replyTo") or {}).get("databaseId"),
            }
          )
      return _FakeResponse(json_data=flat)

    return _FakeResponse(status_code=404, json_data={"message": f"Not found: {url}"})

  def post(self, url: str, headers: dict[str, str] | None = None, json: dict[str, Any] | None = None, timeout: int = 30) -> _FakeResponse:
    self.calls.append({"method": "POST", "url": url, "json": json})
    payload = json or {}

    if url.endswith("/graphql"):
      q = payload.get("query", "")
      if "search(" in q:
        gql_m = "MERGEABLE" if self.pr_mergeable is True else ("CONFLICTING" if self.pr_mergeable is False else "UNKNOWN")
        return _FakeResponse(
          json_data={
            "data": {
              "search": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                  {
                    "number": 192,
                    "title": self.pr_title,
                    "url": "https://github.com/vishvAsa/mahAbhAratam/pull/192",
                    "state": self.pr_state.upper(),
                    "isDraft": False,
                    "merged": self.pr_merged,
                    "mergeable": gql_m,
                    "mergeStateStatus": self.pr_mergeable_state.upper(),
                    "createdAt": "2026-11-16T06:10:19Z",
                    "updatedAt": "2026-11-26T09:39:07Z",
                    "additions": 1,
                    "deletions": 1,
                    "changedFiles": 1,
                    "reviewDecision": "CHANGES_REQUESTED",
                    "baseRefName": "content",
                    "baseRefOid": self.base_sha,
                    "headRefName": "content",
                    "headRefOid": self.head_sha,
                    "headRepository": {"nameWithOwner": "mrunaldatar/mahAbhAratam"},
                    "author": {"login": "mrunaldatar"},
                    "repository": {"name": "mahAbhAratam", "nameWithOwner": "vishvAsa/mahAbhAratam"},
                    "comments": {"totalCount": len(self.issue_comments)},
                    "reviewThreads": {"totalCount": len(self.threads)},
                  }
                ],
              }
            }
          }
        )
      if "reviewThreads(" in q:
        gql_m = "MERGEABLE" if self.pr_mergeable is True else ("CONFLICTING" if self.pr_mergeable is False else "UNKNOWN")
        return _FakeResponse(
          json_data={
            "data": {
              "repository": {
                "pullRequest": {
                  "merged": self.pr_merged,
                  "mergeable": gql_m,
                  "mergeStateStatus": self.pr_mergeable_state.upper(),
                  "reviews": {"nodes": list(self.reviews)},
                  "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": list(self.threads)},
                }
              }
            }
          }
        )
      if "object(oid:" in q:
        return _FakeResponse(json_data={"data": {"repository": {}}})
      if "resolveReviewThread(" in q or "unresolveReviewThread(" in q:
        tid = payload["variables"]["threadId"]
        want = "resolveReviewThread" in q
        for th in self.threads:
          if th["id"] == tid:
            th["isResolved"] = want
        key = "resolveReviewThread" if want else "unresolveReviewThread"
        return _FakeResponse(json_data={"data": {key: {"thread": {"id": tid, "isResolved": want}}}})

    if url.endswith("/issues/192/comments"):
      item = {
        "id": 9001 + len(self.issue_comments),
        "user": {"login": "vvasuki"},
        "body": payload["body"],
        "created_at": "2026-11-27T01:30:00Z",
        "updated_at": "2026-11-27T01:30:00Z",
        "html_url": "https://github.com/vishvAsa/mahAbhAratam/pull/192#issuecomment-1",
      }
      self.issue_comments.append(item)
      return _FakeResponse(status_code=201, json_data=item)

    if url.endswith("/pulls/192/comments"):
      cid = 2000 + len(self.threads)
      self.threads.append(
        {
          "id": f"PRRT_new_{cid}",
          "isResolved": False,
          "isOutdated": False,
          "path": payload["path"],
          "line": payload["line"],
          "originalLine": payload["line"],
          "diffSide": payload["side"],
          "comments": {
            "nodes": [
              {
                "databaseId": cid,
                "body": payload["body"],
                "createdAt": "2026-11-27T01:31:00Z",
                "updatedAt": "2026-11-27T01:31:00Z",
                "url": f"https://github.com/vishvAsa/mahAbhAratam/pull/192#discussion_r{cid}",
                "author": {"login": "vvasuki"},
                "replyTo": None,
              }
            ]
          },
        }
      )
      return _FakeResponse(status_code=201, json_data={"id": cid, "body": payload["body"]})

    if "/pulls/192/comments/" in url and url.endswith("/replies"):
      root_id = int(url.split("/pulls/192/comments/")[1].split("/")[0])
      cid = 3000 + sum(len(t["comments"]["nodes"]) for t in self.threads)
      for th in self.threads:
        if any(c["databaseId"] == root_id for c in th["comments"]["nodes"]):
          th["comments"]["nodes"].append(
            {
              "databaseId": cid,
              "body": payload["body"],
              "createdAt": "2026-11-27T01:32:00Z",
              "updatedAt": "2026-11-27T01:32:00Z",
              "url": f"https://github.com/vishvAsa/mahAbhAratam/pull/192#discussion_r{cid}",
              "author": {"login": "vvasuki"},
              "replyTo": {"databaseId": root_id},
            }
          )
      return _FakeResponse(status_code=201, json_data={"id": cid, "body": payload["body"]})

    if url.endswith("/pulls/192/reviews"):
      rid = 4000 + len(self.reviews)
      self.reviews.append(
        {
          "databaseId": rid,
          "author": {"login": "vvasuki"},
          "state": payload["event"],
          "body": payload.get("body", ""),
          "submittedAt": "2026-11-27T01:33:00Z",
          "url": "",
        }
      )
      for c in payload.get("comments", []):
        cid = 5000 + len(self.threads)
        self.threads.append(
          {
            "id": f"PRRT_rev_{cid}",
            "isResolved": False,
            "isOutdated": False,
            "path": c["path"],
            "line": c["line"],
            "originalLine": c["line"],
            "diffSide": c["side"],
            "comments": {
              "nodes": [
                {
                  "databaseId": cid,
                  "body": c["body"],
                  "createdAt": "2026-11-27T01:33:00Z",
                  "updatedAt": "2026-11-27T01:33:00Z",
                  "url": "",
                  "author": {"login": "vvasuki"},
                  "replyTo": None,
                }
              ]
            },
          }
        )
      return _FakeResponse(status_code=200, json_data={"id": rid, "state": payload["event"]})

    return _FakeResponse(status_code=404, json_data={"message": f"Unhandled POST {url}"})

  def request(self, method: str, url: str, headers: dict[str, str] | None = None, json: dict[str, Any] | None = None, timeout: int = 30) -> _FakeResponse:
    m = method.upper()
    if m == "POST":
      return self.post(url, headers=headers, json=json, timeout=timeout)
    if m == "PATCH":
      self.calls.append({"method": "PATCH", "url": url, "json": json})
      payload = json or {}
      if url.endswith("/pulls/192") and "state" in payload:
        self.pr_state = payload["state"]
        return _FakeResponse(json_data={"number": 192, "state": self.pr_state})
      if "/pulls/comments/" in url and "body" in payload:
        cid = int(url.rsplit("/", 1)[-1])
        for th in self.threads:
          for c in th["comments"]["nodes"]:
            if int(c["databaseId"]) == cid:
              c["body"] = payload["body"]
              return _FakeResponse(json_data={"id": cid, "body": c["body"]})
      if "/issues/comments/" in url and "body" in payload:
        cid = int(url.rsplit("/", 1)[-1])
        for c in self.issue_comments:
          if int(c["id"]) == cid:
            c["body"] = payload["body"]
            return _FakeResponse(json_data=c)
      return _FakeResponse(status_code=404, json_data={"message": f"Unhandled PATCH {url}"})
    if m == "PUT":
      self.calls.append({"method": "PUT", "url": url, "json": json})
      payload = json or {}
      if url.endswith("/pulls/192/merge"):
        if self.pr_mergeable is False or self.pr_state != "open":
          return _FakeResponse(status_code=405, json_data={"message": "Pull Request is not mergeable"})
        self.pr_state = "closed"
        self.pr_merged = True
        return _FakeResponse(json_data={"sha": "mergedsha999", "merged": True, "message": f"Merged via {payload.get('merge_method', 'merge')}"})
      if "/pulls/192/reviews/" in url and "body" in payload:
        rid = int(url.rsplit("/", 1)[-1])
        for r in self.reviews:
          if int(r["databaseId"]) == rid:
            r["body"] = payload["body"]
            return _FakeResponse(json_data={"id": rid, "body": r["body"]})
      return _FakeResponse(status_code=404, json_data={"message": f"Unhandled PUT {url}"})
    raise AssertionError(f"Unexpected method {method}")


def test_normalize_owner_and_resolve_token(tmp_path: Path, monkeypatch):
  assert normalize_owner("vishvAsa") == "vishvAsa"
  assert normalize_owner("https://github.com/vishvAsa/") == "vishvAsa"
  assert normalize_owner("https://github.com/vishvAsa/rAmAnujIyam") == "vishvAsa"
  assert normalize_owner("  ") == "vishvAsa"

  toml_file = tmp_path / "tokens.toml"
  toml_file.write_text('[github]\ngeneral_kv = "tok_toml"\n', encoding="utf-8")

  monkeypatch.delenv("GITHUB_TOKEN", raising=False)
  monkeypatch.delenv("GH_TOKEN", raising=False)
  monkeypatch.setattr("pull_requests_san.auth._read_gh_hosts_token", lambda: None)

  assert resolve_github_token("tok_explicit", cred_path=toml_file, allow_gh_cli=False) == "tok_explicit"
  monkeypatch.setenv("GITHUB_TOKEN", "tok_env")
  assert resolve_github_token(cred_path=toml_file, allow_gh_cli=False) == "tok_env"
  monkeypatch.delenv("GITHUB_TOKEN")
  assert resolve_github_token(cred_path=toml_file, allow_gh_cli=False) == "tok_toml"
  import sys
  monkeypatch.setitem(sys.modules, "curation_utils", None)
  monkeypatch.setattr("pull_requests_san.auth.tomllib", None)
  assert resolve_github_token(cred_path=toml_file, allow_gh_cli=False) == "tok_toml"


def test_compute_char_diff_devanagari_grapheme_and_codepoint():
  # Grapheme mode keeps combining marks (e.g. Vedic svara U+1CD3) attached to their base akshara
  lhs_g, rhs_g = compute_char_diff("इमा᳓म् अ᳓रण्या ते᳓जसा", "इमा᳓म् अर᳓ण्या ते᳓जसा", mode="grapheme")
  assert "".join(s["text"] for s in lhs_g) == "इमा᳓म् अ᳓रण्या ते᳓जसा"
  assert "".join(s["text"] for s in rhs_g) == "इमा᳓म् अर᳓ण्या ते᳓जसा"
  assert [s for s in lhs_g if s["op"] == "del"] == [{"op": "del", "text": "अ᳓र"}]
  assert [s for s in rhs_g if s["op"] == "ins"] == [{"op": "ins", "text": "अर᳓"}]

  # Codepoint mode isolates a single added/removed combining or Vedic accent codepoint
  lhs_c, rhs_c = compute_char_diff("अ᳓रण्या", "अरण्या", mode="codepoint")
  assert [s for s in lhs_c if s["op"] == "del"] == [{"op": "del", "text": "\u1cd3"}]
  assert [s for s in rhs_c if s["op"] == "ins"] == []

  # Space removal before avagraha & U+0966 vs U+0970
  lhs_s, rhs_s = compute_char_diff("प्रणयो ऽस्येति धा०", "प्रणयोऽस्येति धा॰", mode="grapheme")
  assert [s for s in lhs_s if s["op"] == "del"] == [{"op": "del", "text": " "}, {"op": "del", "text": "०"}]
  assert [s for s in rhs_s if s["op"] == "ins"] == [{"op": "ins", "text": "॰"}]


def test_unicode_normalization_and_ignore_punct():
  decomposed = "क\u093c"  # KA + NUKTA (NFC canonical form; U+0958 is a Composition Exclusion)
  composed = "\u0958"     # QA precomposed
  assert apply_normalization(composed, "nfc") == decomposed
  assert strip_punctuation("राम। (सः)") == "राम सः"

  # NFC normalization reconciles U+0958 vs KA+NUKTA Devanagari strings
  lhs_raw, rhs_raw = compute_char_diff(decomposed, composed, mode="codepoint", normalize="none")
  assert any(s["op"] == "del" for s in lhs_raw)
  lhs_nfc, rhs_nfc = compute_char_diff(decomposed, composed, mode="codepoint", normalize="nfc")
  assert [s for s in lhs_nfc if s["op"] != "eq"] == []
  assert [s for s in rhs_nfc if s["op"] != "eq"] == []

  # Punctuation-only changes collapse cleanly when ignore_punct=True
  patch = "@@ -1,2 +1,2 @@\n-राम।\n+राम॥\n सीता\n"
  h_norm = parse_patch_hunks(patch, char_diff_mode="word", ignore_punct=False)
  assert h_norm[0].rows[0].kind == "modify"
  h_ip = parse_patch_hunks(patch, char_diff_mode="word", ignore_punct=True)
  assert h_ip[0].rows[0].kind == "context"
  assert [s for s in h_ip[0].inline_rows[0]["segments"] if s["op"] != "eq"] == []


def test_parse_patch_hunks_aligns_multiedits_and_edge_cases():
  patch = (
    "@@ -10,5 +10,4 @@ section\n"
    " अनयः\n"
    '-" [[अय|अयँ]] " धा० भ्वादिः ।\n'
    '-इति कुशिककुलतिलक-वरदार्य-कृतौ समाप्तम् ।\n'
    '-" [[इण्|इण्]] " धा० अदादिः ।\n'
    '+" अयँ " धा॰ भ्वादिः ।\n'
    '+" इण् " धा॰ अदादिः ।\n'
    " समाप्तम्\n"
    "\\ No newline at end of file"
  )
  hunks = parse_patch_hunks(patch, char_diff_mode="grapheme")
  assert len(hunks) == 1
  rows = hunks[0].rows
  assert [r.kind for r in rows] == ["context", "modify", "del", "modify", "context"]
  assert rows[1].left_num == 11 and rows[1].right_num == 11
  assert rows[2].left_num == 12 and rows[2].right_num is None
  assert rows[3].left_num == 13 and rows[3].right_num == 12
  assert rows[4].left_num == 14 and rows[4].right_num == 13

  # Added file with single line
  added = parse_patch_hunks("@@ -0,0 +1 @@\n+नमः")
  assert len(added) == 1
  assert added[0].rows[0].kind == "add"
  assert added[0].rows[0].right_num == 1

  # Blank context bridging matches paragraphs separated by blank lines & avoids false m=1/n=1 pairing
  ds_patch = (
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
  ds_word = parse_patch_hunks(ds_patch, char_diff_mode="word")
  assert len(ds_word) == 1
  paired_para = [r for r in ds_word[0].rows if "आविध्यति" in (r.left_text or "") or "आविध्यति" in (r.right_text or "")]
  assert len(paired_para) == 1 and paired_para[0].kind == "modify"
  assert paired_para[0].left_num == 339 and paired_para[0].right_num == 343
  deleted_fn = [r for r in ds_word[0].rows if r.left_num == 341]
  assert len(deleted_fn) == 1 and deleted_fn[0].kind == "del" and deleted_fn[0].right_num is None

  # Single-pane space-separated word mode keeps substituted words atomic while highlighting mere deletions/insertions without repetition
  flow_summary = [r for r in ds_word[0].inline_rows if r["left_num"] == 337]
  assert len(flow_summary) == 1
  assert flow_summary[0]["segments"] == [
    {"op": "eq", "text": "<details><summary>वीरराघवः"},
    {"op": "del", "text": " [यान्त्रिकम्]"},
    {"op": "eq", "text": "</summary>"},
  ]
  flow_para = [r for r in ds_word[0].inline_rows if r["left_num"] == 339]
  assert len(flow_para) == 1
  assert any(s == {"op": "del", "text": "[^v5]"} for s in flow_para[0]["segments"])
  assert any(s == {"op": "eq", "text": "आविध्याऽऽलोल्य"} for s in flow_para[0]["segments"])
  assert not any(s["op"] == "ins" and "आविध्याऽऽलोल्य" in s["text"] for s in flow_para[0]["segments"])

  # Mere insertion inside a word-chunk also avoids repeating the unchanged prefix/suffix
  ins_only_segs = compute_stream_diff(
    "<details><summary>वीरराघवः</summary>",
    "<details><summary>वीरराघवः [यान्त्रिकम्]</summary>",
    mode="word",
  )
  assert ins_only_segs == [
    {"op": "eq", "text": "<details><summary>वीरराघवः"},
    {"op": "ins", "text": " [यान्त्रिकम्]"},
    {"op": "eq", "text": "</summary>"},
  ]

  reflow_patch = (
    "@@ -1,4 +1,3 @@\n"
    " intro\n"
    "-alpha beta gamma delta\n"
    "-epsilon zeta eta theta\n"
    "+alpha beta gamma epsilon newword zeta eta theta\n"
    " outro"
  )
  reflow_h = parse_patch_hunks(reflow_patch, char_diff_mode="word")[0]
  assert any(any(s["op"] == "del" and "delta" in s["text"] for s in r["segments"]) for r in reflow_h.inline_rows)

  # Cross-row newline boundary still pairs del/ins words (e.g. पा० -> पा॰) for sub_segments
  cross_patch = "@@ -1,2 +1,1 @@\n-पा०\n-ख\n+पा॰ ख\n"
  cross_h = parse_patch_hunks(cross_patch, char_diff_mode="word")[0]
  del_pa = next(s for r in cross_h.inline_rows for s in r["segments"] if s["op"] == "del")
  ins_pa = next(s for r in cross_h.inline_rows for s in r["segments"] if s["op"] == "ins")
  assert {"op": "del", "text": "०"} in del_pa["sub_segments"]
  assert {"op": "ins", "text": "॰"} in ins_pa["sub_segments"]
  assert any(any(s["op"] == "ins" and "newword" in s["text"] for s in r["segments"]) for r in reflow_h.inline_rows)
  assert any(r["segments"] and any(s["op"] == "eq" and "zeta" in s["text"] for s in r["segments"]) for r in reflow_h.inline_rows)


def test_parse_full_diff_by_file_handles_quoted_and_space_paths():
  raw = (
    'diff --git "a/kAvyam/\\340\\244\\205.md" "b/kAvyam/\\340\\244\\205.md"\n'
    "index 111..222 100644\n"
    "--- \"a/kAvyam/\\340\\244\\205.md\"\n"
    "+++ \"b/kAvyam/\\340\\244\\205.md\"\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
    "diff --git a/folder with space/file.md b/folder with space/file.md\n"
    "--- a/folder with space/file.md\n"
    "+++ b/folder with space/file.md\n"
    "@@ -1 +1 @@\n"
    "-x\n"
    "+y\n"
  )
  by_file = parse_full_diff_by_file(raw)
  assert "kAvyam/अ.md" in by_file
  assert "folder with space/file.md" in by_file
  assert "-old\n+new" in by_file["kAvyam/अ.md"]


  # Quoted Devanagari rename headers & unquoted spaces
  renamed_diff = (
    'diff --git "a/old_\\340\\244\\205.md" "b/new_\\340\\244\\206.md"\n'
    "similarity index 90%\n"
    'rename from "old_\\340\\244\\205.md"\n'
    'rename to "new_\\340\\244\\206.md"\n'
    "--- \"a/old_\\340\\244\\205.md\"\n"
    "+++ \"b/new_\\340\\244\\206.md\"\n"
    "@@ -1 +1 @@\n"
    "-a\n"
    "+b\n"
  )
  renamed_map = parse_full_diff_by_file(renamed_diff)
  assert "new_आ.md" in renamed_map and "old_अ.md" in renamed_map


def test_build_full_file_hunk_marks_in_diff_hunk():
  base = "line1\nline2\nline3 old\nline4\nline5\nline6\nline7\nline8\n"
  head = "line1\nline2\nline3 new\nline4\nline5\nline6\nline7\nline8\n"
  patch = "@@ -2,3 +2,3 @@\n line2\n-line3 old\n+line3 new\n line4"
  hunk = build_full_file_hunk(base, head, existing_patch=patch)
  assert len(hunk.rows) == 8
  assert hunk.rows[0].in_diff_hunk is False
  assert hunk.rows[1].in_diff_hunk is True
  assert hunk.rows[2].kind == "modify" and hunk.rows[2].in_diff_hunk is True
  assert hunk.rows[7].in_diff_hunk is False


def test_client_fallback_v3_diff_and_unauth_guards():
  fake = _FakeSession()
  fake.patch_in_files = False
  client = GitHubClient(token="tok_test", session=fake, local_org_dir=None)
  prs = client.list_org_pull_requests("https://github.com/vishvAsa/")
  assert len(prs) == 1
  assert prs[0].repo_full_name == "vishvAsa/mahAbhAratam"
  assert prs[0].number == 192

  detail = client.get_pull_request_detail("vishvAsa", "mahAbhAratam", 192)
  assert len(detail.files) == 1
  assert len(detail.files[0].hunks) == 1
  mod_row = detail.files[0].hunks[0].rows[1]
  assert mod_row.kind == "modify"
  assert {"op": "del", "text": " "} in mod_row.lhs_segments

  # Unauthenticated client via token="" uses REST search fallback and refuses writes
  client_no_tok = GitHubClient(token="", session=fake, local_org_dir=None)
  assert client_no_tok.token is None
  prs_rest = client_no_tok.list_org_pull_requests("vishvAsa")
  assert len(prs_rest) == 1 and prs_rest[0].number == 192
  with pytest.raises(GitHubAPIError):
    client_no_tok.create_issue_comment("vishvAsa", "mahAbhAratam", 192, "hi")

  # Fork PR head fallback fetches from head_repo_full_name when blob_sha is absent
  fork_detail = client.get_file_full_diff(
    "vishvAsa",
    "mahAbhAratam",
    detail.files[0].__class__(filename="01.md", status="modified", additions=1, deletions=1, changes=2, sha=""),
    base_sha="basesha000",
    head_sha="headsha111",
    head_repo_full_name="mrunaldatar/mahAbhAratam",
  )
  assert any(r.kind == "modify" for r in fork_detail.hunks[0].rows)
  assert any("/repos/mrunaldatar/mahAbhAratam/contents/01.md" in c["url"] for c in fake.calls)

  # Validation checks on line / start_line / extra_comments
  with pytest.raises(ValueError):
    client.create_inline_comment("vishvAsa", "mahAbhAratam", 192, path="01.md", line=0, body="x")
  with pytest.raises(ValueError):
    client.create_inline_comment("vishvAsa", "mahAbhAratam", 192, path="01.md", line=5, start_line=5, body="x")
  with pytest.raises(ValueError):
    client.submit_review("vishvAsa", "mahAbhAratam", 192, event="COMMENT", comments=[{"path": "01.md"}])


def test_gui_endpoints_diff_comments_refresh_and_local_helpers(tmp_path: Path, monkeypatch):
  pytest.importorskip("fastapi")
  from fastapi.testclient import TestClient
  from pull_requests_san import pr_review_gui

  local_org = tmp_path / "vishvAsa"
  local_file = local_org / "mahAbhAratam" / "01.md"
  local_file.parent.mkdir(parents=True)
  local_file.write_text("title\n", encoding="utf-8")
  sibling_file = local_org / "otherRepo" / "secret.md"
  sibling_file.parent.mkdir(parents=True)
  sibling_file.write_text("secret\n", encoding="utf-8")

  fake = _FakeSession()
  client = GitHubClient(token="tok_test", session=fake, local_org_dir=str(local_org))
  session = PRReviewSession(client=client, default_owner="vishvAsa")
  app = pr_review_gui.create_app(session=session)
  tc = TestClient(app)

  r_html = tc.get("/")
  assert r_html.status_code == 200 and "<title>GitHub PR Character-Level Reviewer</title>" in r_html.text
  hdr_top = r_html.text.split("<header>", 1)[1].split("</div>", 1)[0]
  assert "GitHub PR Character-Level Reviewer" not in hdr_top
  for pill_id in ("stateOpenPill", "stateClosedPill", "stateMergedPill", "stateAllPill"):
    assert f'id="{pill_id}"' in hdr_top
    assert r_html.text.count(f'id="{pill_id}"') == 1

  r_meta = tc.get("/api/meta")
  assert r_meta.status_code == 200
  assert r_meta.json()["auth_user"]["login"] == "vvasuki"
  assert "nfc" in r_meta.json()["normalizations"]

  r_prs = tc.get("/api/prs?owner=vishvAsa&state=open")
  assert r_prs.status_code == 200
  assert r_prs.json()["repos"] == ["mahAbhAratam"]

  r_pr = tc.get("/api/pr/vishvAsa/mahAbhAratam/192?diff_mode=grapheme")
  assert r_pr.status_code == 200
  pr_data = r_pr.json()
  assert pr_data["files"][0]["local_file_path"] == str(local_file)
  assert len(pr_data["review_threads"]) == 1
  assert len(pr_data["commits"]) == 2
  assert pr_data["commits"][0]["committed_at"] == "2026-11-26T06:10:00Z"

  # Changelist endpoint returns single commit diff and caches across diff_mode toggles
  c1_sha = pr_data["commits"][0]["sha"]
  r_cl = tc.get(f"/api/pr/vishvAsa/mahAbhAratam/192/changelist?sha={c1_sha}&diff_mode=word")
  assert r_cl.status_code == 200
  cl_data = r_cl.json()
  assert cl_data["sha"] == c1_sha
  assert cl_data["commit"]["title"] == "Initial cleanup"
  assert len(cl_data["files"]) == 1
  calls_before_cl = len(fake.calls)
  r_cl2 = tc.get(f"/api/pr/vishvAsa/mahAbhAratam/192/changelist?sha={c1_sha}&diff_mode=grapheme&ignore_punct=true")
  assert r_cl2.status_code == 200
  assert len(fake.calls) == calls_before_cl
  assert tc.get(f"/api/pr/vishvAsa/mahAbhAratam/192/file_full?filename=01.md&commit_sha={c1_sha}").status_code == 200

  # Switching diff_mode / normalize / ignore_punct recomputes segments from cache without hitting GitHub again
  calls_before = len(fake.calls)
  r_cp = tc.get("/api/pr/vishvAsa/mahAbhAratam/192?diff_mode=codepoint")
  assert r_cp.status_code == 200
  assert len(fake.calls) == calls_before
  r_norm = tc.get("/api/pr/vishvAsa/mahAbhAratam/192?diff_mode=word&normalize=nfc&ignore_punct=true")
  assert r_norm.status_code == 200
  assert r_norm.json()["normalize"] == "nfc"
  assert r_norm.json()["ignore_punct"] is True
  assert len(fake.calls) == calls_before

  # Load full file
  r_full = tc.get("/api/pr/vishvAsa/mahAbhAratam/192/file_full?filename=01.md")
  assert r_full.status_code == 200
  assert r_full.json()["file"]["is_full_file"] is True
  assert len(r_full.json()["file"]["hunks"][0]["rows"]) == 4

  # Post inline comment, reply, issue comment, draft + review, resolve thread
  r_inline = tc.post(
    "/api/pr/vishvAsa/mahAbhAratam/192/inline_comment",
    json={"path": "01.md", "line": 2, "side": "RIGHT", "body": "अत्र सन्धिः कार्यः"},
  )
  assert r_inline.status_code == 200
  assert len(r_inline.json()["pr"]["review_threads"]) == 2

  r_reply = tc.post(
    "/api/pr/vishvAsa/mahAbhAratam/192/reply",
    json={"comment_id": 1001, "body": "समीचीनम्"},
  )
  assert r_reply.status_code == 200
  assert len(r_reply.json()["pr"]["review_threads"][0]["comments"]) == 2

  r_issue = tc.post(
    "/api/pr/vishvAsa/mahAbhAratam/192/issue_comment",
    json={"body": "साधु कृतम्"},
  )
  assert r_issue.status_code == 200
  assert len(r_issue.json()["pr"]["issue_comments"]) == 1

  r_draft = tc.post(
    "/api/pr/vishvAsa/mahAbhAratam/192/draft_comment",
    json={"path": "01.md", "line": 3, "side": "RIGHT", "body": "Draft note"},
  )
  assert r_draft.status_code == 200
  draft_id = r_draft.json()["draft"]["draft_id"]

  r_draft_edit = tc.patch(
    f"/api/pr/vishvAsa/mahAbhAratam/192/draft_comment/{draft_id}",
    json={"body": "Edited draft note"},
  )
  assert r_draft_edit.status_code == 200
  assert r_draft_edit.json()["draft"]["body"] == "Edited draft note"
  assert r_draft_edit.json()["pending_drafts"][0]["body"] == "Edited draft note"

  r_rev = tc.post(
    "/api/pr/vishvAsa/mahAbhAratam/192/review",
    json={"event": "REQUEST_CHANGES", "body": "पुनः पश्यतु", "include_drafts": True},
  )
  assert r_rev.status_code == 200
  assert r_rev.json()["pr"]["pending_drafts"] == []
  assert len(r_rev.json()["pr"]["review_threads"]) == 3
  assert r_rev.json()["pr"]["review_threads"][2]["comments"][0]["body"] == "Edited draft note"
  assert tc.delete(f"/api/pr/vishvAsa/mahAbhAratam/192/draft_comment/{draft_id}").status_code == 404

  r_rc_edit = tc.patch(
    "/api/pr/vishvAsa/mahAbhAratam/192/review_comment/1001",
    json={"body": "सन्धिः संशोधितः"},
  )
  assert r_rc_edit.status_code == 200
  assert r_rc_edit.json()["pr"]["review_threads"][0]["comments"][0]["body"] == "सन्धिः संशोधितः"

  r_ic_edit = tc.patch(
    "/api/pr/vishvAsa/mahAbhAratam/192/issue_comment/9001",
    json={"body": "साधु कृतम् (edited)"},
  )
  assert r_ic_edit.status_code == 200
  assert r_ic_edit.json()["pr"]["issue_comments"][0]["body"] == "साधु कृतम् (edited)"

  r_rs_edit = tc.put(
    "/api/pr/vishvAsa/mahAbhAratam/192/review_summary/4000",
    json={"body": "पुनः पश्यतु (updated)"},
  )
  assert r_rs_edit.status_code == 200
  assert r_rs_edit.json()["pr"]["reviews"][0]["body"] == "पुनः पश्यतु (updated)"

  r_res = tc.post(
    "/api/pr/vishvAsa/mahAbhAratam/192/resolve_thread",
    json={"thread_id": "PRRT_abc1", "resolve": True},
  )
  assert r_res.status_code == 200
  assert r_res.json()["pr"]["review_threads"][0]["is_resolved"] is True

  # Refresh picks up upstream changes (e.g. updated PR title)
  fake.pr_title = "Updated PR title"
  r_ref = tc.post(
    "/api/refresh",
    json={"owner": "vishvAsa", "state": "open", "active_owner": "vishvAsa", "active_repo": "mahAbhAratam", "active_number": 192},
  )
  assert r_ref.status_code == 200
  assert r_ref.json()["prs"][0]["title"] == "Updated PR title"
  assert r_ref.json()["active_pr"]["summary"]["title"] == "Updated PR title"

  # Local IDEA and GoldenDict endpoints + path traversal protection
  launched: list[list[str]] = []
  with monkeypatch.context() as mp:
    mp.setattr(pr_review_gui, "_resolve_idea_binary", lambda: "/usr/bin/idea-mock")
    mp.setattr(pr_review_gui, "_resolve_goldendict_binary", lambda: "/usr/bin/gd-mock")
    mp.setattr(pr_review_gui.subprocess, "Popen", lambda cmd, **kw: launched.append(list(cmd)))

    r_idea = tc.post("/api/open_in_idea", json={"repo": "mahAbhAratam", "path": "01.md", "line": 2})
    assert r_idea.status_code == 200
    assert launched[-1] == ["/usr/bin/idea-mock", "--line", "2", str(local_file.resolve())]

    r_trav = tc.post("/api/open_in_idea", json={"repo": "mahAbhAratam", "path": "../../etc/passwd"})
    assert r_trav.status_code == 400
    r_sib = tc.post("/api/open_in_idea", json={"repo": "mahAbhAratam", "path": "../otherRepo/secret.md"})
    assert r_sib.status_code == 400

    # Verify review_decision is preserved on PR list after mutation
    prs_after = tc.get("/api/prs?owner=vishvAsa&state=open").json()
    assert prs_after["prs"][0]["review_decision"] == "CHANGES_REQUESTED"

    r_gd = tc.post("/api/open_in_goldendict", json={"word": "जी᳓वत्"})
    assert r_gd.status_code == 200
    assert launched[-1] == ["/usr/bin/gd-mock", "जीवत्"]

  # Open PR in main browser + URL safety checks
  opened_urls: list[str] = []
  monkeypatch.setattr(pr_review_gui.webbrowser, "open_new_tab", lambda u: (opened_urls.append(u), True)[1])
  r_br = tc.post("/api/open_in_browser", json={"url": "https://github.com/vishvAsa/mahAbhAratam/pull/192"})
  assert r_br.status_code == 200
  assert opened_urls == ["https://github.com/vishvAsa/mahAbhAratam/pull/192"]
  for bad_url in ("file:///etc/passwd", "javascript:alert(1)", "https://github.com@evil.com/x"):
    assert tc.post("/api/open_in_browser", json={"url": bad_url}).status_code == 400

  # Close PR & Reopen PR endpoints + sidebar open list cleanup
  r_close = tc.post("/api/pr/vishvAsa/mahAbhAratam/192/state", json={"state": "closed"})
  assert r_close.status_code == 200
  assert r_close.json()["pr"]["summary"]["state"] == "closed"
  assert tc.get("/api/prs?owner=vishvAsa&state=open").json()["prs"] == []
  assert tc.post("/api/pr/vishvAsa/mahAbhAratam/192/state", json={"state": "bogus"}).status_code == 400
  r_reopen = tc.post("/api/pr/vishvAsa/mahAbhAratam/192/state", json={"state": "open"})
  assert r_reopen.status_code == 200 and r_reopen.json()["pr"]["summary"]["state"] == "open"

  # Conflict detection + merge blocking when dirty, then successful squash merge when clean
  fake.pr_mergeable = False
  fake.pr_mergeable_state = "dirty"
  r_conf = tc.get("/api/pr/vishvAsa/mahAbhAratam/192?refresh=true")
  assert r_conf.status_code == 200
  conf_data = r_conf.json()
  assert conf_data["mergeable"] is False
  assert conf_data["mergeable_state"] == "dirty"
  assert conf_data["ahead_by"] == 2 and conf_data["behind_by"] == 4
  assert len(conf_data["conflicts"]) == 1
  assert conf_data["conflicts"][0]["filename"] == "01.md"
  assert conf_data["conflicts"][0]["reason"] == "content_conflict"
  assert len(conf_data["conflicts"][0]["hunks"]) == 1
  assert tc.post("/api/pr/vishvAsa/mahAbhAratam/192/merge", json={"merge_method": "squash"}).status_code == 405

  fake.pr_mergeable = True
  fake.pr_mergeable_state = "clean"
  assert tc.post("/api/pr/vishvAsa/mahAbhAratam/192/merge", json={"merge_method": "invalid"}).status_code == 400
  assert tc.get("/api/prs?owner=vishvAsa&state=open&refresh=true").status_code == 200
  r_merge = tc.post("/api/pr/vishvAsa/mahAbhAratam/192/merge", json={"merge_method": "squash"})
  assert r_merge.status_code == 200
  assert r_merge.json()["merged"]["merged"] is True
  assert r_merge.json()["pr"]["merged"] is True
  assert r_merge.json()["pr"]["summary"]["state"] == "closed"
  assert tc.get("/api/prs?owner=vishvAsa&state=open").json()["prs"] == []


def test_extract_three_way_conflicts_and_recompute():
  if not shutil.which("git"):
    pytest.skip("git binary not installed")
  ancestor = "line1\nप्रणयो ऽस्येति\nline3\nline4\nline5\n"
  base_tip = "line1\nअसुहृद्भिर् प्रणयो ऽस्येति\nline3\nline4\nline5\n"
  head_txt = "line1\nदुर्हृद्भिर् प्रणयोऽस्येति\nline3\nline4\nline5\n"
  hunks = extract_three_way_conflicts(base_tip, ancestor, head_txt, char_diff_mode="grapheme")
  assert len(hunks) == 1
  assert "असुहृद्भिर्" in hunks[0]["base_text"]
  assert "दुर्हृद्भिर्" in hunks[0]["head_text"]
  assert hunks[0]["rows"][0]["kind"] == "modify"
  # Non-overlapping edits on opposite ends auto-merge with zero conflicts
  clean_base = "TOP_BASE\nline2\nline3\nline4\nline5\nline6\nline7\nline8\n"
  clean_anc = "line1\nline2\nline3\nline4\nline5\nline6\nline7\nline8\n"
  clean_head = "line1\nline2\nline3\nline4\nline5\nline6\nline7\nBOT_HEAD\n"
  assert extract_three_way_conflicts(clean_base, clean_anc, clean_head) == []


def test_frontend_js_renders_char_diffs_and_comments(tmp_path: Path):
  if not shutil.which("node"):
    pytest.skip("node binary not installed")
  from pull_requests_san import pr_review_gui

  html_src = pr_review_gui.load_html_template()

  assert '--dev: "Adishila SanVedic", "Tiro Devanagari Sanskrit", "Noto Sans Devanagari", "Annapurna SIL", serif;' in html_src
  assert "--dev-font-size: 26px;" in html_src
  assert "font-size: 20px;" in html_src
  assert "--ins-char: #fef3c7;" in html_src
  assert "--red-char: #ffeded;" in html_src
  assert "--del-char-sub: #ffd6db;" in html_src
  assert "#9ef0b5" not in html_src
  assert "Alt+P" not in html_src and "KeyP" not in html_src
  # Comment & review typography matches the main diff pane (var(--dev-font-size))
  for css_rule in (".comment-body {", ".review-textarea {", ".pr-body-box {", "textarea.reply-input {", ".thread-resolved-preview {"):
    assert "font-size: var(--dev-font-size);" in html_src.split(css_rule)[1].split("}")[0]
  assert 'id="reloadBtn"' in html_src and 'onclick="reloadUi()"' in html_src
  assert 'id="settingsBtn"' in html_src and 'id="settingsPane"' in html_src
  assert 'id="singlePaneChk" type="checkbox" checked' in html_src
  assert '<option value="word" selected>Word (space-separated)</option>' in html_src
  assert 'id="normalizeSelect"' in html_src and 'id="ignorePunctChk"' in html_src
  assert 'id="sandhiHighlightBtn"' in html_src and 'onToggleSandhiHighlight()' in html_src
  assert ".ch-ws" not in html_src
  assert "add-comment-btn" not in html_src and 'col class="col-act"' not in html_src and 'class="diff-gap-btn"' not in html_src

  m = re.search(r"<script>\n(.*?)\n</script>", html_src, re.S)
  assert m is not None
  js = m.group(1)

  sample_pr = {
    "summary": {
      "owner": "vishvAsa",
      "repo": "mahAbhAratam",
      "repo_full_name": "vishvAsa/mahAbhAratam",
      "number": 192,
      "title": "changes- ५४७",
      "url": "https://github.com/vishvAsa/mahAbhAratam/pull/192",
      "state": "open",
      "is_draft": False,
      "author": "mrunaldatar",
      "created_at": "2026-11-16T06:10:19Z",
      "updated_at": "2026-11-26T09:39:07Z",
      "base_ref": "content",
      "base_sha": "base0",
      "head_ref": "content",
      "head_sha": "head0",
      "head_repo_full_name": "mrunaldatar/mahAbhAratam",
      "additions": 1,
      "deletions": 1,
      "changed_files": 1,
      "review_decision": "CHANGES_REQUESTED",
      "comment_count": 0,
      "thread_count": 1,
    },
    "body": "Test PR",
    "commits_count": 1,
    "mergeable": True,
    "mergeable_state": "clean",
    "char_diff_mode": "grapheme",
    "pending_drafts": [],
    "issue_comments": [],
    "reviews": [],
    "review_threads": [
      {
        "id": "PRRT_1",
        "path": "01.md",
        "line": 2,
        "original_line": 2,
        "side": "RIGHT",
        "is_resolved": False,
        "is_outdated": False,
        "comments": [
          {
            "id": 1001,
            "author": "vvasuki",
            "body": 'सन्धिः? `code` ![a `b`](https://github.com/user-attachments/assets/abc?x=1&y=2&k=`v`) <script>alert(1)</script>',
            "created_at": "2026-11-26T01:00:00Z",
          }
        ],
      }
    ],
    "files": [
      {
        "filename": "01.md",
        "status": "modified",
        "additions": 1,
        "deletions": 1,
        "changes": 2,
        "previous_filename": None,
        "sha": "sha1",
        "has_patch": True,
        "is_binary": False,
        "is_full_file": False,
        "local_file_path": None,
        "hunks": [
          {
            "header": "@@ -1,2 +1,2 @@",
            "old_start": 1,
            "old_count": 2,
            "new_start": 1,
            "new_count": 2,
            "context": "",
            "rows": [
              {
                "kind": "modify",
                "left_num": 1,
                "right_num": 2,
                "left_text": "प्रणयो ऽस्येति",
                "right_text": "प्रणयोऽस्येति",
                "lhs_segments": [{"op": "eq", "text": "प्रणयो"}, {"op": "del", "text": " \u200b"}, {"op": "eq", "text": "ऽस्येति"}],
                "rhs_segments": [{"op": "eq", "text": "प्रणयोऽस्येति"}],
                "in_diff_hunk": True,
              },
              {
                "kind": "context",
                "left_num": 2,
                "right_num": 3,
                "left_text": "context line",
                "right_text": "context line",
                "lhs_segments": [{"op": "eq", "text": "context line"}],
                "rhs_segments": [{"op": "eq", "text": "context line"}],
                "in_diff_hunk": True,
              }
            ],
            "inline_rows": [
              {
                "kind": "modify",
                "left_num": 1,
                "right_num": 2,
                "in_diff_hunk": True,
                "segments": [
                  {
                    "op": "del",
                    "text": "प्रणयो \u200bऽस्येति",
                    "sub_segments": [
                      {"op": "eq", "text": "प्रणयो"},
                      {"op": "del", "text": " \u200b"},
                      {"op": "eq", "text": "ऽस्येति"},
                    ],
                  },
                  {
                    "op": "ins",
                    "text": "प्रणयोऽस्येति",
                    "sub_segments": [{"op": "eq", "text": "प्रणयोऽस्येति"}],
                  },
                  {"op": "eq", "text": " "},
                  {
                    "op": "del",
                    "text": "पा०",
                    "sub_segments": [{"op": "eq", "text": "पा"}, {"op": "del", "text": "०"}],
                  },
                  {
                    "op": "ins",
                    "text": "पा॰",
                    "sub_segments": [{"op": "eq", "text": "पा"}, {"op": "ins", "text": "॰"}],
                  },
                ],
              },
              {
                "kind": "context",
                "left_num": 2,
                "right_num": 3,
                "in_diff_hunk": True,
                "segments": [{"op": "eq", "text": "context line"}],
              },
            ],
          }
        ],
      }
    ],
  }

  harness = (
    "const elems = {};\n"
    "const listeners = {};\n"
    "const listenerLists = {};\n"
    "const clipboardCopies = [];\n"
    "let mockSelectionText = '';\n"
    "function makeClassList() {\n"
    "  const set = new Set();\n"
    "  return { add(c){ set.add(c); }, remove(c){ set.delete(c); }, toggle(c, f){ if (f === undefined) { if (set.has(c)) set.delete(c); else set.add(c); } else if (f) set.add(c); else set.delete(c); }, contains(c){ return set.has(c); } };\n"
    "}\n"
    "function makeEl(id) {\n"
    "  return { id, value: '', checked: false, innerHTML: '', textContent: '', style: {}, classList: makeClassList(), querySelectorAll(){ return []; }, scrollIntoView(){}, focus(){ this.focused = true; }, select(){} };\n"
    "}\n"
    "const cssProps = {};\n"
    "const lsStore = {};\n"
    "global.document = {\n"
    "  documentElement: { style: { setProperty(k, v){ cssProps[k] = v; } } },\n"
    "  getElementById(id) { if (!elems[id]) elems[id] = makeEl(id); return elems[id]; },\n"
    "  querySelectorAll() { return []; },\n"
    "  addEventListener(evt, fn) { if (!listenerLists[evt]) listenerLists[evt] = []; listenerLists[evt].push(fn); listeners[evt] = (ev) => listenerLists[evt].forEach(h => h(ev)); },\n"
    "};\n"
    "elems['ownerInput'] = Object.assign(makeEl('ownerInput'), { value: 'vishvAsa' });\n"
    "elems['stateSelect'] = Object.assign(makeEl('stateSelect'), { value: 'open' });\n"
    "elems['diffModeSelect'] = Object.assign(makeEl('diffModeSelect'), { value: 'word' });\n"
    "elems['normalizeSelect'] = Object.assign(makeEl('normalizeSelect'), { value: 'none' });\n"
    "elems['ignorePunctChk'] = Object.assign(makeEl('ignorePunctChk'), { checked: false });\n"
    "elems['singlePaneChk'] = Object.assign(makeEl('singlePaneChk'), { checked: true });\n"
    "elems['wrapLinesChk'] = Object.assign(makeEl('wrapLinesChk'), { checked: true });\n"
    "elems['changedOnlyChk'] = Object.assign(makeEl('changedOnlyChk'), { checked: false });\n"
    "elems['settingsPane'] = Object.assign(makeEl('settingsPane'), { style: { display: 'none' } });\n"
    "let reloadedCount = 0;\n"
    "global.localStorage = { getItem(k){ return Object.prototype.hasOwnProperty.call(lsStore, k) ? lsStore[k] : null; }, setItem(k, v){ lsStore[k] = String(v); } };\n"
    "global.window = { location: { reload(){ reloadedCount += 1; } }, confirm(){ return true; }, getSelection(){ return { toString(){ return mockSelectionText; } }; } };\n"
    "Object.defineProperty(global, 'navigator', { value: { clipboard: { writeText: async (txt) => { clipboardCopies.push(String(txt)); } } }, configurable: true });\n"
    "const fetched = [];\n"
    f"const samplePr = {json.dumps(sample_pr)};\n"
    "const secondFile = Object.assign({}, samplePr.files[0], { filename: '02.md' });\n"
    "samplePr.files.push(secondFile);\n"
    "global.fetch = async (url, opts) => {\n"
    "  const method = (opts && opts.method) || 'GET';\n"
    "  fetched.push({ url, method, body: opts && opts.body });\n"
    "  return {\n"
    "    ok: true,\n"
    "    json: async () => {\n"
    "      if (method === 'GET' && url.includes('/changelist')) {\n"
    "        return {\n"
    "          sha: 'c1sha123',\n"
    "          commit: { sha: 'c1sha123', short_sha: 'c1sha12', title: 'Initial cleanup', author: 'mrunaldatar', committed_at: '2026-11-26T06:10:00Z', url: '' },\n"
    "          files: [Object.assign({}, samplePr.files[0], { filename: 'changelist_only.md' })],\n"
    "        };\n"
    "      }\n"
    "      if (method === 'GET' && url.includes('/file_full')) {\n"
    "        const fullRows = [];\n"
    "        for (let ln = 1; ln <= 40; ln++) {\n"
    "          fullRows.push({ kind: ln === 35 ? 'modify' : 'context', left_num: ln, right_num: ln, left_text: 'ctx line ' + ln, right_text: ln === 35 ? 'changed 35' : 'ctx line ' + ln, in_diff_hunk: ln >= 32, lhs_segments: [{ op: 'eq', text: 'ctx line ' + ln }], rhs_segments: [{ op: 'eq', text: ln === 35 ? 'changed 35' : 'ctx line ' + ln }], segments: [{ op: 'eq', text: ln === 35 ? 'changed 35' : 'ctx line ' + ln }] });\n"
    "        }\n"
    "        return { file: { filename: '01.md', status: 'modified', additions: 1, deletions: 1, is_binary: false, is_full_file: true, hunks: [{ header: '@@ -1,40 +1,40 @@ (Full File)', old_start: 1, old_count: 40, new_start: 1, new_count: 40, rows: fullRows, inline_rows: fullRows }] } };\n"
    "      }\n"
    "      if (method === 'GET' && url.startsWith('/api/pr/')) return samplePr;\n"
    "      if (url.endsWith('/state') && opts && opts.body) {\n"
    "        const parsed = JSON.parse(opts.body);\n"
    "        samplePr.summary.state = parsed.state;\n"
    "      } else if (url.endsWith('/merge')) {\n"
    "        samplePr.summary.state = 'closed';\n"
    "        samplePr.summary.merged = true;\n"
    "        samplePr.merged = true;\n"
    "      } else if (method === 'PATCH' && url.includes('/draft_comment/') && opts && opts.body) {\n"
    "        const parsed = JSON.parse(opts.body);\n"
    "        const did = url.split('/draft_comment/')[1];\n"
    "        const drafts = (activePrData && activePrData.pending_drafts) || [];\n"
    "        for (const d of drafts) { if (String(d.draft_id) === String(did)) d.body = parsed.body; }\n"
    "        return { draft: drafts.find(d => String(d.draft_id) === String(did)) || null, pending_drafts: drafts };\n"
    "      } else if (method === 'PATCH' && url.includes('/review_comment/') && opts && opts.body) {\n"
    "        const parsed = JSON.parse(opts.body);\n"
    "        const cid = Number(url.split('/review_comment/')[1]);\n"
    "        for (const t of (samplePr.review_threads || [])) {\n"
    "          for (const c of (t.comments || [])) { if (Number(c.id) === cid) c.body = parsed.body; }\n"
    "        }\n"
    "      }\n"
    "      return {\n"
    "        default_owner: 'vishvAsa', default_state: 'open', default_repo: '', has_token: true,\n"
    "        auth_user: { login: 'vvasuki' }, owner: 'vishvAsa', state: 'open',\n"
    "        repos: ['mahAbhAratam'], prs: [samplePr.summary], active_pr: samplePr,\n"
    "        pr: samplePr, draft: { draft_id: 'd1' }, pending_drafts: [],\n"
    "      };\n"
    "    },\n"
    "  };\n"
    "};\n"
    + js.replace("init();", "")
    + "\n(async () => {\n"
    "  await init();\n"
    "  const html = elems['filesDiffContainer'].innerHTML;\n"
    "  if (!html.includes('diff-table-single') || !html.includes('class=\"ch-del ch-has-sub\"') || !html.includes('class=\"ch-ins ch-has-sub\"') || !html.includes('class=\"ch-sub\"') || html.includes('class=\"ch-ws\"')) {\n"
    "    throw new Error('Default single-pane word diff / intra-word sub-highlight / space highlight removal check failed: ' + html);\n"
    "  }\n"
    "  if (html.includes('प्रणयोऽस्येति<')) {\n"
    "    throw new Error('Pure-deletion [ZWSP] repeated unchanged text instead of highlighting only deleted span: ' + html);\n"
    "  }\n"
    "  if (!html.includes('Reply</button>') || !html.split('Reply</button>')[1].split('</div>')[0].includes('Resolve</button>')) {\n"
    "    throw new Error('Resolve button is not placed next to Reply button: ' + html);\n"
    "  }\n"
    "  const quoteDelSpan = renderChangedText('> ', 'del');\n"
    "  const trimmedSubSpan = renderChangedText(' newword ', 'ins', [{ op: 'eq', text: ' ' }, { op: 'ins', text: 'newword' }, { op: 'eq', text: ' ' }]);\n"
    "  if (!html.includes('[ZWSP]') || !html.includes('class=\"ch-inv\"') || !quoteDelSpan.includes('>&gt; </span>') || !trimmedSubSpan.startsWith(' <span class=\"ch-ins ch-has-sub\"') || !trimmedSubSpan.includes('newword</span></span> ')) {\n"
    "    throw new Error('ch-inv / deleted-space highlight / sub-eq trimming check failed: ' + quoteDelSpan + ' | ' + trimmedSubSpan);\n"
    "  }\n"
    "  // Toggle to 2-Pane LHS/RHS with Alt+S (NOT Alt+P) and back to Single pane\n"
    "  listeners['keydown']({ key: 'p', code: 'KeyP', altKey: true, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (elems['filesDiffContainer'].innerHTML.includes('lhs-cell')) {\n"
    "    throw new Error('Alt+P unexpectedly toggled 2-pane mode');\n"
    "  }\n"
    "  listeners['keydown']({ key: 's', code: 'KeyS', altKey: true, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  const twoPaneHtml = elems['filesDiffContainer'].innerHTML;\n"
    "  if (twoPaneHtml.includes('diff-table-single') || !twoPaneHtml.includes('lhs-cell') || !twoPaneHtml.includes('rhs-cell') || twoPaneHtml.includes('class=\"ch-ws\"')) {\n"
    "    throw new Error('Alt+S 2-pane switch failed: ' + twoPaneHtml);\n"
    "  }\n"
    "  listeners['keydown']({ key: 's', code: 'KeyS', altKey: true, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!elems['filesDiffContainer'].innerHTML.includes('diff-table-single')) throw new Error('Alt+S did not switch back to single pane');\n"
    "  if (!html.includes('<img src=\"https://github.com/user-attachments/assets/abc?x=1&amp;y=2&amp;k=`v`\"') || html.includes('<script>')) {\n"
    "    throw new Error('Image attachment or script escaping failed: ' + html);\n"
    "  }\n"
    "  if (esc(\"a'b\") !== 'a&#39;b') throw new Error('esc did not escape single quote');\n"
    "  // Reload UI button\n"
    "  reloadUi();\n"
    "  if (reloadedCount !== 1) throw new Error('reloadUi did not call window.location.reload()');\n"
    "  // Changed lines only still preserves context rows when a thread or composer is attached\n"
    "  samplePr.review_threads[0].line = 3;\n"
    "  elems['changedOnlyChk'].checked = true;\n"
    "  renderActivePrFiles();\n"
    "  if (!elems['filesDiffContainer'].innerHTML.includes('context line')) {\n"
    "    throw new Error('Changed lines only dropped context line with active thread');\n"
    "  }\n"
    "  elems['changedOnlyChk'].checked = false;\n"
    "  renderActivePrFiles();\n"
    "  // j/k and ArrowLeft/ArrowRight file navigation + collapse/expand\n"
    "  if (focusedFileIdx !== 0) throw new Error('Expected initial focusedFileIdx 0');\n"
    "  listeners['keydown']({ key: 'j', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (focusedFileIdx !== 1) throw new Error('j did not advance focusedFileIdx');\n"
    "  listeners['keydown']({ key: 'ArrowLeft', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!collapsedFiles['02.md']) throw new Error('ArrowLeft did not collapse focused file');\n"
    "  listeners['keydown']({ key: 'ArrowRight', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (collapsedFiles['02.md']) throw new Error('ArrowRight did not expand focused file');\n"
    "  // Alt+1 / Alt+2 / Alt+3 + Ctrl+Enter review action shortcuts\n"
    "  listeners['keydown']({ key: '1', code: 'Digit1', altKey: true, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (selectedReviewAction !== 'APPROVE') throw new Error('Alt+1 did not select APPROVE');\n"
    "  listeners['keydown']({ key: '2', code: 'Digit2', altKey: true, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (selectedReviewAction !== 'REQUEST_CHANGES') throw new Error('Alt+2 did not select REQUEST_CHANGES');\n"
    "  listeners['keydown']({ key: '3', code: 'Digit3', altKey: true, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!elems['prReviewBody'].focused) throw new Error('Alt+3 did not focus prReviewBody');\n"
    "  elems['prReviewBody'].value = 'Please check sandhi';\n"
    "  listeners['keydown']({ key: 'Enter', altKey: false, ctrlKey: true, metaKey: false, shiftKey: false, target: { id: 'prReviewBody', tagName: 'TEXTAREA' }, preventDefault(){} });\n"
    "  await new Promise(r => setTimeout(r, 20));\n"
    "  const revCall = fetched.find(c => c.url.endsWith('/review'));\n"
    "  if (!revCall || !revCall.body.includes('\"event\":\"REQUEST_CHANGES\"')) throw new Error('Ctrl+Enter did not submit selected review');\n"
    "  // Alt+C / Alt+M / Alt+L / Alt+O state filter shortcuts\n"
    "  for (const [key, code, exp] of [['c', 'KeyC', 'closed'], ['m', 'KeyM', 'merged'], ['l', 'KeyL', 'all'], ['o', 'KeyO', 'open']]) {\n"
    "    listeners['keydown']({ key, code, altKey: true, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "    await new Promise(r => setTimeout(r, 10));\n"
    "    if (elems['stateSelect'].value !== exp) throw new Error('Alt+' + key + ' failed state filter');\n"
    "  }\n"
    "  // Open inline composer on row (left=1, right=2), verify side toggle updates target line\n"
    "  startInlineComposer(0, 2, 'RIGHT', 1, 2);\n"
    "  if (!elems['filesDiffContainer'].innerHTML.includes('id=\"compBody\"')) throw new Error('Composer did not render');\n"
    "  onComposerSideChange('LEFT');\n"
    "  if (openComposer.line !== 1 || openComposer.side !== 'LEFT') throw new Error('Side switch did not update line to leftLine');\n"
    "  elems['compBody'].value = 'Check LHS';\n"
    "  elems['compSide'] = Object.assign(makeEl('compSide'), { value: 'LEFT' });\n"
    "  await postInlineComposer();\n"
    "  const postCall = fetched.find(c => c.url.includes('/inline_comment'));\n"
    "  if (!postCall || !postCall.body.includes('\"line\":1') || !postCall.body.includes('\"side\":\"LEFT\"')) {\n"
    "    throw new Error('Inline comment POST payload wrong: ' + JSON.stringify(postCall));\n"
    "  }\n"
    "  // Open active PR in main browser, sidebar browser button, mergeability & conflicts, close & merge + shortcuts checks\n"
    "  if (!elems['prContainer'].innerHTML.includes('id=\"openPrBrowserBtn\"') || !elems['prContainer'].innerHTML.includes('id=\"closePrBtn\"') || !elems['prContainer'].innerHTML.includes('id=\"mergePrBtn\"')) {\n"
    "    throw new Error('Missing openPrBrowserBtn, closePrBtn, or mergePrBtn in PR header');\n"
    "  }\n"
    "  if (!elems['prContainer'].innerHTML.includes('id=\"mergeabilityBox\"') || !elems['prList'].innerHTML.includes('openPrInBrowserIdx(0)')) {\n"
    "    throw new Error('Missing mergeability banner or sidebar open-in-browser button');\n"
    "  }\n"
    "  await openPrInBrowserIdx(0);\n"
    "  listeners['keydown']({ key: 'b', code: 'KeyB', altKey: true, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  await new Promise(r => setTimeout(r, 15));\n"
    "  const brCalls = fetched.filter(c => c.url === '/api/open_in_browser');\n"
    "  if (brCalls.length < 2 || !brCalls[0].body.includes('https://github.com/vishvAsa/mahAbhAratam/pull/192')) {\n"
    "    throw new Error('openPrInBrowserIdx / Alt+B did not post PR URL: ' + JSON.stringify(brCalls));\n"
    "  }\n"
    "  // Dirty/conflicting PR renders conflict hunks and prevents merge\n"
    "  samplePr.mergeable = false;\n"
    "  samplePr.mergeable_state = 'dirty';\n"
    "  samplePr.ahead_by = 2;\n"
    "  samplePr.behind_by = 4;\n"
    "  samplePr.conflicts = [{ filename: '01.md', reason: 'content_conflict', hunks: [{ base_text: 'a', head_text: 'b', rows: [{ kind: 'modify', left_num: 1, right_num: 1, left_text: 'a', right_text: 'b', lhs_segments: [{ op: 'del', text: 'a' }], rhs_segments: [{ op: 'ins', text: 'b' }] }] }] }];\n"
    "  renderActivePr();\n"
    "  if (!elems['prContainer'].innerHTML.includes('Overlapping edits') || !elems['filesDiffContainer'].innerHTML.includes('⚠ Overlapping edits')) {\n"
    "    throw new Error('Conflict UI did not render');\n"
    "  }\n"
    "  // Reset to clean open PR, test Alt+Shift+M merge & Alt+Shift+C close/reopen\n"
    "  samplePr.mergeable = true;\n"
    "  samplePr.mergeable_state = 'clean';\n"
    "  samplePr.conflicts = [];\n"
    "  renderActivePr();\n"
    "  const origSummaryState = samplePr.summary.state;\n"
    "  listeners['keydown']({ key: 'C', code: 'KeyC', altKey: true, ctrlKey: false, metaKey: false, shiftKey: true, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  await new Promise(r => setTimeout(r, 20));\n"
    "  const closeCall = fetched.find(c => c.url.endsWith('/state'));\n"
    "  if (!closeCall || !closeCall.body.includes('\"state\":\"closed\"') || !elems['prContainer'].innerHTML.includes('id=\"reopenPrBtn\"')) {\n"
    "    throw new Error('Alt+Shift+C close did not update state/UI');\n"
    "  }\n"
    "  samplePr.summary.state = origSummaryState;\n"
    "  samplePr.summary.merged = false;\n"
    "  samplePr.merged = false;\n"
    "  renderActivePr();\n"
    "  elems['mergeMethodSelect'] = Object.assign(makeEl('mergeMethodSelect'), { value: 'squash' });\n"
    "  listeners['keydown']({ key: 'M', code: 'KeyM', altKey: true, ctrlKey: false, metaKey: false, shiftKey: true, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  await new Promise(r => setTimeout(r, 20));\n"
    "  const mergeCall = fetched.find(c => c.url.endsWith('/merge'));\n"
    "  if (!mergeCall || !mergeCall.body.includes('\"merge_method\":\"squash\"') || !elems['prContainer'].innerHTML.includes('✓ Merged')) {\n"
    "    throw new Error('Alt+Shift+M did not submit squash merge: ' + JSON.stringify(mergeCall));\n"
    "  }\n"
    "  samplePr.summary.state = origSummaryState;\n"
    "  samplePr.summary.merged = false;\n"
    "  samplePr.merged = false;\n"
    "  // Ctrl+R triggers refreshAll\n"
    "  listeners['keydown']({ key: 'r', ctrlKey: true, altKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  await new Promise(r => setTimeout(r, 20));\n"
    "  if (!fetched.some(c => c.url === '/api/refresh')) throw new Error('Missing /api/refresh call on Ctrl+R');\n"
    "  // Changing normalize / ignore_punct triggers PR reload with new query params\n"
    "  elems['normalizeSelect'].value = 'nfc';\n"
    "  elems['ignorePunctChk'].checked = true;\n"
    "  await changeDiffMode();\n"
    "  if (!fetched.some(c => c.url.includes('normalize=nfc') && c.url.includes('ignore_punct=true'))) {\n"
    "    throw new Error('normalize / ignore_punct query params missing: ' + JSON.stringify(fetched));\n"
    "  }\n"
    "  // Resolved threads collapse in <details class=\"thread-box resolved\"> and outdated/nearby threads attach near their line\n"
    "  samplePr.review_threads.push({\n"
    "    id: 'PRRT_res_outdated', path: '01.md', line: null, original_line: 10, side: 'RIGHT', is_resolved: true, is_outdated: true,\n"
    "    comments: [{ id: 2002, author: 'vvasuki', body: 'शोधितम्', created_at: '2026-11-26T02:00:00Z' }],\n"
    "  });\n"
    "  samplePr.review_threads.push({\n"
    "    id: 'PRRT_far', path: '01.md', line: 500, original_line: 500, side: 'RIGHT', is_resolved: true, is_outdated: false,\n"
    "    comments: [{ id: 2003, author: 'vvasuki', body: 'दूरस्थम्', created_at: '2026-11-26T02:00:00Z' }],\n"
    "  });\n"
    "  renderActivePrFiles();\n"
    "  const thHtml = elems['filesDiffContainer'].innerHTML;\n"
    "  if (!thHtml.includes('<details class=\"thread-box resolved\"') || thHtml.includes('<details class=\"thread-box resolved\" data-thread-id=\"PRRT_res_outdated\" open')) {\n"
    "    throw new Error('Resolved thread did not collapse by default: ' + thHtml);\n"
    "  }\n"
    "  if (!thHtml.includes('near L3') && !thHtml.includes('shown at L')) {\n"
    "    throw new Error('Nearby outdated thread did not anchor near hunk line: ' + thHtml);\n"
    "  }\n"
    "  if (!thHtml.includes('Threads Outside Diff Hunks')) {\n"
    "    throw new Error('Genuinely distant thread should stay in Threads Outside Diff Hunks: ' + thHtml);\n"
    "  }\n"
    "  // Even without server sub_segments, word diff JS renders intra-word char highlights (पा० -> पा॰ highlights ० and ॰)\n"
    "  const paHtml = renderDiffSegments([{ op: 'del', text: 'पा०' }, { op: 'ins', text: 'पा॰' }]);\n"
    "  if (!paHtml.includes('<span class=\"ch-sub\">०</span>') || !paHtml.includes('<span class=\"ch-sub\">॰</span>')) {\n"
    "    throw new Error('Missing intra-word ० / ॰ sub-highlights: ' + paHtml);\n"
    "  }\n"
    "  // Changelist dropdown renders default 'All changelists' + commits with date/time, and switches files cleanly\n"
    "  samplePr.commits = [\n"
    "    { sha: 'c1sha123', short_sha: 'c1sha12', title: 'Initial cleanup', message: 'Initial cleanup', author: 'mrunaldatar', committed_at: '2026-11-26T06:10:00Z', url: '', parents: [] },\n"
    "  ];\n"
    "  renderActivePr();\n"
    "  const prHtmlWithCl = elems['prContainer'].innerHTML;\n"
    "  if (!prHtmlWithCl.includes('id=\"changelistSelect\"') || !prHtmlWithCl.includes('All changelists (1 commit') || !prHtmlWithCl.includes('2026-11-26 06:10')) {\n"
    "    throw new Error('Changelist dropdown missing default or date/time: ' + prHtmlWithCl);\n"
    "  }\n"
    "  await selectChangelist('c1sha123');\n"
    "  if (!fetched.some(c => c.url.includes('/changelist?sha=c1sha123')) || !elems['filesDiffContainer'].innerHTML.includes('changelist_only.md')) {\n"
    "    throw new Error('selectChangelist did not fetch or render changelist files');\n"
    "  }\n"
    "  await selectChangelist('');\n"
    "  if (selectedChangelistSha !== '' || elems['filesDiffContainer'].innerHTML.includes('changelist_only.md')) {\n"
    "    throw new Error('Resetting changelist dropdown to All did not restore PR files');\n"
    "  }\n"
    "  // Settings pane renders color pickers, applies custom colors to CSS vars & localStorage, resets cleanly, and closes on Escape\n"
    "  toggleSettingsPane();\n"
    "  if (!isSettingsPaneOpen() || !elems['settingsPane'].innerHTML.includes('type=\"color\"') || !elems['settingsPane'].innerHTML.includes('colorRedChar')) {\n"
    "    throw new Error('Settings pane did not render color pickers');\n"
    "  }\n"
    "  setDiffColor('--red-char', '#fff0f5');\n"
    "  if (cssProps['--red-char'] !== '#fff0f5' || !lsStore[DIFF_COLOR_STORAGE_KEY].includes('#fff0f5')) {\n"
    "    throw new Error('setDiffColor did not apply CSS var or persist');\n"
    "  }\n"
    "  resetDiffColors();\n"
    "  if (cssProps['--red-char'] !== '#ffeded') {\n"
    "    throw new Error('resetDiffColors did not restore lighter deleted pink default');\n"
    "  }\n"
    "  listeners['keydown']({ key: 'Escape', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (isSettingsPaneOpen()) throw new Error('Escape did not close settings pane');\n"
    "  // Normal click / drag does not select row or open composer; Shift+Click selects row; Ctrl+Click opens composer\n"
    "  selectedDiffRowKey = null; openComposer = null;\n"
    "  handleDiffRowCtrlClick({ ctrlKey: false, altKey: false, metaKey: false, shiftKey: false, target: {} }, 0, 1, 2, true, '01.md:0:0');\n"
    "  if (openComposer !== null || selectedDiffRowKey !== null) throw new Error('Normal click unexpectedly opened composer or selected row');\n"
    "  handleDiffRowCtrlClick({ ctrlKey: false, altKey: false, metaKey: false, shiftKey: true, target: {}, preventDefault(){} }, 0, 1, 2, true, '01.md:0:0');\n"
    "  if (openComposer !== null || selectedDiffRowKey !== '01.md:0:0') throw new Error('Shift+Click did not select row cleanly');\n"
    "  selectedDiffRowKey = null;\n"
    "  // Multiline reply textarea allows Shift+Enter newlines and submits via Ctrl+Enter without triggering PR review/composer\n"
    "  if (!html.includes('<textarea id=\"replyInput_0\" class=\"reply-input\"') || !html.includes(\"event.key==='Enter'&&!event.shiftKey\")) {\n"
    "    throw new Error('Reply box is not a multiline textarea supporting Shift+Enter: ' + html);\n"
    "  }\n"
    "  elems['replyInput_0'] = Object.assign(makeEl('replyInput_0'), { value: 'Line 1\\nLine 2' });\n"
    "  const beforeReplyCount = fetched.filter(c => c.url.endsWith('/reply')).length;\n"
    "  listeners['keydown']({ key: 'Enter', altKey: false, ctrlKey: true, metaKey: false, shiftKey: false, target: { id: 'replyInput_0', tagName: 'TEXTAREA' }, preventDefault(){} });\n"
    "  await new Promise(r => setTimeout(r, 20));\n"
    "  const replyCall = fetched.filter(c => c.url.endsWith('/reply')).slice(beforeReplyCount)[0];\n"
    "  if (!replyCall || !replyCall.body.includes('Line 1\\\\nLine 2')) {\n"
    "    throw new Error('Ctrl+Enter inside multiline replyInput_0 did not submit multiline reply: ' + JSON.stringify(replyCall));\n"
    "  }\n"
    "  openComposer = null;\n"
    "  handleDiffRowCtrlClick({ ctrlKey: true, altKey: false, metaKey: false, shiftKey: false, target: { closest(sel){ return sel.includes('.ch-del') ? {} : null; } }, preventDefault(){}, stopPropagation(){} }, 0, 1, 2, true);\n"
    "  if (!openComposer || openComposer.side !== 'LEFT' || openComposer.line !== 1) throw new Error('Ctrl+Click on del did not open LEFT composer');\n"
    "  openComposer = null;\n"
    "  handleDiffRowCtrlClick({ ctrlKey: true, altKey: false, metaKey: false, shiftKey: false, target: { closest(){ return null; } }, preventDefault(){}, stopPropagation(){} }, 0, 1, 2, true);\n"
    "  if (!openComposer || openComposer.side !== 'RIGHT' || openComposer.line !== 2) throw new Error('Ctrl+Click did not open RIGHT composer');\n"
    "  openComposer = null;\n"
    "  // Partial PR hunk renders full-row clickable gap bar (no extra buttons) & expands cleanly\n"
    "  samplePr.files[0].hunks[0].old_start = 32; samplePr.files[0].hunks[0].new_start = 32; samplePr.files[0].hunks[0].old_count = 6; samplePr.files[0].hunks[0].new_count = 6;\n"
    "  renderActivePrFiles();\n"
    "  const gapBarHtml = elems['filesDiffContainer'].innerHTML;\n"
    "  if (!gapBarHtml.includes('diff-gap-row') || !gapBarHtml.includes('expandPrHunkGap(0, \\'above\\'') || gapBarHtml.includes('diff-gap-btn') || gapBarHtml.includes('act-cell')) {\n"
    "    throw new Error('Gap bar extra buttons or act-cell present: ' + gapBarHtml);\n"
    "  }\n"
    "  await expandPrHunkGap(0, 'above', 0, 'all', 0);\n"
    "  const expandedHtml = elems['filesDiffContainer'].innerHTML;\n"
    "  if (!expandedHtml.includes('ctx line 12') || !expandedHtml.includes('ctx line 1') || expandedHtml.includes('expandPrHunkGap(0, \\'above\\'')) {\n"
    "    throw new Error('Expanding gap bar above PR hunk did not reveal lines cleanly: ' + expandedHtml);\n"
    "  }\n"
    "  // Line navigation (ArrowDown/ArrowUp select lines, Alt+ArrowDown/Alt+ArrowUp jump diff lines, Ctrl+Enter comments on selected line)\n"
    "  selectedDiffRowKey = null; openComposer = null; prPartialFullMode = {}; prExpandedHunkWindows = {};\n"
    "  const savedHunk0 = samplePr.files[0].hunks[0];\n"
    "  samplePr.files[0].is_full_file = false;\n"
    "  samplePr.files[0].hunks[0] = Object.assign({}, savedHunk0, { old_start: 1, new_start: 1, rows: savedHunk0.rows.filter(r => r.left_num <= 2 || r.kind !== 'context') });\n"
    "  // Jump to modify row with Alt+ArrowDown, verify ArrowDown/ArrowUp & Alt+ArrowUp, then Ctrl+Enter on selected line\n"
    "  renderActivePrFiles();\n"
    "  listeners['keydown']({ key: 'ArrowDown', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!selectedDiffRowKey) throw new Error('ArrowDown did not select first line');\n"
    "  listeners['keydown']({ key: 'ArrowDown', altKey: true, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  const modKey = selectedDiffRowKey;\n"
    "  const modMeta = renderedDiffRows.find(r => r.rowKey === modKey);\n"
    "  if (!modMeta || modMeta.kind === 'context') throw new Error('Alt+ArrowDown did not land on a diff line: ' + JSON.stringify(modMeta));\n"
    "  listeners['keydown']({ key: 'ArrowDown', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (selectedDiffRowKey === modKey) throw new Error('ArrowDown did not advance past diff line');\n"
    "  listeners['keydown']({ key: 'ArrowUp', altKey: true, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (selectedDiffRowKey !== modKey) throw new Error('Alt+ArrowUp did not jump back to prev diff line');\n"
    "  listeners['keydown']({ key: 'Enter', altKey: false, ctrlKey: true, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!openComposer || openComposer.line !== (modMeta.rightNum ?? modMeta.leftNum)) {\n"
    "    throw new Error('Ctrl+Enter did not open inline comment composer on selected line: ' + JSON.stringify(openComposer));\n"
    "  }\n"
    "  openComposer = null;\n"
    "  // Interword sandhi highlighting is ON by default (including ऽस्मिन्न् इति— vs ऽस्मिन् इति—),\n"
    "  // OLD+NEW pairs wrap in a single .ch-pair box, and Alt+Shift+Up/Down jumps across .ch-sandhi rows\n"
    "  if (!isSandhiHighlightEnabled()) throw new Error('Expected Sandhi diff highlight to be active by default');\n"
    "  const sandhiRendered = renderDiffSegments([{ op: 'del', text: 'अस्ति इति' }, { op: 'ins', text: 'अस्तीति' }, { op: 'eq', text: ' ' }, { op: 'del', text: 'पा०' }, { op: 'ins', text: 'पा॰' }], 'word');\n"
    "  if (!sandhiRendered.includes('ch-pair ch-sandhi') || !sandhiRendered.includes('पा<span class=\"ch-sub\">०</span>') || sandhiRendered.split('ch-sandhi').length !== 4 || sandhiRendered.split('<span class=\"ch-pair').length !== 3) {\n"
    "    throw new Error('Sandhi / single dashed box check failed: ' + sandhiRendered);\n"
    "  }\n"
    "  const bhSandhiHtml = renderDiffSegments([{ op: 'eq', text: 'अतो व्युत्थित-चित्तैः निष्ठीयते शुभाश्रय-भूते ' }, { op: 'del', text: 'ऽस्मिन्न्' }, { op: 'ins', text: 'ऽस्मिन्' }, { op: 'eq', text: ' इति—' }], 'word');\n"
    "  if (!bhSandhiHtml.includes('ch-pair ch-sandhi')) throw new Error('ऽस्मिन्न् इति— sandhi failed to highlight: ' + bhSandhiHtml);\n"
    "  const bugText = buildRowBugReportDiffText({ filename: 'test.md', leftNum: 5, rightNum: 5, leftText: 'OLD line', rightText: 'NEW line' }, 'pull_requests_san');\n"
    "  if (!bugText.includes('test.md') || !bugText.includes('OLD line') || !bugText.includes('NEW line')) throw new Error('buildRowBugReportDiffText malformed: ' + bugText);\n"
    "  setSandhiHighlightEnabled(false, false);\n"
    "  // PageDown / PageUp scrolls #mainPane and moves selected line to the first readable visible line\n"
    "  elems['mainPane'] = Object.assign(makeEl('mainPane'), {\n"
    "    scrollTop: 0, clientHeight: 200, scrollHeight: 800,\n"
    "    getBoundingClientRect(){ return { top: 80, bottom: 280, height: 200, width: 800 }; },\n"
    "  });\n"
    "  samplePr.files[0].hunks[0] = savedHunk0;\n"
    "  renderActivePrFiles();\n"
    "  const r0 = renderedDiffRows[0], r1 = renderedDiffRows[1], r2 = renderedDiffRows[2];\n"
    "  elems[r0.domId] = Object.assign(makeEl(r0.domId), { getBoundingClientRect(){ const st = elems['mainPane'].scrollTop; return { top: 80 - st, bottom: 120 - st, height: 40, width: 600 }; } });\n"
    "  elems[r1.domId] = Object.assign(makeEl(r1.domId), { getBoundingClientRect(){ const st = elems['mainPane'].scrollTop; return { top: 245 - st, bottom: 285 - st, height: 40, width: 600 }; } });\n"
    "  elems[r2.domId] = Object.assign(makeEl(r2.domId), { getBoundingClientRect(){ const st = elems['mainPane'].scrollTop; return { top: 285 - st, bottom: 325 - st, height: 40, width: 600 }; } });\n"
    "  listeners['keydown']({ key: 'PageDown', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (elems['mainPane'].scrollTop !== 175 || selectedDiffRowKey !== r1.rowKey) {\n"
    "    throw new Error('PageDown did not scroll mainPane and select first visible row: scrollTop=' + elems['mainPane'].scrollTop + ' selected=' + selectedDiffRowKey + ' expected=' + r1.rowKey);\n"
    "  }\n"
    "  listeners['keydown']({ key: 'PageUp', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (elems['mainPane'].scrollTop !== 0 || selectedDiffRowKey !== r0.rowKey) {\n"
    "    throw new Error('PageUp did not scroll up and select top visible row: scrollTop=' + elems['mainPane'].scrollTop + ' selected=' + selectedDiffRowKey);\n"
    "  }\n"
    "  // Tab steps diff tokens inside selected row & across rows; Ctrl+Space marks checked-until-here\n"
    "  const mkTok = (cls) => ({ classList: makeClassList(), parentElement: { closest(){ return null; } }, scrollIntoView(){}, _cls: cls });\n"
    "  const r1Toks = [mkTok('ch-pair'), mkTok('ch-pair')];\n"
    "  const r2Toks = [mkTok('ch-pair')];\n"
    "  elems[r0.domId].querySelectorAll = () => [];\n"
    "  elems[r1.domId].querySelectorAll = () => r1Toks;\n"
    "  elems[r2.domId].querySelectorAll = () => r2Toks;\n"
    "  selectDiffRow(r0.rowKey, { scroll: false });\n"
    "  listeners['keydown']({ key: 'Tab', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (selectedDiffRowKey !== r1.rowKey || !r1Toks[0].classList.contains('diff-focus-cur')) throw new Error('Tab from r0 did not focus r1 first diff');\n"
    "  listeners['keydown']({ key: 'Tab', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!r1Toks[1].classList.contains('diff-focus-cur') || r1Toks[0].classList.contains('diff-focus-cur')) throw new Error('2nd Tab did not advance within r1');\n"
    "  listeners['keydown']({ key: 'Space', code: 'Space', altKey: false, ctrlKey: true, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!elems[r1.domId].classList.contains('diff-checked-until') || !r1Toks[1].classList.contains('diff-checked-token') || !elems['checkedUntilPill'].innerHTML.includes('Checked:')) {\n"
    "    throw new Error('Ctrl+Space did not set checked-until marker on r1');\n"
    "  }\n"
    "  listeners['keydown']({ key: 'Tab', altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (selectedDiffRowKey !== r2.rowKey || !r2Toks[0].classList.contains('diff-focus-cur')) throw new Error('3rd Tab did not advance to r2');\n"
    "  if (!elems[r1.domId].classList.contains('diff-checked-until') || !r1Toks[1].classList.contains('diff-checked-token')) throw new Error('Checked marker disappeared after Tab');\n"
    "  listeners['keydown']({ key: 'Tab', altKey: false, ctrlKey: false, metaKey: false, shiftKey: true, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (selectedDiffRowKey !== r1.rowKey || !r1Toks[1].classList.contains('diff-focus-cur')) throw new Error('Shift+Tab did not return to r1 tok 1');\n"
    "  // Ctrl+Enter on newly selected line replaces stale un-focused composer; Ctrl+Enter inside compBody posts once\n"
    "  openComposer = { filename: '01.md', line: 1, side: 'RIGHT', body: '' };\n"
    "  r1.inDiffHunk = true;\n"
    "  listeners['keydown']({ key: 'Enter', altKey: false, ctrlKey: true, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!openComposer || openComposer.line !== (r1.rightNum ?? r1.leftNum)) throw new Error('Ctrl+Enter did not open review comment composer on selected r1');\n"
    "  // Pending review draft renders inline with 'Pending' badge & Submit Pending Review buttons\n"
    "  activePrData.pending_drafts = [{ draft_id: 'draft_99', path: '01.md', line: r1.rightNum ?? 2, side: 'RIGHT', body: 'Pending check note' }];\n"
    "  renderActivePr();\n"
    "  const filesHtml = elems['filesDiffContainer'].innerHTML;\n"
    "  const hdrHtml = elems['prContainer'].innerHTML;\n"
    "  if (!filesHtml.includes('pending-draft') || !filesHtml.includes('>Pending</span>') || !filesHtml.includes('Submit Pending Review (1)') || !hdrHtml.includes('id=\"submitPendingReviewHeaderBtn\"') || !hdrHtml.includes('id=\"submitPendingReviewTopBtn\"')) {\n"
    "    throw new Error('Pending review inline card or Submit Pending Review button missing');\n"
    "  }\n"
    "  // _findPreferredDiffFindIdx picks first match in/after active row instead of match 0\n"
    "  const tokBefore = { id: 'tb' }, tokCur = { id: 'tc' }, tokAfter = { id: 'ta' };\n"
    "  const rowBefore = { id: 'rb' }, rowAfter = { id: 'ra' };\n"
    "  const rowCur = {\n"
    "    id: 'rc',\n"
    "    contains(n){ return n === tokCur; },\n"
    "    compareDocumentPosition(n){ return n === tokBefore ? 2 : (n === tokAfter ? 4 : 20); },\n"
    "  };\n"
    "  document.querySelector = (s) => s === 'tr.diff-row-selected' ? rowCur : null;\n"
    "  clearActiveDiffFocus();\n"
    "  const prefIn = _findPreferredDiffFindIdx([\n"
    "    { rowEl: rowBefore, range: { startContainer: tokBefore } },\n"
    "    { rowEl: rowCur, range: { startContainer: tokCur } },\n"
    "    { rowEl: rowAfter, range: { startContainer: tokAfter } },\n"
    "  ]);\n"
    "  if (prefIn !== 1) throw new Error('Expected _findPreferredDiffFindIdx=1 (in current line), got ' + prefIn);\n"
    "  const prefAfter = _findPreferredDiffFindIdx([\n"
    "    { rowEl: rowBefore, range: { startContainer: tokBefore } },\n"
    "    { rowEl: rowAfter, range: { startContainer: tokAfter } },\n"
    "  ]);\n"
    "  if (prefAfter !== 1) throw new Error('Expected _findPreferredDiffFindIdx=1 (after current line), got ' + prefAfter);\n"
    "  // Ctrl+G and Ctrl+Shift+G step Find matches next/prev and select both the row and the diff box containing the match\n"
    "  elems['diffFindBar'] = Object.assign(makeEl('diffFindBar'), { style: { display: 'flex' } });\n"
    "  const findTxtNode1 = { nodeType: 3, parentElement: r1Toks[1] };\n"
    "  r1Toks[1].contains = (n) => n === r1Toks[1] || n === findTxtNode1;\n"
    "  const findTxtNode2 = { nodeType: 3, parentElement: r2Toks[0] };\n"
    "  r2Toks[0].contains = (n) => n === r2Toks[0] || n === findTxtNode2;\n"
    "  selectDiffRow(r0.rowKey, { scroll: false });\n"
    "  _diffFindMatches = [\n"
    "    { visualRange: {}, block: r1Toks[1], rowEl: elems[r1.domId], range: { startContainer: findTxtNode1, endContainer: findTxtNode1 } },\n"
    "    { visualRange: {}, block: r2Toks[0], rowEl: elems[r2.domId], range: { startContainer: findTxtNode2, endContainer: findTxtNode2 } },\n"
    "  ];\n"
    "  _diffFindIdx = -1;\n"
    "  stepDiffFind(1);\n"
    "  if (_diffFindIdx !== 0 || selectedDiffRowKey !== r1.rowKey || !r1Toks[1].classList.contains('diff-focus-cur')) {\n"
    "    throw new Error('Find Next did not select row r1 and box r1Toks[1]: idx=' + _diffFindIdx + ' sel=' + selectedDiffRowKey);\n"
    "  }\n"
    "  listeners['keydown']({ key: 'g', ctrlKey: true, altKey: false, metaKey: false, shiftKey: false, target: { tagName: 'INPUT' }, preventDefault(){} });\n"
    "  if (_diffFindIdx !== 1 || selectedDiffRowKey !== r2.rowKey || !r2Toks[0].classList.contains('diff-focus-cur') || r1Toks[1].classList.contains('diff-focus-cur')) {\n"
    "    throw new Error('Ctrl+G did not select row r2 and box r2Toks[0]: idx=' + _diffFindIdx + ' sel=' + selectedDiffRowKey);\n"
    "  }\n"
    "  listeners['keydown']({ key: 'G', ctrlKey: true, altKey: false, metaKey: false, shiftKey: true, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (_diffFindIdx !== 0 || selectedDiffRowKey !== r1.rowKey || !r1Toks[1].classList.contains('diff-focus-cur')) {\n"
    "    throw new Error('Ctrl+Shift+G did not select row r1 and box r1Toks[1]: idx=' + _diffFindIdx + ' sel=' + selectedDiffRowKey);\n"
    "  }\n"
    "  // Ctrl+D and Ctrl+Shift+D cycle through PR comments in DOM order without duplicating nested comments or auto-expanding resolved threads\n"
    "  const mkCommentBox = (extraCls = [], isOpen = false, parentBox = null) => {\n"
    "    const cl = makeClassList();\n"
    "    for (const c of extraCls) cl.add(c);\n"
    "    return { classList: cl, open: isOpen, scrolled: 0, scrollIntoView(){ this.scrolled += 1; }, closest(sel){ return sel === '.thread-box' ? parentBox : null; } };\n"
    "  };\n"
    "  const cConv = mkCommentBox(['thread-comment']);\n"
    "  const cOpen = mkCommentBox(['thread-box']);\n"
    "  const cNestedChild = mkCommentBox(['thread-comment'], false, cOpen);\n"
    "  const cResolved = mkCommentBox(['thread-box', 'resolved'], false);\n"
    "  const cPending = mkCommentBox(['thread-box', 'pending-draft']);\n"
    "  const prevPrQsa = elems['prContainer'].querySelectorAll;\n"
    "  elems['prContainer'].querySelectorAll = (s) => s.includes('.thread-box') ? [cConv, cOpen, cNestedChild, cResolved, cPending] : [];\n"
    "  clearActivePrCommentHighlight();\n"
    "  selectedDiffRowKey = null;\n"
    "  listeners['keydown']({ key: 'd', ctrlKey: true, altKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!cConv.classList.contains('diff-comment-cur') || cConv.scrolled !== 1) throw new Error('1st Ctrl+D did not select conversation comment');\n"
    "  listeners['keydown']({ key: 'd', ctrlKey: true, altKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!cOpen.classList.contains('diff-comment-cur') || cOpen.scrolled !== 1 || cNestedChild.classList.contains('diff-comment-cur')) {\n"
    "    throw new Error('2nd Ctrl+D did not select open thread-box (without duplicating nested child)');\n"
    "  }\n"
    "  listeners['keydown']({ key: 'd', ctrlKey: true, altKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!cResolved.classList.contains('diff-comment-cur') || cResolved.open !== false) {\n"
    "    throw new Error('3rd Ctrl+D failed or auto-expanded resolved comment: open=' + cResolved.open);\n"
    "  }\n"
    "  listeners['keydown']({ key: 'd', ctrlKey: true, altKey: false, metaKey: false, shiftKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!cPending.classList.contains('diff-comment-cur')) throw new Error('4th Ctrl+D did not select pending draft');\n"
    "  listeners['keydown']({ key: 'D', ctrlKey: true, altKey: false, metaKey: false, shiftKey: true, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  if (!cResolved.classList.contains('diff-comment-cur') || cResolved.open !== false) {\n"
    "    throw new Error('Ctrl+Shift+D did not return to collapsed resolved comment without expanding');\n"
    "  }\n"
    "  elems['prContainer'].querySelectorAll = prevPrQsa;\n"
    "  // Samasa-hyphenated sandhi bug report caught in word, grapheme, codepoint, 2-pane, and across del_nl reflows\n"
    "  setSandhiHighlightEnabled(true, false);\n"
    "  const hypOld = 'अतो व्युत्थित-चित्तैः निष्ठीयते शुभाश्रय-भूते ऽस्मिन्न् इति—  ';\n"
    "  const hypNew = 'अतो व्युत्थित-चित्तैः निष्ठीयते शुभाश्रय-भूतेऽस्मिन् इति—';\n"
    "  const eng = _getDiffSanEngine();\n"
    "  for (const mode of ['word', 'grapheme', 'codepoint']) {\n"
    "    const hk = eng.buildFullFileHunk(hypOld, hypNew, { charDiffMode: mode });\n"
    "    const inlHtml = renderDiffSegments(hk.inline_rows[0].segments, mode);\n"
    "    if (!inlHtml.includes('ch-sandhi')) throw new Error('Inline sandhi missed compound boundary in ' + mode + ': ' + inlHtml);\n"
    "    const tpMarks = eng.findSandhiMarkedIndices(hk.rows[0].lhs_segments, { otherSegments: hk.rows[0].rhs_segments, leftText: hypOld, rightText: hypNew });\n"
    "    if (!tpMarks.size) throw new Error('2-pane sandhi missed compound boundary in ' + mode);\n"
    "  }\n"
    "  const nonSandhiHyp = eng.buildFullFileHunk('शुभाश्रय-भूते', 'शुभाश्रयभूते', { charDiffMode: 'word' });\n"
    "  if (eng.findSandhiMarkedIndices(nonSandhiHyp.inline_rows[0].segments).size !== 0) throw new Error('Plain hyphen removal false-positived as sandhi');\n"
    "  const nlSandhi = eng.buildFullFileHunk('अतो शुभाश्रय-भूते ऽस्मिन्न्\\nइति—', 'अतो शुभाश्रय-भूतेऽस्मिन् इति—', { charDiffMode: 'grapheme' });\n"
    "  const nlMarks = eng.findSandhiMarkedIndices(nlSandhi.inline_rows[0].segments);\n"
    "  if (nlMarks.size < 2) throw new Error('Reflowed del_nl sandhi missed boundary/ngamut marks: ' + JSON.stringify([...nlMarks]));\n"
    "  // Plain single click on a diff box (.ch-pair / .ch-del / .ch-ins) copies OLD vs NEW while preserving Shift+Click & drag selection\n"
    "  const pairHtmlWithAttrs = renderDiffSegments(eng.buildFullFileHunk(hypOld, hypNew, { charDiffMode: 'word' }).inline_rows[0].segments, 'word');\n"
    "  if (!pairHtmlWithAttrs.includes('data-old=\"शुभाश्रय-भूते ऽस्मिन्न्\"') || !pairHtmlWithAttrs.includes('data-new=\"शुभाश्रय-भूतेऽस्मिन्\"')) {\n"
    "    throw new Error('Rendered .ch-pair missing data-old/data-new attributes: ' + pairHtmlWithAttrs);\n"
    "  }\n"
    "  const fakeRow = { contains(el){ return true; }, closest(sel){ return sel.includes('tr.diff-row') ? fakeRow : null; } };\n"
    "  const pairClassList = makeClassList(); pairClassList.add('ch-pair');\n"
    "  const delChild = { nodeType: 1, classList: makeClassList(), childNodes: [{ nodeType: 3, nodeValue: 'ऽस्मिन्न्' }] };\n"
    "  delChild.classList.add('ch-del');\n"
    "  const fakePairEl = {\n"
    "    classList: pairClassList,\n"
    "    getAttribute(k){ return k === 'data-old' ? 'शुभाश्रय-भूते ऽस्मिन्न्' : (k === 'data-new' ? 'शुभाश्रय-भूतेऽस्मिन्' : null); },\n"
    "    childNodes: [delChild],\n"
    "    closest(sel){ if (sel.includes('.ch-pair')) return fakePairEl; if (sel.includes('tr.diff-row')) return fakeRow; return null; },\n"
    "  };\n"
    "  const clickLeaf = { closest(sel){ if (sel.includes('.ch-pair')) return fakePairEl; if (sel.includes('tr.diff-row')) return fakeRow; if (sel.includes('.ch-del')) return delChild; return null; } };\n"
    "  const zwnjSpan = { nodeType: 1, classList: makeClassList(), getAttribute(k){ return k === 'data-ch' ? '\\u200c' : null; }, textContent: '[ZWNJ]' };\n"
    "  const standaloneIns = { nodeType: 1, classList: makeClassList(), childNodes: [{ nodeType: 3, nodeValue: 'इति' }, zwnjSpan] };\n"
    "  standaloneIns.classList.add('ch-ins');\n"
    "  if (extractDiffBoxOldNewText(standaloneIns) !== 'OLD:\\n\\nNEW:\\nइति\\u200c') throw new Error('Standalone diff box format failed: ' + extractDiffBoxOldNewText(standaloneIns));\n"
    "  clipboardCopies.length = 0;\n"
    "  listeners['mousedown']({ button: 0, clientX: 100, clientY: 100, shiftKey: false, ctrlKey: false, altKey: false, metaKey: false, target: clickLeaf });\n"
    "  listeners['click']({ button: 0, clientX: 101, clientY: 100, shiftKey: false, ctrlKey: false, altKey: false, metaKey: false, target: clickLeaf });\n"
    "  await new Promise(r => setTimeout(r, 15));\n"
    "  if (clipboardCopies.length !== 1 || clipboardCopies[0] !== 'OLD:\\nशुभाश्रय-भूते ऽस्मिन्न्\\nNEW:\\nशुभाश्रय-भूतेऽस्मिन्' || !fakePairEl.classList.contains('diff-box-copied')) {\n"
    "    throw new Error('Click on diff box did not copy expected OLD/NEW: ' + JSON.stringify(clipboardCopies));\n"
    "  }\n"
    "  // Shift+Click and drag-selection must NOT copy\n"
    "  mockSelectionText = 'शुभाश्रय';\n"
    "  listeners['mousedown']({ button: 0, clientX: 100, clientY: 100, shiftKey: false, ctrlKey: false, altKey: false, metaKey: false, target: clickLeaf });\n"
    "  listeners['click']({ button: 0, clientX: 100, clientY: 100, shiftKey: false, ctrlKey: false, altKey: false, metaKey: false, target: clickLeaf });\n"
    "  mockSelectionText = '';\n"
    "  listeners['click']({ button: 0, clientX: 100, clientY: 100, shiftKey: true, ctrlKey: false, altKey: false, metaKey: false, target: clickLeaf });\n"
    "  listeners['mousedown']({ button: 0, clientX: 10, clientY: 10, shiftKey: false, ctrlKey: false, altKey: false, metaKey: false, target: clickLeaf });\n"
    "  listeners['click']({ button: 0, clientX: 50, clientY: 10, shiftKey: false, ctrlKey: false, altKey: false, metaKey: false, target: clickLeaf });\n"
    "  await new Promise(r => setTimeout(r, 10));\n"
    "  if (clipboardCopies.length !== 1) throw new Error('Selection/Shift+Click/Drag unexpectedly copied diff box: ' + JSON.stringify(clipboardCopies));\n"
    "  // Deleted trailing space after blockquote '>' highlights cleanly, short intervening context+newlines bridge without breaking long paragraph word diff\n"
    "  const qHunk = eng.buildFullFileHunk('> उच्चलद्-भङ्गाक्रान्त', 'उच्चलद्-भङ्गाक्रान्त  ', { charDiffMode: 'word' });\n"
    "  const qHtml = renderDiffSegments(qHunk.inline_rows[0].segments, 'word');\n"
    "  if (!qHtml.includes('>&gt; </span>उच्चलद्-भङ्गाक्रान्त<span class=\"ch-ins\"')) throw new Error('Deleted space after > was not highlighted: ' + qHtml);\n"
    "  const bridgeOld = 'नित्य-निरपेक्षश् [[P203]] चेत् साध्यांशः कः ? तर्हि पुनः कृत्यांशः कः ? इति चेत् साध्यांशो अस्तीत्य् आह युवानम् इत्यादिना ॥ ४ ॥\\n\\n[^202_10_4_2]: अवाऽऽदरे बहुवचनम् ।\\n\\n</details>\\n';\n"
    "  const bridgeNew = 'नित्य-निरपेक्षश् चेत् साध्यांशः कः ?\\n\\n[^_10_4_1]: अवाऽऽदरे बहुवचनम् ।\\n\\n[[P203]]\\n\\n\\nतर्हि पुनः कृत्यांशः कः ? इति चेत् साध्यांशो अस्तीत्य् आह युवानम् इत्यादिना ॥ ४ ॥\\n\\n</details>\\n';\n"
    "  const bHunk = eng.buildFullFileHunk(bridgeOld, bridgeNew, { charDiffMode: 'word' });\n"
    "  const keptLine8 = bHunk.inline_rows.find(r => (r.segments || []).some(s => s.op === 'eq' && (s.text || '').includes('तर्हि पुनः कृत्यांशः कः')));\n"
    "  if (!keptLine8) throw new Error('Intervening short context + newlines broke word diff match on long paragraph: ' + JSON.stringify(bHunk.inline_rows));\n"
    "  // Sticky header PR action bar mirrors Merge PR & Close PR buttons, and both pending + submitted comments support inline Edit\n"
    "  if (!elems['headerPrActions'].innerHTML.includes('id=\"headerMergePrBtn\"') || !elems['headerPrActions'].innerHTML.includes('id=\"headerClosePrBtn\"')) {\n"
    "    throw new Error('Header sticky Merge/Close PR buttons missing: ' + elems['headerPrActions'].innerHTML);\n"
    "  }\n"
    "  startEditComment('draft', 'draft_99', 'inline');\n"
    "  if (!elems['filesDiffContainer'].innerHTML.includes('id=\"commentEditInput\"')) throw new Error('Pending draft edit box did not open');\n"
    "  elems['commentEditInput'].value = 'Updated draft note';\n"
    "  await saveEditComment();\n"
    "  if (!fetched.some(c => c.method === 'PATCH' && c.url.endsWith('/draft_comment/draft_99')) || !elems['filesDiffContainer'].innerHTML.includes('Updated draft note')) {\n"
    "    throw new Error('Pending draft edit did not save via PATCH');\n"
    "  }\n"
    "  startEditComment('review_comment', '1001', 'inline');\n"
    "  if (!elems['filesDiffContainer'].innerHTML.includes('id=\"commentEditInput\"')) throw new Error('Submitted review comment edit box did not open');\n"
    "  elems['commentEditInput'].value = 'Edited inline comment';\n"
    "  await saveEditComment();\n"
    "  if (!fetched.some(c => c.method === 'PATCH' && c.url.endsWith('/review_comment/1001'))) {\n"
    "    throw new Error('Submitted review comment edit did not PATCH /review_comment/1001');\n"
    "  }\n"
    "})().catch(err => { console.error(err); process.exit(1); });\n"
  )
  js_file = tmp_path / "test_github_gui.js"
  js_file.write_text(harness, encoding="utf-8")
  subprocess.check_call(["node", str(js_file)])


def test_static_pr_export_and_github_rest_adapter(tmp_path: Path):
  if not shutil.which("node"):
    pytest.skip("node binary not installed")
  import base64
  import hashlib
  from pull_requests_san import pr_review_gui
  from pull_requests_san.diff import parse_patch_hunks

  patch = "@@ -1,3 +1,3 @@\n ॐ\n-पा० देवः अस्ति\n+पा॰ देवोऽस्ति\n समाप्तम्\n"
  py_hunk = parse_patch_hunks(patch, char_diff_mode="word")[0]

  out_static = tmp_path / "static_pr" / "index.html"
  assert pr_review_gui.main(["--export-static", str(out_static)]) == 0
  static_html = out_static.read_text(encoding="utf-8")
  assert "const IS_STATIC_HOST = true" in static_html
  assert "https://sanskrit-coders.github.io/diff_san/local_diff/" in static_html
  assert "pip install pull_requests_san" in static_html

  m = re.search(r"<script>\n(.*?)\n</script>", static_html, re.S)
  assert m is not None
  static_js = m.group(1)

  b64_left = base64.b64encode("ॐ\nपा० देवः अस्ति\nसमाप्तम्\n".encode("utf-8")).decode("ascii")
  b64_right = base64.b64encode("ॐ\nपा॰ देवोऽस्ति\nसमाप्तम्\n".encode("utf-8")).decode("ascii")

  expected_sha_ascii = hashlib.sha256(b"a.md").hexdigest()
  expected_sha_utf8 = hashlib.sha256("skandha_10/ऋक्_01.md".encode("utf-8")).hexdigest()

  harness = (
    "const elems = {};\n"
    "const listeners = {};\n"
    "const lsStore = {};\n"
    "const clipboardWrites = [];\n"
    "function makeClassList(){ const s = new Set(); return { add(c){s.add(c);}, remove(c){s.delete(c);}, toggle(c,f){ if(f===undefined){if(s.has(c))s.delete(c);else s.add(c);} else if(f)s.add(c); else s.delete(c); }, contains(c){return s.has(c);} }; }\n"
    "function makeEl(id){ return { id, value: '', checked: false, innerHTML: '', textContent: '', style: {}, classList: makeClassList(), querySelectorAll(){ return []; }, focus(){}, scrollIntoView(){} }; }\n"
    "global.document = {\n"
    "  documentElement: { style: { setProperty(){} } },\n"
    "  getElementById(id){ if (!elems[id]) elems[id] = makeEl(id); return elems[id]; },\n"
    "  querySelectorAll(){ return []; },\n"
    "  addEventListener(e, fn){ listeners[e] = fn; },\n"
    "};\n"
    "elems['ownerInput'] = Object.assign(makeEl('ownerInput'), { value: 'vishvAsa' });\n"
    "elems['stateSelect'] = Object.assign(makeEl('stateSelect'), { value: 'open' });\n"
    "elems['diffModeSelect'] = Object.assign(makeEl('diffModeSelect'), { value: 'word' });\n"
    "elems['normalizeSelect'] = Object.assign(makeEl('normalizeSelect'), { value: 'none' });\n"
    "elems['ignorePunctChk'] = Object.assign(makeEl('ignorePunctChk'), { checked: false });\n"
    "elems['singlePaneChk'] = Object.assign(makeEl('singlePaneChk'), { checked: true });\n"
    "elems['wrapLinesChk'] = Object.assign(makeEl('wrapLinesChk'), { checked: true });\n"
    "elems['changedOnlyChk'] = Object.assign(makeEl('changedOnlyChk'), { checked: false });\n"
    "global.localStorage = { getItem(k){ return Object.prototype.hasOwnProperty.call(lsStore, k) ? lsStore[k] : null; }, setItem(k, v){ lsStore[k] = String(v); }, removeItem(k){ delete lsStore[k]; } };\n"
    "global.window = global;\n"
    "global.location = { protocol: 'https:', search: '', href: 'https://sanskrit-coders.github.io/diff_san/pull_requests/' };\n"
    "global.history = { replaceState(){} };\n"
    "global.getSelection = () => ({ toString(){ return ''; } });\n"
    "Object.defineProperty(global, 'navigator', { value: { clipboard: { writeText: async (txt) => { clipboardWrites.push(String(txt)); } } }, configurable: true });\n"
    "const ghCalls = [];\n"
    "global.fetch = async (url) => {\n"
    "  ghCalls.push(url);\n"
    "  const mk = (obj) => ({ ok: true, status: 200, headers: { get(){ return 'application/json'; } }, json: async () => obj });\n"
    "  if (url.endsWith('/pulls/9')) return mk({ number: 9, title: 'Live GH Mock', body: 'Test', state: 'open', merged: false, draft: false, mergeable: true, mergeable_state: 'clean', html_url: 'https://github.com/vishvAsa/test/pull/9', user: { login: 'u' }, base: { ref: 'master', sha: 'b9' }, head: { ref: 'feat', sha: 'h9' }, additions: 1, deletions: 1, changed_files: 1, commits: 1 });\n"
    f"  if (url.includes('/pulls/9/files')) return mk([{{ filename: 'a.md', status: 'modified', additions: 1, deletions: 1, changes: 2, patch: {json.dumps(patch)} }}]);\n"
    "  if (url.includes('/pulls/9/commits')) return mk([{ sha: 'h9', html_url: '', author: { login: 'u' }, commit: { message: 'Update', committer: { date: '2026-11-27T09:00:00Z' } }, parents: [{ sha: 'b9' }] }]);\n"
    "  if (url.includes('/pulls/9/comments')) return mk([{ id: 701, path: 'a.md', line: 2, original_line: 2, side: 'RIGHT', position: 1, user: { login: 'rev' }, body: 'Nice!', created_at: '2026-11-27T09:05:00Z' }]);\n"
    "  if (url.includes('/issues/9/comments') || url.includes('/pulls/9/reviews')) return mk([]);\n"
    f"  if (url.includes('/contents/a.md?ref=b9')) return mk({{ encoding: 'base64', content: {json.dumps(b64_left)} }});\n"
    f"  if (url.includes('/contents/a.md?ref=h9')) return mk({{ encoding: 'base64', content: {json.dumps(b64_right)} }});\n"
    "  throw new Error('Unexpected fetch URL: ' + url);\n"
    "};\n"
    + static_js.replace("init();", "")
    + "\n(async () => {\n"
    f"  const canonRow = r => ({{ kind: r.kind, left_num: r.left_num, right_num: r.right_num, left_text: r.left_text, right_text: r.right_text, in_diff_hunk: r.in_diff_hunk, lhs_segments: r.lhs_segments, rhs_segments: r.rhs_segments }});\n"
    f"  const jsHunk = DiffSanEngine.parsePatchHunks({json.dumps(patch)}, {{ charDiffMode: 'word' }})[0];\n"
    f"  if (JSON.stringify(jsHunk.rows.map(canonRow)) !== JSON.stringify({json.dumps([r.__dict__ for r in py_hunk.rows])}.map(canonRow))) throw new Error('parsePatchHunks rows mismatch');\n"
    f"  if (JSON.stringify(jsHunk.inline_rows) !== JSON.stringify({json.dumps(py_hunk.inline_rows)})) throw new Error('parsePatchHunks inline_rows mismatch');\n"
    "  if (decodeBase64Utf8(" + json.dumps(b64_left) + ").indexOf('देवः') < 0) throw new Error('decodeBase64Utf8 Devanagari failed');\n"
    "  if (JSON.stringify(parseStaticPrRef('https://github.com/vishvAsa/test/pull/9')) !== JSON.stringify({ owner: 'vishvAsa', repo: 'test', number: 9 })) throw new Error('parseStaticPrRef URL failed');\n"
    "  if (JSON.stringify(parseStaticPrRef('vishvAsa/test#12')) !== JSON.stringify({ owner: 'vishvAsa', repo: 'test', number: 12 })) throw new Error('parseStaticPrRef short ref failed');\n"
    "  await init();\n"
    "  if (ghCalls.length !== 0 || !elems['prContainer'].innerHTML.includes('welcomeSamplePrBtn')) throw new Error('Static PR welcome screen failed');\n"
    "  await loadStaticSamplePr();\n"
    "  if (ghCalls.length !== 0 || !elems['filesDiffContainer'].innerHTML.includes('वीरराघवः') || !elems['filesDiffContainer'].innerHTML.includes('पा<span class=\"ch-sub\">०</span>')) throw new Error('Sample PR offline load failed');\n"
    "  await openStaticPrRef('https://github.com/vishvAsa/test/pull/9');\n"
    "  if (!elems['filesDiffContainer'].innerHTML.includes('Nice!') || !elems['filesDiffContainer'].innerHTML.includes('वोऽ</span>स्ति')) throw new Error('Mock GitHub REST PR load failed: ' + elems['errorBanner'].textContent + ' | ' + elems['filesDiffContainer'].innerHTML);\n"
    "  const callsBeforeFull = ghCalls.length;\n"
    "  await loadFullFile(0);\n"
    "  if (ghCalls.length !== callsBeforeFull + 2 || !activePrData.files[0].is_full_file) throw new Error('Static full-file content load failed');\n"
    f"  if (sha256Hex('a.md') !== {json.dumps(expected_sha_ascii)} || sha256Hex('skandha_10/ऋक्_01.md') !== {json.dumps(expected_sha_utf8)}) throw new Error('sha256Hex UTF-8 mismatch');\n"
    "  if (!elems['prList'].innerHTML.includes('pr-item-copy-url-btn') || !elems['prList'].innerHTML.includes('pr-item-browser-btn')) throw new Error('Sidebar copy PR URL button missing next to globe');\n"
    "  if (!elems['prContainer'].innerHTML.includes('id=\"copyPrUrlBtn\"')) throw new Error('PR header copyPrUrlBtn missing');\n"
    "  await copyActivePrUrl();\n"
    "  await copyPrUrlIdx(0);\n"
    "  if (clipboardWrites[0] !== 'https://github.com/vishvAsa/test/pull/9' || clipboardWrites[1] !== 'https://github.com/vishvAsa/test/pull/9') throw new Error('Copy PR URL mismatch: ' + JSON.stringify(clipboardWrites));\n"
    "  const modRow = renderedDiffRows.find(r => r.filename === 'a.md' && r.rightNum === 2);\n"
    f"  const expectedGhLine = 'https://github.com/vishvAsa/test/pull/9/files#diff-{expected_sha_ascii}R2';\n"
    f"  const expectedGhLeftLine = 'https://github.com/vishvAsa/test/pull/9/files#diff-{expected_sha_ascii}L2';\n"
    "  if (buildGithubPrLineUrl(modRow, null) !== expectedGhLine) throw new Error('buildGithubPrLineUrl RIGHT mismatch: ' + buildGithubPrLineUrl(modRow, null));\n"
    "  const delFakeEl = { closest(sel){ return sel.includes('.ch-del') ? {} : null; } };\n"
    "  if (buildGithubPrLineUrl(modRow, delFakeEl) !== expectedGhLeftLine) throw new Error('buildGithubPrLineUrl LEFT mismatch: ' + buildGithubPrLineUrl(modRow, delFakeEl));\n"
    "  const viewerUrl = buildDiffSanPrLineUrl(modRow, null);\n"
    "  if (!viewerUrl.includes('pr=vishvAsa%2Ftest%239') || !viewerUrl.includes('file=a.md') || !viewerUrl.includes('side=R') || !viewerUrl.includes('line=2')) throw new Error('buildDiffSanPrLineUrl mismatch: ' + viewerUrl);\n"
    "  // Alt+W toggles between word and grapheme diff units\n"
    "  listeners['keydown']({ key: 'w', code: 'KeyW', altKey: true, shiftKey: false, ctrlKey: false, metaKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  await new Promise(r => setTimeout(r, 15));\n"
    "  if (elems['diffModeSelect'].value !== 'grapheme' || lsStore['github_pr_review_diff_mode'] !== 'grapheme') throw new Error('Alt+W did not switch word->grapheme: ' + elems['diffModeSelect'].value);\n"
    "  listeners['keydown']({ key: 'w', code: 'KeyW', altKey: true, shiftKey: false, ctrlKey: false, metaKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  await new Promise(r => setTimeout(r, 15));\n"
    "  if (elems['diffModeSelect'].value !== 'word' || lsStore['github_pr_review_diff_mode'] !== 'word') throw new Error('Alt+W did not toggle grapheme->word: ' + elems['diffModeSelect'].value);\n"
    "  elems['diffModeSelect'].value = 'codepoint';\n"
    "  listeners['keydown']({ key: 'w', code: 'KeyW', altKey: true, shiftKey: false, ctrlKey: false, metaKey: false, target: { tagName: 'BODY' }, preventDefault(){} });\n"
    "  await new Promise(r => setTimeout(r, 15));\n"
    "  if (elems['diffModeSelect'].value !== 'word') throw new Error('Alt+W from codepoint should switch to word');\n"
    "  // Deep link to ?pr=sample&file=skandha_10/01.md&side=R&line=17 auto-selects matching row\n"
    "  global.location.search = '?pr=sample&file=skandha_10/01.md&side=R&line=17';\n"
    "  await init();\n"
    "  if (!selectedDiffRowKey || !selectedDiffRowKey.startsWith('skandha_10/01.md:') || !renderedDiffRows.some(r => r.rowKey === selectedDiffRowKey && r.rightNum === 17)) {\n"
    "    throw new Error('Deep link did not select target row: ' + selectedDiffRowKey);\n"
    "  }\n"
    "})().catch(err => { console.error(err); process.exit(1); });\n"
  )
  js_file = tmp_path / "test_static_pr.js"
  js_file.write_text(harness, encoding="utf-8")
  subprocess.check_call(["node", str(js_file)])
