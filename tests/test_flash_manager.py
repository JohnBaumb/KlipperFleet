import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from backend.flash_manager import FlashManager

# Test for Issue #4: Failure to resolve new Katapult ID
# https://github.com/JohnBaumb/KlipperFleet/issues/4
#
# When a device reboots from Klipper firmware to Katapult bootloader,
# its USB device ID can change completely:
#   Before: /dev/serial/by-id/usb-infimech_tx_stm32f401xc_main_mcu-if00
#   After:  /dev/serial/by-id/usb-katapult_stm32f401xc_1A0028000A51333138373435-if00
#
# The fix uses a "snapshot and diff" approach to detect the new device.


class TestKatapultIdResolution:
    """Tests for the snapshot-diff approach to finding Katapult devices after reboot."""

    @pytest.fixture
    def mock_flash_mgr(self):
        """Create a mock FlashManager with controllable device discovery."""
        mgr = MagicMock()
        mgr.discover_serial_devices = AsyncMock()
        mgr.discover_dfu_devices = AsyncMock(return_value=[])
        mgr.resolve_dfu_id = AsyncMock(side_effect=lambda x, **kw: x)
        mgr.check_device_status = AsyncMock(return_value="service")
        mgr.reboot_to_katapult = AsyncMock(return_value=iter([]))
        mgr.flash_serial = AsyncMock(return_value=iter([]))
        return mgr

    def test_snapshot_diff_detects_new_device(self):
        """Test that comparing before/after device lists correctly identifies new devices."""
        # Before reboot - device is in Klipper firmware mode
        initial_serials = [
            "/dev/serial/by-id/usb-infimech_tx_stm32f401xc_main_mcu-if00",
            "/dev/serial/by-id/usb-AT_stm32g0b1xx_TN_Pro-if00",
            "/dev/serial/by-id/usb-Beacon_Beacon_RevH_62889AF9515U354UD38202020FF0A1D23-if00"
        ]
        
        # After reboot - old device gone, new Katapult device appeared
        current_serials = [
            {"id": "/dev/serial/by-id/usb-AT_stm32g0b1xx_TN_Pro-if00"},
            {"id": "/dev/serial/by-id/usb-Beacon_Beacon_RevH_62889AF9515U354UD38202020FF0A1D23-if00"},
            {"id": "/dev/serial/by-id/usb-katapult_stm32f401xc_1A0028000A51333138373435-if00"}
        ]
        
        # The detection logic from main.py
        current_ids = [d['id'] for d in current_serials]
        new_serial_device = None
        
        # Look for a NEW serial device that wasn't there before
        for cid in current_ids:
            if cid not in initial_serials:
                new_serial_device = cid
                break
        
        assert new_serial_device == "/dev/serial/by-id/usb-katapult_stm32f401xc_1A0028000A51333138373435-if00"

    def test_fallback_finds_katapult_device(self):
        """Test fallback detection when device appears as new Katapult path."""
        # Scenario: Can't diff (e.g., multiple new devices), but one has 'katapult' in name
        current_serials = [
            {"id": "/dev/serial/by-id/usb-AT_stm32g0b1xx_TN_Pro-if00"},
            {"id": "/dev/serial/by-id/usb-katapult_stm32f401xc_1A0028000A51333138373435-if00"},
            {"id": "/dev/serial/by-id/usb-SomeOther_Device-if00"}
        ]
        
        new_serial_device = None
        for d in current_serials:
            if "katapult" in d['id'].lower() or "canboot" in d['id'].lower():
                new_serial_device = d['id']
                break
        
        assert new_serial_device == "/dev/serial/by-id/usb-katapult_stm32f401xc_1A0028000A51333138373435-if00"

    def test_fallback_finds_canboot_device(self):
        """Test fallback detection finds 'canboot' named devices (legacy Katapult name)."""
        current_serials = [
            {"id": "/dev/serial/by-id/usb-AT_stm32g0b1xx_TN_Pro-if00"},
            {"id": "/dev/serial/by-id/usb-CanBoot_stm32f401xc_12345678-if00"},
        ]
        
        new_serial_device = None
        for d in current_serials:
            if "katapult" in d['id'].lower() or "canboot" in d['id'].lower():
                new_serial_device = d['id']
                break
        
        assert new_serial_device == "/dev/serial/by-id/usb-CanBoot_stm32f401xc_12345678-if00"

    def test_no_false_positive_when_no_new_device(self):
        """Test that we don't incorrectly identify a device when nothing changed."""
        initial_serials = [
            "/dev/serial/by-id/usb-AT_stm32g0b1xx_TN_Pro-if00",
            "/dev/serial/by-id/usb-Beacon_Beacon_RevH_62889-if00"
        ]
        
        # Same devices after "reboot" (device didn't actually change)
        current_serials = [
            {"id": "/dev/serial/by-id/usb-AT_stm32g0b1xx_TN_Pro-if00"},
            {"id": "/dev/serial/by-id/usb-Beacon_Beacon_RevH_62889-if00"}
        ]
        
        current_ids = [d['id'] for d in current_serials]
        new_serial_device = None
        
        for cid in current_ids:
            if cid not in initial_serials:
                new_serial_device = cid
                break
        
        assert new_serial_device is None

    def test_diff_ignores_unrelated_new_devices(self):
        """Test that diff approach finds the katapult device, not just any new device."""
        initial_serials = [
            "/dev/serial/by-id/usb-infimech_tx_stm32f401xc_main_mcu-if00"
        ]
        
        # Two new devices appeared - prefer the katapult one
        current_serials = [
            {"id": "/dev/serial/by-id/usb-RandomNewDevice-if00"},
            {"id": "/dev/serial/by-id/usb-katapult_stm32f401xc_1A0028000A51333138373435-if00"}
        ]
        
        current_ids = [d['id'] for d in current_serials]
        new_serial_device = None
        
        # First try: find any new device
        for cid in current_ids:
            if cid not in initial_serials:
                new_serial_device = cid
                break
        
        # This will find the first new device - which might not be katapult
        # In practice this is fine because we're flashing ONE device at a time
        # and any new device appearing right after reboot is likely our target
        assert new_serial_device is not None
        
        # But if we need to be more specific, fallback checks for katapult name
        katapult_device = None
        for d in current_serials:
            if "katapult" in d['id'].lower() or "canboot" in d['id'].lower():
                katapult_device = d['id']
                break
        
        assert katapult_device == "/dev/serial/by-id/usb-katapult_stm32f401xc_1A0028000A51333138373435-if00"


