import feedparser
import requests
import os
import json
import logging
import hashlib
import re
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
RSS_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; dayly-news/1.0)"}

GITHUB_PAGES_BASE = "https://yamaguchidesign.github.io/dayly-news"
DOCS_DIR = Path(__file__).parent.parent / "docs" / "articles"

# Haiku 4.5 pricing (USD/1M tokens)
HAIKU_INPUT_PRICE = 1.00
HAIKU_OUTPUT_PRICE = 5.00
USD_TO_JPY = 150

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
LOOKBACK_HOURS = 100
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

    flat: list[dict] = []
    for category, articles in articles_by_category.items():
        for article in articles:
            flat.append({"id": len(flat), "category": category, **article})

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


def fetch_article_content(url: str) -> str:
    """記事本文をHTMLから取得してテキスト化する"""
    try:
        resp = requests.get(url, headers=RSS_HEADERS, timeout=15)
        resp.raise_for_status()
        text = resp.text
        # script/styleタグを除去
        text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
        # HTMLタグ・エンティティを除去
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&[a-zA-Z]+;", " ", text)
        text = re.sub(r"&#\d+;", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:5000]
    except Exception as e:
        log.warning(f"Failed to fetch article content from {url}: {e}")
        return ""


def translate_article_content(title: str, content: str) -> tuple[str, dict]:
    """記事本文をHaikuで日本語翻訳する。(翻訳文, usage_info) を返す"""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = f"""以下の英語記事を自然な日本語に翻訳してください。
見出しは「## 見出し」形式で、段落は改行で区切ってください。
タイトル: {title}

本文:
{content}

翻訳文のみ返してください。"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        return response.content[0].text, usage
    except Exception as e:
        log.error(f"Translation failed: {e}")
        return "", {}


def url_to_slug(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:8]


def he(s: str) -> str:
    """HTML-escape"""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def generate_article_html(article: dict) -> str:
    title_ja = he(article.get("title_ja", article["title"]))
    source = he(article["source"])
    published = he(article.get("published", ""))
    category = he(article.get("category", ""))
    summary = article.get("summary", [])
    importance = article.get("importance", "")
    content_ja = article.get("content_ja", "")
    original_url = he(article["link"])

    summary_items = "".join(f"<li>{he(s)}</li>" for s in summary)
    importance_html = f'<p class="importance">💡 {he(importance)}</p>' if importance else ""

    usage = article.get("translation_usage", {})
    if usage:
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        cost_usd = (in_tok * HAIKU_INPUT_PRICE + out_tok * HAIKU_OUTPUT_PRICE) / 1_000_000
        cost_jpy = cost_usd * USD_TO_JPY
        token_info_html = (
            f'<p class="token-info">'
            f'翻訳使用トークン: 入力 {in_tok:,} / 出力 {out_tok:,}'
            f'&nbsp;&nbsp;|&nbsp;&nbsp;費用: 約 ¥{cost_jpy:.3f}'
            f'&nbsp;(claude-haiku-4-5, $1={USD_TO_JPY}円換算)'
            f'</p>'
        )
    else:
        token_info_html = ""

    if content_ja:
        paragraphs = []
        for line in content_ja.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("## "):
                paragraphs.append(f"<h2>{he(line[3:])}</h2>")
            elif line.startswith("# "):
                paragraphs.append(f"<h2>{he(line[2:])}</h2>")
            else:
                paragraphs.append(f"<p>{he(line)}</p>")
        content_html = "\n".join(paragraphs)
    else:
        content_html = "<p>（記事本文の取得に失敗しました。原文リンクからご確認ください。）</p>"

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_ja}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans','Yu Gothic',sans-serif;max-width:720px;margin:0 auto;padding:40px 20px 80px;color:#111;line-height:1.8;background:#fafafa}}
.tag{{font-size:.72rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#0070f3;margin-bottom:14px}}
h1{{font-size:1.75rem;font-weight:800;line-height:1.3;margin-bottom:10px;color:#000}}
.meta{{font-size:.82rem;color:#888;margin-bottom:32px}}
.summary-box{{background:#fff;border:1px solid #e5e5e5;border-radius:12px;padding:24px;margin-bottom:36px}}
.summary-box .label{{font-size:.68rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#aaa;margin-bottom:10px}}
.summary-box ul{{list-style:none;margin-bottom:14px}}
.summary-box li{{padding:4px 0 4px 18px;position:relative;font-size:.94rem}}
.summary-box li::before{{content:"—";position:absolute;left:0;color:#0070f3}}
.importance{{font-size:.87rem;color:#555;padding-top:12px;border-top:1px solid #f0f0f0;margin-top:4px}}
.content{{background:#fff;border-radius:12px;padding:32px;border:1px solid #e5e5e5}}
.content p{{margin-bottom:1.4em;font-size:.96rem}}
.content h2{{font-size:1.05rem;font-weight:700;margin:1.8em 0 .6em;color:#000}}
footer{{margin-top:36px;padding-top:18px;border-top:1px solid #eee;font-size:.82rem}}
footer a{{color:#aaa;text-decoration:none}}
footer a:hover{{color:#0070f3}}
.token-info{{margin-top:14px;font-size:.75rem;color:#bbb}}
</style>
</head>
<body>
<p class="tag">{category}</p>
<h1>{title_ja}</h1>
<p class="meta">{source}&nbsp;&nbsp;·&nbsp;&nbsp;{published}</p>
<div class="summary-box">
  <p class="label">要約</p>
  <ul>{summary_items}</ul>
  {importance_html}
</div>
<div class="content">
{content_html}
</div>
<footer>
  <a href="{original_url}" target="_blank" rel="noopener">原文を読む → {source}</a>
  {token_info_html}
</footer>
</body>
</html>"""


