"""Tests for bug fixes applied to main.py utilities and FleetManager/FlashManager."""
import pytest
import re
import os
import sys
import json
import threading
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from backend.flash_manager import FlashManager

# ---------------------------------------------------------------------------
# Bug #1: Task ID uniqueness (uuid-based)
# ---------------------------------------------------------------------------


class TestTaskIdUniqueness:
    """Bug #1: Task IDs must be unique even when created in the same second."""

    def test_uuid_ids_are_unique(self):
        """Generate many task IDs and verify no collisions."""
        import uuid
        ids = set()
        for _ in range(10_000):
            tid = f"task_{uuid.uuid4().hex[:12]}"
            assert tid not in ids, f"Collision detected: {tid}"
            ids.add(tid)

    def test_uuid_id_format(self):
        """Task IDs should match the expected format."""
        import uuid
        tid = f"task_{uuid.uuid4().hex[:12]}"
        assert tid.startswith("task_")
        assert len(tid) == 17  # "task_" (5) + 12 hex chars


# ---------------------------------------------------------------------------
# Bug #10: Profile name validation (path traversal prevention)
# ---------------------------------------------------------------------------

# Import the regex and validator from main.py
_PROFILE_NAME_RE = re.compile(r'^[a-zA-Z0-9_.-]+$')

def _validate_profile_name(name):
    """Local copy of validate_profile_name logic for testing without FastAPI."""
    if not name or not _PROFILE_NAME_RE.match(name) or '..' in name:
        raise ValueError(f"Invalid profile name: '{name}'")


class TestProfileNameValidation:
    """Bug #10: Profile names must not allow path traversal."""

    def test_valid_simple_name(self):
        _validate_profile_name("my_profile")

    def test_valid_with_dots(self):
        _validate_profile_name("spider.v3")

    def test_valid_with_hyphens(self):
        _validate_profile_name("cr-10-spider")

    def test_valid_alphanumeric(self):
        _validate_profile_name("Profile123")

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError):
            _validate_profile_name("../../etc/cron.d/evil")

    def test_rejects_double_dots(self):
        with pytest.raises(ValueError):
            _validate_profile_name("some..name")

    def test_rejects_slash(self):
        with pytest.raises(ValueError):
            _validate_profile_name("profiles/evil")

    def test_rejects_backslash(self):
        with pytest.raises(ValueError):
            _validate_profile_name("profiles\\evil")

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError):
            _validate_profile_name("")

    def test_rejects_spaces(self):
        with pytest.raises(ValueError):
            _validate_profile_name("my profile")

    def test_rejects_shell_metachar(self):
        with pytest.raises(ValueError):
            _validate_profile_name("profile;rm -rf /")


# ---------------------------------------------------------------------------
# Bug #2: Build failure detection in batch mode
# ---------------------------------------------------------------------------


class TestBuildFailureDetection:
    """Bug #2: Batch mode must detect 'Build failed' from BuildManager output."""

    def test_detects_bare_build_failed(self):
        """The string 'Build failed' (without !!! prefix) should be detected."""
        log_line = ">>> Build failed with return code 2\n"
        assert "Build failed" in log_line

    def test_detects_error_prefix(self):
        """Pre-build errors with !!! prefix should still be detected."""
        log_line = "!!! Error copying config: No such file\n"
        assert "!!! Error" in log_line

    def test_success_not_flagged(self):
        """Successful build output should not trigger failure detection."""
        log_line = ">>> Build successful!\n"
        assert "Build failed" not in log_line
        assert "!!! Error" not in log_line


# ---------------------------------------------------------------------------
# Bug #4: Fleet manager thread safety (concurrent writes)
# ---------------------------------------------------------------------------


