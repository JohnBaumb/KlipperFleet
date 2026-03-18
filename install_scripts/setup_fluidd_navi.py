#!/usr/bin/env python3
"""Add a KlipperFleet entry to Fluidd's navigation via Moonraker's database API.

Usage: python3 setup_fluidd_navi.py [--remove] [--moonraker-url URL]

Requires fluidd-core/fluidd#1786 (custom navigation links).
Idempotent: removes any existing KlipperFleet entry before adding the current one.

Reference: https://github.com/fluidd-core/fluidd/pull/1802#issuecomment-4085253599
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid


MOONRAKER_DEFAULT_URL = "http://localhost:7125"
DB_NAMESPACE = "fluidd"
DB_KEY = "uiSettings.navigation.customLinks"

# Stable UUID v5 derived from the URL namespace and our identifier.
# This ensures the same ID across installs without hardcoding a random UUID.
KLIPPERFLEET_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "klipperfleet"))

KLIPPERFLEET_LINK = {
    "id": KLIPPERFLEET_ID,
    "title": "KlipperFleet",
    "url": "/klipperfleet.html",
    "icon": "mdi-ferry",
    "position": 86,
}


def moonraker_request(base_url, method, path, data=None):
    """Make a request to Moonraker's API."""
    url = f"{base_url}{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError:
        return None


def get_existing_links(base_url):
    """Fetch existing custom navigation links from Moonraker DB."""
    result = moonraker_request(
        base_url, "GET",
        f"/server/database/item?namespace={DB_NAMESPACE}&key={DB_KEY}"
    )
    if result and "result" in result:
        value = result["result"].get("value", [])
        if isinstance(value, list):
            return value
    return []


def save_links(base_url, links):
    """Save custom navigation links to Moonraker DB."""
    result = moonraker_request(
        base_url, "POST",
        "/server/database/item",
        {"namespace": DB_NAMESPACE, "key": DB_KEY, "value": links}
    )
    return result is not None


def is_klipperfleet(entry):
    """Match KlipperFleet entries by id or title."""
    return isinstance(entry, dict) and (
        entry.get("id") == KLIPPERFLEET_ID or entry.get("title") == "KlipperFleet"
    )


def install(base_url):
    """Add KlipperFleet link to Fluidd navigation."""
    links = get_existing_links(base_url)

    # Remove any existing KlipperFleet entry, then append
    links = [l for l in links if not is_klipperfleet(l)]
    links.append(KLIPPERFLEET_LINK)

    if save_links(base_url, links):
        print("KlipperFleet: Fluidd navigation configured.")
    else:
        print("KlipperFleet: WARNING: Could not configure Fluidd navigation "
              "(Moonraker may not be running or fluidd#1786 not yet available).",
              file=sys.stderr)


def remove(base_url):
    """Remove KlipperFleet link from Fluidd navigation."""
    links = get_existing_links(base_url)
    filtered = [l for l in links if not is_klipperfleet(l)]

    if len(filtered) == len(links):
        print("KlipperFleet: No Fluidd navigation entry found (skipped).")
        return

    if save_links(base_url, filtered):
        print("KlipperFleet: Fluidd navigation entry removed.")
    else:
        print("KlipperFleet: WARNING: Could not remove Fluidd navigation entry.",
              file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Manage KlipperFleet's Fluidd sidebar link")
    parser.add_argument("--remove", action="store_true", help="Remove the link instead of adding it")
    parser.add_argument("--moonraker-url", default=MOONRAKER_DEFAULT_URL,
                        help=f"Moonraker base URL (default: {MOONRAKER_DEFAULT_URL})")
    args = parser.parse_args()

    if args.remove:
        remove(args.moonraker_url)
    else:
        install(args.moonraker_url)


if __name__ == "__main__":
    main()
