"""The report questions, shared with the website (docs/assets/issues.json).

Turning a public report into an admin report is deterministic (no AI):
the most serious issue decides the category, and emergencies are always critical.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "docs" / "assets" / "issues.json"


@lru_cache(maxsize=1)
def schema() -> dict:
    return json.loads(SCHEMA_FILE.read_text())


def issue_map() -> dict[str, dict]:
    return {i["code"]: i for i in schema()["issues"]}


def urgency_codes() -> list[str]:
    return [u["code"] for u in schema()["urgency"]]


def category_for(issues: list[str]) -> str:
    """The most important category among the issues ("emergency" beats "facility" ...)."""
    order = schema()["categories"]
    found = {issue_map()[c]["category"] for c in issues if c in issue_map()}
    return next((cat for cat in order if cat in found), "other")


def priority_for(issues: list[str], urgency: str) -> str:
    if "emergency" in issues:
        return "critical"
    return {"high": "high", "medium": "normal", "low": "low"}.get(urgency, "normal")


def title_for(issues: list[str]) -> str:
    labels = [issue_map()[c]["label"].split(" (")[0] for c in issues if c in issue_map()]
    title = ", ".join(labels) or "Report"
    return title if len(title) <= 80 else title[:77] + "…"
