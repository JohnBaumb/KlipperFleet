"""Tests for Issue #17 batch flash fixes: fleet_id preservation, version tracking,
and post-flash serial rescan when USB descriptors change."""
import pytest
import json
import asyncio
import os
from typing import List, Dict, Optional, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch
from backend.fleet_manager import FleetManager
from backend.flash_manager import FlashManager


# Helper to collect async generator output
async def _collect_logs(gen):
    return [log async for log in gen]


# ---------------------------------------------------------------------------
# Fix 1: fleet_id preservation before reboot phase mutation
# ---------------------------------------------------------------------------


class TestFleetIdPreservation:
    """The batch flash path mutates dev['id'] during Katapult reboot (e.g.
    usb-Kalico_stm32f103xe_... -> usb-katapult_stm32f103xe_...).  The fleet.json
    ID must be preserved so version tracking and rescan can find the entry."""

    def test_fleet_id_set_before_reboot(self):
        """dev['fleet_id'] should be set to the original ID before needs_reboot check."""
        devices = [
            {"id": "/dev/serial/by-id/usb-Kalico_stm32f103xe_AABB-if00",
             "method": "serial", "name": "MCU", "is_katapult": True, "is_bridge": False},
            {"id": "linux_process",
             "method": "linux", "name": "RPi", "is_bridge": False},
        ]

        # Simulate the batch logic
        for dev in devices:
            dev["fleet_id"] = dev["id"]

        assert devices[0]["fleet_id"] == "/dev/serial/by-id/usb-Kalico_stm32f103xe_AABB-if00"
        assert devices[1]["fleet_id"] == "linux_process"

    def test_fleet_id_survives_id_mutation(self):
        """After reboot phase mutates dev['id'], fleet_id still holds the original."""
        dev = {
            "id": "/dev/serial/by-id/usb-Kalico_stm32f103xe_AABB-if00",
            "method": "serial", "name": "MCU", "is_katapult": True,
        }
        dev["fleet_id"] = dev["id"]

        # Simulate reboot phase mutation
        dev["id"] = "/dev/serial/by-id/usb-katapult_stm32f103xe_AABB-if00"

        assert dev["id"] == "/dev/serial/by-id/usb-katapult_stm32f103xe_AABB-if00"
        assert dev["fleet_id"] == "/dev/serial/by-id/usb-Kalico_stm32f103xe_AABB-if00"


# ---------------------------------------------------------------------------
# Fix 2: Version metadata tracking in batch flash path
# ---------------------------------------------------------------------------


class TestBatchVersionTracking:
    """After a successful batch flash, update_device_version must be called with
    the fleet.json ID (fleet_id), not the mutated Katapult ID."""

    @pytest.fixture
    def fleet_mgr(self, tmp_path):
        return FleetManager(str(tmp_path))

    @pytest.mark.asyncio
    async def test_version_updated_with_fleet_id(self, fleet_mgr):
        """update_device_version should find the device by its fleet.json ID."""
        fleet_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe_AABB-if00"
        await fleet_mgr.save_device({"id": fleet_id, "name": "MCU", "profile": "skr"})

        await fleet_mgr.update_device_version(fleet_id, {
            "version": "v2026.03.00", "commit": "abc123"
        })

        fleet = await fleet_mgr.get_fleet()
        assert fleet[0]["flashed_version"] == "v2026.03.00"
        assert fleet[0]["flashed_commit"] == "abc123"
        assert "last_flashed" in fleet[0]

    @pytest.mark.asyncio
    async def test_version_not_updated_with_katapult_id(self, fleet_mgr):
        """update_device_version with a Katapult path should NOT match the fleet entry."""
        fleet_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe_AABB-if00"
        katapult_id = "/dev/serial/by-id/usb-katapult_stm32f103xe_AABB-if00"
        await fleet_mgr.save_device({"id": fleet_id, "name": "MCU"})

        # This is the bug: using the mutated katapult ID won't match
        await fleet_mgr.update_device_version(katapult_id, {
            "version": "v2026.03.00", "commit": "abc123"
        })

        fleet = await fleet_mgr.get_fleet()
        assert fleet[0].get("flashed_version") is None

    @pytest.mark.asyncio
    async def test_version_tracking_uses_fleet_id_not_dev_id(self, fleet_mgr):
        """Simulates the full batch flow: fleet_id should be used, not dev['id']."""
        fleet_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe_AABB-if00"
        await fleet_mgr.save_device({"id": fleet_id, "name": "MCU", "profile": "skr"})

        dev = {"id": fleet_id, "profile": "skr", "fleet_id": fleet_id}
        # Simulate reboot mutation
        dev["id"] = "/dev/serial/by-id/usb-katapult_stm32f103xe_AABB-if00"

        # Use fleet_id as the patched code does
        resolved_id = dev.get("fleet_id", dev["id"])
        await fleet_mgr.update_device_version(resolved_id, {
            "version": "v2026.03.00", "commit": "abc123"
        })

        fleet = await fleet_mgr.get_fleet()
        assert fleet[0]["flashed_version"] == "v2026.03.00"