def generate_article_pages(articles_flat: list[dict]) -> dict[str, str]:
    """記事HTMLを生成・保存し {original_url: pages_url} を返す"""
    today = datetime.now(JST).strftime("%Y-%m-%d")
    output_dir = DOCS_DIR / today
    output_dir.mkdir(parents=True, exist_ok=True)

    url_map: dict[str, str] = {}

    for article in articles_flat:
        log.info(f"Translating: {article['title'][:50]}")
        raw_content = fetch_article_content(article["link"])
        if raw_content:
            content_ja, usage = translate_article_content(article["title"], raw_content)
        else:
            content_ja, usage = "", {}
        article["content_ja"] = content_ja
        article["translation_usage"] = usage

        slug = url_to_slug(article["link"])
        html = generate_article_html(article)
        (output_dir / f"{slug}.html").write_text(html, encoding="utf-8")

        pages_url = f"{GITHUB_PAGES_BASE}/articles/{today}/{slug}.html"
        url_map[article["link"]] = pages_url
        log.info(f"Saved: articles/{today}/{slug}.html")

    return url_map


def build_slack_blocks(articles_by_category: dict[str, list[dict]], url_map: dict[str, str]) -> list[dict]:
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
            pages_url = url_map.get(a["link"], a["link"])
            source = a["source"]
            published = a.get("published", "")
            summary = a.get("summary", [])
            importance = a.get("importance", "")

            meta = f"`{source}`"
            if published:
                meta += f"  _{published}_"

            lines = [f"*<{pages_url}|{title_ja}>*  {meta}"]
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
        log.info("Processing with AI (summaries)...")
        processed = process_with_ai(articles_by_category)

        articles_flat = [a for articles in processed.values() for a in articles]

        log.info("Translating full articles and generating pages...")
        url_map = generate_article_pages(articles_flat)

        log.info("Building Slack message...")
        blocks = build_slack_blocks(processed, url_map)

        log.info("Posting to Slack...")
        post_to_slack(blocks)
