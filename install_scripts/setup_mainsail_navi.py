#!/usr/bin/env python3
"""Add a KlipperFleet entry to Mainsail's navi.json sidebar navigation.

Usage: python3 setup_mainsail_navi.py <navi.json path> <hostname>

Idempotent: removes any existing KlipperFleet entry before adding the current one.
"""
import json
import os
import sys


def main():
    if len(sys.argv) < 3:
        print("Usage: setup_mainsail_navi.py <navi_json_path> <hostname>", file=sys.stderr)
        sys.exit(1)

    path = sys.argv[1]
    hostname = sys.argv[2]

    entry = {
        "title": "KlipperFleet",
        "href": f"http://{hostname}:8321",
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

    # Remove stale KlipperFleet entries, then append current one
    data = [item for item in data if not (isinstance(item, dict) and item.get("title") == "KlipperFleet")]
    data.append(entry)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    print("KlipperFleet: Mainsail navigation configured.")


if __name__ == "__main__":
    main()