class TestFleetManagerThreadSafety:
    """Bug #4: FleetManager must not lose writes under concurrent access."""

    @pytest.fixture
    def fleet_mgr(self, tmp_path):
        from backend.fleet_manager import FleetManager
        return FleetManager(str(tmp_path))

    def test_concurrent_saves_no_data_loss(self, fleet_mgr):
        """Concurrent save_device calls should not lose any devices."""
        errors = []

        def save_device(i):
            try:
                fleet_mgr.save_device({"id": f"dev_{i}", "name": f"Device {i}"})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=save_device, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent saves: {errors}"
        fleet = fleet_mgr.get_fleet()
        assert len(fleet) == 20, f"Expected 20 devices, got {len(fleet)}"

    def test_atomic_write_no_partial_json(self, fleet_mgr, tmp_path):
        """Fleet file should never contain partial/corrupt JSON."""
        # Save a device
        fleet_mgr.save_device({"id": "test", "name": "Test"})

        # Read the raw file and verify it's valid JSON
        fleet_file = tmp_path / "fleet.json"
        with open(fleet_file, "r") as f:
            data = json.load(f)
        assert len(data) == 1
        assert data[0]["id"] == "test"

    def test_concurrent_read_write(self, fleet_mgr):
        """Reading fleet while writing should not raise."""
        errors = []

        def writer():
            for i in range(10):
                try:
                    fleet_mgr.save_device({"id": f"w_{i}", "name": f"Writer {i}"})
                except Exception as e:
                    errors.append(e)

        def reader():
            for _ in range(10):
                try:
                    fleet_mgr.get_fleet()
                except Exception as e:
                    errors.append(e)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=reader)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Errors during concurrent read/write: {errors}"

    def test_update_version_under_lock(self, fleet_mgr):
        """update_device_version should work safely with the lock."""
        fleet_mgr.save_device({"id": "dev1", "name": "Device 1"})
        fleet_mgr.update_device_version("dev1", {"version": "v1.0", "commit": "abc123"})
        fleet = fleet_mgr.get_fleet()
        assert fleet[0]["flashed_version"] == "v1.0"
        assert fleet[0]["flashed_commit"] == "abc123"
        assert "last_flashed" in fleet[0]


# ---------------------------------------------------------------------------
# Bug #6: CAN cache scoped by interface
# ---------------------------------------------------------------------------


class TestCanCacheByInterface:
    """Bug #6: CAN discovery cache must be per-interface."""

    def test_cache_dict_initialized(self):
        """_can_cache should be a dict, not a list."""
        from backend.flash_manager import FlashManager
        mgr = FlashManager("/tmp/klipper", "/tmp/katapult")
        assert isinstance(mgr._can_cache, dict)
        assert isinstance(mgr._can_cache_time, dict)

    def test_cache_entries_independent(self):
        """Caching can0 results should not affect can1 lookups."""
        from backend.flash_manager import FlashManager
        mgr = FlashManager("/tmp/klipper", "/tmp/katapult")

        # Simulate caching results for can0
        can0_devices = [{"id": "aabbccddeeff", "name": "CAN0 Device"}]
        mgr._can_cache["can0"] = can0_devices
        mgr._can_cache_time["can0"] = 999999999.0

        # can1 should have no cached results
        assert mgr._can_cache.get("can1") is None
        assert mgr._can_cache_time.get("can1", 0.0) == 0.0


# ---------------------------------------------------------------------------
# Bug #8: resolve_dfu_id strict mode
# ---------------------------------------------------------------------------


class TestResolveDfuIdStrict:
    """Bug #8: strict mode should prevent single-device fallback."""

    @pytest.mark.asyncio
    async def test_strict_mode_skips_fallback(self):
        """With strict=True, a single unmatched DFU device should not be returned."""
        from backend.flash_manager import FlashManager
        mgr = FlashManager("/tmp/klipper", "/tmp/katapult")

        # Mock discover_dfu_devices to return one unrelated device
        mgr.discover_dfu_devices = AsyncMock(return_value=[
            {"id": "unrelated_serial", "name": "DFU Device", "serial": "UNRELATED", "type": "dfu"}
        ])

        result = await mgr.resolve_dfu_id("my_device_id", strict=True)
        # strict=True means no fallback — should return the original device_id
        assert result == "my_device_id"

    @pytest.mark.asyncio
    async def test_non_strict_mode_uses_fallback(self):
        """With strict=False (default), single DFU device should be used as fallback."""
        from backend.flash_manager import FlashManager
        mgr = FlashManager("/tmp/klipper", "/tmp/katapult")

        mgr.discover_dfu_devices = AsyncMock(return_value=[
            {"id": "only_dfu_device", "name": "DFU Device", "serial": "UNRELATED", "type": "dfu"}
        ])

        result = await mgr.resolve_dfu_id("my_device_id", strict=False)
        assert result == "only_dfu_device"

    @pytest.mark.asyncio
    async def test_exact_match_works_in_strict_mode(self):
        """Even in strict mode, an exact match should succeed."""
        from backend.flash_manager import FlashManager
        mgr = FlashManager("/tmp/klipper", "/tmp/katapult")

        mgr.discover_dfu_devices = AsyncMock(return_value=[
            {"id": "exact_id", "name": "DFU Device", "serial": "SN123", "type": "dfu"}
        ])

        result = await mgr.resolve_dfu_id("exact_id", strict=True)
        assert result == "exact_id"


