#!/usr/bin/env python3
"""Make a QR code + printable poster for a restroom's report link.

    .venv/bin/python tools/make_qr.py 4F-M "4th Floor · Male"
    .venv/bin/python tools/make_qr.py --general            # any-restroom code

Writes exports/qr/report-<CODE>.svg and exports/qr/poster-<CODE>.html (open it and print).
Also add the restroom to docs/assets/config.js so it appears in the report page's list.
"""
from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

BASE_URL = "https://aureo-clean.github.io/aureoclean/report/"
ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("code", nargs="?", help="short code, letters/numbers/dashes, e.g. 4F-M")
    parser.add_argument("name", nargs="?", help='name shown on the poster, e.g. "4th Floor · Male"')
    parser.add_argument("--general", action="store_true", help="one code for any restroom")
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()

    try:
        import segno
    except ImportError:
        print("Missing package: run  .venv/bin/pip install segno  (or Update AureoClean).")
        return 1

    if args.general:
        code, name, url = "ANY", "Any restroom", args.base_url
    else:
        if not args.code or not re.fullmatch(r"[A-Za-z0-9-]{1,30}", args.code):
            parser.error("give a code made of letters, numbers and dashes, e.g. 4F-M")
        code, name = args.code.upper(), (args.name or args.code)[:60]
        url = f"{args.base_url}?r={code}"

    out = ROOT / "exports" / "qr"
    out.mkdir(parents=True, exist_ok=True)
    svg_path = out / f"report-{code}.svg"
    segno.make(url, error="m").save(str(svg_path), scale=10, border=4, dark="#14336E")
    poster = (ROOT / "docs" / "qr" / "posters.css").read_text()
    logo = (ROOT / "docs" / "assets" / "logo.svg").read_text()
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Poster {html.escape(code)}</title>
<style>{poster}</style></head><body><section class="poster">
<header><div class="logo">{logo}</div><div><p class="name"><span>Aureo</span>Clean</p>
<p class="tag">AUTOMATED HYGIENE MONITORING &amp; MAINTENANCE SYSTEM</p></div></header>
<h1>Something wrong in this restroom?</h1>
<p class="sub">Scan to tell the maintenance team. It takes 30 seconds, and you don't need an app.</p>
<img class="qr" src="{svg_path.name}" alt="QR code">
<p class="place">{html.escape(name)}</p>
<ul><li>No soap or tissue</li><li>Dirty or smelly</li><li>Clogged, leaking or broken</li><li>Emergency</li></ul>
<p class="url">{html.escape(url.replace("https://", ""))}</p></section></body></html>"""
    poster_path = out / f"poster-{code}.html"
    poster_path.write_text(page)
    print(f"QR code: {svg_path}\nPoster:  {poster_path}  (open in the browser and print)\nLink:    {url}")
    if not args.general:
        print(f'\nAdd to docs/assets/config.js → locations:  {{ code: "{code}", name: "{name}" }},')
    return 0


if __name__ == "__main__":
    sys.exit(main())
