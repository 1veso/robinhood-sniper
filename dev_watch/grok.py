# grok.py
# the only file that reads the xAI key, and the only place the desk talks to Grok.
import csv, datetime, json, os, time
import requests
from config import MODEL, COSTS, env

URL = "https://api.x.ai/v1/responses"


def _text(resp: dict) -> str:
    if isinstance(resp.get("output_text"), str):
        return resp["output_text"]
    return "\n".join(c.get("text") or "" for item in resp.get("output") or []
                     if item.get("type") == "message"
                     for c in item.get("content") or [] if c.get("type") in ("output_text", "text"))


def _bill(job: str, usage: dict):
    """every call writes what it cost. report.py adds it up, so the bill is never a guess."""
    usd = (usage.get("cost_in_usd_ticks") or 0) / 1e10
    posts = (usage.get("server_side_tool_usage_details") or {}).get("x_posts_fetched", 0)
    new = not os.path.exists(COSTS)
    with open(COSTS, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "job", "usd", "x_posts"])
        w.writerow([time.strftime("%Y-%m-%d %H:%M"), job, round(usd, 5), posts])


def ask(job: str, system: str, payload: dict, days_back=2, effort="medium", turns=2) -> dict:
    """one call, JSON in, JSON out. anything that isn't JSON raises, and a raise is a SKIP upstream."""
    key = env("XAI_API_KEY")
    if not key:
        raise RuntimeError("no XAI_API_KEY in .env")
    since = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()
    body = {"model": MODEL, "reasoning": {"effort": effort}, "max_turns": turns,
            "tools": [{"type": "x_search", "from_date": since}],
            "input": [{"role": "system", "content": system},
                      {"role": "user", "content": json.dumps(payload)}]}
    r = requests.post(URL, json=body, timeout=180, headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    resp = r.json()
    _bill(job, resp.get("usage") or {})
    text = _text(resp)
    a, b = text.find("{"), text.rfind("}")
    if a == -1:
        raise ValueError(f"no json in answer: {text[:160]}")
    return json.loads(text[a:b + 1])