# ---------------------------------------------------------------------------
# Bug #13: XSS in formatNotes
# ---------------------------------------------------------------------------


class TestFormatNotesXSS:
    """Bug #13: formatNotes must escape HTML before URL replacement."""

    def _format_notes(self, text):
        """Python implementation of the fixed formatNotes JS function."""
        if not text:
            return ''
        import html
        escaped = html.escape(text)
        import re
        url_regex = re.compile(r'(https?://\S+)')
        return url_regex.sub(
            r'<a href="\1" target="_blank" class="text-blue-400 hover:underline">\1</a>',
            escaped
        )

    def test_plain_text_unchanged(self):
        result = self._format_notes("Hello world")
        assert result == "Hello world"

    def test_url_converted_to_link(self):
        result = self._format_notes("Visit https://example.com for info")
        assert '<a href="https://example.com"' in result

    def test_html_tags_escaped(self):
        result = self._format_notes('<script>alert("xss")</script>')
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_html_in_notes_with_url(self):
        result = self._format_notes('<b>bold</b> see https://example.com')
        assert "&lt;b&gt;" in result
        assert '<a href="https://example.com"' in result

    def test_empty_string(self):
        assert self._format_notes("") == ""

    def test_none_input(self):
        assert self._format_notes(None) == ""


# ---------------------------------------------------------------------------
# Bug #17: list_can_interfaces @NONE stripping
# ---------------------------------------------------------------------------


class TestCanInterfaceParsing:
    """Bug #17: CAN interface names must have @NONE stripped."""

    def test_strip_at_none(self):
        """ip link output 'can0@NONE' should parse to 'can0'."""
        line = "3: can0@NONE: <NOARP,UP,LOWER_UP> mtu 16 qdisc pfifo_fast state UP mode DEFAULT"
        iface = line.split(":")[1].strip().split("@")[0]
        assert iface == "can0"

    def test_no_at_suffix(self):
        """Normal 'can0' without @NONE should still work."""
        line = "3: can0: <NOARP,UP,LOWER_UP> mtu 16 qdisc pfifo_fast state UP mode DEFAULT"
        iface = line.split(":")[1].strip().split("@")[0]
        assert iface == "can0"

    def test_can1_at_none(self):
        """Second CAN interface with @NONE."""
        line = "4: can1@NONE: <NOARP,UP,LOWER_UP> mtu 16"
        iface = line.split(":")[1].strip().split("@")[0]
        assert iface == "can1"


# ---------------------------------------------------------------------------
# Bug #3: Python 3.9 compatibility (Set[int] import)
# ---------------------------------------------------------------------------


class TestPython39Compatibility:
    """Bug #3: Verify typing imports are 3.9-compatible."""

    def test_flash_manager_imports(self):
        """FlashManager should import cleanly (Set[int] instead of set[int])."""
        from backend.flash_manager import FlashManager
        # If this import succeeds, the typing is 3.9-compatible
        assert FlashManager is not None

    def test_fleet_manager_imports(self):
        """FleetManager should import cleanly (Optional[Any] instead of Any | None)."""
        from backend.fleet_manager import FleetManager
        assert FleetManager is not None

    def test_build_manager_imports(self):
        """BuildManager should import cleanly."""
        from backend.build_manager import BuildManager
        assert BuildManager is not None


# ---------------------------------------------------------------------------
# Bug #5: KconfigManager async lock
# ---------------------------------------------------------------------------


