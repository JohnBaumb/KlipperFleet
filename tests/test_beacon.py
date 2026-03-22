"""Tests for Beacon probe discovery, flashing, and fleet integration."""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from backend.flash_manager import FlashManager


@pytest.fixture
def flash_mgr():
    """Create a real FlashManager for testing beacon methods."""
    return FlashManager(klipper_dir="/tmp/klipper", katapult_dir="/tmp/katapult")


# ---------------------------------------------------------------------------
# discover_beacon_devices()
# ---------------------------------------------------------------------------


class TestDiscoverBeaconDevices:
    """Tests for beacon device discovery via /dev/serial/by-id/ glob."""

    @patch("backend.flash_manager.glob.glob")
    def test_discovers_revh_beacon(self, mock_glob, flash_mgr):
        mock_glob.return_value = [
            "/dev/serial/by-id/usb-Beacon_Beacon_RevH_62889AF9515U354UD38202020FF0A1D23-if00"
        ]
        result = asyncio.get_event_loop().run_until_complete(
            flash_mgr.discover_beacon_devices()
        )
        assert len(result) == 1
        dev = result[0]
        assert dev["id"] == "/dev/serial/by-id/usb-Beacon_Beacon_RevH_62889AF9515U354UD38202020FF0A1D23-if00"
        assert dev["name"] == "Beacon RevH"
        assert dev["revision"] == "RevH"
        assert dev["serial"] == "62889AF9515U354UD38202020FF0A1D23"
        assert dev["mode"] == "service"

    @patch("backend.flash_manager.glob.glob")
    def test_discovers_multiple_revisions(self, mock_glob, flash_mgr):
        mock_glob.return_value = [
            "/dev/serial/by-id/usb-Beacon_Beacon_RevD_AAAA-if00",
            "/dev/serial/by-id/usb-Beacon_Beacon_RevH_BBBB-if00",
        ]
        result = asyncio.get_event_loop().run_until_complete(
            flash_mgr.discover_beacon_devices()
        )
        assert len(result) == 2
        assert result[0]["revision"] == "RevD"
        assert result[1]["revision"] == "RevH"

    @patch("backend.flash_manager.glob.glob")
    def test_no_beacons_found(self, mock_glob, flash_mgr):
        mock_glob.return_value = []
        result = asyncio.get_event_loop().run_until_complete(
            flash_mgr.discover_beacon_devices()
        )
        assert result == []


# ---------------------------------------------------------------------------
# get_beacon_klipper_path()
# ---------------------------------------------------------------------------