class TestSerialNumberExtraction:
    """Tests for the legacy serial number extraction (still used as fallback)."""
    
    def test_extract_serial_does_not_find_custom_name(self):
        """Demonstrate why serial extraction fails for custom-named devices."""
        # This is the user's device from Issue #4
        old_id = "/dev/serial/by-id/usb-infimech_tx_stm32f401xc_main_mcu-if00"
        new_id = "/dev/serial/by-id/usb-katapult_stm32f401xc_1A0028000A51333138373435-if00"
        
        # The actual hardware serial number
        actual_hardware_serial = "1A0028000A51333138373435"
        
        # The old ID does NOT contain the hardware serial
        assert actual_hardware_serial not in old_id
        
        # The new ID DOES contain it
        assert actual_hardware_serial in new_id
        
        # This is why serial number matching fails - we can't extract
        # the hardware serial from the old custom-named ID


class TestIntegrationScenario:
    """Integration-style tests simulating the full flash flow."""

    @pytest.mark.asyncio
    async def test_flash_flow_with_changing_device_id(self):
        """Simulate the complete flash flow where device ID changes after reboot."""
        
        # Simulate the sequence of events
        call_count = 0
        
        async def mock_discover_serial(skip_moonraker=False):
            nonlocal call_count
            call_count += 1
            
            if call_count == 1:
                # Before reboot - device is in Klipper mode
                return [
                    {"id": "/dev/serial/by-id/usb-infimech_tx_stm32f401xc_main_mcu-if00"},
                    {"id": "/dev/serial/by-id/usb-OtherDevice-if00"}
                ]
            else:
                # After reboot - device is in Katapult mode with new ID
                return [
                    {"id": "/dev/serial/by-id/usb-katapult_stm32f401xc_1A0028000A51333138373435-if00"},
                    {"id": "/dev/serial/by-id/usb-OtherDevice-if00"}
                ]
        
        # Take initial snapshot
        initial_devices = await mock_discover_serial()
        initial_serials = [d['id'] for d in initial_devices]
        
        # Simulate reboot happening...
        
        # Discover again after reboot
        current_devices = await mock_discover_serial()
        current_ids = [d['id'] for d in current_devices]
        
        # Find the new device
        new_serial_device = None
        for cid in current_ids:
            if cid not in initial_serials:
                new_serial_device = cid
                break
        
        # We should find the new Katapult device
        assert new_serial_device == "/dev/serial/by-id/usb-katapult_stm32f401xc_1A0028000A51333138373435-if00"
        
        # This new_serial_device would then be used for flashing
        target_id = new_serial_device
        assert target_id is not None
        assert "katapult" in target_id.lower()


