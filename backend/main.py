import feedparser
import httpx
import asyncio
import time
import re
import os
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

app = FastAPI(title="Veille IA API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── Sources RSS ────────────────────────────────────────────────────────────────
SOURCES = [
    {"id": "tldr",       "name": "TLDR AI",              "tag": "news",  "url": "https://tldr.tech/ai/rss"},
    {"id": "rundown",    "name": "The Rundown AI",        "tag": "news",  "url": "https://www.therundown.ai/rss"},
    {"id": "bensbites",  "name": "Ben's Bites",           "tag": "news",  "url": "https://www.bensbites.com/feed"},
    {"id": "superhuman", "name": "Superhuman AI",         "tag": "news",  "url": "https://www.superhuman.ai/rss"},
    {"id": "aithere",    "name": "There's An AI For That","tag": "tools", "url": "https://theresanaiforthat.com/rss"},
    {"id": "hwpapers",   "name": "HuggingFace Papers",    "tag": "deep",  "url": "https://huggingface.co/papers/rss"},
    {"id": "importai",   "name": "Import AI",             "tag": "deep",  "url": "https://importai.substack.com/feed"},
    {"id": "fireship",   "name": "Fireship",              "tag": "video", "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCsBjURrPoezykLs9EqgamOA"},
    {"id": "mattwolfe",  "name": "Matt Wolfe",            "tag": "video", "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCfHnS-v7pFGLnxrKbyyMzYg"},
    {"id": "aibriefing", "name": "AI Daily Brief",        "tag": "video", "url": "https://feeds.buzzsprout.com/2126097.rss"},
]

# ── Cache ──────────────────────────────────────────────────────────────────────
_cache: dict = {"articles": [], "ts": 0}


# ── Helpers ────────────────────────────────────────────────────────────────────
def strip_html(text: str) -> str:
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:800]


def parse_date(entry) -> str:
    for field in ("published_parsed", "updated_parsed"):
        t = getattr(entry, field, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
    return datetime.now(timezone.utc).isoformat()


async def fetch_feed(source: dict, client: httpx.AsyncClient) -> list[dict]:
    try:
        resp = await client.get(source["url"], timeout=10)
        feed = feedparser.parse(resp.text)
        articles = []
        for entry in feed.entries[:8]:
            raw = strip_html(
                getattr(entry, "summary", None)
                or getattr(entry, "description", None)
                or getattr(entry, "content", [{}])[0].get("value", "")
            )
            articles.append({
                "id":      entry.get("id", entry.get("link", "")),
                "source":  source["name"],
                "tag":     source["tag"],
                "title":   entry.get("title", ""),
                "url":     entry.get("link", ""),
                "excerpt": raw[:400],
                "date":    parse_date(entry),
                "summary": None,
            })
        return articles
    except Exception as e:
        print(f"[feed error] {source['name']}: {e}")
        return []


async def summarize_groq(text: str, title: str, groq_key: str) -> str:
    if not groq_key or not text.strip():
        return ""
    prompt = (
        f"Article : {title}\n\n{text}\n\n"
        "Résume cet article en 2 phrases courtes en français. "
        "Sois factuel et concis, sans introduction."
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {groq_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "llama3-8b-8192",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 120,
                    "temperature": 0.3,
                },
            )
            data = resp.json()
            if "choices" not in data:
                print(f"[groq error] réponse inattendue (status {resp.status_code}): {data}")
                return ""
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[groq error] {e}")
        return ""


async def refresh_cache(groq_key: str = ""):
    async with httpx.AsyncClient(follow_redirects=True) as client:
        tasks = [fetch_feed(s, client) for s in SOURCES]
        results = await asyncio.gather(*tasks)

    articles: list[dict] = []
    for batch in results:
        articles.extend(batch)

    articles.sort(key=lambda a: a["date"], reverse=True)

    # Résumés Groq (seulement les 20 premiers pour limiter les appels)
    if groq_key:
        sem = asyncio.Semaphore(3)

        async def safe_summarize(a):
            async with sem:
                if a["excerpt"]:
                    a["summary"] = await summarize_groq(a["excerpt"], a["title"], groq_key)
                return a

        articles = await asyncio.gather(*[safe_summarize(a) for a in articles[:20]])
        articles = list(articles) + [a for a in articles[20:]]

    _cache["articles"] = articles
    _cache["ts"] = time.time()
    print(f"[cache] {len(articles)} articles chargés")


# ── Routes ─────────────────────────────────────────────────────────────────────
import os

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")


scheduler = AsyncIOScheduler()

@app.on_event("startup")
async def startup():
    # Chargement initial au démarrage
    await refresh_cache(GROQ_API_KEY)
    # Scheduler : tous les jours à 20h UTC = 7h heure de Nouméa (GMT+11)
    scheduler.add_job(
        refresh_cache,
        CronTrigger(hour=20, minute=0, timezone="UTC"),
        args=[GROQ_API_KEY],
        id="daily_refresh",
        replace_existing=True,
    )
    scheduler.start()
    print("[scheduler] Refresh programmé chaque jour à 20h UTC (7h Nouméa)")

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()


@app.get("/api/feed")
async def get_feed(
    tag: Optional[str] = Query(None),
    q:   Optional[str] = Query(None),
    limit: int = Query(50, le=200),
):
    # Refresh si cache expiré

    articles = _cache["articles"]

    if tag and tag != "all":
        articles = [a for a in articles if a["tag"] == tag]

    if q:
        q_lower = q.lower()
        articles = [
            a for a in articles
            if q_lower in a["title"].lower() or q_lower in (a["excerpt"] or "").lower()
        ]

    return {
        "articles": articles[:limit],
        "total": len(articles),
        "cached_at": datetime.fromtimestamp(_cache["ts"], tz=timezone.utc).isoformat() if _cache["ts"] else None,
    }


@app.get("/api/sources")
async def get_sources():
    return {"sources": SOURCES}


@app.get("/api/refresh")
async def manual_refresh():
    await refresh_cache(GROQ_API_KEY)
    return {"ok": True, "articles": len(_cache["articles"])}


@app.get("/health")
async def health():
    return {"status": "ok", "articles": len(_cache["articles"]), "cached_at": _cache["ts"]}