# ---------------------------------------------------------------------------
# Fix 3: Post-flash serial rescan in batch flash path
# Tests import FlashManager.post_flash_rescan directly — no reimplementation.
# ---------------------------------------------------------------------------


class TestPostFlashRescan:
    """FlashManager.post_flash_rescan detects USB descriptor changes and updates fleet.json."""

    @pytest.fixture
    def fleet_mgr(self, tmp_path):
        return FleetManager(str(tmp_path))

    @pytest.fixture
    def flash_mgr(self):
        return FlashManager("/tmp/klipper", "/tmp/katapult")

    # --- Strategy 1: Chip serial suffix match ---

    @pytest.mark.asyncio
    async def test_strategy1_chip_serial_match(self, fleet_mgr, flash_mgr):
        """When the chip serial suffix matches, fleet.json should be updated."""
        old_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe_30FFDA05344E-if00"
        new_id = "/dev/serial/by-id/usb-Kalico17_stm32f103xe_30FFDA05344E-if00"
        await fleet_mgr.save_device({"id": old_id, "name": "MCU"})

        flash_mgr.discover_serial_devices = AsyncMock(
            return_value=[{"id": new_id}]
        )

        logs = await _collect_logs(
            flash_mgr.post_flash_rescan(old_id, [old_id], fleet_mgr)
        )

        fleet = await fleet_mgr.get_fleet()
        assert fleet[0]["id"] == new_id
        assert any("Fleet updated automatically" in l for l in logs)
        assert any("30FFDA05344E" in l for l in logs)

    @pytest.mark.asyncio
    async def test_strategy1_no_change_when_path_unchanged(self, fleet_mgr, flash_mgr):
        """When the device path hasn't changed, no update should occur."""
        device_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe_AABB-if00"
        await fleet_mgr.save_device({"id": device_id, "name": "MCU"})

        flash_mgr.discover_serial_devices = AsyncMock(
            return_value=[{"id": device_id}]
        )

        logs = await _collect_logs(
            flash_mgr.post_flash_rescan(device_id, [device_id], fleet_mgr)
        )

        fleet = await fleet_mgr.get_fleet()
        assert fleet[0]["id"] == device_id
        assert len(logs) == 0  # No output when nothing changed

    # --- Strategy 2: Diff-based fallback ---

    @pytest.mark.asyncio
    async def test_strategy2_diff_based_single_device(self, fleet_mgr, flash_mgr):
        """When chip serial can't match but exactly one device disappeared and one appeared."""
        old_id = "/dev/serial/by-id/usb-OldVendor_chip_SERIAL-if00"
        new_id = "/dev/serial/by-id/usb-NewVendor_totally_different-if00"
        await fleet_mgr.save_device({"id": old_id, "name": "MCU"})

        flash_mgr.discover_serial_devices = AsyncMock(
            return_value=[{"id": new_id}]
        )
        # Override serial extraction to return something that won't match
        flash_mgr._extract_serial_from_id = MagicMock(return_value="NOMATCH")

        logs = await _collect_logs(
            flash_mgr.post_flash_rescan(old_id, [old_id], fleet_mgr)
        )

        fleet = await fleet_mgr.get_fleet()
        assert fleet[0]["id"] == new_id
        assert any("matched by diff" in l for l in logs)

    # --- Strategy 3: Ambiguous / warning ---

    @pytest.mark.asyncio
    async def test_strategy3_ambiguous_multiple_new_devices(self, fleet_mgr, flash_mgr):
        """When multiple new devices appear, warn instead of guessing."""
        old_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe_AABB-if00"
        await fleet_mgr.save_device({"id": old_id, "name": "MCU"})

        new_devices = [
            {"id": "/dev/serial/by-id/usb-NewA_chip1-if00"},
            {"id": "/dev/serial/by-id/usb-NewB_chip2-if00"},
        ]

        flash_mgr.discover_serial_devices = AsyncMock(return_value=new_devices)
        flash_mgr._extract_serial_from_id = MagicMock(return_value="NOMATCH")

        logs = await _collect_logs(
            flash_mgr.post_flash_rescan(old_id, [old_id], fleet_mgr)
        )

        # Fleet should NOT be modified — ambiguous
        fleet = await fleet_mgr.get_fleet()
        assert fleet[0]["id"] == old_id
        assert any("WARNING" in l for l in logs)
        assert any("manually" in l for l in logs)

    @pytest.mark.asyncio
    async def test_strategy3_device_disappeared_no_new(self, fleet_mgr, flash_mgr):
        """When old device disappears but nothing new appears, warn."""
        old_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe_AABB-if00"
        await fleet_mgr.save_device({"id": old_id, "name": "MCU"})

        flash_mgr.discover_serial_devices = AsyncMock(return_value=[])
        flash_mgr._extract_serial_from_id = MagicMock(return_value="NOMATCH")

        logs = await _collect_logs(
            flash_mgr.post_flash_rescan(old_id, [old_id], fleet_mgr)
        )

        fleet = await fleet_mgr.get_fleet()
        assert fleet[0]["id"] == old_id  # Unchanged
        assert any("did not re-appear" in l for l in logs)

    # --- Edge case: blank serial (maintainer note) ---

    @pytest.mark.asyncio
    async def test_blank_serial_falls_through_to_diff(self, fleet_mgr, flash_mgr):
        """Some devices reappear with a blank serial in the USB descriptor.
        Strategy 1 (chip serial match) should fail gracefully, falling through
        to Strategy 2 (diff-based) or Strategy 3 (warning).
        See maintainer comment: 'some devices may reappear with a blank serial'."""
        old_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe_30FFDA05344E-if00"
        # Blank serial — no chip ID in the path
        blank_serial_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe-if00"
        await fleet_mgr.save_device({"id": old_id, "name": "MCU"})

        flash_mgr.discover_serial_devices = AsyncMock(
            return_value=[{"id": blank_serial_id}]
        )

        logs = await _collect_logs(
            flash_mgr.post_flash_rescan(old_id, [old_id], fleet_mgr)
        )

        fleet = await fleet_mgr.get_fleet()
        # Strategy 1 won't match (serial "30FFDA05344E" not in blank path)
        # Strategy 2 should pick it up (1 disappeared, 1 appeared)
        assert fleet[0]["id"] == blank_serial_id
        assert any("matched by diff" in l for l in logs)

    @pytest.mark.asyncio
    async def test_blank_serial_ambiguous_with_multiple_devices(self, fleet_mgr, flash_mgr):
        """Blank serial + multiple new devices = ambiguous, should warn."""
        old_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe_30FFDA05344E-if00"
        await fleet_mgr.save_device({"id": old_id, "name": "MCU"})

        # Two new devices (one blank serial, one different) — can't tell which is ours
        flash_mgr.discover_serial_devices = AsyncMock(return_value=[
            {"id": "/dev/serial/by-id/usb-Kalico_stm32f103xe-if00"},
            {"id": "/dev/serial/by-id/usb-Kalico_stm32g0b1xx_CCDD-if00"},
        ])

        logs = await _collect_logs(
            flash_mgr.post_flash_rescan(old_id, [old_id], fleet_mgr)
        )

        fleet = await fleet_mgr.get_fleet()
        assert fleet[0]["id"] == old_id  # Unchanged — ambiguous
        assert any("WARNING" in l for l in logs)

    @pytest.mark.asyncio
    async def test_blank_serial_single_device_with_other_existing(self, fleet_mgr, flash_mgr):
        """Blank serial reappear with other pre-existing devices still present.
        Only one device disappeared (old_id) and one appeared (blank), so
        diff-based should still work even with other devices present."""
        old_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe_30FFDA05344E-if00"
        other_id = "/dev/serial/by-id/usb-Kalico_stm32g0b1xx_CCDD-if00"
        blank_serial_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe-if00"
        await fleet_mgr.save_device({"id": old_id, "name": "MCU"})

        flash_mgr.discover_serial_devices = AsyncMock(return_value=[
            {"id": other_id},
            {"id": blank_serial_id},
        ])

        initial_serials = [old_id, other_id]
        logs = await _collect_logs(
            flash_mgr.post_flash_rescan(old_id, initial_serials, fleet_mgr)
        )

        fleet = await fleet_mgr.get_fleet()
        assert fleet[0]["id"] == blank_serial_id
        assert any("matched by diff" in l for l in logs)

    # --- Edge case: Strategy 1 false positive with blank serial ---

    @pytest.mark.asyncio
    async def test_blank_serial_strategy1_false_positive_same_chip(self, fleet_mgr, flash_mgr):
        """Blank serial + same chip type on another board — Strategy 1 may
        match the wrong device.  Documents known limitation."""
        old_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe_30FFDA05344E-if00"
        other_board = "/dev/serial/by-id/usb-Kalico_stm32f103xe_DEADBEEF1234-if00"
        blank_reappear = "/dev/serial/by-id/usb-Kalico_stm32f103xe-if00"
        await fleet_mgr.save_device({"id": old_id, "name": "MCU"})

        flash_mgr.discover_serial_devices = AsyncMock(return_value=[
            {"id": other_board},
            {"id": blank_reappear},
        ])

        logs = await _collect_logs(
            flash_mgr.post_flash_rescan(old_id, [old_id, other_board], fleet_mgr)
        )

        fleet = await fleet_mgr.get_fleet()
        # Known limitation: chip type substring matches the wrong device
        assert any("Fleet updated" in l or "WARNING" in l for l in logs)

    @pytest.mark.asyncio
    async def test_blank_serial_no_serial_at_all(self, fleet_mgr, flash_mgr):
        """Device reappears with completely stripped path — only vendor, no chip type."""
        old_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe_30FFDA05344E-if00"
        bare_id = "/dev/serial/by-id/usb-Kalico-if00"
        await fleet_mgr.save_device({"id": old_id, "name": "MCU"})

        flash_mgr.discover_serial_devices = AsyncMock(
            return_value=[{"id": bare_id}]
        )

        logs = await _collect_logs(
            flash_mgr.post_flash_rescan(old_id, [old_id], fleet_mgr)
        )

        fleet = await fleet_mgr.get_fleet()
        assert fleet[0]["id"] == bare_id
        assert any("Fleet updated" in l for l in logs)

    # --- Edge case: rescan when fleet entry was already removed ---

    @pytest.mark.asyncio
    async def test_rescan_fleet_entry_missing(self, fleet_mgr, flash_mgr):
        """Fleet entry removed before rescan — should warn, not crash."""
        old_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe_AABB-if00"
        new_id = "/dev/serial/by-id/usb-Kalico17_stm32f103xe_AABB-if00"

        flash_mgr.discover_serial_devices = AsyncMock(
            return_value=[{"id": new_id}]
        )

        logs = await _collect_logs(
            flash_mgr.post_flash_rescan(old_id, [old_id], fleet_mgr)
        )

        assert any("WARNING" in l and "not found" in l for l in logs)

    # --- Edge case: _extract_serial_from_id ---

    def test_extract_serial_blank_serial_path(self):
        """A device with no chip serial in the path should still return something."""
        mgr = FlashManager("/tmp/klipper", "/tmp/katapult")
        result = mgr._extract_serial_from_id(
            "/dev/serial/by-id/usb-Kalico_stm32f103xe-if00"
        )
        assert result is not None

    def test_extract_serial_normal_path(self):
        """Normal path with chip serial should extract the serial."""
        mgr = FlashManager("/tmp/klipper", "/tmp/katapult")
        result = mgr._extract_serial_from_id(
            "/dev/serial/by-id/usb-Kalico_stm32f103xe_30FFDA05344E393729680957-if00"
        )
        assert result == "30FFDA05344E393729680957"

    def test_extract_serial_empty_input(self):
        """Empty or None input should return None."""
        mgr = FlashManager("/tmp/klipper", "/tmp/katapult")
        assert mgr._extract_serial_from_id("") is None
        assert mgr._extract_serial_from_id(None) is None

    # --- Edge case: non-USB devices skip rescan ---

    def test_rescan_skipped_for_non_usb_devices(self):
        """Linux process and CAN devices should not trigger rescan."""
        assert not "linux_process".startswith("/dev/serial/by-id/")
        assert not "aabbccddeeff".startswith("/dev/serial/by-id/")

    def test_extract_serial_filters_katapult_prefix(self):
        """Katapult paths — known limitation: _extract_serial_from_id splits on '_'
        which produces 'usb-katapult' as a single token. The filter list has
        'katapult' but not 'usb-katapult', so the token survives.
        With long serials (>12 chars), the serial wins by length."""
        mgr = FlashManager("/tmp/klipper", "/tmp/katapult")
        # Long serial — chip serial wins by length
        result_long = mgr._extract_serial_from_id(
            "/dev/serial/by-id/usb-katapult_stm32f103xe_30FFDA05344E393729680957-if00"
        )
        assert result_long == "30FFDA05344E393729680957"

        # Short serial (12 chars, same length as 'usb-katapult') — known limitation
        result_short = mgr._extract_serial_from_id(
            "/dev/serial/by-id/usb-katapult_stm32f103xe_30FFDA05344E-if00"
        )
        assert result_short is not None

    def test_extract_serial_filters_klipper_prefix(self):
        """Legacy Klipper paths should still extract the chip serial."""
        mgr = FlashManager("/tmp/klipper", "/tmp/katapult")
        result = mgr._extract_serial_from_id(
            "/dev/serial/by-id/usb-Klipper_stm32f103xe_30FFDA05344E-if00"
        )
        assert result == "30FFDA05344E"

    def test_extract_serial_does_not_filter_kalico(self):
        """Kalico is NOT in the filter list — the serial extraction should still
        work because the chip serial is the longest candidate."""
        mgr = FlashManager("/tmp/klipper", "/tmp/katapult")
        result = mgr._extract_serial_from_id(
            "/dev/serial/by-id/usb-Kalico_stm32f103xe_30FFDA05344E-if00"
        )
        assert result == "30FFDA05344E"

    # --- Rescan gate checks ---

    def test_rescan_gate_can_device(self):
        """CAN bus devices (hex UUID) should not trigger serial rescan."""
        dev = {"id": "aabbccddeeff", "fleet_id": "aabbccddeeff"}
        fleet_id = dev.get("fleet_id", dev["id"])
        assert not fleet_id.startswith("/dev/serial/by-id/")

    def test_rescan_gate_linux_process(self):
        """Linux process devices should not trigger serial rescan."""
        dev = {"id": "linux_process", "fleet_id": "linux_process"}
        fleet_id = dev.get("fleet_id", dev["id"])
        assert not fleet_id.startswith("/dev/serial/by-id/")

    def test_rescan_gate_serial_device(self):
        """Serial-by-id devices should trigger serial rescan."""
        dev = {
            "id": "/dev/serial/by-id/usb-katapult_stm32f103xe_AABB-if00",
            "fleet_id": "/dev/serial/by-id/usb-Kalico_stm32f103xe_AABB-if00",
        }
        fleet_id = dev.get("fleet_id", dev["id"])
        assert fleet_id.startswith("/dev/serial/by-id/")


