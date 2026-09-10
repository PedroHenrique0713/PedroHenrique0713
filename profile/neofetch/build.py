"""Render the neofetch-style profile card (dark.svg, light.svg) from live GitHub data.

Runs daily in Actions with the standard library only. GH_TOKEN must be able to read
the owner's private and organization repositories, or the commit and line counts
only cover what it can see.
"""
import base64
import datetime as dt
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).parent
LOGIN = os.environ.get("GH_LOGIN", "PedroHenrique0713")
TOKEN = os.environ["GH_TOKEN"]
CAREER_START = dt.date(2020, 5, 1)
# PRs to these owners are day-job or personal work, not upstream open source.
OWN_OWNERS = [LOGIN, "SOLABS-ORG", "starhash-ai"]
OWNER_NAMES = {"supabase": "Supabase", "questdb": "QuestDB", "calcom": "Cal.com", "Infisical": "Infisical"}
# A commit this big is a lockfile, a vendored library or generated code, not writing.
MAX_COMMIT_LINES = 5000
WIDTH = 64  # info panel, in characters

THEMES = {
    "dark": {
        "bar": "#161b22", "body": "#0d1117", "border": "#30363d",
        "txt": "#e6edf3", "dim": "#484f58", "key": "#39d353", "val": "#a5d6ff",
        "add": "#3fb950", "del": "#f85149", "title": "#8b949e",
        "spark": ["#2d333b", "#0e4429", "#006d32", "#26a641", "#39d353"],
    },
    "light": {
        "bar": "#f6f8fa", "body": "#ffffff", "border": "#d0d7de",
        "txt": "#1f2328", "dim": "#afb8c1", "key": "#1a7f37", "val": "#0550ae",
        "add": "#1a7f37", "del": "#cf222e", "title": "#57606a",
        "spark": ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"],
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
    query = f"is:pr author:{LOGIN} {state} {excluded}"
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


def uptime(today):
    months = (today.year - CAREER_START.year) * 12 + today.month - CAREER_START.month
    years, months = divmod(months, 12)
    plural = lambda n, word: f"{n} {word}{'' if n == 1 else 's'}"
    return f"{plural(years, 'year')}, {plural(months, 'month')} since the first client"


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
        "uptime": uptime(today),
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


def dots(n):
    return " " + "." * n + " " if n > 1 else " " * max(n + 1, 1)


def kv(key, value, total=WIDTH):
    if isinstance(value, str):
        value = [(value, "val")]
    head = [(". ", "dim"), (key, "key"), (":", "txt")]
    gap = total - width(head) - width(value) - 2
    return head + [(dots(gap), "dim")] + value


def section(title):
    return [("- ", "dim"), (title, "txt"), (" " + "─" * (WIDTH - len(title) - 3), "dim")]


def blank():
    return [(".", "dim")]


def prompt(command):
    return [("pedro@solabs", "key"), (":", "txt"), ("~", "val"), ("$ ", "txt"), (command, "txt")]


def num(n):
    return f"{n:,}"


def lines(d):
    owners = ", ".join(f"{OWNER_NAMES.get(o, o)}: {n}" for o, n in d["merged_by_owner"])
    latest = d["latest"]
    latest_text = (f"{latest['repository']['owner']['login']}/{latest['repository']['name']}"
                   f"#{latest['number']} · {latest['mergedAt'][:10]}" if latest else "none yet")
    half = WIDTH // 2 + 4
    stats = (kv("Repos", [(num(d["repos"]), "val"), (" {", "dim"), ("Contributed: ", "key"),
                          (num(d["contributed"]), "val"), ("}", "dim")], half)
             + [(" | ", "dim")]
             + kv("Commits", num(d["commits"]), WIDTH - half - 3)[1:])
    return [
        prompt("neofetch"),
        [("pedro", "key"), ("@", "txt"), ("solabs", "key"), (" " + "─" * (WIDTH - 13), "dim")],
        kv("OS", "Fedora Linux 44"),
        kv("Uptime", d["uptime"]),
        kv("Host", "Solabs · AI Lab"),
        kv("Kernel", "AI Engineer · marketing → data → code"),
        kv("IDE", "VS Code + Claude Code"),
        kv("Memory", "brain-template · one vault for every AI CLI"),
        blank(),
        kv("Languages.Programming", "TypeScript, Python, SQL, Dart"),
        kv("Languages.Real", "Portuguese, English"),
        kv("Stack.Automation", "n8n, Make, Supabase, Postgres"),
        kv("Stack.App", "React, Next.js, Flutter, FastAPI"),
        kv("Stack.AI", "Claude, OpenRouter, Gemini, Groq"),
        section("In production"),
        kv("Agents", "AI SDRs on WhatsApp + IG, 300+ leads/day"),
        kv("Tracking", "Meta CAPI + Google Ads offline conversions"),
        kv("CRM", "multi-tenant SaaS, RLS, 20 active clients"),
        section("Open Source"),
        kv("Merged.Upstream", [(num(d["merged"]), "val"), (" {", "dim"), (owners, "key"), ("}", "dim")]),
        kv("In.Review", f"{d['open']} PRs across {d['open_repos']} repos"),
        kv("Latest.Merge", latest_text),
        section("Contact"),
        kv("Email", "pluspedrohenrique@gmail.com"),
        kv("LinkedIn", "in/pedrohenriquequadro"),
        kv("Web", "dev.pedroquadro.com"),
        section("GitHub Stats"),
        stats,
        kv("Contributions (last 52 weeks)", num(d["contributions"])),
        "SPARK",
        kv("Lines of Code on GitHub", [(num(d["loc_add"] - d["loc_del"]), "val"), (" ( ", "dim"),
                                       (num(d["loc_add"]) + "++", "add"), (", ", "dim"),
                                       (num(d["loc_del"]) + "--", "del"), (" )", "dim")]),
        prompt(""),
    ]


def spark_runs(weeks):
    peak = max(weeks) or 1
    runs = [(". ", "dim"), (" " * (WIDTH - 2 - len(weeks)), "dim")]
    for count in weeks:
        level = 0 if count == 0 else min(4, 1 + int(3 * count / peak + 0.5))
        block = "▁▂▃▄▅▆▇█"[0 if count == 0 else max(1, round(7 * count / peak))]
        runs.append((block, f"s{level}"))
    return runs


def svg(theme, data, portrait, font_b64, today):
    t = THEMES[theme]
    pad, bar, gap, lh, fs = 20, 36, 28, 20, 15
    cw = fs * 0.6
    cols, rows = portrait["cols"], portrait["rows"]
    cell_w = portrait["cell"][0]
    body = [spark_runs(data["weeks"]) if line == "SPARK" else line for line in lines(data)]
    panel_x = pad + cols * cell_w + gap
    top = bar + 18
    panel_h = len(body) * lh
    w = round(panel_x + WIDTH * cw + pad)
    h = top + panel_h + 14
    ascii_lh = panel_h / rows

    css = [
        f"@font-face{{font-family:'SCP';src:url(data:font/woff2;base64,{font_b64}) format('woff2');}}",
        "text{font-family:'SCP',ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre;}",
        f".p{{font-size:{fs}px}}.a{{font-size:{cell_w / 0.6:g}px}}.t{{font-size:12px;fill:{t['title']}}}",
    ]
    css += [f".{k}{{fill:{t[k]}}}" for k in ("txt", "dim", "key", "val", "add", "del")]
    css += [f".s{i}{{fill:{c}}}" for i, c in enumerate(t["spark"])]
    css += [f".c{i}{{fill:{c}}}" for i, c in enumerate(portrait[theme]["palette"])]

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'role="img" aria-labelledby="title">',
        "<title id=\"title\">Pedro Henrique, AI Engineer: stack, systems in production, "
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
    for i, color in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
        out.append(f'<circle cx="{22 + i * 20}" cy="{bar / 2}" r="6" fill="{color}"/>')
    out.append(f'<text x="{w / 2}" y="{bar / 2 + 4}" text-anchor="middle" class="t">pedro@solabs: ~</text>')
    out.append(f'<text x="{w - 16}" y="{bar / 2 + 4}" text-anchor="end" class="t">updated {today.isoformat()}</text>')

    for y, runs in enumerate(portrait[theme]["rows"]):
        spans = "".join(f'<tspan class="c{c}">{escape(s)}</tspan>' for s, c in runs)
        out.append(f'<text x="{pad}" y="{top + ascii_lh * (y + 0.8):.1f}" class="a">{spans}</text>')

    for i, runs in enumerate(body):
        y = top + lh * i + 14
        spans = "".join(f'<tspan class="{c}">{escape(s)}</tspan>' for s, c in runs if s)
        out.append(f'<text x="{panel_x}" y="{y}" class="p">{spans}</text>')

    cursor_x = panel_x + width(body[-1]) * cw
    out.append(
        f'<rect x="{cursor_x:.1f}" y="{top + lh * (len(body) - 1)}" width="{cw:.1f}" height="17" '
        f'fill="{t["key"]}"><animate attributeName="opacity" values="1;1;0;0" '
        'keyTimes="0;0.5;0.5;1" dur="1.1s" repeatCount="indefinite"/></rect>'
    )
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main():
    today = dt.datetime.now(dt.timezone.utc).date()
    data = fetch(today)
    portrait = json.loads((HERE / "portrait.json").read_text())
    font_b64 = base64.b64encode((HERE / "mono-subset.woff2").read_bytes()).decode()
    for theme in THEMES:
        (HERE / f"{theme}.svg").write_text(svg(theme, data, portrait, font_b64, today))
    print(f"card rendered: {data['merged']} merged upstream, {data['open']} open, "
          f"{data['commits']} commits, {data['contributions']} contributions in 52 weeks")


if __name__ == "__main__":
    main()
