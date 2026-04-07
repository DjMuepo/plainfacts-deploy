import os
import json
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlite_utils import Database

PLAINFACTS_API_BASE = os.getenv("PLAINFACTS_API_BASE", "http://api:8000")
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
DB_PATH = os.getenv("PUSH_DB_PATH", "push_relay.sqlite")
DEFAULT_POLL_SECONDS = int(os.getenv("PUSH_POLL_SECONDS", "900"))
HTTP_TIMEOUT = 20

TOPIC_COOLDOWN_SECONDS = int(os.getenv("TOPIC_COOLDOWN_SECONDS", str(6 * 3600)))
DAILY_DEVICE_CAP = int(os.getenv("DAILY_DEVICE_CAP", "20"))

class TopicPref(BaseModel):
    topic: str
    min_confidence: float = 0.65
    evidence_only: bool = False
    open_exact_on_tap: bool = True

class RegisterRequest(BaseModel):
    token: str
    topics: List[TopicPref] = Field(default_factory=list)

class UnregisterRequest(BaseModel):
    token: str

class TestPushRequest(BaseModel):
    token: str
    title: str = "PlainFacts"
    body: str = "Test notification"

class RunCheckRequest(BaseModel):
    max_topics_per_device: int = 8
    max_briefs_per_topic: int = 8


db = Database(DB_PATH)

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def day_utc_iso():
    return datetime.now(timezone.utc).date().isoformat()