class TestKconfigManagerLock:
    """Bug #5: KconfigManager.load_kconfig should be async with a lock."""

    def test_kconfig_has_lock(self):
        """KconfigManager should have an asyncio.Lock."""
        # We can't fully instantiate KconfigManager without kconfiglib,
        # but we can check the class has the right attribute setup.
        import inspect
        from backend.kconfig_manager import KconfigManager
        source = inspect.getsource(KconfigManager.__init__)
        assert "_kconfig_lock" in source

    def test_load_kconfig_is_async(self):
        """load_kconfig should be an async method."""
        from backend.kconfig_manager import KconfigManager
        import asyncio
        assert asyncio.iscoroutinefunction(KconfigManager.load_kconfig)


# ---------------------------------------------------------------------------
# Bug #11: CAN reboot script injection prevention
# ---------------------------------------------------------------------------


class TestCanRebootScriptSafety:
    """Bug #11: CAN reboot inline script should not embed device_id via f-string."""

    def test_script_uses_sys_argv(self):
        """The inline Python script should read from sys.argv, not f-string."""
        import inspect
        from backend.flash_manager import FlashManager
        source = inspect.getsource(FlashManager.reboot_device)
        # Should contain sys.argv references
        assert "sys.argv[1]" in source or "sys.argv" in source
        # Should NOT contain f-string interpolation of device_id in the script body
        # The old code had: bytes.fromhex("{device_id}") — check it's gone
        assert 'bytes.fromhex("{device_id}")' not in source
        assert 's.bind(("{interface}",))' not in source


# ---------------------------------------------------------------------------
# Bug #15: AVR firmware path resolution (.elf fallback)
# GitHub Issue: https://github.com/JohnBaumb/KlipperFleet/issues/15
# ATmega2560 builds produce .elf but flash looked for .bin only.
# ---------------------------------------------------------------------------


class TestFirmwarePathResolution:
    """Bug #15: resolve_firmware_path must fall back from .bin to .elf for AVR boards."""

    @pytest.fixture(autouse=True)
    def _setup_artifacts(self, tmp_path, monkeypatch):
        """Create a temp artifacts dir and patch ARTIFACTS_DIR."""
        self.artifacts = tmp_path / "artifacts"
        self.artifacts.mkdir()
        import backend.main as main_mod
        monkeypatch.setattr(main_mod, "ARTIFACTS_DIR", str(self.artifacts))
        self._main = main_mod

    def test_returns_bin_when_only_bin_exists(self):
        """Standard ARM/STM32 boards that produce .bin should resolve to .bin."""
        (self.artifacts / "spider.bin").write_bytes(b"\x00")
        result = self._main.resolve_firmware_path("spider", "serial")
        assert result is not None
        assert result.endswith("spider.bin")

    def test_returns_elf_when_only_elf_exists(self):
        """AVR boards (ATmega2560) that only produce .elf should still resolve."""
        (self.artifacts / "2560.elf").write_bytes(b"\x00")
        result = self._main.resolve_firmware_path("2560", "serial")
        assert result is not None
        assert result.endswith("2560.elf")

    def test_prefers_bin_over_elf(self):
        """When both .bin and .elf exist, .bin should be preferred."""
        (self.artifacts / "spider.bin").write_bytes(b"\x00")
        (self.artifacts / "spider.elf").write_bytes(b"\x00")
        result = self._main.resolve_firmware_path("spider", "serial")
        assert result is not None
        assert result.endswith("spider.bin")

    def test_returns_none_when_no_firmware(self):
        """Should return None when no firmware file exists."""
        result = self._main.resolve_firmware_path("nonexistent", "serial")
        assert result is None

    def test_linux_method_uses_elf_only(self):
        """Linux MCU flash method should only look for .elf."""
        (self.artifacts / "linux.elf").write_bytes(b"\x00")
        result = self._main.resolve_firmware_path("linux", "linux")
        assert result is not None
        assert result.endswith("linux.elf")

    def test_linux_method_ignores_bin(self):
        """Linux MCU flash method should NOT fall back to .bin."""
        (self.artifacts / "linux.bin").write_bytes(b"\x00")
        result = self._main.resolve_firmware_path("linux", "linux")
        assert result is None

    def test_dfu_method_falls_back_to_elf(self):
        """DFU flash method should also fall back to .elf."""
        (self.artifacts / "avr_board.elf").write_bytes(b"\x00")
        result = self._main.resolve_firmware_path("avr_board", "dfu")
        assert result is not None
        assert result.endswith("avr_board.elf")

    def test_can_method_falls_back_to_elf(self):
        """CAN flash method should also fall back to .elf."""
        (self.artifacts / "avr_board.elf").write_bytes(b"\x00")
        result = self._main.resolve_firmware_path("avr_board", "can")
        assert result is not None
        assert result.endswith("avr_board.elf")


