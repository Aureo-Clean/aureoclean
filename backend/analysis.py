"""Report data prepared for analysis by an LLM (and for charts in the Reports tab later).

Three steps, each usable on its own:

1. ``dataset(service, days)``  - every report as a flat, consistent record. Personal data
   (reporter name/number, device hash) is left out on purpose.
2. ``summarize(records)``      - counts and patterns computed in plain Python, so the LLM
   gets facts to reason about instead of having to do arithmetic.
3. ``build_prompt(...)``       - a ready-to-send system + user prompt containing the summary
   and the records as JSON. ``analyze(...)`` sends it with any client you plug in.

Safety rule (from the project plan): emergencies are handled by fixed rules when the report
arrives (see AureoService.import_public_reports). The LLM only looks for patterns and gives
maintenance recommendations afterwards; it never decides whether to raise an alarm.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .report_schema import issue_map, schema

RECORD_FIELDS = (
    "id", "createdAt", "weekday", "hour", "source", "channel", "category", "priority", "status",
    "location", "locationCode", "issues", "urgency", "rating", "details", "resolvedAt",
    "minutesToResolve", "notes",
)
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
LOCAL_TZ = timezone(timedelta(hours=8))  # Philippines (UTC+8): hours of day as staff see them


def _parse(ts: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def to_record(report_id: str, r: dict) -> dict:
    created = _parse(r.get("reportedAt") or r.get("createdAt"))
    resolved = _parse(r.get("resolvedAt"))
    local = created.astimezone(LOCAL_TZ) if created else None
    return {
        "id": report_id,
        "createdAt": created.isoformat() if created else None,
        "weekday": WEEKDAYS[local.weekday()] if local else None,
        "hour": local.hour if local else None,
        "source": r.get("source", "admin"),
        "channel": r.get("channel"),
        "category": r.get("category"),
        "priority": r.get("priority"),
        "status": r.get("status"),
        "location": r.get("location") or None,
        "locationCode": r.get("locationCode"),
        "issues": list(r.get("issues") or []),
        "urgency": r.get("urgency"),
        "rating": r.get("rating"),
        "details": (r.get("details") or "")[:300] or None,
        "resolvedAt": resolved.isoformat() if resolved else None,
        "minutesToResolve": round((resolved - created).total_seconds() / 60) if created and resolved else None,
        "notes": [n.get("text", "")[:120] for n in (r.get("notes") or []) if n.get("by") != "sms"][-5:],
    }


def dataset(service, days: int = 30) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    records = [to_record(doc_id, r) for doc_id, r in service.store.query(service.C_REPORTS)]
    records = [x for x in records if (x["createdAt"] or "") >= since]
    records.sort(key=lambda x: x["createdAt"] or "")
    return records


def summarize(records: list[dict]) -> dict:
    labels = {code: i["label"] for code, i in issue_map().items()}
    by_location = defaultdict(list)
    for rec in records:
        by_location[rec["location"] or rec["locationCode"] or "unknown"].append(rec)

    issue_counts = Counter(code for rec in records for code in rec["issues"])
    pairs = Counter((rec["location"] or "unknown", code) for rec in records for code in rec["issues"])
    resolve = defaultdict(list)
    for rec in records:
        if rec["minutesToResolve"] is not None:
            resolve[rec["category"]].append(rec["minutesToResolve"])

    return {
        "period": {"from": records[0]["createdAt"] if records else None,
                   "to": records[-1]["createdAt"] if records else None},
        "totalReports": len(records),
        "openReports": sum(rec["status"] != "resolved" for rec in records),
        "byCategory": dict(Counter(rec["category"] for rec in records).most_common()),
        "byIssue": {labels.get(c, c): n for c, n in issue_counts.most_common()},
        "byLocation": {loc: len(rs) for loc, rs in sorted(by_location.items(), key=lambda kv: -len(kv[1]))},
        "byHour": dict(sorted(Counter(rec["hour"] for rec in records if rec["hour"] is not None).items())),
        "byWeekday": {d: n for d in WEEKDAYS
                      if (n := sum(rec["weekday"] == d for rec in records))},
        "averageCleanlinessByLocation": {
            loc: round(statistics.mean(r["rating"] for r in rs if r["rating"]), 2)
            for loc, rs in by_location.items() if any(r["rating"] for r in rs)},
        "medianMinutesToResolveByCategory": {cat: round(statistics.median(v)) for cat, v in resolve.items()},
        "recurringProblems": [
            {"location": loc, "issue": labels.get(code, code), "count": n}
            for (loc, code), n in pairs.most_common(10) if n >= 2],
        "emergencies": sum(rec["category"] == "emergency" for rec in records),
    }


SYSTEM_PROMPT = """You are the maintenance analyst for AureoClean, an automated restroom hygiene \
monitoring system at STI College Caloocan (Philippines). You receive restroom problem reports \
(from QR codes, the report page and administrators) and a pre-computed summary.

Rules:
- Base every statement on the data given. Quote counts from the summary; do not invent numbers.
- Emergencies were already handled by fixed alert rules when they arrived. Do not decide alerts; \
you may point out patterns in them.
- Be concrete and practical for a small janitorial team. Plain English, short sentences.
- Return JSON only, matching the requested format."""

OUTPUT_FORMAT = {
    "headline": "one sentence on the overall state of the restrooms",
    "patterns": [{"finding": "...", "evidence": "counts from the summary", "locations": ["..."]}],
    "recommendations": [{"action": "...", "where": "...", "when": "e.g. before 10:00 on weekdays",
                         "priority": "high|medium|low", "reason": "..."}],
    "supplyPlan": [{"item": "soap|toilet paper|paper towels|...", "where": "...", "suggestion": "..."}],
    "facilityRepairs": [{"problem": "...", "where": "...", "suggestion": "..."}],
    "dataQuality": ["anything missing or odd in the data that limits the analysis"],
}


def build_prompt(records: list[dict], summary: Optional[dict] = None, question: str = "",
                 max_records: int = 400) -> dict:
    """Return {"system": ..., "user": ...} ready for any chat-style LLM API."""
    summary = summary or summarize(records)
    shown = records[-max_records:]
    user = {
        "task": question or "Analyse these restroom reports and recommend what the maintenance team should do.",
        "issueCodes": {i["code"]: i["label"] for i in schema()["issues"]},
        "summary": summary,
        "records": shown,
        "recordsOmitted": max(0, len(records) - len(shown)),
        "answerFormat": OUTPUT_FORMAT,
    }
    return {"system": SYSTEM_PROMPT, "user": json.dumps(user, ensure_ascii=False, separators=(",", ":"))}


def analyze(service, ask: Optional[Callable[[str, str], str]] = None, days: int = 30, question: str = "") -> dict:
    """Prepare the data and, if ``ask(system, user) -> text`` is given, send it to an LLM.

    ``ask`` is where a provider (e.g. the Claude API) plugs in later. Without it, the
    prompt is returned so it can be checked or sent by hand."""
    records = dataset(service, days)
    summary = summarize(records)
    prompt = build_prompt(records, summary, question)
    result = {"summary": summary, "prompt": prompt, "answer": None}
    if ask:
        text = ask(prompt["system"], prompt["user"])
        try:
            result["answer"] = json.loads(text[text.find("{"): text.rfind("}") + 1])
        except ValueError:
            result["answer"] = {"raw": text}
    return result
