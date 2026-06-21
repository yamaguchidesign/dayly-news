import feedparser
import requests
import os
import json
import logging
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
RSS_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; dayly-news/1.0)"}

FEEDS = {
    "🎨 デザイン": [
        ("Sidebar.io", "https://sidebar.io/feed"),
        ("Designer News", "https://www.designernews.co/?format=rss"),
        ("Figma Blog", "https://www.figma.com/blog/rss/"),
        ("Airbnb Design", "https://airbnb.design/feed/"),
        ("Smashing Magazine", "https://www.smashingmagazine.com/feed/"),
        ("UX Collective", "https://uxdesign.cc/feed"),
    ],
    "📦 プロダクト・SaaS": [
        ("Linear Blog", "https://linear.app/blog/rss.xml"),
        ("Stripe Blog", "https://stripe.com/blog/feed.rss"),
        ("Vercel Blog", "https://vercel.com/blog/rss.xml"),
        ("Lenny's Newsletter", "https://www.lennysnewsletter.com/feed"),
        ("Mind the Product", "https://www.mindtheproduct.com/feed/"),
        ("First Round Review", "https://review.firstround.com/feed/"),
    ],
    "🚀 スタートアップ・グロース": [
        ("SaaStr", "https://www.saastr.com/feed/"),
        ("Andrew Chen", "https://andrewchen.com/feed/"),
        ("Product Hunt", "https://www.producthunt.com/feed"),
        ("Y Combinator Blog", "https://www.ycombinator.com/blog/rss.xml"),
    ],
    "🤖 AI・テクノロジー": [
        ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
        ("VentureBeat AI", "https://venturebeat.com/ai/feed/"),
        ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
        ("MIT Technology Review", "https://www.technologyreview.com/feed/"),
    ],
}

MAX_ITEMS_PER_SOURCE = 2
LOOKBACK_HOURS = 100  # 週2回配信（最大96時間間隔）に合わせて余裕を持たせる
MAX_TOTAL_ARTICLES = 5


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


def fetch_all_articles() -> dict[str, list[dict]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    result: dict[str, list[dict]] = {}

    for category, sources in FEEDS.items():
        articles = []
        for name, url in sources:
            try:
                resp = requests.get(url, headers=RSS_HEADERS, timeout=15)
                resp.raise_for_status()
                feed = feedparser.parse(resp.text)
            except Exception as e:
                log.warning(f"Failed to fetch {name}: {e}")
                continue

            count = 0
            for entry in feed.entries:
                if count >= MAX_ITEMS_PER_SOURCE:
                    break
                pub = parse_published(entry)
                if pub and pub < cutoff:
                    continue
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                if title and link:
                    articles.append({
                        "source": name,
                        "title": title,
                        "link": link,
                        "published": pub.astimezone(JST).strftime("%m/%d %H:%M") if pub else "",
                    })
                    count += 1

        if articles:
            result[category] = articles

    return result


def process_with_ai(articles_by_category: dict[str, list[dict]]) -> dict[str, list[dict]]:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # 全記事をフラットリストに変換（IDで紐付け）
    flat: list[dict] = []
    for category, articles in articles_by_category.items():
        for article in articles:
            flat.append({
                "id": len(flat),
                "category": category,
                **article,
            })

    flat = flat[:MAX_TOTAL_ARTICLES]

    articles_for_prompt = [{"id": a["id"], "source": a["source"], "title": a["title"]} for a in flat]
    articles_json = json.dumps(articles_for_prompt, ensure_ascii=False, indent=2)

    prompt = f"""あなたはFDD（Functional Design Direction：事業を理解して解決策を設計するデザイナー）の視点でニュースを解説するアシスタントです。

以下の記事リストを日本語に翻訳・要約してください。
各記事について以下のJSON形式で返してください：

{{
  "id": <元のid>,
  "title_ja": "日本語タイトル（自然な日本語に翻訳）",
  "summary": ["1行目（20-40文字）", "2行目（20-40文字）", "3行目（20-40文字）"],
  "importance": "なぜ重要か（40-80文字。FDD視点＝デザインと事業成長・UX・プロダクト戦略の交差点で解説）"
}}

FDD視点とは：デザインを見た目だけでなく、事業成長・ユーザー体験・プロダクト戦略・グロースへの影響として捉えること。

記事リスト：
{articles_json}

JSONの配列のみ返してください。```json などのマークダウンや説明文は不要。"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
            raw = raw.rsplit("```", 1)[0].strip()
        ai_results = json.loads(raw)
        ai_map = {r["id"]: r for r in ai_results}
        log.info(f"AI processed {len(ai_map)} articles.")
    except Exception as e:
        log.error(f"AI processing failed: {e}")
        ai_map = {}

    # AI結果をカテゴリ別に再構築
    enriched_by_category: dict[str, list[dict]] = {}
    for article in flat:
        cat = article["category"]
        ai = ai_map.get(article["id"], {})
        enriched = {
            **article,
            "title_ja": ai.get("title_ja", article["title"]),
            "summary": ai.get("summary", []),
            "importance": ai.get("importance", ""),
        }
        enriched_by_category.setdefault(cat, []).append(enriched)

    return enriched_by_category


def build_slack_blocks(articles_by_category: dict[str, list[dict]]) -> list[dict]:
    now_jst = datetime.now(JST).strftime("%Y年%m月%d日")
    total = sum(len(v) for v in articles_by_category.values())

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📰 Dayly News — {now_jst}", "emoji": True},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"デザイン / プロダクト / AI / スタートアップ  ·  {total} 記事"}],
        },
        {"type": "divider"},
    ]

    for category, articles in articles_by_category.items():
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{category}*"},
        })

        for a in articles:
            title_ja = a.get("title_ja", a["title"])
            link = a["link"]
            source = a["source"]
            published = a.get("published", "")
            summary = a.get("summary", [])
            importance = a.get("importance", "")

            meta = f"`{source}`"
            if published:
                meta += f"  _{published}_"

            lines = [f"*<{link}|{title_ja}>*  {meta}"]
            for s in summary:
                lines.append(f"• {s}")
            if importance:
                lines.append(f"💡 _{importance}_")

            text = "\n".join(lines)
            if len(text) > 2900:
                text = text[:2900] + "…"

            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            })

        blocks.append({"type": "divider"})

    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f"_直近{LOOKBACK_HOURS}時間の新着 · {datetime.now(JST).strftime('%H:%M JST')}_"}
        ],
    })

    return blocks


def post_to_slack(blocks: list[dict]) -> None:
    webhook_url = os.environ["SLACK_WEBHOOK_URL"]
    resp = requests.post(webhook_url, json={"blocks": blocks}, timeout=30)
    resp.raise_for_status()
    log.info("Posted to Slack successfully.")


if __name__ == "__main__":
    log.info("Fetching articles...")
    articles_by_category = fetch_all_articles()

    total = sum(len(v) for v in articles_by_category.values())
    log.info(f"Fetched {total} articles across {len(articles_by_category)} categories.")

    if total == 0:
        log.info("No new articles. Skipping.")
    else:
        log.info("Processing with AI...")
        processed = process_with_ai(articles_by_category)

        log.info("Building Slack message...")
        blocks = build_slack_blocks(processed)

        log.info("Posting to Slack...")
        post_to_slack(blocks)
