(function (globalScope) {
  "use strict";

  const STATIC_PAT_STORAGE_KEY = "diff_san_github_pat_v1";
  const STATIC_RECENT_PRS_KEY = "diff_san_recent_prs_v1";
  const GH_API_BASE = "https://api.github.com";

  const staticPrRawCache = {};
  const staticChangelistRawCache = {};
  const staticRepoPrsCache = {};
  const staticFileContentCache = {};
  const staticDraftStore = {};
  const staticRecentPrs = _loadRecentPrs();
  let staticRepoTarget = null; // { owner, repo } when browsing a repo's PR list
  let staticAuthUser = null;

  function _getEngine() {
    if (typeof DiffSanEngine !== "undefined" && DiffSanEngine) return DiffSanEngine;
    if (globalScope && globalScope.DiffSanEngine) return globalScope.DiffSanEngine;
    return null;
  }

  function _prKeyStr(owner, repo, number) {
    return `${String(owner || "").toLowerCase()}/${String(repo || "").toLowerCase()}#${Number(number)}`;
  }

  function getStaticGithubToken() {
    try {
      return (localStorage.getItem(STATIC_PAT_STORAGE_KEY) || "").trim();
    } catch (_) {
      return "";
    }
  }

  function setStaticGithubToken(tok) {
    const clean = String(tok || "").trim();
    staticAuthUser = null;
    try {
      if (clean) localStorage.setItem(STATIC_PAT_STORAGE_KEY, clean);
      else localStorage.removeItem(STATIC_PAT_STORAGE_KEY);
    } catch (_) {}
    return clean;
  }

  function _loadRecentPrs() {
    try {
      const raw = localStorage.getItem(STATIC_RECENT_PRS_KEY);
      const arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr : [];
    } catch (_) {
      return [];
    }
  }

  function _rememberRecentPr(summary) {
    if (!summary || !summary.owner || !summary.repo || !summary.number) return;
    const key = _prKeyStr(summary.owner, summary.repo, summary.number);
    const filtered = staticRecentPrs.filter(p => _prKeyStr(p.owner, p.repo, p.number) !== key);
    filtered.unshift({ ...summary });
    if (filtered.length > 12) filtered.length = 12;
    staticRecentPrs.length = 0;
    staticRecentPrs.push(...filtered);
    try { localStorage.setItem(STATIC_RECENT_PRS_KEY, JSON.stringify(staticRecentPrs)); } catch (_) {}
  }

  function parseStaticPrRef(raw, fallbackRepoFullName = "") {
    const s = String(raw || "").trim();
    if (!s) return null;
    let m = s.match(/github\.com\/([^/\s#?]+)\/([^/\s#?]+)\/pull\/(\d+)/i);
    if (m) return { owner: m[1], repo: m[2].replace(/\.git$/i, ""), number: Number(m[3]) };
    m = s.match(/github\.com\/([^/\s#?]+)\/([^/\s#?]+)(?:[/?#]|$)/i);
    if (m) return { owner: m[1], repo: m[2].replace(/\.git$/i, ""), number: null };
    m = s.match(/^([^/\s#]+)\/([^/\s#]+)(?:\/pull\/|#|\/)(\d+)$/i);
    if (m) return { owner: m[1], repo: m[2], number: Number(m[3]) };
    m = s.match(/^([^/\s#]+)\/([^/\s#]+)$/);
    if (m) return { owner: m[1], repo: m[2], number: null };
    m = s.match(/^#?(\d+)$/);
    if (m && fallbackRepoFullName && fallbackRepoFullName.includes("/")) {
      const [owner, repo] = fallbackRepoFullName.split("/", 2);
      if (owner && repo) return { owner, repo, number: Number(m[1]) };
    }
    return null;
  }

  function decodeBase64Utf8(b64) {
    const clean = String(b64 || "").replace(/\s+/g, "");
    if (!clean) return "";
    const bin = atob(clean);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    if (typeof TextDecoder !== "undefined") {
      return new TextDecoder("utf-8").decode(bytes);
    }
    return decodeURIComponent(escape(bin));
  }

  function _parseNextLink(linkHdr) {
    if (!linkHdr) return null;
    for (const part of String(linkHdr).split(",")) {
      const m = part.match(/<([^>]+)>\s*;\s*rel="next"/i);
      if (m) return m[1];
    }
    return null;
  }

  async function _ghRequest(pathOrUrl, { method = "GET", body = undefined, accept = "application/vnd.github+json", returnHeaders = false } = {}) {
    const url = pathOrUrl.startsWith("http") ? pathOrUrl : `${GH_API_BASE}${pathOrUrl}`;
    const headers = { Accept: accept };
    const tok = getStaticGithubToken();
    if (tok) headers.Authorization = `Bearer ${tok}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const resp = await fetch(url, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    let data = null;
    const ct = resp.headers && typeof resp.headers.get === "function" ? (resp.headers.get("content-type") || "") : "";
    if (ct.includes("json")) {
      try { data = await resp.json(); } catch (_) { data = null; }
    } else {
      try {
        const txt = await resp.text();
        try { data = JSON.parse(txt); } catch (_) { data = txt; }
      } catch (_) { data = null; }
    }
    if (!resp.ok) {
      const remaining = resp.headers && typeof resp.headers.get === "function" ? resp.headers.get("x-ratelimit-remaining") : null;
      const ghMsg = (data && typeof data === "object" && data.message) ? data.message : `HTTP ${resp.status}`;
      if (resp.status === 403 && remaining === "0") {
        throw new Error(`GitHub API rate limit reached (${ghMsg}). Click '🔑 Token' above to add a Personal Access Token.`);
      }
      if (resp.status === 404) {
        throw new Error(`Not found on GitHub (${ghMsg}). If this is a private repository, add a token via '🔑 Token'.`);
      }
      if (resp.status === 401) {
        throw new Error(`GitHub authentication failed (${ghMsg}). Check your token via '🔑 Token'.`);
      }
      throw new Error(`GitHub API error (${resp.status}): ${ghMsg}`);
    }
    return returnHeaders ? { data, headers: resp.headers } : data;
  }

  async function _ghFetchAllPages(path, maxPages = 5) {
    const out = [];
    let nextUrl = path;
    let pages = 0;
    while (nextUrl && pages < maxPages) {
      const { data, headers } = await _ghRequest(nextUrl, { returnHeaders: true });
      if (Array.isArray(data)) out.push(...data);
      nextUrl = headers && typeof headers.get === "function" ? _parseNextLink(headers.get("link")) : null;
      pages += 1;
    }
    return out;
  }

  async function _ghGraphQl(query, variables = {}) {
    const tok = getStaticGithubToken();
    if (!tok) return null;
    try {
      const res = await _ghRequest("/graphql", { method: "POST", body: { query, variables } });
      if (res && res.errors && res.errors.length) return null;
      return res ? res.data : null;
    } catch (_) {
      return null;
    }
  }

  async function _fetchThreadResolvedMap(owner, repo, number) {
    const query = `query($owner:String!, $repo:String!, $number:Int!) {
      repository(owner:$owner, name:$repo) {
        pullRequest(number:$number) {
          reviewDecision
          reviewThreads(first:100) {
            nodes {
              id
              isResolved
              isOutdated
              comments(first:1) { nodes { databaseId } }
            }
          }
        }
      }
    }`;
    const g = await _ghGraphQl(query, { owner, repo, number: Number(number) });
    const pr = g?.repository?.pullRequest;
    if (!pr) return { reviewDecision: null, byRootId: {} };
    const byRootId = {};
    for (const node of (pr.reviewThreads?.nodes || [])) {
      const dbId = node?.comments?.nodes?.[0]?.databaseId;
      if (dbId) {
        byRootId[Number(dbId)] = {
          nodeId: node.id,
          isResolved: Boolean(node.isResolved),
          isOutdated: Boolean(node.isOutdated),
        };
      }
    }
    return { reviewDecision: pr.reviewDecision || null, byRootId };
  }

  function _normalizePrSummary(owner, repo, prObj, { reviewDecision = null, commentCount = null, threadCount = null } = {}) {
    const merged = Boolean(prObj.merged || prObj.merged_at);
    const state = merged ? "closed" : (prObj.state || "open");
    return {
      owner,
      repo,
      repo_full_name: `${owner}/${repo}`,
      number: Number(prObj.number),
      title: prObj.title || "",
      state,
      merged,
      is_draft: Boolean(prObj.draft),
      url: prObj.html_url || `https://github.com/${owner}/${repo}/pull/${prObj.number}`,
      author: prObj.user?.login || "unknown",
      base_ref: prObj.base?.ref || "main",
      head_ref: prObj.head?.ref || "head",
      created_at: prObj.created_at || "",
      updated_at: prObj.updated_at || "",
      additions: Number(prObj.additions || 0),
      deletions: Number(prObj.deletions || 0),
      changed_files: Number(prObj.changed_files || 0),
      mergeable: prObj.mergeable !== undefined ? prObj.mergeable : null,
      mergeable_state: prObj.mergeable_state || null,
      review_decision: reviewDecision,
      comment_count: commentCount != null ? commentCount : Number(prObj.comments || 0),
      thread_count: threadCount != null ? threadCount : Number(prObj.review_comments || 0),
    };
  }

  function _groupReviewThreads(rawComments, resolvedByRootId = {}) {
    const roots = [];
    const byId = {};
    const ordered = [...(rawComments || [])].sort((a, b) => Number(a.id) - Number(b.id));
    for (const c of ordered) {
      const item = {
        id: Number(c.id),
        author: c.user?.login || "unknown",
        body: c.body || "",
        created_at: c.created_at || "",
        url: c.html_url || "",
      };
      const parentId = c.in_reply_to_id ? Number(c.in_reply_to_id) : null;
      if (parentId && byId[parentId]) {
        byId[parentId].comments.push(item);
      } else {
        const gInfo = resolvedByRootId[Number(c.id)];
        const thread = {
          id: gInfo?.nodeId || String(c.id),
          path: c.path || "",
          line: c.line ?? c.original_line ?? null,
          original_line: c.original_line ?? c.line ?? null,
          side: c.side || "RIGHT",
          is_resolved: gInfo ? gInfo.isResolved : false,
          is_outdated: gInfo ? gInfo.isOutdated : (c.position == null && c.line == null),
          comments: [item],
        };
        roots.push(thread);
        byId[Number(c.id)] = thread;
      }
    }
    return roots;
  }

  function _buildDiffFiles(rawFiles, opts) {
    const eng = _getEngine();
    const parseOpts = {
      charDiffMode: opts.diff_mode || "word",
      normalize: opts.normalize || "none",
      ignorePunct: Boolean(opts.ignore_punct),
    };
    return (rawFiles || []).map(f => {
      const patch = f.patch || "";
      const isBinary = !patch && (f.status === "modified" || f.status === "added" || f.status === "removed") && Number(f.changes || 0) > 0;
      const hunks = (patch && eng && typeof eng.parsePatchHunks === "function")
        ? eng.parsePatchHunks(patch, parseOpts)
        : [];
      return {
        filename: f.filename || "",
        previous_filename: f.previous_filename || null,
        status: f.status || "modified",
        additions: Number(f.additions || 0),
        deletions: Number(f.deletions || 0),
        patch,
        is_binary: isBinary,
        is_full_file: false,
        patch_truncated: Boolean(!patch && (f.additions || f.deletions)),
        local_file_path: null,
        hunks,
      };
    });
  }

  function _buildPrDetailFromRaw(rawBundle, opts) {
    const { owner, repo, prObj, rawFiles, rawCommits, rawReviewComments, rawIssueComments, rawReviews, resolvedInfo, isSample } = rawBundle;
    const threads = _groupReviewThreads(rawReviewComments, resolvedInfo?.byRootId || {});
    const summary = _normalizePrSummary(owner, repo, prObj, {
      reviewDecision: resolvedInfo?.reviewDecision || null,
      commentCount: (rawIssueComments || []).length,
      threadCount: threads.length,
    });
    const commits = (rawCommits || []).map(c => {
      const msg = c.commit?.message || c.message || "";
      const title = msg.split(/\r?\n/, 1)[0] || "";
      return {
        sha: c.sha,
        short_sha: String(c.sha || "").slice(0, 7),
        title,
        message: msg,
        author: c.author?.login || c.commit?.author?.name || "unknown",
        committed_at: c.commit?.committer?.date || c.commit?.author?.date || "",
        url: c.html_url || "",
        parents: (c.parents || []).map(p => p.sha || p),
      };
    });
    const issueComments = (rawIssueComments || []).map(c => ({
      id: Number(c.id),
      author: c.user?.login || "unknown",
      body: c.body || "",
      created_at: c.created_at || "",
      url: c.html_url || "",
    }));
    const reviews = (rawReviews || []).map(r => ({
      id: Number(r.id),
      author: r.user?.login || "unknown",
      state: r.state || "COMMENTED",
      body: r.body || "",
      submitted_at: r.submitted_at || "",
      url: r.html_url || "",
    }));
    const key = _prKeyStr(owner, repo, summary.number);
    return {
      summary,
      body: prObj.body || "",
      merged: summary.merged,
      mergeable: summary.mergeable,
      mergeable_state: summary.mergeable_state,
      ahead_by: null,
      behind_by: null,
      base_sha: prObj.base?.sha || null,
      head_sha: prObj.head?.sha || null,
      commits_count: commits.length || Number(prObj.commits || 0),
      commits,
      files: _buildDiffFiles(rawFiles, opts),
      review_threads: threads,
      issue_comments: issueComments,
      reviews,
      conflicts: [],
      pending_drafts: staticDraftStore[key] || [],
      is_sample: Boolean(isSample),
    };
  }

  async function _fetchOrGetRawPrBundle(owner, repo, number, forceRefresh = false) {
    const key = _prKeyStr(owner, repo, number);
    if (!forceRefresh && staticPrRawCache[key]) return staticPrRawCache[key];
    const base = `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`;
    const [prObj, rawFiles, rawCommits, rawReviewComments, rawIssueComments, rawReviews, resolvedInfo] = await Promise.all([
      _ghRequest(`${base}/pulls/${number}`),
      _ghFetchAllPages(`${base}/pulls/${number}/files?per_page=100`, 5),
      _ghFetchAllPages(`${base}/pulls/${number}/commits?per_page=100`, 3),
      _ghFetchAllPages(`${base}/pulls/${number}/comments?per_page=100`, 3),
      _ghFetchAllPages(`${base}/issues/${number}/comments?per_page=100`, 3),
      _ghFetchAllPages(`${base}/pulls/${number}/reviews?per_page=100`, 3),
      _fetchThreadResolvedMap(owner, repo, number),
    ]);
    const bundle = { owner, repo, prObj, rawFiles, rawCommits, rawReviewComments, rawIssueComments, rawReviews, resolvedInfo, isSample: false };
    staticPrRawCache[key] = bundle;
    const summary = _normalizePrSummary(owner, repo, prObj, {
      reviewDecision: resolvedInfo?.reviewDecision || null,
      commentCount: rawIssueComments.length,
    });
    _rememberRecentPr(summary);
    return bundle;
  }

  async function _fetchRawContentAtRef(owner, repo, path, sha, rawBundle = null) {
    if (rawBundle?.isSample && rawBundle.sampleContents) {
      const map = sha === rawBundle.prObj.base.sha ? rawBundle.sampleContents.base : rawBundle.sampleContents.head;
      return map[path] ?? "";
    }
    const cacheKey = `${owner}/${repo}@${sha}:${path}`;
    if (Object.prototype.hasOwnProperty.call(staticFileContentCache, cacheKey)) {
      return staticFileContentCache[cacheKey];
    }
    const encPath = String(path || "").split("/").map(encodeURIComponent).join("/");
    let text = "";
    try {
      const data = await _ghRequest(`/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/contents/${encPath}?ref=${encodeURIComponent(sha)}`);
      if (data && typeof data === "object" && data.encoding === "base64" && data.content != null) {
        text = decodeBase64Utf8(data.content);
      } else if (data && data.download_url) {
        const resp = await fetch(data.download_url);
        if (resp.ok) text = await resp.text();
      }
    } catch (e) {
      if (!String(e.message || "").includes("404") && !String(e.message || "").includes("Not found")) throw e;
      text = "";
    }
    staticFileContentCache[cacheKey] = text;
    return text;
  }

  function _seedSamplePrBundle() {
    const key = _prKeyStr("vishvAsa", "bhAgavatam", 42);
    const leftText = [
      "ॐ नमो भगवते वासुदेवाय।",
      "<details><summary>वीरराघवः [यान्त्रिकम्]</summary>",
      "",
      "श्रीमद्भागवतम् — पा० पाठभेदः।",
      ...Array.from({ length: 12 }, (_, idx) => `प्रसङ्गपङ्क्तिः ${idx + 5} धर्मक्षेत्रे कुरुक्षेत्रे।`),
      "देवः अस्ति इति मुनयः वदन्ति।",
      "तथा च अस्ति इति श्रूयते।",
      "इति प्रथमः खण्डः।",
    ].join("\n") + "\n";
    const rightText = [
      "ॐ नमो भगवते वासुदेवाय।",
      "<details><summary>वीरराघवः</summary>",
      "",
      "श्रीमद्भागवतम् — पा॰ पाठभेदः।",
      ...Array.from({ length: 12 }, (_, idx) => `प्रसङ्गपङ्क्तिः ${idx + 5} धर्मक्षेत्रे कुरुक्षेत्रे।`),
      "देवोऽस्ति इति मुनयः वदन्ति।",
      "तथा च अस्तीति श्रूयते।",
      "इति प्रथमः खण्डः।",
    ].join("\n") + "\n";

    const patch = [
      "@@ -1,5 +1,5 @@",
      " ॐ नमो भगवते वासुदेवाय।",
      "-<details><summary>वीरराघवः [यान्त्रिकम्]</summary>",
      "+<details><summary>वीरराघवः</summary>",
      " ",
      "-श्रीमद्भागवतम् — पा० पाठभेदः।",
      "+श्रीमद्भागवतम् — पा॰ पाठभेदः।",
      " प्रसङ्गपङ्क्तिः 5 धर्मक्षेत्रे कुरुक्षेत्रे।",
      "@@ -16,4 +16,4 @@",
      " प्रसङ्गपङ्क्तिः 16 धर्मक्षेत्रे कुरुक्षेत्रे।",
      "-देवः अस्ति इति मुनयः वदन्ति।",
      "-तथा च अस्ति इति श्रूयते।",
      "+देवोऽस्ति इति मुनयः वदन्ति।",
      "+तथा च अस्तीति श्रूयते।",
      " इति प्रथमः खण्डः।",
    ].join("\n");

    const bundle = {
      owner: "vishvAsa",
      repo: "bhAgavatam",
      isSample: true,
      sampleContents: {
        base: { "skandha_10/01.md": leftText },
        head: { "skandha_10/01.md": rightText },
      },
      prObj: {
        number: 42,
        title: "दशमस्कन्धः पाठभेदाः — Sandhi, पा०→पा॰ & Proofreading Fixes (Sample PR)",
        body: "Sample offline Pull Request demonstrating single-pane word flow, intra-word `पा०`/`पा॰` highlights, `Sandhi diff`, collapsible context gaps, and review threads.",
        state: "open",
        merged: false,
        draft: false,
        mergeable: true,
        mergeable_state: "clean",
        html_url: "https://github.com/sanskrit-coders/diff_san",
        user: { login: "vvasuki" },
        base: { ref: "master", sha: "sample_base_sha_1111" },
        head: { ref: "proofread-skandha10", sha: "sample_head_sha_2222" },
        created_at: "2026-11-26T05:30:00Z",
        updated_at: "2026-11-27T08:00:00Z",
        additions: 4,
        deletions: 4,
        changed_files: 1,
        commits: 2,
      },
      rawFiles: [
        {
          filename: "skandha_10/01.md",
          status: "modified",
          additions: 4,
          deletions: 4,
          changes: 8,
          patch,
        },
      ],
      rawCommits: [
        {
          sha: "samplec1111111",
          html_url: "https://github.com/sanskrit-coders/diff_san",
          author: { login: "vvasuki" },
          commit: { message: "Fix पा० → पा॰ and remove [यान्त्रिकम्] tag", committer: { date: "2026-11-26T06:10:00Z" } },
          parents: [{ sha: "sample_base_sha_1111" }],
        },
        {
          sha: "samplec2222222",
          html_url: "https://github.com/sanskrit-coders/diff_san",
          author: { login: "vvasuki" },
          commit: { message: "Normalize देवोऽस्ति / अस्तीति sandhi", committer: { date: "2026-11-27T08:00:00Z" } },
          parents: [{ sha: "samplec1111111" }],
        },
      ],
      rawReviewComments: [
        {
          id: 9001,
          path: "skandha_10/01.md",
          line: 17,
          original_line: 17,
          side: "RIGHT",
          position: 1,
          user: { login: "reviewer1" },
          body: "Toggle **Sandhi diff** in the toolbar to highlight `देवः अस्ति` ↔ `देवोऽस्ति` and `अस्ति इति` ↔ `अस्तीति`.",
          created_at: "2026-11-27T08:05:00Z",
        },
        {
          id: 9002,
          path: "skandha_10/01.md",
          line: 2,
          original_line: 2,
          side: "RIGHT",
          position: 1,
          user: { login: "vvasuki" },
          body: "Removed redundant `[यान्त्रिकम्]` marker cleanly.",
          created_at: "2026-11-26T07:00:00Z",
        },
      ],
      rawIssueComments: [
        {
          id: 8001,
          user: { login: "vvasuki" },
          body: "Click the collapsed context bar (`⋯ 10 collapsed lines`) between hunks to expand context.",
          created_at: "2026-11-27T08:02:00Z",
        },
      ],
      rawReviews: [],
      resolvedInfo: {
        reviewDecision: null,
        byRootId: {
          9002: { nodeId: "PRRT_sample_resolved_2", isResolved: true, isOutdated: false },
        },
      },
    };
    staticPrRawCache[key] = bundle;
    staticChangelistRawCache[`${key}:samplec1111111`] = {
      sha: "samplec1111111",
      html_url: "https://github.com/sanskrit-coders/diff_san",
      author: { login: "vvasuki" },
      commit: { message: "Fix पा० → पा॰ and remove [यान्त्रिकम्] tag", committer: { date: "2026-11-26T06:10:00Z" } },
      parents: [{ sha: "sample_base_sha_1111" }],
      files: [{
        filename: "skandha_10/01.md",
        status: "modified",
        additions: 2,
        deletions: 2,
        patch: patch.split("@@ -16")[0].trimEnd(),
      }],
    };
    staticChangelistRawCache[`${key}:samplec2222222`] = {
      sha: "samplec2222222",
      html_url: "https://github.com/sanskrit-coders/diff_san",
      author: { login: "vvasuki" },
      commit: { message: "Normalize देवोऽस्ति / अस्तीति sandhi", committer: { date: "2026-11-27T08:00:00Z" } },
      parents: [{ sha: "samplec1111111" }],
      files: [{
        filename: "skandha_10/01.md",
        status: "modified",
        additions: 2,
        deletions: 2,
        patch: "@@ -16" + patch.split("@@ -16")[1],
      }],
    };
    return bundle;
  }

  function _collectPrSidebarSummaries(filterRepo = null) {
    const map = {};
    for (const s of staticRecentPrs) {
      map[_prKeyStr(s.owner, s.repo, s.number)] = s;
    }
    if (filterRepo && filterRepo.owner && filterRepo.repo) {
      const rKey = `${filterRepo.owner.toLowerCase()}/${filterRepo.repo.toLowerCase()}`;
      for (const s of (staticRepoPrsCache[rKey] || [])) {
        map[_prKeyStr(s.owner, s.repo, s.number)] = s;
      }
    }
    const list = Object.values(map);
    const repos = [...new Set(list.map(p => p.repo))];
    return { prs: list, repos };
  }

  async function handleStaticPrApi(url, opts = null) {
    const u = new URL(url, "https://local.invalid");
    const path = u.pathname;
    const params = u.searchParams;
    const method = ((opts && opts.method) || "GET").toUpperCase();
    const reqBody = (opts && opts.body && typeof opts.body === "string") ? JSON.parse(opts.body) : {};

    if (path === "/api/meta") {
      const tok = getStaticGithubToken();
      if (tok && !staticAuthUser) {
        try { staticAuthUser = await _ghRequest("/user"); } catch (_) { staticAuthUser = null; }
      }
      return {
        default_owner: staticRepoTarget?.owner || "vishvAsa",
        default_state: "open",
        default_repo: staticRepoTarget?.repo || "",
        has_token: Boolean(tok),
        auth_user: staticAuthUser ? { login: staticAuthUser.login } : null,
        static_mode: true,
      };
    }

    if (path === "/api/prs") {
      const state = (params.get("state") || "open").toLowerCase();
      if (staticRepoTarget && staticRepoTarget.owner && staticRepoTarget.repo) {
        const ghState = (state === "closed" || state === "merged") ? "closed" : (state === "all" ? "all" : "open");
        const rawList = await _ghRequest(`/repos/${encodeURIComponent(staticRepoTarget.owner)}/${encodeURIComponent(staticRepoTarget.repo)}/pulls?state=${ghState}&per_page=30`);
        let summaries = (rawList || []).map(p => _normalizePrSummary(staticRepoTarget.owner, staticRepoTarget.repo, p));
        if (state === "merged") summaries = summaries.filter(p => p.merged);
        else if (state === "closed") summaries = summaries.filter(p => !p.merged);
        staticRepoPrsCache[`${staticRepoTarget.owner.toLowerCase()}/${staticRepoTarget.repo.toLowerCase()}`] = summaries;
        return { prs: summaries, repos: [staticRepoTarget.repo] };
      }
      return _collectPrSidebarSummaries();
    }

    if (path === "/api/open_in_browser") {
      if (reqBody.url && typeof window !== "undefined" && typeof window.open === "function") {
        window.open(reqBody.url, "_blank", "noopener,noreferrer");
      }
      return { ok: true };
    }

    if (path === "/api/refresh") {
      if (reqBody.active_owner && reqBody.active_repo && reqBody.active_number) {
        const raw = await _fetchOrGetRawPrBundle(reqBody.active_owner, reqBody.active_repo, reqBody.active_number, true);
        const detail = _buildPrDetailFromRaw(raw, reqBody);
        const side = _collectPrSidebarSummaries(staticRepoTarget);
        return { ...side, active_pr: detail };
      }
      return handleStaticPrApi(`/api/prs?state=${encodeURIComponent(reqBody.state || "open")}&refresh=true`);
    }

    let m = path.match(/^\/api\/pr\/([^/]+)\/([^/]+)\/(\d+)$/);
    if (m) {
      const [, owner, repo, numStr] = m;
      const number = Number(numStr);
      const diffOpts = {
        diff_mode: params.get("diff_mode") || "word",
        normalize: params.get("normalize") || "none",
        ignore_punct: params.get("ignore_punct") === "true",
      };
      const raw = await _fetchOrGetRawPrBundle(owner, repo, number, params.get("refresh") === "true");
      return _buildPrDetailFromRaw(raw, diffOpts);
    }

    m = path.match(/^\/api\/pr\/([^/]+)\/([^/]+)\/(\d+)\/changelist$/);
    if (m) {
      const [, owner, repo, numStr] = m;
      const sha = params.get("sha") || "";
      const cKey = `${_prKeyStr(owner, repo, numStr)}:${sha}`;
      const rawCommit = (params.get("refresh") !== "true" && staticChangelistRawCache[cKey])
        ? staticChangelistRawCache[cKey]
        : await _ghRequest(`/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/commits/${encodeURIComponent(sha)}`);
      staticChangelistRawCache[cKey] = rawCommit;
      const msg = rawCommit.commit?.message || "";
      const commitMeta = {
        sha: rawCommit.sha,
        short_sha: String(rawCommit.sha || "").slice(0, 7),
        title: msg.split(/\r?\n/, 1)[0] || "",
        message: msg,
        author: rawCommit.author?.login || rawCommit.commit?.author?.name || "unknown",
        committed_at: rawCommit.commit?.committer?.date || rawCommit.commit?.author?.date || "",
        url: rawCommit.html_url || "",
        parents: (rawCommit.parents || []).map(p => p.sha || p),
      };
      const diffOpts = {
        diff_mode: params.get("diff_mode") || "word",
        normalize: params.get("normalize") || "none",
        ignore_punct: params.get("ignore_punct") === "true",
      };
      return { sha: rawCommit.sha, commit: commitMeta, files: _buildDiffFiles(rawCommit.files || [], diffOpts) };
    }

    m = path.match(/^\/api\/pr\/([^/]+)\/([^/]+)\/(\d+)\/file_full$/);
    if (m) {
      const [, owner, repo, numStr] = m;
      const number = Number(numStr);
      const filename = params.get("filename") || "";
      const commitSha = params.get("commit_sha") || "";
      const raw = await _fetchOrGetRawPrBundle(owner, repo, number, false);
      let fileMeta = null;
      let baseSha = raw.prObj.base?.sha;
      let headSha = raw.prObj.head?.sha;
      if (commitSha) {
        const cKey = `${_prKeyStr(owner, repo, number)}:${commitSha}`;
        const rawCommit = staticChangelistRawCache[cKey] || await _ghRequest(`/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/commits/${encodeURIComponent(commitSha)}`);
        staticChangelistRawCache[cKey] = rawCommit;
        fileMeta = (rawCommit.files || []).find(f => f.filename === filename) || { filename, status: "modified" };
        headSha = rawCommit.sha;
        baseSha = rawCommit.parents?.[0]?.sha || `${rawCommit.sha}^`;
      } else {
        fileMeta = (raw.rawFiles || []).find(f => f.filename === filename) || { filename, status: "modified" };
      }
      const leftPath = fileMeta.previous_filename || fileMeta.filename;
      const [baseText, headText] = await Promise.all([
        fileMeta.status === "added" ? "" : _fetchRawContentAtRef(owner, repo, leftPath, baseSha, raw),
        fileMeta.status === "removed" ? "" : _fetchRawContentAtRef(owner, repo, fileMeta.filename, headSha, raw),
      ]);
      const diffOpts = {
        charDiffMode: params.get("diff_mode") || "word",
        normalize: params.get("normalize") || "none",
        ignorePunct: params.get("ignore_punct") === "true",
      };
      const eng = _getEngine();
      const origHunks = (fileMeta.patch && eng && typeof eng.parsePatchHunks === "function")
        ? eng.parsePatchHunks(fileMeta.patch, diffOpts)
        : [];
      const oldHunkNums = new Set();
      const newHunkNums = new Set();
      for (const h of origHunks) {
        for (const r of (h.rows || [])) {
          if (r.left_num != null) oldHunkNums.add(r.left_num);
          if (r.right_num != null) newHunkNums.add(r.right_num);
        }
      }
      const fullHunk = eng.buildFullFileHunk(baseText, headText, diffOpts);
      if (origHunks.length) {
        for (const r of fullHunk.rows) {
          r.in_diff_hunk = (r.left_num != null && oldHunkNums.has(r.left_num)) || (r.right_num != null && newHunkNums.has(r.right_num));
        }
        for (const r of (fullHunk.inline_rows || [])) {
          r.in_diff_hunk = (r.left_num != null && oldHunkNums.has(r.left_num)) || (r.right_num != null && newHunkNums.has(r.right_num));
        }
      }
      return {
        file: {
          filename: fileMeta.filename,
          previous_filename: fileMeta.previous_filename || null,
          status: fileMeta.status || "modified",
          additions: Number(fileMeta.additions ?? fullHunk.additions ?? 0),
          deletions: Number(fileMeta.deletions ?? fullHunk.deletions ?? 0),
          patch: fileMeta.patch || "",
          is_binary: false,
          is_full_file: true,
          local_file_path: null,
          hunks: [fullHunk],
        },
      };
    }

    m = path.match(/^\/api\/pr\/([^/]+)\/([^/]+)\/(\d+)\/draft_comment(?:\/([^/]+))?$/);
    if (m) {
      const [, owner, repo, numStr, draftId] = m;
      const key = _prKeyStr(owner, repo, numStr);
      const list = (staticDraftStore[key] ||= []);
      let targetDraft = null;
      if (method === "POST") {
        targetDraft = { ...reqBody, draft_id: `d_${Date.now()}_${list.length}` };
        list.push(targetDraft);
      } else if (method === "PATCH" && draftId) {
        const nextBody = String(reqBody?.body || "").trim();
        if (!nextBody) throw new Error("Draft comment body cannot be empty.");
        targetDraft = list.find(d => String(d.draft_id) === String(draftId)) || null;
        if (!targetDraft) throw new Error(`Draft comment ${draftId} not found.`);
        targetDraft.body = nextBody;
      } else if (method === "DELETE" && draftId) {
        staticDraftStore[key] = list.filter(d => d.draft_id !== draftId);
      }
      return { draft: targetDraft, pending_drafts: staticDraftStore[key] };
    }

    m = path.match(/^\/api\/pr\/([^/]+)\/([^/]+)\/(\d+)\/(review_comment|issue_comment|review_summary)\/(\d+)$/);
    if (m) {
      const [, owner, repo, numStr, editKind, itemId] = m;
      const number = Number(numStr);
      if (!getStaticGithubToken()) {
        throw new Error("Read-only static mode: click '🔑 Token' above to add a GitHub Personal Access Token for editing comments or reviews.");
      }
      const raw = await _fetchOrGetRawPrBundle(owner, repo, number, false);
      if (raw.isSample) {
        throw new Error("Sample PR is offline-only and cannot be submitted to GitHub.");
      }
      const nextBody = String(reqBody?.body || "").trim();
      if (!nextBody) throw new Error("Comment body cannot be empty.");
      const base = `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`;
      let updated = null;
      if (editKind === "review_comment") {
        updated = await _ghRequest(`${base}/pulls/comments/${Number(itemId)}`, { method: "PATCH", body: { body: nextBody } });
      } else if (editKind === "issue_comment") {
        updated = await _ghRequest(`${base}/issues/comments/${Number(itemId)}`, { method: "PATCH", body: { body: nextBody } });
      } else if (editKind === "review_summary") {
        updated = await _ghRequest(`${base}/pulls/${number}/reviews/${Number(itemId)}`, { method: "PUT", body: { body: nextBody } });
      }
      const updatedRaw = await _fetchOrGetRawPrBundle(owner, repo, number, true);
      return { updated, pr: _buildPrDetailFromRaw(updatedRaw, reqBody) };
    }

    // Remaining write mutations to GitHub require a token
    m = path.match(/^\/api\/pr\/([^/]+)\/([^/]+)\/(\d+)\/(inline_comment|reply|issue_comment|review|resolve_thread|state|merge)$/);
    if (m) {
      const [, owner, repo, numStr, action] = m;
      const number = Number(numStr);
      if (!getStaticGithubToken()) {
        throw new Error("Read-only static mode: click '🔑 Token' above to add a GitHub Personal Access Token for posting comments or reviews.");
      }
      const key = _prKeyStr(owner, repo, number);
      const raw = await _fetchOrGetRawPrBundle(owner, repo, number, false);
      if (raw.isSample) {
        throw new Error("Sample PR is offline-only and cannot be submitted to GitHub.");
      }
      const base = `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`;
      if (action === "inline_comment") {
        const payload = { body: reqBody.body, commit_id: raw.prObj.head?.sha, path: reqBody.path, line: Number(reqBody.line), side: reqBody.side || "RIGHT" };
        if (reqBody.start_line) { payload.start_line = Number(reqBody.start_line); payload.start_side = reqBody.start_side || payload.side; }
        await _ghRequest(`${base}/pulls/${number}/comments`, { method: "POST", body: payload });
      } else if (action === "reply") {
        await _ghRequest(`${base}/pulls/${number}/comments/${Number(reqBody.comment_id)}/replies`, { method: "POST", body: { body: reqBody.body } });
      } else if (action === "issue_comment") {
        await _ghRequest(`${base}/issues/${number}/comments`, { method: "POST", body: { body: reqBody.body } });
      } else if (action === "review") {
        const drafts = reqBody.include_drafts ? (staticDraftStore[key] || []) : [];
        const bodyObj = { event: reqBody.event || "COMMENT", body: reqBody.body || "" };
        if (drafts.length) {
          bodyObj.comments = drafts.map(d => ({
            path: d.path,
            line: Number(d.line),
            side: d.side || "RIGHT",
            body: d.body,
            ...(d.start_line ? { start_line: Number(d.start_line), start_side: d.start_side || d.side || "RIGHT" } : {}),
          }));
        }
        await _ghRequest(`${base}/pulls/${number}/reviews`, { method: "POST", body: bodyObj });
        if (drafts.length) staticDraftStore[key] = [];
      } else if (action === "state") {
        await _ghRequest(`${base}/pulls/${number}`, { method: "PATCH", body: { state: reqBody.state } });
      } else if (action === "merge") {
        await _ghRequest(`${base}/pulls/${number}/merge`, { method: "PUT", body: { merge_method: reqBody.merge_method || "merge", ...(reqBody.commit_title ? { commit_title: reqBody.commit_title } : {}) } });
      } else if (action === "resolve_thread") {
        const mutName = reqBody.resolve ? "resolveReviewThread" : "unresolveReviewThread";
        await _ghGraphQl(`mutation($id:ID!){ ${mutName}(input:{threadId:$id}){ thread{ isResolved } } }`, { id: reqBody.thread_id });
      }
      const updatedRaw = await _fetchOrGetRawPrBundle(owner, repo, number, true);
      return { pr: _buildPrDetailFromRaw(updatedRaw, reqBody), merged: updatedRaw.prObj.merged };
    }

    throw new Error(`Unsupported static PR endpoint: ${path}`);
  }

  function _updateStaticPrUrlQuery(refStr, preserveTargetParams = false) {
    try {
      if (typeof window === "undefined" || !window.history || typeof window.history.replaceState !== "function") return;
      const u = new URL(window.location.href);
      if (refStr) u.searchParams.set("pr", refStr);
      else u.searchParams.delete("pr");
      if (!preserveTargetParams) {
        u.searchParams.delete("file");
        u.searchParams.delete("side");
        u.searchParams.delete("line");
        u.searchParams.delete("commit");
      }
      window.history.replaceState({}, "", u.toString());
    } catch (_) {}
  }

  function _showStaticWelcomeCard() {
    const container = document.getElementById("prContainer");
    if (!container) return;
    const recentHtml = staticRecentPrs.length
      ? `<div style="margin-top:8px;">
          <div style="font-size:15px; font-weight:600; color:var(--muted); margin-bottom:5px;">Recently Viewed PRs:</div>
          <div class="row" style="gap:6px;">
            ${staticRecentPrs.slice(0, 6).map(p => `<button class="small" type="button" onclick="openStaticPrRef('${String(`${p.owner}/${p.repo}#${p.number}`).replace(/'/g, "\\'")}')">${p.repo_full_name || `${p.owner}/${p.repo}`}#${p.number}</button>`).join("")}
          </div>
        </div>`
      : "";
    container.innerHTML = `<div class="card" id="staticWelcomeCard">
      <div style="font-size:19px; font-weight:700;">GitHub PR Sanskrit Character-Level Viewer (Static Browser Edition)</div>
      <div style="color:var(--muted); font-size:16px;">Paste any public GitHub Pull Request URL (e.g. <code>https://github.com/vishvAsa/bhAgavatam/pull/42</code> or <code>owner/repo#42</code>) or a repository (<code>owner/repo</code>) in the bar above and click <strong>Load PR</strong> — everything runs directly in your browser using the GitHub REST API and pure-JS <code>DiffSanEngine</code>.</div>
      <div class="row" style="gap:8px; margin-top:4px;">
        <button id="welcomeSamplePrBtn" class="primary" type="button" onclick="loadStaticSamplePr()">Load Sample Sanskrit PR (Offline Demo)</button>
        <button class="small" type="button" onclick="toggleStaticTokenRow()">🔑 Optional GitHub Token (Private repos / Posting)</button>
      </div>
      ${recentHtml}
    </div>`;
    const diffEl = document.getElementById("filesDiffContainer");
    if (diffEl) diffEl.innerHTML = "";
  }

  async function openStaticPrRef(refText, opts = {}) {
    const inp = document.getElementById("staticPrUrlInput");
    if (inp && refText) inp.value = refText;
    const preserveTargetParams = Boolean(opts && opts.preserveTargetParams);
    const parsed = parseStaticPrRef(refText, activePrKey ? `${activePrKey.owner}/${activePrKey.repo}` : (staticRepoTarget ? `${staticRepoTarget.owner}/${staticRepoTarget.repo}` : ""));
    if (!parsed) {
      if (typeof showError === "function") showError("Enter a valid GitHub PR URL (e.g. https://github.com/owner/repo/pull/123), owner/repo#123, or owner/repo.");
      return;
    }
    if (typeof showError === "function") showError("");
    staticRepoTarget = { owner: parsed.owner, repo: parsed.repo };
    const ownerEl = document.getElementById("ownerInput");
    if (ownerEl) ownerEl.value = parsed.owner;
    if (parsed.number != null) {
      _updateStaticPrUrlQuery(`${parsed.owner}/${parsed.repo}#${parsed.number}`, preserveTargetParams);
      if (typeof openPr === "function") {
        await openPr(parsed.owner, parsed.repo, parsed.number, false);
      }
      if (typeof allPrs !== "undefined") {
        const side = _collectPrSidebarSummaries(staticRepoTarget);
        allPrs = side.prs;
        if (typeof populateRepoSelect === "function") populateRepoSelect(side.repos, parsed.repo);
        if (typeof applyPrFilters === "function") applyPrFilters();
      }
    } else {
      _updateStaticPrUrlQuery(`${parsed.owner}/${parsed.repo}`, preserveTargetParams);
      if (typeof loadPrList === "function") {
        await loadPrList(false, parsed.repo);
      }
    }
  }

  function loadStaticPrFromInput() {
    const raw = document.getElementById("staticPrUrlInput")?.value || "";
    return openStaticPrRef(raw);
  }

  async function loadStaticSamplePr() {
    if (typeof showError === "function") showError("");
    const b = _seedSamplePrBundle();
    staticRepoTarget = { owner: b.owner, repo: b.repo };
    const opts = typeof getDiffOptions === "function" ? getDiffOptions() : { diff_mode: "word", normalize: "none", ignore_punct: false };
    const detail = _buildPrDetailFromRaw(b, opts);
    _rememberRecentPr(detail.summary);
    if (typeof allPrs !== "undefined") allPrs = [detail.summary];
    if (typeof populateRepoSelect === "function") populateRepoSelect([b.repo], b.repo);
    if (typeof applyPrFilters === "function") applyPrFilters();
    const ownerEl = document.getElementById("ownerInput");
    if (ownerEl) ownerEl.value = b.owner;
    const inp = document.getElementById("staticPrUrlInput");
    if (inp) inp.value = `${b.owner}/${b.repo}#${b.prObj.number}`;
    if (typeof activePrKey !== "undefined") activePrKey = { owner: b.owner, repo: b.repo, number: b.prObj.number };
    if (typeof activePrData !== "undefined") activePrData = detail;
    if (typeof renderActivePr === "function") renderActivePr();
    if (typeof setStatus === "function") setStatus("Loaded offline Sample Sanskrit PR", 2500);
  }

  function toggleStaticTokenRow() {
    const row = document.getElementById("staticTokenRow");
    if (!row) return;
    const opening = row.style.display === "none" || row.style.display === "";
    row.style.display = opening ? "flex" : "none";
    if (opening) {
      const ti = document.getElementById("staticTokenInput");
      if (ti) {
        ti.value = getStaticGithubToken();
        if (typeof ti.focus === "function") ti.focus();
      }
    }
  }

  async function saveStaticGithubToken(clear = false) {
    const ti = document.getElementById("staticTokenInput");
    const val = clear ? "" : (ti?.value || "");
    if (clear && ti) ti.value = "";
    setStaticGithubToken(val);
    const row = document.getElementById("staticTokenRow");
    if (row) row.style.display = "none";
    const meta = await handleStaticPrApi("/api/meta");
    const badge = document.getElementById("authBadge");
    if (badge) {
      if (meta.auth_user?.login) {
        badge.className = "pill pill-green";
        badge.textContent = `@${meta.auth_user.login}`;
      } else if (meta.has_token) {
        badge.className = "pill pill-blue";
        badge.textContent = "Token configured";
      } else {
        badge.className = "pill pill-amber";
        badge.textContent = "Public API (read-only)";
      }
    }
    if (typeof setStatus === "function") setStatus(val ? "GitHub token saved in browser localStorage" : "GitHub token cleared", 2500);
  }

  async function initStaticPrMode() {
    globalScope.staticApiHandler = handleStaticPrApi;
    const ctrlBar = document.getElementById("staticPrControls");
    if (ctrlBar) ctrlBar.style.display = "inline-flex";
    const ownerWrap = document.getElementById("ownerWrap");
    if (ownerWrap) ownerWrap.style.display = "none";
    const gdBtn = document.getElementById("gdSelectionBtn");
    if (gdBtn) gdBtn.style.display = "none";
    const badge = document.getElementById("authBadge");
    if (badge) {
      if (getStaticGithubToken()) {
        badge.className = "pill pill-blue";
        badge.textContent = "Token configured";
        try {
          const meta = await handleStaticPrApi("/api/meta");
          if (meta?.auth_user?.login) {
            badge.className = "pill pill-green";
            badge.textContent = `@${meta.auth_user.login}`;
          }
        } catch (_) {}
      } else {
        badge.className = "pill pill-amber";
        badge.textContent = "Public API (read-only)";
      }
    }

    let queryPr = "";
    try {
      const search = (globalScope.location && globalScope.location.search) || "";
      const usp = new URLSearchParams(search);
      queryPr = usp.get("pr") || usp.get("url") || "";
      if (!queryPr) {
        const qOwner = usp.get("owner") || "";
        const qRepo = usp.get("repo") || "";
        const qNum = usp.get("number") || usp.get("num") || "";
        if (qOwner && qRepo && qNum) queryPr = `${qOwner}/${qRepo}#${qNum}`;
        else if (qOwner && qRepo) queryPr = `${qOwner}/${qRepo}`;
      }
    } catch (_) {}

    if (queryPr) {
      if (queryPr.toLowerCase() === "sample") {
        await loadStaticSamplePr();
        return { handledInitialLoad: true };
      }
      await openStaticPrRef(queryPr, { preserveTargetParams: true });
      return { handledInitialLoad: true };
    }
    if (typeof allPrs !== "undefined") allPrs = staticRecentPrs.slice();
    if (typeof populateRepoSelect === "function") populateRepoSelect([], "");
    if (typeof applyPrFilters === "function") applyPrFilters();
    _showStaticWelcomeCard();
    return { handledInitialLoad: true };
  }

  globalScope.parseStaticPrRef = parseStaticPrRef;
  globalScope.decodeBase64Utf8 = decodeBase64Utf8;
  globalScope.handleStaticPrApi = handleStaticPrApi;
  globalScope.openStaticPrRef = openStaticPrRef;
  globalScope.loadStaticPrFromInput = loadStaticPrFromInput;
  globalScope.loadStaticSamplePr = loadStaticSamplePr;
  globalScope.toggleStaticTokenRow = toggleStaticTokenRow;
  globalScope.saveStaticGithubToken = saveStaticGithubToken;
  globalScope.initStaticPrMode = initStaticPrMode;
})(typeof window !== "undefined" ? window : globalThis);
