#!/usr/bin/env python3
"""Add or remove the KlipperFleet entry in Mainsail's navi.json sidebar navigation.

Usage: python3 setup_mainsail_navi.py <navi.json path> [--remove]

The href points to /printer-klipperfleet.html, a redirect shim that preserves whatever
hostname or IP the user used to reach Mainsail. The name must start with
"printer" (no slash after it) so Mainsail's PWA service worker lets the
navigation reach nginx over HTTPS instead of serving its own SPA (issue #39).

Idempotent: removes any existing KlipperFleet entry before adding the current one.
--remove does only the removal half, for uninstall.sh. Both directions share
_without_klipperfleet() so the uninstaller cannot fall out of step with the
installer about which entries are ours.
"""
import json
import os
import sys

ENTRY = {
    "title": "KlipperFleet",
    "href": "/printer-klipperfleet.html",
    "target": "_self",
    "icon": "M20,21V19L17,16H13V13H16V11H13V8H16V6H13V3H11V6H8V8H11V11H8V13H11V16H7L4,19V21H20Z",
    "position": 86,
}

# Every shim path we have ever shipped: /klipperfleet.html predates issue #39.
OUR_HREFS = ("/klipperfleet.html", "/printer-klipperfleet.html")


def _without_klipperfleet(data):
    """Drop our entries, matching on title or on any shim href we have used.

    Also drops entries with neither a title nor a recognised href: the old
    sed-based uninstall deleted just the `"title": "KlipperFleet"` line and
    left the rest of the object behind, so upgrades have to clean up after it.
    """
    kept = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("title") == "KlipperFleet" or item.get("href") in OUR_HREFS:
            continue
        if not item.get("title"):
            continue  # decapitated leftover from the old uninstaller
        kept.append(item)
    return kept


def _load(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            return loaded if isinstance(loaded, list) else []
    except Exception:
        return []


def _save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: setup_mainsail_navi.py <navi_json_path> [--remove]",
            file=sys.stderr,
        )
        sys.exit(1)

    path = sys.argv[1]
    remove = "--remove" in sys.argv[2:]

    data = _without_klipperfleet(_load(path))
    if not remove:
        data.append(ENTRY)

    _save(path, data)

    if remove:
        print("KlipperFleet: Mainsail navigation entry removed.")
    else:
        print("KlipperFleet: Mainsail navigation configured.")


if __name__ == "__main__":
    main()