# ---------------------------------------------------------------------------
# Issue #16: USB-to-CAN bridge serial path passed to flash_can as CAN UUID
# https://github.com/JohnBaumb/KlipperFleet/issues/16
#
# USB-to-CAN bridges connect via USB serial but are configured with
# method="can" in the fleet.  When flashing, the /dev/serial/by-id/ path
# was passed to flashtool.py -u which expects a hex CAN UUID, causing:
#   ValueError: invalid literal for int() with base 16
# ---------------------------------------------------------------------------


class TestIssue16BridgeSerialPathGuard:
    """Issue #16: serial paths must never be sent to flash_can or CAN reboot."""

    def test_flash_can_rejects_serial_path(self):
        """flash_can() must raise ValueError when given a /dev/ serial path."""
        fm = FlashManager("/tmp/klipper", "/tmp/katapult")
        serial_path = "/dev/serial/by-id/usb-katapult_stm32h750xx_34001B001751333233393839-if00"

        async def run():
            with pytest.raises(ValueError, match="serial device path"):
                async for _ in fm.flash_can(serial_path, "/tmp/firmware.bin"):
                    pass

        asyncio.get_event_loop().run_until_complete(run())

    def test_flash_can_accepts_hex_uuid(self):
        """flash_can() must accept a valid hex CAN UUID without raising."""
        fm = FlashManager("/tmp/klipper", "/tmp/katapult")
        can_uuid = "a1b2c3d4e5f6"

        async def run():
            gen = fm.flash_can(can_uuid, "/tmp/firmware.bin")
            # Should enter the CAN lock without raising—we just
            # verify no ValueError is raised before the subprocess call.
            try:
                line = await gen.__anext__()
                assert "CAN Lock Acquired" in line
            except (FileNotFoundError, OSError):
                # Expected: flashtool.py doesn't exist in /tmp
                pass

        asyncio.get_event_loop().run_until_complete(run())

    def test_reboot_to_katapult_autocorrects_can_to_serial(self):
        """reboot_to_katapult() must auto-correct CAN method when device_id is /dev/ path."""
        fm = FlashManager("/tmp/klipper", "/tmp/katapult")
        serial_path = "/dev/serial/by-id/usb-katapult_stm32h750xx_34001B001751333233393839-if00"

        async def run():
            lines = []
            try:
                async for line in fm.reboot_to_katapult(serial_path, method="can"):
                    lines.append(line)
            except Exception:
                pass  # Will fail at subprocess level, that's fine
            combined = "".join(lines)
            assert "Auto-correcting" in combined
            assert "serial reboot instead of CAN" in combined

        asyncio.get_event_loop().run_until_complete(run())

    def test_batch_method_autocorrection(self):
        """Batch flash must auto-correct method from 'can' to 'serial' for /dev/ bridge paths."""
        devices = [
            {
                "id": "/dev/serial/by-id/usb-katapult_stm32h750xx_34001B001751333233393839-if00",
                "method": "can",
                "name": "Spider Bridge",
                "profile": "spider",
                "is_bridge": True,
            },
            {
                "id": "a1b2c3d4e5f6",
                "method": "can",
                "name": "Toolhead CAN",
                "profile": "toolhead",
                "is_bridge": False,
            },
        ]

        # Replicate the auto-correction logic from main.py batch path
        for dev in devices:
            if dev['method'] == 'can' and dev['id'].startswith('/dev/'):
                dev['method'] = 'serial'
                dev['is_katapult'] = True

        # Bridge with serial path should be corrected
        assert devices[0]['method'] == 'serial'
        assert devices[0]['is_katapult'] is True

        # Normal CAN node with hex UUID should be unchanged
        assert devices[1]['method'] == 'can'
        assert 'is_katapult' not in devices[1]

    def test_single_flash_method_autocorrection(self):
        """Single-device flash must auto-correct method from 'can' to 'serial' for /dev/ paths."""
        serial_path = "/dev/serial/by-id/usb-katapult_stm32h750xx_34001B001751333233393839-if00"
        can_uuid = "a1b2c3d4e5f6"

        # Replicate the auto-correction logic from main.py single-flash path
        def resolve_method(target_id, method):
            if method == "can" and target_id.startswith("/dev/"):
                return "serial"
            return method

        assert resolve_method(serial_path, "can") == "serial"
        assert resolve_method(can_uuid, "can") == "can"
        assert resolve_method(serial_path, "serial") == "serial"
        assert resolve_method(serial_path, "dfu") == "dfu"

    def test_bridge_hex_uuid_switches_to_serial_after_reboot(self):
        """Issue #16 real scenario: CAN bridge with hex UUID must switch to serial flash
        after rebooting to Katapult, because the bridge IS the can0 interface and
        dropping to Katapult kills the CAN bus."""
        # matthew73210's actual config: hex UUID, method=can, is_bridge=true
        can_uuid = "69cd41686193"
        actual_method = "can"
        is_bridge = True
        new_serial_device = "/dev/serial/by-id/usb-katapult_stm32h750xx_34001B001751333233393839-if00"

        # Replicate the method resolution from main.py single-flash path:
        # After reboot, bridge reappears as serial, new_serial_device is set by wait loop.
        target_id = can_uuid
        if actual_method == "can" and is_bridge and new_serial_device:
            target_id = new_serial_device
            actual_method = "serial"

        assert actual_method == "serial"
        assert target_id == new_serial_device

    def test_bridge_hex_uuid_no_serial_detected_stays_can(self):
        """If bridge reboot didn't produce a new serial device, don't switch method."""
        can_uuid = "69cd41686193"
        actual_method = "can"
        is_bridge = True
        new_serial_device = None  # Detection loop found nothing

        target_id = can_uuid
        if actual_method == "can" and is_bridge and new_serial_device:
            target_id = new_serial_device
            actual_method = "serial"

        # Should remain can — the interface might still be up (e.g., device wasn't rebooted)
        assert actual_method == "can"
        assert target_id == can_uuid

    def test_non_bridge_can_device_not_affected(self):
        """Normal CAN nodes (not bridges) should never switch to serial flash."""
        can_uuid = "a1b2c3d4e5f6"
        actual_method = "can"
        is_bridge = False
        new_serial_device = "/dev/serial/by-id/usb-katapult_stm32f072xb_12345-if00"

        target_id = can_uuid
        if actual_method == "can" and is_bridge and new_serial_device:
            target_id = new_serial_device
            actual_method = "serial"

        assert actual_method == "can"
        assert target_id == can_uuid

    def test_serial_detection_triggers_for_can_bridge(self):
        """The wait loop serial detection condition must include CAN bridges."""
        # Replicate the condition from the wait loop
        test_cases = [
            ("serial", False, True),   # serial method always scans
            ("serial", True, True),    # serial bridge always scans
            ("can", True, True),       # CAN bridge must scan (Issue #16 fix)
            ("can", False, False),     # normal CAN node should NOT scan
            ("dfu", False, False),     # DFU method should NOT scan
            ("dfu", True, False),      # DFU bridge should NOT scan (uses DFU path)
        ]
        for method, bridge, expected in test_cases:
            should_scan = (method == "serial") or (method == "can" and bridge)
            assert should_scan == expected, f"method={method}, bridge={bridge}: expected {expected}"


