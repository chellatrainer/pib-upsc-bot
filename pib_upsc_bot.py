"""
Daily PIB -> UPSC digest -> Telegram.

Env vars required:
  GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Optional:
  GEMINI_MODEL (optional; default tries several free models), MAX_ITEMS (default 15), LOOKBACK_HOURS (default 30)
"""
import os
import time
import calendar
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from bs4 import BeautifulSoup

# All-India English press releases. Verify/choose other feeds at https://www.pib.gov.in/RssMain.aspx
PIB_RSS = "https://www.pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=3"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PIB-UPSC-Digest/1.0)"}

# Tries models in order; moves to the next one if a model returns 404 (retired/renamed)
_env_model = os.getenv("GEMINI_MODEL")
MODELS = [_env_model] if _env_model else [
    "gemini-3.8-flash", "gemini-4-hydrogen", "gemini-3.5-flash-lite", "gemini-2.5-flash-lite"]
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "15"))
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "30"))

PROMPT = """You are a UPSC Civil Services mentor. Below are today's PIB (Press Information Bureau) releases.

Create a daily UPSC-oriented digest:

1. Pick only the releases that matter for UPSC (Prelims + Mains). Skip routine/ceremonial items.
2. Group them under: Polity & Governance | Economy | Environment & Ecology | Science & Tech |
   International Relations | Social Issues & Schemes | Defence & Security | Misc/Prelims Facts.
3. For each selected item give:
   - Headline (one line)
   - Key facts (2-3 short points: numbers, names, dates, scheme details)
   - UPSC angle: GS paper + syllabus topic (e.g. GS2 - Government policies)
   - One possible Prelims-style fact OR Mains-style question hook
4. End with "Quick Revision": 5 one-line facts for Prelims.

Rules: plain text only (no markdown symbols like ** or #), use emojis for section headers, be concise,
do not invent facts beyond the text given. Under 3000 words.

RELEASES:
{releases}
"""


def recent_entries():
    feed = feedparser.parse(PIB_RSS, request_headers=HEADERS)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    out = []
    for e in feed.entries:
        ts = e.get("published_parsed") or e.get("updated_parsed")
        if ts:
            dt = datetime.fromtimestamp(calendar.timegm(ts), tz=timezone.utc)
            if dt < cutoff:
                continue
        out.append(e)
    return out[:MAX_ITEMS]


def article_text(url, limit=1800):
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")
        paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = " ".join(p for p in paras if len(p) > 40)
        return text[:limit]
    except Exception as ex:
        return f"(could not fetch: {ex})"


def summarise(releases_text):
    # Free tier via Google AI Studio key (https://aistudio.google.com/apikey)
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(releases=releases_text)}]}],
        "generationConfig": {"maxOutputTokens": 16000, "temperature": 0.3},
    }
    headers = {"x-goog-api-key": os.environ["GEMINI_API_KEY"]}
    last_err = None
    for model in MODELS:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent")
        r = None
        for attempt in range(5):
            try:
                r = requests.post(url, json=body, timeout=300, headers=headers)
            except requests.RequestException as ex:
                last_err = str(ex)
                time.sleep(20 * (attempt + 1))
                continue
            # temporary problems: rate limit / overloaded -> wait and retry
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = f"{model}: {r.status_code} {r.text[:200]}"
                print(f"{model} busy ({r.status_code}), retry {attempt + 1}/5")
                time.sleep(20 * (attempt + 1))
                continue
            break
        if r is None or r.status_code in (404, 429, 500, 502, 503, 504):
            print(f"Skipping {model}, trying next model...")
            if r is not None:
                last_err = f"{model}: {r.status_code} {r.text[:200]}"
            continue
        if not r.ok:
            raise RuntimeError(f"Gemini error {r.status_code}: {r.text[:500]}")
        parts = r.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
        print(f"Used model: {model}")
        return text
    raise RuntimeError(f"No Gemini model worked. Last error: {last_err}")


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Telegram limit is 4096 chars; split on line breaks
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        r = requests.post(url, data={"chat_id": chat_id, "text": c,
                                     "disable_web_page_preview": True}, timeout=30)
        r.raise_for_status()
        time.sleep(1)


def main():
    entries = recent_entries()
    today = datetime.now().strftime("%d %b %Y")
    if not entries:
        send_telegram(f"PIB UPSC Digest - {today}\n\nNo new releases found in the feed.")
        return
    blocks = []
    for i, e in enumerate(entries, 1):
        blocks.append(f"[{i}] {e.title}\n{article_text(e.link)}\n")
        time.sleep(0.5)
    digest = summarise("\n".join(blocks))
    send_telegram(f"📰 PIB UPSC Digest - {today}\n\n{digest}")


if __name__ == "__main__":
    main()
