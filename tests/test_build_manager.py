"""Tests for BuildManager bug fixes."""
import pytest
import json

from backend.build_manager import BuildManager


@pytest.fixture
def build_mgr(tmp_path):
    """Create a BuildManager with a temp klipper dir and artifacts dir."""
    klipper_dir = tmp_path / "klipper"
    klipper_dir.mkdir()
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    return BuildManager(str(klipper_dir), str(artifacts_dir))


class TestBuildInfoPersistence:
    """Bug #7: Build info should survive service restarts."""

    def test_empty_artifacts_dir_loads_cleanly(self, build_mgr):
        """No crash when artifacts dir has no .build_info.json files."""
        assert build_mgr._last_build_info == {}

    def test_build_info_loaded_from_disk(self, tmp_path):
        """Build info JSON files should be loaded on construction."""
        klipper_dir = tmp_path / "klipper"
        klipper_dir.mkdir()
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        # Pre-populate a build info file
        info = {
            "version": "v0.12.0-100-gabcdef",
            "commit": "abcdef123456",
            "date": "2025-01-01 12:00:00",
            "built_at": "2025-01-01 12:05:00"
        }
        info_path = artifacts_dir / "my_profile.build_info.json"
        with open(info_path, "w") as f:
            json.dump(info, f)

        mgr = BuildManager(str(klipper_dir), str(artifacts_dir))
        loaded = mgr.get_last_build_info("my_profile")
        assert loaded is not None
        assert loaded["version"] == "v0.12.0-100-gabcdef"
        assert loaded["commit"] == "abcdef123456"

    def test_multiple_build_infos_loaded(self, tmp_path):
        """All .build_info.json files should be loaded, not just one."""
        klipper_dir = tmp_path / "klipper"
        klipper_dir.mkdir()
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        for name in ["profile_a", "profile_b", "profile_c"]:
            info_path = artifacts_dir / f"{name}.build_info.json"
            with open(info_path, "w") as f:
                json.dump({"version": f"v-{name}", "commit": "abc"}, f)

        mgr = BuildManager(str(klipper_dir), str(artifacts_dir))
        assert mgr.get_last_build_info("profile_a")["version"] == "v-profile_a"
        assert mgr.get_last_build_info("profile_b")["version"] == "v-profile_b"
        assert mgr.get_last_build_info("profile_c")["version"] == "v-profile_c"

    def test_corrupt_build_info_does_not_crash(self, tmp_path):
        """A corrupt JSON file should not prevent startup."""
        klipper_dir = tmp_path / "klipper"
        klipper_dir.mkdir()
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        info_path = artifacts_dir / "bad_profile.build_info.json"
        info_path.write_text("{invalid json")

        # Should not raise — just prints a warning
        mgr = BuildManager(str(klipper_dir), str(artifacts_dir))
        assert mgr.get_last_build_info("bad_profile") is None

    def test_non_build_info_files_ignored(self, tmp_path):
        """Only .build_info.json files should be loaded, not .bin or .elf."""
        klipper_dir = tmp_path / "klipper"
        klipper_dir.mkdir()
        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        # Create some non-build-info files
        (artifacts_dir / "profile.bin").write_bytes(b"\x00" * 100)
        (artifacts_dir / "profile.elf").write_bytes(b"\x00" * 100)

        mgr = BuildManager(str(klipper_dir), str(artifacts_dir))
        assert mgr._last_build_info == {}

    def test_get_last_build_info_returns_none_for_unknown(self, build_mgr):
        """Querying an unknown profile returns None."""
        assert build_mgr.get_last_build_info("nonexistent") is None
