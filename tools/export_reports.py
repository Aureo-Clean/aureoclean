#!/usr/bin/env python3
"""Export reports for analysis (spreadsheets, charts, or an LLM).

    .venv/bin/python tools/export_reports.py            # last 30 days
    .venv/bin/python tools/export_reports.py --days 90

Writes into exports/ (not uploaded to GitHub):
    reports-YYYYMMDD.jsonl   one report per line (no personal data)
    reports-YYYYMMDD.csv     same, opens in Excel / Google Sheets
    summary-YYYYMMDD.json    counts and patterns
    prompt-YYYYMMDD.txt      ready-to-send prompt for an LLM
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.analysis import RECORD_FIELDS, build_prompt, dataset, summarize  # noqa: E402
from backend.factory import build_service  # noqa: E402


def export(service, out_dir: Path, days: int) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    records = dataset(service, days)
    summary = summarize(records)
    prompt = build_prompt(records, summary)

    paths = {k: out_dir / f"{k}-{stamp}.{ext}" for k, ext in
             (("reports", "jsonl"), ("reports_csv", "csv"), ("summary", "json"), ("prompt", "txt"))}
    paths["reports_csv"] = out_dir / f"reports-{stamp}.csv"
    with paths["reports"].open("w") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with paths["reports_csv"].open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=RECORD_FIELDS)
        writer.writeheader()
        for rec in records:
            writer.writerow({**rec, "issues": " ".join(rec["issues"]), "notes": " | ".join(rec["notes"])})
    paths["summary"].write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    paths["prompt"].write_text(f"=== SYSTEM ===\n{prompt['system']}\n\n=== USER ===\n{prompt['user']}\n")
    return {"count": len(records), "paths": paths}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "exports"))
    args = parser.parse_args()
    result = export(build_service(), Path(args.out), args.days)
    print(f"Exported {result['count']} report(s):")
    for p in result["paths"].values():
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
