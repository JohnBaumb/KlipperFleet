#!/usr/bin/env python3
"""Add/update the KlipperFleet update_manager section in moonraker.conf.

Usage: python3 setup_moonraker.py <moonraker.conf path> <KlipperFleet repo path>

Idempotent: creates the section if missing, migrates deprecated options if
the section already exists (e.g. install_script -> system_dependencies).
"""
import os
import re
import sys

# The three declarative dependency lines Moonraker needs to handle updates.
# - virtualenv + requirements: pip deps installed into venv on every update
# - system_dependencies: apt packages installed on every update
MANAGED_DEPS = """\
virtualenv: {kf_path}/venv
requirements: backend/requirements.txt
system_dependencies: install_scripts/system-dependencies.json"""


def _extract_klipperfleet_section(content: str):
    """Return (start, end) char offsets of the [update_manager klipperfleet] section."""
    m = re.search(r"^\[update_manager klipperfleet\]", content, re.MULTILINE)
    if not m:
        return None, None
    start = m.start()
    # Section ends at the next [section] header or EOF
    next_section = re.search(r"^\[", content[m.end():], re.MULTILINE)
    end = m.end() + next_section.start() if next_section else len(content)
    return start, end


def migrate_moonraker_conf(conf_path: str, kf_path: str) -> bool:
    """Migrate an existing moonraker.conf in-place.

    Returns True if the file was changed, False otherwise.
    This is also used by main.py's startup self-heal.
    """
    if not os.path.isfile(conf_path):
        return False

    with open(conf_path, "r", encoding="utf-8") as f:
        content = f.read()

    start, end = _extract_klipperfleet_section(content)
    if start is None:
        return False  # No klipperfleet section

    section = content[start:end]
    deps = MANAGED_DEPS.format(kf_path=kf_path)
    changed = False

    # Remove deprecated install_script line
    if "install_script:" in section:
        section = re.sub(r"\n?install_script:.*", "", section)
        changed = True

    # Add missing dependency lines
    for line in deps.splitlines():
        key = line.split(":")[0].strip()
        if key + ":" not in section:
            # Insert before is_system_service or at end of section
            m_sys = re.search(r"^is_system_service:.*$", section, re.MULTILINE)
            if m_sys:
                section = section[:m_sys.start()] + line + "\n" + section[m_sys.start():]
            else:
                section = section.rstrip() + "\n" + line + "\n"
            changed = True

    if changed:
        content = content[:start] + section + content[end:]
        with open(conf_path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    return False


def main():
    if len(sys.argv) < 3:
        print("Usage: setup_moonraker.py <moonraker_conf_path> <kf_repo_path>", file=sys.stderr)
        sys.exit(1)

    conf_path = sys.argv[1]
    kf_path = sys.argv[2]

    SECTION_MARKER = "[update_manager klipperfleet]"
    deps = MANAGED_DEPS.format(kf_path=kf_path)

    SECTION_BLOCK = f"""
[update_manager klipperfleet]
type: git_repo
path: {kf_path}
origin: https://github.com/JohnBaumb/KlipperFleet.git
primary_branch: main
managed_services: klipperfleet
{deps}
is_system_service: False
"""

    if not os.path.isfile(conf_path):
        print(
            f"KlipperFleet: WARNING: moonraker.conf not found at {conf_path}; "
            "skipping update_manager integration.",
            file=sys.stderr,
        )
        sys.exit(0)

    with open(conf_path, "r", encoding="utf-8") as f:
        content = f.read()

    if SECTION_MARKER in content:
        if migrate_moonraker_conf(conf_path, kf_path):
            print("KlipperFleet: Migrated moonraker.conf (added virtualenv/requirements/system_dependencies).")
        else:
            print("KlipperFleet: update_manager section already up to date in moonraker.conf.")
        sys.exit(0)

    # Append the section, ensuring a leading newline for clean separation.
    with open(conf_path, "a", encoding="utf-8") as f:
        f.write(SECTION_BLOCK)

    print("KlipperFleet: Added update_manager section to moonraker.conf.")


if __name__ == "__main__":
    main()