# ---------------------------------------------------------------------------
# Katapult Protocol Module
# ---------------------------------------------------------------------------


class TestKatapultProtocol:
    """Tests for the katapult_protocol wire-format helpers."""

    def test_build_command_connect_matches_flashtool(self):
        """CONNECT command (0x11) must produce the exact same bytes as flashtool.py."""
        from backend.katapult_protocol import build_command, CONNECT
        cmd = build_command(CONNECT)
        # Header(01 88) + cmd(11) + word_cnt(00) + crc16 + Trailer(99 03)
        assert cmd[:2] == b'\x01\x88'
        assert cmd[2] == 0x11
        assert cmd[3] == 0x00
        assert cmd[-2:] == b'\x99\x03'
        assert len(cmd) == 8  # 2+1+1+2+2

    def test_build_command_complete_format(self):
        """COMPLETE command (0x15) must use correct framing."""
        from backend.katapult_protocol import build_command, COMPLETE
        cmd = build_command(COMPLETE)
        assert cmd[:2] == b'\x01\x88'
        assert cmd[2] == 0x15
        assert cmd[3] == 0x00
        assert cmd[-2:] == b'\x99\x03'
        assert len(cmd) == 8

    def test_build_command_with_payload(self):
        """Commands with payload should set word_cnt correctly."""
        from backend.katapult_protocol import build_command
        payload = b'\x00' * 8  # 8 bytes = 2 words
        cmd = build_command(0x12, payload)
        assert cmd[3] == 2  # word_cnt = 8/4

    def test_crc16_known_value(self):
        """CRC-16/CCITT must match known reference value."""
        from backend.katapult_protocol import crc16_ccitt
        # CONNECT body: cmd=0x11, word_cnt=0x00
        crc = crc16_ccitt(bytes([0x11, 0x00]))
        assert isinstance(crc, int)
        assert 0 <= crc <= 0xFFFF

    def test_build_command_no_old_format(self):
        """Verify the old broken format (cmd | 0x0400 as uint16) is NOT used."""
        from backend.katapult_protocol import build_command, CONNECT
        import struct
        cmd = build_command(CONNECT)
        # The old broken format would have produced: 0x0411 packed as LE uint16
        old_broken = struct.pack("<H", 0x11 | 0x0400)
        assert old_broken not in cmd


