#!/usr/bin/env python3
"""Add a KlipperFleet entry to Mainsail's navi.json sidebar navigation.

Usage: python3 setup_mainsail_navi.py <navi.json path>

The href points to /klipperfleet.html, a redirect shim that preserves whatever
hostname or IP the user used to reach Mainsail.

Idempotent: removes any existing KlipperFleet entry before adding the current one.
"""
import json
import os
import sys


def main():
    if len(sys.argv) < 2:
        print("Usage: setup_mainsail_navi.py <navi_json_path>", file=sys.stderr)
        sys.exit(1)

    path = sys.argv[1]

    entry = {
        "title": "KlipperFleet",
        "href": "/klipperfleet.html",
        "target": "_self",
        "icon": "M20,21V19L17,16H13V13H16V11H13V8H16V6H13V3H11V6H8V8H11V11H8V13H11V16H7L4,19V21H20Z",
        "position": 86,
    }

    data = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, list):
                    data = loaded
        except Exception:
            data = []

    # Remove stale KlipperFleet entries (by title or href), then append current one
    data = [item for item in data if not (isinstance(item, dict) and (
        item.get("title") == "KlipperFleet" or item.get("href") in ("/klipperfleet.html", entry["href"])
    ))]
    data.append(entry)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    print("KlipperFleet: Mainsail navigation configured.")


if __name__ == "__main__":
    main()
