import feedparser
import requests
import os
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

JST = timezone(timedelta(hours=9))

FEEDS = {
    "🎨 デザイン": [
        ("Smashing Magazine", "https://www.smashingmagazine.com/feed/"),
        ("UX Collective", "https://uxdesign.cc/feed"),
        ("Figma Blog", "https://www.figma.com/blog/rss/"),
        ("Creative Bloq", "https://www.creativebloq.com/rss"),
        ("Awwwards", "https://www.awwwards.com/blog/feed/"),
    ],
    "🤖 AI・テクノロジー": [
        ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
        ("VentureBeat AI", "https://venturebeat.com/ai/feed/"),
        ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
        ("MIT Technology Review", "https://www.technologyreview.com/feed/"),
        ("AI News", "https://artificialintelligence-news.com/feed/"),
    ],
}

MAX_ITEMS_PER_SOURCE = 3
LOOKBACK_HOURS = 28  # スケジュールのズレを考慮して少し長めに


def parse_published(entry) -> datetime | None:
    for attr in ("published", "updated"):
        raw = entry.get(attr)
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass
    return None


def fetch_section(category: str, sources: list[tuple]) -> tuple[str, int]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    lines = [f"## {category}\n"]
    total = 0

    for name, url in sources:
        try:
            feed = feedparser.parse(url)
        except Exception:
            continue

        items = []
        for entry in feed.entries:
            pub = parse_published(entry)
            if pub and pub < cutoff:
                continue
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            if title and link:
                items.append((title, link, pub))

        items = items[:MAX_ITEMS_PER_SOURCE]
        if not items:
            continue

        lines.append(f"### {name}")
        for title, link, pub in items:
            time_str = pub.astimezone(JST).strftime("%H:%M JST") if pub else ""
            suffix = f" `{time_str}`" if time_str else ""
            lines.append(f"- [{title}]({link}){suffix}")
        lines.append("")
        total += len(items)

    return "\n".join(lines), total


def build_body() -> str:
    now_jst = datetime.now(JST).strftime("%Y年%m月%d日 %H:%M JST")
    sections = []
    total = 0

    for category, sources in FEEDS.items():
        section, count = fetch_section(category, sources)
        sections.append(section)
        total += count

    footer = (
        "\n---\n"
        f"*{now_jst} 時点の直近ニュースをまとめました。*  \n"
        f"*取得件数: {total} 件*"
    )

    if total == 0:
        return "直近24時間以内の新着ニュースは見つかりませんでした。\n" + footer

    return "\n".join(sections) + footer


def ensure_label(repo: str, token: str, headers: dict) -> None:
    url = f"https://api.github.com/repos/{repo}/labels/news"
    resp = requests.get(url, headers=headers, timeout=10)
    if resp.status_code == 404:
        requests.post(
            f"https://api.github.com/repos/{repo}/labels",
            json={"name": "news", "color": "0075ca", "description": "Daily news digest"},
            headers=headers,
            timeout=10,
        )


def post_issue(title: str, body: str) -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ["GITHUB_TOKEN"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    ensure_label(repo, token, headers)
    payload = {
        "title": title,
        "body": body,
        "labels": ["news"],
    }
    resp = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        json=payload,
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    issue_url = resp.json().get("html_url", "")
    print(f"Issue created: {issue_url}")


if __name__ == "__main__":
    date_str = datetime.now(JST).strftime("%Y/%m/%d")
    title = f"📰 Daily Design & AI News — {date_str}"
    body = build_body()
    post_issue(title, body)