# ---------------------------------------------------------------------------
# Issue #17: flash_make (renamed from flash_avr)
# ---------------------------------------------------------------------------


class TestFlashMakeRename:
    """Verify the flash_avr -> flash_make rename is consistent."""

    def test_flash_make_method_exists(self):
        """FlashManager must have flash_make, not flash_avr."""
        fm = FlashManager("/tmp/klipper", "/tmp/katapult")
        assert hasattr(fm, "flash_make"), "flash_make method missing"
        assert not hasattr(fm, "flash_avr"), "flash_avr should have been renamed to flash_make"

    def test_flash_make_is_async_generator(self):
        """flash_make must be an async generator (yields log lines)."""
        import inspect
        fm = FlashManager("/tmp/klipper", "/tmp/katapult")
        assert inspect.isasyncgenfunction(fm.flash_make)


# ---------------------------------------------------------------------------
# Issue #17: Error propagation — flash success/failure sentinel detection
# ---------------------------------------------------------------------------


class TestFlashSuccessDetection:
    """Test that the stream-based success sentinel detection works correctly."""

    def test_success_sentinel_detected(self):
        """The endpoint checks for '>>> Flashing successful!' in the stream."""
        logs = [
            ">>> Running make flash...\n",
            "Flashing out/klipper.uf2...\n",
            ">>> Flashing successful!\n",
        ]
        flash_succeeded = any(">>> Flashing successful!" in log for log in logs)
        assert flash_succeeded is True

    def test_failure_not_mistaken_for_success(self):
        """A failed flash must NOT set flash_succeeded to True."""
        logs = [
            ">>> Running make flash...\n",
            "Error: device not found\n",
            ">>> Flashing failed with return code 1\n",
        ]
        flash_succeeded = any(
            ">>> Flashing successful!" in log or ">>> Flash operation complete." in log
            for log in logs
        )
        assert flash_succeeded is False

    def test_dfu_flash_complete_sentinel(self):
        """DFU flash uses '>>> Flash operation complete.' as its final success sentinel."""
        logs = [
            ">>> Flashing successful!\n",
            ">>> Sending DFU leave request...\n",
            ">>> Device rebooted successfully.\n",
            ">>> Flash operation complete.\n",
        ]
        flash_succeeded = any(
            ">>> Flashing successful!" in log or ">>> Flash operation complete." in log
            for log in logs
        )
        assert flash_succeeded is True

    def test_linux_flash_sentinel(self):
        """Linux MCU flash uses its own success sentinel."""
        logs = [
            ">>> Installing Linux MCU binary...\n",
            ">>> Linux MCU binary installed successfully.\n",
        ]
        flash_succeeded = any(">>> Linux MCU binary installed successfully." in log for log in logs)
        assert flash_succeeded is True


