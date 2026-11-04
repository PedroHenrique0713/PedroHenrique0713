"""Render the Fedora terminal profile card from live GitHub data.

Runs daily in Actions with the standard library only. GH_TOKEN must be able to read
the owner's private and organization repositories, or the commit and line counts
only cover what it can see.
"""
import base64
import datetime as dt
import hashlib
import itertools
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).parent
LOGIN = os.environ.get("GH_LOGIN", "PedroHenrique0713")
TOKEN = os.environ["GH_TOKEN"]
# PRs to these owners are day-job or personal work, not upstream open source.
OWN_OWNERS = [LOGIN, "SOLABS-ORG", "starhash-ai", "Starhash-LTDA"]
OWNER_NAMES = {"supabase": "Supabase", "questdb": "QuestDB", "calcom": "Cal.com", "Infisical": "Infisical"}
# A commit this big is a lockfile, a vendored library or generated code, not writing.
MAX_COMMIT_LINES = 5000
WIDTH = 58  # information beside the Fedora logo, in monospace characters
VALUE_COLUMN = 15

THEMES = {
    "dark": {
        "bar": "#303030", "body": "#000000", "border": "#454545",
        "txt": "#f6f5f4", "dim": "#a6a6a6", "key": "#99c1f1", "val": "#f6f5f4",
        "add": "#8ff0a4", "del": "#ff7b63", "title": "#deddda", "control": "#454545",
        "logo1": "#51a2da", "logo2": "#f6f5f4",
        "spark": ["#454545", "#1c4c80", "#3584e4", "#62a0ea", "#99c1f1"],
    },
    "light": {
        "bar": "#ebebeb", "body": "#ffffff", "border": "#c0bfbc",
        "txt": "#241f31", "dim": "#626262", "key": "#1c71d8", "val": "#241f31",
        "add": "#1a7f37", "del": "#c01c28", "title": "#3d3846", "control": "#d5d3d0",
        "logo1": "#3c6eb4", "logo2": "#1f3b73",
        "spark": ["#deddda", "#b5d5ff", "#62a0ea", "#3584e4", "#1c71d8"],
    },
}