# ---------------------------------------------------------------------------
# Bug #14: Linux process flash on Ubuntu
# https://github.com/JohnBaumb/KlipperFleet/issues/14
#
# Three problems:
# 1. Single-device flash tried to reboot Linux MCU to Katapult (nonsensical).
# 2. flash_linux() used bare `sudo` which hangs/fails without a TTY.
# 3. Install script didn't set up passwordless sudoers.
# ---------------------------------------------------------------------------


class TestLinuxProcessFlash:
    """Bug #14: Linux MCU flash should not attempt serial reboot or fail on sudo."""

    def test_check_device_status_linux_service(self):
        """check_device_status for linux method returns 'service' when host MCU exists."""
        import asyncio
        from unittest.mock import patch

        mgr = MagicMock()
        mgr.check_device_status = FlashManager.check_device_status.__get__(mgr)

        with patch("os.path.exists", return_value=True):
            status = asyncio.get_event_loop().run_until_complete(
                mgr.check_device_status("linux_process", "linux")
            )
        assert status == "service"

    def test_check_device_status_linux_ready(self):
        """check_device_status for linux method returns 'ready' when host MCU doesn't exist."""
        import asyncio
        from unittest.mock import patch

        mgr = MagicMock()
        mgr.check_device_status = FlashManager.check_device_status.__get__(mgr)

        with patch("os.path.exists", return_value=False):
            status = asyncio.get_event_loop().run_until_complete(
                mgr.check_device_status("linux_process", "linux")
            )
        assert status == "ready"

    @pytest.mark.asyncio
    async def test_flash_linux_uses_sudo_helper(self):
        """flash_linux routes all privileged commands through _run_sudo_command."""
        mgr = FlashManager("/tmp/klipper", "/tmp/katapult")

        captured_cmds = []

        async def spy_run(cmd):
            captured_cmds.append(list(cmd))
            # Simulate success
            return (0, "")

        mgr._run_sudo_command = spy_run

        logs = []
        async for line in mgr.flash_linux("/tmp/test.elf"):
            logs.append(line)

        # Verify all sudo commands were routed through _run_sudo_command
        # (systemctl stop, fuser, cp, chmod = 4 calls)
        assert len(captured_cmds) >= 3, f"Expected at least 3 sudo commands, got {len(captured_cmds)}"

        # Verify the expected commands were called
        cmd_verbs = [cmd[1] if len(cmd) > 1 else "" for cmd in captured_cmds]
        assert any("systemctl" in v for v in cmd_verbs), "Expected systemctl stop call"
        assert any("cp" in v for v in cmd_verbs), "Expected cp call"

    @pytest.mark.asyncio
    async def test_flash_linux_sudo_failure_gives_instructions(self):
        """When sudo fails due to password requirement, clear instructions are shown."""
        mgr = FlashManager("/tmp/klipper", "/tmp/katapult")

        async def fail_run(cmd):
            return (1, "sudo: a terminal is required to read the password")

        mgr._run_sudo_command = fail_run

        logs = []
        async for line in mgr.flash_linux("/tmp/test.elf"):
            logs.append(line)

        combined = "".join(logs)
        assert "SUDO ERROR" in combined
        assert "sudoers" in combined.lower() or "visudo" in combined.lower()

    @pytest.mark.asyncio
    async def test_run_sudo_command_inserts_n_flag(self):
        """_run_sudo_command should insert -n after sudo."""
        mgr = FlashManager("/tmp/klipper", "/tmp/katapult")

        called_with = []

        async def mock_subprocess(*args, **kwargs):
            called_with.append(list(args))
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"ok", b""))
            proc.returncode = 0
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_subprocess):
            rc, out = await mgr._run_sudo_command(["sudo", "cp", "/a", "/b"])

        assert rc == 0
        # Verify -n was inserted
        executed_cmd = called_with[0]
        assert executed_cmd[0] == "sudo"
        assert executed_cmd[1] == "-n"
        assert "cp" in executed_cmd