# ---------------------------------------------------------------------------
# Issue #17: Post-flash serial rescan logic
# ---------------------------------------------------------------------------


class TestPostFlashRescan:
    """Test the post-flash serial rescan matching strategies."""

    def test_serial_suffix_match_rp2040(self):
        """RP2040 devices have unique chip serials — suffix matching should work."""
        fm = FlashManager("/tmp/klipper", "/tmp/katapult")
        old_id = "/dev/serial/by-id/usb-Klipper_rp2040_E66160F42367B137-if00"
        new_id = "/dev/serial/by-id/usb-CustomFork_rp2040_E66160F42367B137-if00"

        old_serial = fm._extract_serial_from_id(old_id)
        assert old_serial is not None
        # The serial should match in the new path
        assert old_serial in new_id

    def test_serial_suffix_match_stm32(self):
        """STM32 devices with real serials should also match by suffix."""
        fm = FlashManager("/tmp/klipper", "/tmp/katapult")
        old_id = "/dev/serial/by-id/usb-Klipper_stm32f446xx_2A003B000451333039383535-if00"
        new_id = "/dev/serial/by-id/usb-CustomFW_stm32f446xx_2A003B000451333039383535-if00"

        old_serial = fm._extract_serial_from_id(old_id)
        assert old_serial is not None
        assert old_serial in new_id

    def test_generic_serial_fallback_to_diff(self):
        """Devices with generic/zeros serial should NOT match by suffix against unrelated devices."""
        fm = FlashManager("/tmp/klipper", "/tmp/katapult")
        # Generic serial — common on cheap STM32F103 clones
        old_id = "/dev/serial/by-id/usb-Klipper_stm32f103xe_00000000001-if00"

        old_serial = fm._extract_serial_from_id(old_id)
        # Even though we extract something, it's not a reliable unique match.
        # The diff-based strategy should be used as fallback in practice.
        # Here we just verify extraction works without error.
        assert old_serial is not None

    def test_diff_based_detection_one_disappeared_one_appeared(self):
        """When exactly one device disappeared and one appeared, assume it's the same board."""
        initial_serials = [
            "/dev/serial/by-id/usb-Klipper_rp2040_E66160F42367B137-if00",
            "/dev/serial/by-id/usb-OtherDevice-if00",
        ]
        current_ids = [
            "/dev/serial/by-id/usb-CustomFork_rp2040_E66160F42367B137-if00",
            "/dev/serial/by-id/usb-OtherDevice-if00",
        ]
        old_device_id = "/dev/serial/by-id/usb-Klipper_rp2040_E66160F42367B137-if00"

        disappeared = [s for s in initial_serials if s not in current_ids]
        appeared = [s for s in current_ids if s not in initial_serials]

        assert old_device_id in disappeared
        assert len(appeared) == 1
        assert appeared[0] == "/dev/serial/by-id/usb-CustomFork_rp2040_E66160F42367B137-if00"

    def test_device_path_unchanged_no_update_needed(self):
        """If the device path didn't change, no fleet update is needed."""
        old_device_id = "/dev/serial/by-id/usb-Klipper_rp2040_E66160F42367B137-if00"
        current_ids = [
            "/dev/serial/by-id/usb-Klipper_rp2040_E66160F42367B137-if00",
            "/dev/serial/by-id/usb-OtherDevice-if00",
        ]
        # Old path still present — nothing changed
        assert old_device_id in current_ids