def gql(query, **variables):
    payload = json.dumps({"query": query, "variables": variables}).encode()
    for attempt in range(4):
        req = urllib.request.Request(
            "https://api.github.com/graphql",
            data=payload,
            headers={"Authorization": f"bearer {TOKEN}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as err:
            if err.code in (502, 503, 504) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise SystemExit(f"GitHub GraphQL returned HTTP {err.code}")
        if body.get("errors"):
            # Messages can name private repositories, and Actions logs are public.
            raise SystemExit(f"GitHub GraphQL error: {body['errors'][0].get('type', 'unknown')}")
        return body["data"]


PROFILE_QUERY = """
query($login: String!) {
  user(login: $login) {
    id
    repositories(ownerAffiliations: OWNER, isFork: false) { totalCount }
    repositoriesContributedTo(first: 1, includeUserRepositories: true,
                              contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY]) { totalCount }
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { contributionCount } }
      }
    }
  }
}"""

SEARCH_QUERY = """
query($q: String!, $cursor: String) {
  search(query: $q, type: ISSUE, first: 100, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { number mergedAt repository { name owner { login } } } }
  }
}"""

REPOS_QUERY = """
query($login: String!, $author: ID!, $cursor: String) {
  user(login: $login) {
    repositories(first: 50, after: $cursor, isFork: false,
                 ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        defaultBranchRef { target { ... on Commit { history(author: {id: $author}) { totalCount } } } }
      }
    }
  }
}"""

HISTORY_QUERY = """
query($owner: String!, $name: String!, $author: ID!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { target { ... on Commit {
      history(first: 100, after: $cursor, author: {id: $author}) {
        pageInfo { hasNextPage endCursor }
        nodes { additions deletions }
      }
    } } }
  }
}"""


def search_prs(state):
    excluded = " ".join(f"-user:{owner}" for owner in OWN_OWNERS)
    query = f"is:pr is:public author:{LOGIN} {state} {excluded}"
    nodes, cursor = [], None
    while True:
        page = gql(SEARCH_QUERY, q=query, cursor=cursor)["search"]
        nodes += [n for n in page["nodes"] if n]
        if not page["pageInfo"]["hasNextPage"]:
            return page["issueCount"], nodes
        cursor = page["pageInfo"]["endCursor"]


def lines_of_code(author_id):
    """Sum additions/deletions of the author's commits on every default branch.

    Repositories are cached by a hash of their name (never the name: the cache is
    public and many of these repos are private) plus the author's commit count, so
    only repos with new commits are walked again.
    """
    cache_file = HERE / "loc-cache.json"
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    fresh, cursor = {}, None
    while True:
        page = gql(REPOS_QUERY, login=LOGIN, author=author_id, cursor=cursor)["user"]["repositories"]
        for repo in page["nodes"]:
            branch = repo["defaultBranchRef"]
            commits = branch["target"]["history"]["totalCount"] if branch else 0
            if not commits:
                continue
            key = hashlib.sha256(repo["nameWithOwner"].encode()).hexdigest()
            if key in cache and cache[key][0] == commits:
                fresh[key] = cache[key]
                continue
            owner, name = repo["nameWithOwner"].split("/")
            added = deleted = 0
            hist_cursor = None
            while True:
                hist = gql(HISTORY_QUERY, owner=owner, name=name, author=author_id,
                           cursor=hist_cursor)["repository"]["defaultBranchRef"]["target"]["history"]
                for c in hist["nodes"]:
                    if c["additions"] + c["deletions"] <= MAX_COMMIT_LINES:
                        added += c["additions"]
                        deleted += c["deletions"]
                if not hist["pageInfo"]["hasNextPage"]:
                    break
                hist_cursor = hist["pageInfo"]["endCursor"]
            fresh[key] = [commits, added, deleted]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    cache_file.write_text(json.dumps(fresh, indent=0, sort_keys=True) + "\n")
    return (sum(v[0] for v in fresh.values()),
            sum(v[1] for v in fresh.values()),
            sum(v[2] for v in fresh.values()))


def fetch(today):
    user = gql(PROFILE_QUERY, login=LOGIN)["user"]
    calendar = user["contributionsCollection"]["contributionCalendar"]
    weeks = [sum(d["contributionCount"] for d in w["contributionDays"]) for w in calendar["weeks"]][-52:]
    merged_count, merged = search_prs("is:merged")
    open_count, open_prs = search_prs("is:open")
    by_owner = {}
    for pr in merged:
        owner = pr["repository"]["owner"]["login"]
        by_owner[owner] = by_owner.get(owner, 0) + 1
    latest = max(merged, key=lambda pr: pr["mergedAt"]) if merged else None
    commits, added, deleted = lines_of_code(user["id"])
    return {
        "repos": user["repositories"]["totalCount"],
        "contributed": user["repositoriesContributedTo"]["totalCount"],
        "contributions": calendar["totalContributions"],
        "weeks": weeks,
        "merged": merged_count,
        "merged_by_owner": sorted(by_owner.items(), key=lambda kv: (-kv[1], kv[0].lower())),
        "latest": latest,
        "open": open_count,
        "open_repos": len({(p["repository"]["owner"]["login"], p["repository"]["name"]) for p in open_prs}),
        "commits": commits,
        "loc_add": added,
        "loc_del": deleted,
    }


# A line is a list of (text, css class) runs; its visible width is the sum of the texts.

def width(runs):
    return sum(len(t) for t, _ in runs)


def kv(key, value):
    if isinstance(value, str):
        value = [(value, "val")]
    return [(key, "key"), (":", "dim"), (" " * max(2, VALUE_COLUMN - len(key) - 1), "txt")] + value


def section(title):
    return [("# " + title, "dim")]


def blank():
    return []


def prompt(command):
    return [("[", "txt"), ("pedro@fedora", "add"), (" ", "txt"),
            ("~", "key"), ("]$ ", "txt"), (command, "txt")]


def num(n):
    return f"{n:,}"


def lines(d):
    owners = ", ".join(f"{OWNER_NAMES.get(o, o)}: {n}" for o, n in d["merged_by_owner"])
    latest = d["latest"]
    latest_text = (f"{latest['repository']['owner']['login']}/{latest['repository']['name']}"
                   f"#{latest['number']} · {latest['mergedAt'][:10]}" if latest else "none yet")
    return [
        prompt("neofetch"),
        blank(),
        [("Pedro Henrique", "txt")],
        kv("OS", "Fedora Linux 44"),
        kv("Role", "AI Engineer · marketing → data → code"),
        kv("Work", "Solabs · AI Lab"),
        kv("Editor", "VS Code + Claude Code"),
        kv("Memory", "brain-template · one vault for every AI CLI"),
        blank(),
        kv("Languages", "TypeScript, Python, SQL, Dart"),
        kv("Spoken", "Portuguese, English"),
        kv("Automation", "n8n, Make, Supabase, Postgres"),
        kv("Applications", "React, Next.js, Flutter, FastAPI"),
        kv("AI", "Claude, OpenRouter, Gemini, Groq"),
        section("In production"),
        kv("Agents", "AI SDRs on WhatsApp + IG, 300+ leads/day"),
        kv("Tracking", "Meta CAPI + Google Ads offline conversions"),
        kv("CRM", "multi-tenant SaaS, RLS, 20 active clients"),
        section("Open Source"),
        kv("Merged", f"{num(d['merged'])} PRs · {owners}"),
        kv("In review", f"{d['open']} PRs across {d['open_repos']} repos"),
        kv("Latest merge", latest_text),
        section("GitHub · last 52 weeks of contributions"),
        kv("Repositories", f"{num(d['repos'])} own · {num(d['contributed'])} contributed to"),
        kv("Commits", num(d["commits"])),
        kv("Contributions", num(d["contributions"])),
        "SPARK",
        kv("Lines of code", [(num(d["loc_add"] - d["loc_del"]), "val"), (" (", "dim"),
                                       (num(d["loc_add"]) + "++", "add"), (", ", "dim"),
                                       (num(d["loc_del"]) + "--", "del"), (")", "dim")]),
        section("Contact"),
        kv("Email", "pluspedrohenrique@gmail.com"),
        kv("LinkedIn", "in/pedrohenriquequadro"),
        kv("Web", "dev.pedroquadro.com"),
        blank(),
        prompt(""),
    ]


def spark_runs(weeks):
    peak = max(weeks) or 1
    runs = [(" " * VALUE_COLUMN, "dim")]
    for count in weeks:
        level = 0 if count == 0 else min(4, 1 + int(3 * count / peak + 0.5))
        block = "▁▂▃▄▅▆▇█"[0 if count == 0 else max(1, round(7 * count / peak))]
        runs.append((block, f"s{level}"))
    return runs


def wrap_runs(runs, columns=WIDTH):
    chars = [(char, cls) for text, cls in runs for char in text]
    while len(chars) > columns:
        split = next((i for i in range(columns, VALUE_COLUMN, -1) if chars[i][0] == " "), columns)
        yield [("".join(char for char, _ in group), cls)
               for cls, group in itertools.groupby(chars[:split], key=lambda item: item[1])]
        rest = chars[split:]
        while rest and rest[0][0] == " ":
            rest.pop(0)
        chars = [(" ", "txt")] * VALUE_COLUMN + rest
    yield [("".join(char for char, _ in group), cls)
           for cls, group in itertools.groupby(chars, key=lambda item: item[1])]


def fedora_logo():
    """Read Fastfetch's two-color ASCII; attribution is in FEDORA-LOGO-LICENSE.txt."""
    result = []
    color = "logo1"
    for line in (HERE / "fedora.txt").read_text().splitlines():
        runs = []
        for part in re.split(r"(\$[12])", line):
            if part in ("$1", "$2"):
                color = "logo" + part[1]
            elif part:
                runs.append((part, color))
        result.append(runs)
    return result


def svg(theme, data, font_b64, today):
    t = THEMES[theme]
    pad, bar, lh, fs, gap, logo_lh = 26, 48, 22, 16, 28, 18
    cw = fs * 0.6
    logo = fedora_logo()
    logo_w = max(width(line) for line in logo) * cw
    panel_x = pad + logo_w + gap
    w = round(panel_x + WIDTH * cw + pad)
    full_columns = int((w - 2 * pad) / cw)
    body = lines(data)
    split = body.index(section("Open Source"))
    intro = [wrapped for line in body[2:split] for wrapped in wrap_runs(line)]
    rest = [wrapped for line in body[split:]
            for wrapped in wrap_runs(spark_runs(data["weeks"]) if line == "SPARK" else line, full_columns)]
    top = bar + 22
    intro_top = top + 2 * lh
    rest_top = intro_top + max(len(intro) * lh, len(logo) * logo_lh) + 12
    h = rest_top + len(rest) * lh + 26

    css = [
        f"@font-face{{font-family:'SCP';src:url(data:font/woff2;base64,{font_b64}) format('woff2');}}",
        "text{font-family:'SCP',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre;}",
        f".p,.logo{{font-size:{fs}px}}.t{{font-size:13px;fill:{t['title']}}}",
    ]
    css += [f".{k}{{fill:{t[k]}}}" for k in ("txt", "dim", "key", "val", "add", "del", "logo1", "logo2")]
    css += [f".s{i}{{fill:{c}}}" for i, c in enumerate(t["spark"])]

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'role="img" aria-labelledby="title" xml:space="preserve">',
        "<title id=\"title\">Pedro Henrique, AI Engineer — Fedora neofetch with ASCII logo: stack, systems in production, "
        "open source and live GitHub stats</title>",
        f"<style>{''.join(css)}</style>",
        f'<clipPath id="frame"><rect width="{w}" height="{h}" rx="12"/></clipPath>',
        '<g clip-path="url(#frame)">',
        f'<rect width="{w}" height="{h}" fill="{t["body"]}"/>',
        f'<rect width="{w}" height="{bar}" fill="{t["bar"]}"/>',
        f'<line x1="0" y1="{bar}" x2="{w}" y2="{bar}" stroke="{t["border"]}"/>',
        "</g>",
        f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="12" fill="none" stroke="{t["border"]}"/>',
    ]
    out += [
        f'<g fill="none" stroke="{t["title"]}" stroke-width="1.5" stroke-linecap="round">',
        '<path d="M21 16h17v14H21z M29.5 16v14 M26 23h7"/>',
        f'<path d="M{w - 77} 19h14 M{w - 77} 24h14 M{w - 77} 29h14"/>',
        '</g>',
        f'<circle cx="{w - 27}" cy="24" r="12" fill="{t["control"]}"/>',
        f'<path d="M{w - 30} 21l6 6 M{w - 30} 27l6 -6" fill="none" '
        f'stroke="{t["title"]}" stroke-width="1.5" stroke-linecap="round"/>',
        f'<text x="{w / 2}" y="29" text-anchor="middle" class="t">pedro@fedora: ~</text>',
    ]

    positioned = [(pad, top, body[0], "p")]
    positioned += [(pad, intro_top + logo_lh * i, runs, "logo") for i, runs in enumerate(logo)]
    positioned += [(panel_x, intro_top + lh * i, runs, "p") for i, runs in enumerate(intro)]
    positioned += [(pad, rest_top + lh * i, runs, "p") for i, runs in enumerate(rest)]
    for x, y, runs, cls in positioned:
        spans = "".join(f'<tspan class="{c}">{escape(s)}</tspan>' for s, c in runs if s)
        # Keep the ASCII grid exact even when font hinting rounds glyph advances.
        length = f' textLength="{width(runs) * cw:g}" lengthAdjust="spacingAndGlyphs"' if runs else ""
        out.append(f'<text x="{x:g}" y="{y + 14:g}" class="{cls}"{length}>{spans}</text>')

    cursor_x = pad + width(rest[-1]) * cw
    out.append(
        f'<rect x="{cursor_x:.1f}" y="{rest_top + lh * (len(rest) - 1)}" width="{cw:.1f}" height="17" '
        f'fill="{t["txt"]}"/>'
    )
    out.append(f'<text x="{w - pad}" y="{h - 18}" text-anchor="end" class="t">updated {today.isoformat()}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main():
    today = dt.datetime.now(dt.timezone.utc).date()
    data = fetch(today)
    font_b64 = base64.b64encode((HERE / "mono-subset.woff2").read_bytes()).decode()
    for theme in THEMES:
        (HERE / f"{theme}.svg").write_text(svg(theme, data, font_b64, today))
    print(f"card rendered: {data['merged']} merged upstream, {data['open']} open, "
          f"{data['commits']} commits, {data['contributions']} contributions in 52 weeks")


if __name__ == "__main__":
    main()