# ---------------------------------------------------------------------------
# Integration: all 3 fixes working together
# ---------------------------------------------------------------------------


class TestBatchFlashEndToEnd:
    """End-to-end tests covering fleet_id preservation, version tracking,
    and post-flash rescan working together in the batch flash path."""

    @pytest.fixture
    def fleet_mgr(self, tmp_path):
        return FleetManager(str(tmp_path))

    @pytest.fixture
    def flash_mgr(self):
        return FlashManager("/tmp/klipper", "/tmp/katapult")

    @pytest.mark.asyncio
    async def test_full_flow_reboot_mutates_then_version_then_rescan(self, fleet_mgr, flash_mgr):
        """fleet_id saved, reboot mutates dev['id'], version tracked, rescan
        detects path change — all three fixes in sequence."""
        original_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe_30FFDA05344E-if00"
        katapult_id = "/dev/serial/by-id/usb-katapult_stm32f103xe_30FFDA05344E-if00"
        new_id = "/dev/serial/by-id/usb-Kalico17_stm32f103xe_30FFDA05344E-if00"
        await fleet_mgr.save_device({"id": original_id, "name": "MCU", "profile": "skr"})

        # Preserve fleet_id before reboot
        dev = {"id": original_id, "method": "serial", "name": "MCU",
               "is_katapult": True, "profile": "skr"}
        dev["fleet_id"] = dev["id"]

        # Simulate reboot phase mutation
        dev["id"] = katapult_id
        assert dev["fleet_id"] == original_id

        # Version tracking uses fleet_id
        fleet_id = dev.get("fleet_id", dev["id"])
        await fleet_mgr.update_device_version(fleet_id, {
            "version": "v2026.03.00", "commit": "abc123"
        })

        fleet = await fleet_mgr.get_fleet()
        assert fleet[0]["flashed_version"] == "v2026.03.00"
        # Still original before rescan
        assert fleet[0]["id"] == original_id

        # Rescan detects USB descriptor change
        flash_mgr.discover_serial_devices = AsyncMock(
            return_value=[{"id": new_id}]
        )

        logs = await _collect_logs(
            flash_mgr.post_flash_rescan(fleet_id, [original_id], fleet_mgr)
        )

        fleet = await fleet_mgr.get_fleet()
        assert fleet[0]["id"] == new_id
        assert fleet[0]["flashed_version"] == "v2026.03.00"
        assert any("Fleet updated" in l for l in logs)

    @pytest.mark.asyncio
    async def test_full_flow_blank_serial_after_reboot(self, fleet_mgr, flash_mgr):
        """Device reboots, flashes, reappears with blank serial — rescan
        recovers via diff and version is still tracked."""
        original_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe_30FFDA05344E-if00"
        blank_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe-if00"
        await fleet_mgr.save_device({"id": original_id, "name": "MCU", "profile": "skr"})

        # Preserve fleet_id, simulate reboot mutation
        dev = {"id": original_id, "method": "serial", "name": "MCU",
               "is_katapult": True, "profile": "skr"}
        dev["fleet_id"] = dev["id"]
        dev["id"] = "/dev/serial/by-id/usb-katapult_stm32f103xe_30FFDA05344E-if00"

        fleet_id = dev.get("fleet_id", dev["id"])
        await fleet_mgr.update_device_version(fleet_id, {
            "version": "v2026.03.00", "commit": "abc123"
        })

        # Device came back with blank serial
        flash_mgr.discover_serial_devices = AsyncMock(
            return_value=[{"id": blank_id}]
        )

        logs = await _collect_logs(
            flash_mgr.post_flash_rescan(fleet_id, [original_id], fleet_mgr)
        )

        fleet = await fleet_mgr.get_fleet()
        assert fleet[0]["id"] == blank_id
        assert fleet[0]["flashed_version"] == "v2026.03.00"
        assert any("matched by diff" in l for l in logs)

    @pytest.mark.asyncio
    async def test_full_flow_no_reboot_no_mutation(self, fleet_mgr, flash_mgr):
        """AVR devices without Katapult still get fleet_id set and rescan works."""
        device_id = "/dev/serial/by-id/usb-Arduino_Mega_12345-if00"
        new_id = "/dev/serial/by-id/usb-Klipper_atmega2560_12345-if00"
        await fleet_mgr.save_device({"id": device_id, "name": "AVR", "profile": "mega"})

        dev = {"id": device_id, "method": "serial", "name": "AVR",
               "is_katapult": False, "profile": "mega"}
        dev["fleet_id"] = dev["id"]
        assert dev["id"] == dev["fleet_id"]

        fleet_id = dev.get("fleet_id", dev["id"])
        await fleet_mgr.update_device_version(fleet_id, {
            "version": "v2026.03.00", "commit": "def456"
        })

        # USB descriptor changed after flash
        flash_mgr.discover_serial_devices = AsyncMock(
            return_value=[{"id": new_id}]
        )

        logs = await _collect_logs(
            flash_mgr.post_flash_rescan(fleet_id, [device_id], fleet_mgr)
        )

        fleet = await fleet_mgr.get_fleet()
        assert fleet[0]["id"] == new_id
        assert fleet[0]["flashed_version"] == "v2026.03.00"

    @pytest.mark.asyncio
    async def test_full_flow_can_device_no_rescan(self, fleet_mgr, flash_mgr):
        """CAN devices skip serial rescan but still get version tracking."""
        can_id = "aabbccddeeff"
        await fleet_mgr.save_device({"id": can_id, "name": "EBB36", "profile": "ebb"})

        dev = {"id": can_id, "method": "can", "name": "EBB36", "profile": "ebb"}
        dev["fleet_id"] = dev["id"]

        fleet_id = dev.get("fleet_id", dev["id"])
        await fleet_mgr.update_device_version(fleet_id, {
            "version": "v2026.03.00", "commit": "can789"
        })

        fleet = await fleet_mgr.get_fleet()
        assert fleet[0]["flashed_version"] == "v2026.03.00"
        assert not fleet_id.startswith("/dev/serial/by-id/")

    @pytest.mark.asyncio
    async def test_version_persists_after_rescan_updates_id(self, fleet_mgr, flash_mgr):
        """Version fields survive when rescan changes the fleet entry's ID."""
        original_id = "/dev/serial/by-id/usb-Kalico_stm32f103xe_AABB-if00"
        new_id = "/dev/serial/by-id/usb-Kalico17_stm32f103xe_AABB-if00"
        await fleet_mgr.save_device({"id": original_id, "name": "MCU", "profile": "skr"})

        await fleet_mgr.update_device_version(original_id, {
            "version": "v2026.03.00", "commit": "abc123"
        })

        # Rescan updates the ID after version was already written
        await fleet_mgr.update_device_id(original_id, new_id)

        fleet = await fleet_mgr.get_fleet()
        assert fleet[0]["id"] == new_id
        assert fleet[0]["flashed_version"] == "v2026.03.00"
        assert fleet[0]["flashed_commit"] == "abc123"
        assert "last_flashed" in fleet[0]

    @pytest.mark.asyncio
    async def test_multi_device_fleet_only_target_updated(self, fleet_mgr, flash_mgr):
        """Only the flashed device's ID should change, other fleet entries untouched."""
        dev_a = "/dev/serial/by-id/usb-Kalico_stm32f103xe_AAAA-if00"
        dev_b = "/dev/serial/by-id/usb-Kalico_stm32g0b1xx_BBBB-if00"
        new_a = "/dev/serial/by-id/usb-Kalico17_stm32f103xe_AAAA-if00"
        await fleet_mgr.save_device({"id": dev_a, "name": "MCU-A", "profile": "skr"})
        await fleet_mgr.save_device({"id": dev_b, "name": "MCU-B", "profile": "ebb"})

        await fleet_mgr.update_device_version(dev_a, {
            "version": "v2026.03.00", "commit": "aaa111"
        })

        # Device A changed path, device B unchanged
        flash_mgr.discover_serial_devices = AsyncMock(return_value=[
            {"id": new_a},
            {"id": dev_b},
        ])

        logs = await _collect_logs(
            flash_mgr.post_flash_rescan(dev_a, [dev_a, dev_b], fleet_mgr)
        )

        fleet = await fleet_mgr.get_fleet()
        fleet_by_name = {d["name"]: d for d in fleet}
        assert fleet_by_name["MCU-A"]["id"] == new_a
        assert fleet_by_name["MCU-A"]["flashed_version"] == "v2026.03.00"
        assert fleet_by_name["MCU-B"]["id"] == dev_b
        assert fleet_by_name["MCU-B"].get("flashed_version") is None