def parse_iso(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None

def init_db():
    db["devices"].create({"token": str, "created_at": str, "updated_at": str}, pk="token", if_not_exists=True)
    db["topics"].create(
        {
            "id": int,
            "token": str,
            "topic": str,
            "min_confidence": float,
            "evidence_only": int,
            "open_exact_on_tap": int,
        },
        pk="id",
        if_not_exists=True,
    )
    db["topics"].create_index(["token", "topic"], unique=True, if_not_exists=True)

    db["seen"].create(
        {"id": int, "token": str, "topic": str, "cluster_id": str, "seen_at": str},
        pk="id",
        if_not_exists=True,
    )
    db["seen"].create_index(["token", "topic", "cluster_id"], unique=True, if_not_exists=True)

    db["push_log"].create(
        {"id": int, "token": str, "topic": str, "pushed_at": str, "day": str},
        pk="id",
        if_not_exists=True,
    )
    db["push_log"].create_index(["token", "topic", "pushed_at"], if_not_exists=True)
    db["push_log"].create_index(["token", "day"], if_not_exists=True)

init_db()


def already_seen(token: str, topic: str, cluster_id: str) -> bool:
    return db["seen"].exists(token=token, topic=topic, cluster_id=cluster_id)

def mark_seen(token: str, topic: str, cluster_id: str):
    db["seen"].insert({"token": token, "topic": topic, "cluster_id": cluster_id, "seen_at": now_iso()}, ignore=True)

def device_over_daily_cap(token: str) -> bool:
    day = day_utc_iso()
    row = db["push_log"].execute("SELECT COUNT(1) FROM push_log WHERE token = ? AND day = ?", [token, day]).fetchone()
    count = int(row[0]) if row else 0
    return count >= DAILY_DEVICE_CAP

def topic_in_cooldown(token: str, topic: str) -> bool:
    row = db["push_log"].execute(
        "SELECT pushed_at FROM push_log WHERE token = ? AND topic = ? ORDER BY pushed_at DESC LIMIT 1",
        [token, topic],
    ).fetchone()
    if not row:
        return False
    last = parse_iso(row[0])
    if not last:
        return False
    return (datetime.now(timezone.utc) - last).total_seconds() < TOPIC_COOLDOWN_SECONDS

def log_push(token: str, topic: str):
    db["push_log"].insert({"token": token, "topic": topic, "pushed_at": now_iso(), "day": day_utc_iso()})


async def fetch_briefs(topic: str, max_clusters: int) -> List[Dict[str, Any]]:
    url = f"{PLAINFACTS_API_BASE}/briefs"
    params = {"q": topic, "max_clusters": str(max_clusters)}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


def passes_filters(brief: Dict[str, Any], min_conf: float, evidence_only: bool) -> bool:
    conf = float(brief.get("confidence") or 0.0)
    if conf < min_conf:
        return False
    if evidence_only:
        ev = brief.get("evidence_links") or []
        if not isinstance(ev, list) or len(ev) == 0:
            return False
    return True


async def expo_push(token: str, title: str, body: str, data: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    payload = {
        "to": token,
        "title": title,
        "body": body,
        "sound": "default",
        "data": data or {},
        "channelId": "plainfacts",
    }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.post(EXPO_PUSH_URL, json=payload)
        try:
            j = r.json()
        except Exception:
            return False, f"Non-JSON response: {r.text[:200]}"
    try:
        status = j["data"][0]["status"]
        if status == "ok":
            return True, "ok"
        return False, json.dumps(j)[:300]
    except Exception:
        return False, json.dumps(j)[:300]


async def run_check_once(max_topics_per_device: int, max_briefs_per_topic: int) -> Dict[str, Any]:
    devices = list(db["devices"].rows)
    pushed = 0
    checked = 0
    errors = 0

    for d in devices:
        token = d["token"]
        topic_rows = list(db["topics"].rows_where("token = ?", [token], order_by="id desc", limit=max_topics_per_device))

        for tr in topic_rows:
            topic = tr["topic"]
            min_conf = float(tr["min_confidence"])
            evidence_only = bool(tr["evidence_only"])
            open_exact = bool(tr.get("open_exact_on_tap") or 0)
            checked += 1

            try:
                briefs = await fetch_briefs(topic, max_briefs_per_topic)
                eligible = [b for b in briefs if passes_filters(b, min_conf, evidence_only)]
                new_items = [b for b in eligible if b.get("cluster_id") and not already_seen(token, topic, b["cluster_id"])]

                if not new_items:
                    continue

                # Rate limiting
                if device_over_daily_cap(token) or topic_in_cooldown(token, topic):
                    for b in new_items:
                        mark_seen(token, topic, b["cluster_id"])
                    continue

                for b in new_items:
                    mark_seen(token, topic, b["cluster_id"])

                count = len(new_items)
                title = "PlainFacts update"
                body = f"{count} new brief(s) for: {topic}"
                if evidence_only:
                    body = f"{count} evidence-backed update(s) for: {topic}"

                ok, msg = await expo_push(
                    token,
                    title=title,
                    body=body,
                    data={
                        "topic": topic,
                        "count": count,
                        "top_cluster_id": new_items[0].get("cluster_id"),
                        "open_exact_on_tap": open_exact,
                    },
                )
                if ok:
                    pushed += 1
                    log_push(token, topic)
                else:
                    errors += 1

            except Exception:
                errors += 1

    return {"devices": len(devices), "topics_checked": checked, "pushes_sent": pushed, "errors": errors, "time_utc": now_iso()}


app = FastAPI(title="PlainFacts Push Relay", version="0.3.0")


@app.get("/health")
def health():
    return {"ok": True, "time_utc": now_iso(), "plainfacts_api_base": PLAINFACTS_API_BASE}


@app.post("/push/register")
def register(req: RegisterRequest):
    db["devices"].upsert({"token": req.token, "created_at": now_iso(), "updated_at": now_iso()}, pk="token")

    for t in req.topics:
        topic = t.topic.strip()
        if len(topic) < 2:
            continue
        db["topics"].upsert(
            {
                "token": req.token,
                "topic": topic,
                "min_confidence": float(t.min_confidence),
                "evidence_only": 1 if t.evidence_only else 0,
                "open_exact_on_tap": 1 if t.open_exact_on_tap else 0,
            },
            pk=("token", "topic"),
        )

    return {"ok": True, "token": req.token, "topics": len(req.topics)}


@app.post("/push/unregister")
def unregister(req: UnregisterRequest):
    token = req.token
    if not db["devices"].exists(token=token):
        return {"ok": True, "removed": False}

    db["devices"].delete(token)
    db["topics"].delete_where("token = ?", [token])
    db["seen"].delete_where("token = ?", [token])
    db["push_log"].delete_where("token = ?", [token])
    return {"ok": True, "removed": True}


@app.post("/push/test")
async def test_push(req: TestPushRequest):
    ok, msg = await expo_push(req.token, req.title, req.body, data={"test": True})
    if not ok:
        raise HTTPException(status_code=500, detail=f"Expo push failed: {msg}")
    return {"ok": True}


@app.post("/push/run_check")
async def run_check(req: RunCheckRequest):
    return await run_check_once(req.max_topics_per_device, req.max_briefs_per_topic)


@app.post("/push/daemon")
async def daemon():
    poll = DEFAULT_POLL_SECONDS
    while True:
        await run_check_once(max_topics_per_device=8, max_briefs_per_topic=8)
        await asyncio.sleep(poll)
