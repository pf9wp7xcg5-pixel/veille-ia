import feedparser
import httpx
import asyncio
import time
import re
import os
import json
from pathlib import Path
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

SOURCES_FILE = Path(__file__).parent / "sources.json"

def _load_sources() -> list[dict]:
    try:
        return json.loads(SOURCES_FILE.read_text())
    except Exception as e:
        print(f"[sources] erreur lecture sources.json: {e}")

SOURCES: list[dict] = _load_sources()

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

import re as _re  # déjà importé plus haut, ok si présent une seule fois

async def summarize_groq(text: str, title: str, groq_key: str, max_retries: int = 3) -> dict:
    if not groq_key or not text.strip():
        return {"summary": "", "title_fr": ""}
    title_only = text.strip() == title.strip()
    if title_only:
        prompt = (
            f"Traduis ce titre en français et propose une description probable en 2 phrases.\n"
            f"Titre : {title}\n\n"
            "Réponds UNIQUEMENT avec ces deux lignes (rien d'autre) :\n"
            "TITRE: <traduction française du titre>\n"
            "RESUME: <description probable en 2 phrases courtes en français>"
        )
    else:
        prompt = (
            f"Article : {title}\n\n{text}\n\n"
            "Réponds UNIQUEMENT avec ces deux lignes (rien d'autre) :\n"
            "TITRE: <traduction française du titre>\n"
            "RESUME: <résumé en 2 phrases courtes en français, factuel et concis>"
        )

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {groq_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "llama-3.1-8b-instant",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 150,
                        "temperature": 0.3,
                    },
                )
                data = resp.json()

                if resp.status_code == 429:
                    # Groq renvoie le délai d'attente dans le message d'erreur
                    msg = data.get("error", {}).get("message", "")
                    m = _re.search(r"try again in ([\d.]+)s", msg)
                    wait_s = float(m.group(1)) + 0.3 if m else 2.0
                    print(f"[groq] 429, retry dans {wait_s:.1f}s (tentative {attempt+1}/{max_retries})")
                    await asyncio.sleep(wait_s)
                    continue

                if "choices" not in data:
                    print(f"[groq error] réponse inattendue (status {resp.status_code}): {data}")
                    return {"summary": "", "title_fr": ""}

                content = data["choices"][0]["message"]["content"].strip()
                titre_m = _re.search(r'^TITRE\s*:\s*(.+)$', content, _re.IGNORECASE | _re.MULTILINE)
                resume_m = _re.search(r'^RESUME\s*:\s*(.+)$', content, _re.IGNORECASE | _re.MULTILINE)
                return {
                    "summary": resume_m.group(1).strip() if resume_m else "",
                    "title_fr": titre_m.group(1).strip() if titre_m else "",
                }
        except Exception as e:
            print(f"[groq error] {e}")
            return {"summary": "", "title_fr": ""}

    return {"summary": "", "title_fr": ""}


async def refresh_cache(groq_key: str = ""):
    async with httpx.AsyncClient(follow_redirects=True) as client:
        tasks = [fetch_feed(s, client) for s in SOURCES]
        results = await asyncio.gather(*tasks)

    articles: list[dict] = []
    for batch in results:
        articles.extend(batch)

    articles.sort(key=lambda a: a["date"], reverse=True)

    # Résumés Groq (seulement les 30 premiers pour limiter les appels)
    if groq_key:
        sem = asyncio.Semaphore(1)   # au lieu de 3

        async def safe_summarize(a):
            async with sem:
                excerpt = a["excerpt"]
                if excerpt and (re.match(r'^\s*(PLUS|ALSO|PS|P\.S\.)\s*:', excerpt, re.I) or len(excerpt.strip()) < 40):
                    excerpt = ""
                text = excerpt or a["title"]
                if text:
                    result = await summarize_groq(text, a["title"], groq_key)
                    a["summary"] = result["summary"]
                    a["title_fr"] = result["title_fr"]
                await asyncio.sleep(0.4)   # espace les appels pour rester sous le TPM
                return a

        first_batch = articles[:30]
        rest = articles[30:]  # préserver AVANT le gather (articles sera réassigné)
        summarized = await asyncio.gather(*[safe_summarize(a) for a in first_batch])
        articles = list(summarized) + rest

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
    min_per_source: int = Query(2, le=10),
):
    articles = _cache["articles"]

    if tag and tag != "all":
        articles = [a for a in articles if a["tag"] == tag]

    if q:
        q_lower = q.lower()
        articles = [
            a for a in articles
            if q_lower in a["title"].lower() or q_lower in (a["excerpt"] or "").lower()
        ]

    # Garantir une place minimale à chaque source avant de tronquer par date globale
    selected = []
    selected_ids = set()

    by_source: dict[str, list[dict]] = {}
    for a in articles:  # déjà trié par date décroissante
        by_source.setdefault(a["source"], []).append(a)

    for source_articles in by_source.values():
        for a in source_articles[:min_per_source]:
            if a["id"] not in selected_ids:
                selected.append(a)
                selected_ids.add(a["id"])

    # Compléter avec le reste par ordre chronologique global jusqu'à la limite
    for a in articles:
        if len(selected) >= limit:
            break
        if a["id"] not in selected_ids:
            selected.append(a)
            selected_ids.add(a["id"])

    # Re-trier le résultat final pour un affichage cohérent
    selected.sort(key=lambda a: a["date"], reverse=True)
    selected = selected[:limit]

    return {
        "articles": selected,
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


@app.get("/api/translate")
async def translate_article(
    url: str = Query(...),
    title: str = Query(""),
    excerpt: str = Query(""),
):
    if not GROQ_API_KEY:
        return {"error": "GROQ_API_KEY non configurée"}

    # Tente de récupérer le contenu complet de l'article
    content = ""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            raw_html = resp.text
            content = re.sub(r"<script[^>]*>.*?</script>", " ", raw_html, flags=re.DOTALL)
            content = re.sub(r"<style[^>]*>.*?</style>", " ", content, flags=re.DOTALL)
            content = re.sub(r"<[^>]+>", " ", content)
            content = re.sub(r"\s+", " ", content).strip()
            content = content[:4000]
    except Exception as e:
        print(f"[translate] fetch error: {e}")

    # Fallback sur l'extrait si le fetch a échoué ou retourné peu de contenu
    text = content if len(content) > len(excerpt) else excerpt

    if not text.strip():
        return {"error": "Impossible de récupérer le contenu de l'article"}

    prompt = (
        f"Titre : {title}\n\n{text}\n\n"
        "Traduis et résume cet article en français de manière complète et détaillée. "
        "Structure ta réponse avec des paragraphes clairs. "
        "Commence directement par le contenu, sans introduction ni mention de traduction."
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1024,
                    "temperature": 0.3,
                },
            )
            data = resp.json()
            if "choices" not in data:
                return {"error": "Erreur Groq", "detail": str(data)}
            return {"translation": data["choices"][0]["message"]["content"].strip()}
    except Exception as e:
        return {"error": str(e)}


@app.get("/health")
async def health():
    return {"status": "ok", "articles": len(_cache["articles"]), "cached_at": _cache["ts"]}