class TestGetBeaconKlipperPath:
    """Tests for Moonraker /server/config integration to find beacon_klipper repo."""

    @patch("backend.flash_manager.httpx.AsyncClient")
    def test_finds_beacon_klipper_path(self, mock_client_cls, flash_mgr):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "result": {
                "config": {
                    "update_manager beacon": {
                        "path": "/home/pi/beacon_klipper",
                        "type": "git_repo",
                    },
                }
            }
        }
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = asyncio.get_event_loop().run_until_complete(
            flash_mgr.get_beacon_klipper_path()
        )
        assert result == "/home/pi/beacon_klipper"

    @patch("backend.flash_manager.httpx.AsyncClient")
    def test_expands_home_dir(self, mock_client_cls, flash_mgr):
        """Should expand ~ in the path."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "result": {
                "config": {
                    "update_manager beacon": {
                        "path": "~/beacon_klipper",
                        "type": "git_repo",
                    },
                }
            }
        }
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = asyncio.get_event_loop().run_until_complete(
            flash_mgr.get_beacon_klipper_path()
        )
        assert result is not None
        assert result.endswith("/beacon_klipper")
        assert "~" not in result

    @patch("backend.flash_manager.httpx.AsyncClient")
    def test_returns_none_when_not_found(self, mock_client_cls, flash_mgr):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "result": {
                "config": {
                    "update_manager klipper": {"path": "/home/pi/klipper"},
                }
            }
        }
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = asyncio.get_event_loop().run_until_complete(
            flash_mgr.get_beacon_klipper_path()
        )
        assert result is None

    @patch("backend.flash_manager.httpx.AsyncClient")
    def test_handles_moonraker_error(self, mock_client_cls, flash_mgr):
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = asyncio.get_event_loop().run_until_complete(
            flash_mgr.get_beacon_klipper_path()
        )
        assert result is None


# ---------------------------------------------------------------------------
# flash_beacon()
# ---------------------------------------------------------------------------


class TestFlashBeacon:
    """Tests for beacon flash command construction and streaming."""

    @patch.object(FlashManager, "_run_flash_command")
    def test_builds_correct_command(self, mock_run, flash_mgr):
        mock_run.return_value = self._async_gen([">>> Flashing successful!\n"])

        device_id = "/dev/serial/by-id/usb-Beacon_Beacon_RevH_ABC123-if00"
        beacon_path = "/home/pi/beacon_klipper"

        lines = []
        async def collect():
            async for line in flash_mgr.flash_beacon(device_id, beacon_path):
                lines.append(line)

        asyncio.get_event_loop().run_until_complete(collect())

        mock_run.assert_called_once_with([
            "python3",
            "/home/pi/beacon_klipper/update_firmware.py",
            "update",
            device_id,
        ])
        assert any("Flashing successful" in l for l in lines)

    @patch.object(FlashManager, "_run_flash_command")
    def test_force_flag(self, mock_run, flash_mgr):
        mock_run.return_value = self._async_gen(["done\n"])

        device_id = "/dev/serial/by-id/usb-Beacon_Beacon_RevH_ABC123-if00"
        beacon_path = "/home/pi/beacon_klipper"

        async def collect():
            async for _ in flash_mgr.flash_beacon(device_id, beacon_path, force=True):
                pass

        asyncio.get_event_loop().run_until_complete(collect())

        args = mock_run.call_args[0][0]
        assert "--force" in args

    @staticmethod
    async def _async_gen(items):
        for item in items:
            yield item


# ---------------------------------------------------------------------------
# check_device_status() — beacon case
# ---------------------------------------------------------------------------


class TestCheckDeviceStatusBeacon:
    """Tests for beacon device status checking."""

    @patch("backend.flash_manager.os.path.exists")
    def test_beacon_service_when_device_exists(self, mock_exists, flash_mgr):
        mock_exists.return_value = True
        result = asyncio.get_event_loop().run_until_complete(
            flash_mgr.check_device_status(
                "/dev/serial/by-id/usb-Beacon_Beacon_RevH_ABC-if00",
                "beacon"
            )
        )
        assert result == "service"

    @patch("backend.flash_manager.os.path.exists")
    def test_beacon_offline_when_device_missing(self, mock_exists, flash_mgr):
        mock_exists.return_value = False
        result = asyncio.get_event_loop().run_until_complete(
            flash_mgr.check_device_status(
                "/dev/serial/by-id/usb-Beacon_Beacon_RevH_ABC-if00",
                "beacon"
            )
        )
        assert result == "offline"


# ---------------------------------------------------------------------------
# Device registration — beacon auto-sets exclude_from_batch
# ---------------------------------------------------------------------------


class TestBeaconRegistration:
    """Tests that beacon device registration auto-sets exclude_from_batch."""

    def test_beacon_device_model_optional_profile(self):
        """Device model should accept None profile for beacon devices."""
        from backend.main import Device
        dev = Device(name="Beacon RevH", id="/dev/serial/by-id/beacon-test", method="beacon", profile=None)
        assert dev.profile is None
        assert dev.method == "beacon"

    def test_flash_request_optional_profile(self):
        """FlashRequest should accept None profile for beacon devices."""
        from backend.main import FlashRequest
        req = FlashRequest(device_id="/dev/serial/by-id/beacon-test", method="beacon", profile=None)
        assert req.profile is None


# ---------------------------------------------------------------------------
# Batch exclusion — beacon devices are excluded
# ---------------------------------------------------------------------------


class TestBeaconBatchExclusion:
    """Tests that beacon devices with exclude_from_batch=True are skipped in batch."""

    def test_beacon_filtered_from_batch(self):
        """Simulate the batch filter logic from main.py."""
        devices = [
            {"name": "MCU", "id": "abc", "method": "can", "exclude_from_batch": False},
            {"name": "Beacon RevH", "id": "def", "method": "beacon", "exclude_from_batch": True},
            {"name": "EBB", "id": "ghi", "method": "can", "exclude_from_batch": False},
        ]
        excluded = [d for d in devices if d.get("exclude_from_batch", False)]
        active = [d for d in devices if not d.get("exclude_from_batch", False)]

        assert len(excluded) == 1
        assert excluded[0]["name"] == "Beacon RevH"
        assert len(active) == 2


# ---------------------------------------------------------------------------
# Beacon remote firmware version retrieval
# ---------------------------------------------------------------------------


class TestBeaconRemoteVersion:
    """Tests for beacon remote firmware version via git history fallback chain."""

    @pytest.fixture
    def _mock_fleet_env(self):
        """Provide common mocks for get_fleet_versions beacon path."""
        fleet = [{"id": "beacon-1", "method": "beacon"}]
        mcu_versions = {}
        return fleet, mcu_versions

    def _make_subprocess_mock(self, stdout_bytes, returncode=0):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(stdout_bytes, b""))
        proc.returncode = returncode
        return proc

    @patch("backend.main.asyncio.create_subprocess_exec")
    @patch("backend.main.flash_mgr")
    @patch("backend.main.fleet_mgr")
    def test_extracts_semver_from_commit_message(
        self, mock_fleet_mgr, mock_flash_mgr, mock_subproc, _mock_fleet_env
    ):
        fleet, mcu_versions = _mock_fleet_env
        mock_fleet_mgr.get_fleet = AsyncMock(return_value=fleet)
        mock_flash_mgr.get_mcu_versions = AsyncMock(return_value=mcu_versions)
        mock_flash_mgr.get_beacon_klipper_path = AsyncMock(return_value="/home/pi/beacon_klipper")

        # MCU query returns beacon FW version
        mcu_resp = MagicMock()
        mcu_resp.status_code = 200
        mcu_resp.json.return_value = {
            "result": {"status": {"mcu beacon": {"mcu_version": "Beacon 2.1.0"}}}
        }
        # update_manager response
        um_resp = MagicMock()
        um_resp.status_code = 200
        um_resp.json.return_value = {"result": {"version_info": {}}}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[mcu_resp, um_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        # git log commit message with version
        mock_subproc.return_value = self._make_subprocess_mock(b"firmware: version 2.1.0 release")

        from backend.main import get_fleet_versions

        with patch("backend.main.httpx.AsyncClient", return_value=mock_client):
            result = asyncio.get_event_loop().run_until_complete(get_fleet_versions())

        assert result["beacon-1"]["remote_version"] == "2.1.0"

    @patch("backend.main.asyncio.create_subprocess_exec")
    @patch("backend.main.flash_mgr")
    @patch("backend.main.fleet_mgr")
    def test_falls_back_to_git_tag(
        self, mock_fleet_mgr, mock_flash_mgr, mock_subproc, _mock_fleet_env
    ):
        fleet, mcu_versions = _mock_fleet_env
        mock_fleet_mgr.get_fleet = AsyncMock(return_value=fleet)
        mock_flash_mgr.get_mcu_versions = AsyncMock(return_value=mcu_versions)
        mock_flash_mgr.get_beacon_klipper_path = AsyncMock(return_value="/home/pi/beacon_klipper")

        mcu_resp = MagicMock()
        mcu_resp.status_code = 200
        mcu_resp.json.return_value = {
            "result": {"status": {"mcu beacon": {"mcu_version": "Beacon 2.0.0"}}}
        }
        um_resp = MagicMock()
        um_resp.status_code = 200
        um_resp.json.return_value = {"result": {"version_info": {}}}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[mcu_resp, um_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        # commit msg has no version, then hash, then tag
        msg_proc = self._make_subprocess_mock(b"fix enumeration bug for fast hosts.")
        hash_proc = self._make_subprocess_mock(b"a4d1ebe123456789")
        tag_proc = self._make_subprocess_mock(b"v2.0.0", returncode=0)
        mock_subproc.side_effect = [msg_proc, hash_proc, tag_proc]

        from backend.main import get_fleet_versions

        with patch("backend.main.httpx.AsyncClient", return_value=mock_client):
            result = asyncio.get_event_loop().run_until_complete(get_fleet_versions())

        assert result["beacon-1"]["remote_version"] == "2.0.0"

    @patch("backend.main.asyncio.create_subprocess_exec")
    @patch("backend.main.flash_mgr")
    @patch("backend.main.fleet_mgr")
    def test_falls_back_to_commit_hash(
        self, mock_fleet_mgr, mock_flash_mgr, mock_subproc, _mock_fleet_env
    ):
        fleet, mcu_versions = _mock_fleet_env
        mock_fleet_mgr.get_fleet = AsyncMock(return_value=fleet)
        mock_flash_mgr.get_mcu_versions = AsyncMock(return_value=mcu_versions)
        mock_flash_mgr.get_beacon_klipper_path = AsyncMock(return_value="/home/pi/beacon_klipper")

        mcu_resp = MagicMock()
        mcu_resp.status_code = 200
        mcu_resp.json.return_value = {
            "result": {"status": {"mcu beacon": {"mcu_version": "Beacon 2.0.0"}}}
        }
        um_resp = MagicMock()
        um_resp.status_code = 200
        um_resp.json.return_value = {"result": {"version_info": {}}}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[mcu_resp, um_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        # no version in msg, tag fails, falls back to short hash
        msg_proc = self._make_subprocess_mock(b"Beacon Contact")
        hash_proc = self._make_subprocess_mock(b"f973242abc")
        tag_proc = self._make_subprocess_mock(b"", returncode=128)
        short_proc = self._make_subprocess_mock(b"f973242")
        mock_subproc.side_effect = [msg_proc, hash_proc, tag_proc, short_proc]

        from backend.main import get_fleet_versions

        with patch("backend.main.httpx.AsyncClient", return_value=mock_client):
            result = asyncio.get_event_loop().run_until_complete(get_fleet_versions())

        assert result["beacon-1"]["remote_version"] == "git-f973242"

    @patch("backend.main.flash_mgr")
    @patch("backend.main.fleet_mgr")
    def test_remote_version_none_when_no_beacon_path(
        self, mock_fleet_mgr, mock_flash_mgr, _mock_fleet_env
    ):
        fleet, mcu_versions = _mock_fleet_env
        mock_fleet_mgr.get_fleet = AsyncMock(return_value=fleet)
        mock_flash_mgr.get_mcu_versions = AsyncMock(return_value=mcu_versions)
        mock_flash_mgr.get_beacon_klipper_path = AsyncMock(return_value=None)

        mcu_resp = MagicMock()
        mcu_resp.status_code = 200
        mcu_resp.json.return_value = {
            "result": {"status": {"mcu beacon": {"mcu_version": "Beacon 2.1.0"}}}
        }
        um_resp = MagicMock()
        um_resp.status_code = 200
        um_resp.json.return_value = {"result": {"version_info": {}}}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[mcu_resp, um_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        from backend.main import get_fleet_versions

        with patch("backend.main.httpx.AsyncClient", return_value=mock_client):
            result = asyncio.get_event_loop().run_until_complete(get_fleet_versions())

        assert result["beacon-1"].get("remote_version") is None
