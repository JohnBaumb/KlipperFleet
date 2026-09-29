"""Tests for the Mainsail navi.json add/remove logic.

The old sed-based uninstall deleted only the `"title": "KlipperFleet"` line and
left the rest of the object behind, so Mainsail showed a nameless sidebar entry
pointing at a dead link. Observed on a real CR10 uninstall/reinstall round trip.
"""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'install_scripts',
    'setup_mainsail_navi.py',
)


@pytest.fixture
def navi():
    spec = importlib.util.spec_from_file_location('setup_mainsail_navi', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


KRASH = {
    'title': 'KRASH',
    'href': '/KRASH/index.html',
    'target': '_self',
    'icon': 'M13,14C9.64,14',
    'position': 85,
}

# What the old uninstaller actually left behind: the object minus its title.
DECAPITATED = {
    'href': '/printer-klipperfleet.html',
    'target': '_self',
    'icon': 'M20,21V19L17,16H13V13H16V11H13V8H16V6H13V3H11V6H8V8H11V11H8V13H11V16H7L4,19V21H20Z',
    'position': 86,
}


def _run(tmp_path, data, *args):
    path = tmp_path / 'navi.json'
    path.write_text(json.dumps(data, indent=2), encoding='utf-8')
    result = subprocess.run(
        [sys.executable, SCRIPT, str(path), *args],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(path.read_text(encoding='utf-8')), result.stdout


class TestRemove:
    def test_removes_our_entry_and_keeps_others(self, tmp_path, navi):
        data, out = _run(tmp_path, [KRASH, navi.ENTRY], '--remove')
        assert data == [KRASH]
        assert 'removed' in out.lower()

    def test_cleans_up_the_decapitated_leftover(self, tmp_path):
        """The exact broken state the old sed-based uninstall produced."""
        data, _ = _run(tmp_path, [KRASH, DECAPITATED], '--remove')
        assert data == [KRASH]

    def test_removes_the_pre_issue39_shim_path(self, tmp_path):
        old = dict(KRASH, title='KlipperFleet', href='/klipperfleet.html')
        data, _ = _run(tmp_path, [KRASH, old], '--remove')
        assert data == [KRASH]

    def test_empty_file_stays_valid(self, tmp_path):
        data, _ = _run(tmp_path, [], '--remove')
        assert data == []

    def test_is_idempotent(self, tmp_path, navi):
        path = tmp_path / 'navi.json'
        path.write_text(json.dumps([KRASH, navi.ENTRY]), encoding='utf-8')
        for _ in range(2):
            subprocess.run(
                [sys.executable, SCRIPT, str(path), '--remove'], check=True
            )
        assert json.loads(path.read_text(encoding='utf-8')) == [KRASH]


class TestAdd:
    def test_adds_entry_alongside_existing(self, tmp_path, navi):
        data, out = _run(tmp_path, [KRASH])
        assert data == [KRASH, navi.ENTRY]
        assert 'configured' in out.lower()

    def test_replaces_rather_than_duplicates(self, tmp_path, navi):
        data, _ = _run(tmp_path, [KRASH, navi.ENTRY])
        assert data == [KRASH, navi.ENTRY]
        assert sum(1 for e in data if e.get('title') == 'KlipperFleet') == 1

    def test_upgrades_the_old_shim_path(self, tmp_path, navi):
        old = dict(navi.ENTRY, href='/klipperfleet.html')
        data, _ = _run(tmp_path, [old])
        assert data == [navi.ENTRY]

    def test_install_after_broken_uninstall_leaves_one_entry(self, tmp_path, navi):
        """Reinstalling over the old uninstaller's mess must not keep the orphan."""
        data, _ = _run(tmp_path, [KRASH, DECAPITATED])
        assert data == [KRASH, navi.ENTRY]

    def test_missing_file_is_created(self, tmp_path, navi):
        path = tmp_path / 'navi.json'
        subprocess.run([sys.executable, SCRIPT, str(path)], check=True)
        assert json.loads(path.read_text(encoding='utf-8')) == [navi.ENTRY]

    def test_corrupt_file_is_replaced(self, tmp_path, navi):
        path = tmp_path / 'navi.json'
        path.write_text('not json at all', encoding='utf-8')
        subprocess.run([sys.executable, SCRIPT, str(path)], check=True)
        assert json.loads(path.read_text(encoding='utf-8')) == [navi.ENTRY]
