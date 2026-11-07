"""Jev (TypeSafe AI) message sorter.

Job: read each client message and put it in one of 10 categories, so the app can spot
clients who want to leave, want to buy more, or send a referral, whatever words they use
(including Spanish), instead of relying on fixed keyword lists.

API: POST https://api.typesafe.ai/v1/systemone with a "state" (the message) and typed
questions. We ask one Choice question (category) and one Noul question (is this a sign
the client may leave). Docs: https://docs.typesafe.ai
"""
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from labels import CATEGORIES

URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"


def ask(text, key, timeout=30):
    body = {
        "model": MODEL,
        "state": f"A message a client sent to a video production studio:\n\n{text}",
        "questions": {
            "category": {
                "type": "choice",
                "instructions": "What is this message mainly about?",
                "criteria": CATEGORIES,
            },
            "may_leave": {
                "type": "noul",
                "instructions": "The client is unhappy enough that they may stop working with the studio.",
            },
        },
    }
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out = json.loads(resp.read())
            a = out.get("answers", out)
            cat = a.get("category", {})
            leave = a.get("may_leave", {})
            return {
                "jev_category": cat.get("choice"),
                "jev_confidence": cat.get("confidence"),
                "jev_may_leave": leave.get("noul"),
                "jev_model": out.get("model"),
            }
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            return {"jev_category": None, "jev_error": f"HTTP {e.code}: {e.read()[:200]!r}"}
        except Exception as e:  # network, timeout
            if attempt < 3:
                time.sleep(1 + attempt)
                continue
            return {"jev_category": None, "jev_error": str(e)[:200]}


def classify_many(texts, key, workers=6):
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(lambda t: ask(t, key), texts))
