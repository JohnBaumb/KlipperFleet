import json
import os
import threading
import time
from typing import List, Dict, Any, Optional

class FleetManager:
    def __init__(self, data_dir: str) -> None:
        self.data_dir: str = data_dir
        self.fleet_file: str = os.path.join(data_dir, "fleet.json")
        self._lock = threading.Lock()
        self._ensure_data_dir()

    def _ensure_data_dir(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        if not os.path.exists(self.fleet_file):
            with open(self.fleet_file, 'w') as f:
                json.dump([], f)

    def get_fleet(self) -> List[Dict[str, Any]]:
        """Returns the list of registered devices in the fleet."""
        with self._lock:
            with open(self.fleet_file, 'r') as f:
                return json.load(f)

    def _write_fleet(self, fleet: List[Dict[str, Any]]) -> None:
        """Atomically writes the fleet data to disk."""
        tmp_path = self.fleet_file + ".tmp"
        with open(tmp_path, 'w') as f:
            json.dump(fleet, f, indent=4)
        os.replace(tmp_path, self.fleet_file)

    def save_device(self, device: Dict[str, Any]) -> None:
        """Adds or updates a device in the fleet."""
        with self._lock:
            with open(self.fleet_file, 'r') as f:
                fleet: List[Dict[str, Any]] = json.load(f)
            old_id: Optional[str] = device.get('old_id')
            target_id = old_id if old_id else device['id']
            
            # Check if device already exists by ID or old_id
            for i, d in enumerate(fleet):
                if d['id'] == target_id:
                    # Remove old_id from the saved data
                    save_data: Dict[str, Any] = device.copy()
                    save_data.pop('old_id', None)
                    fleet[i] = save_data
                    break
            else:
                save_data: Dict[str, Any] = device.copy()
                save_data.pop('old_id', None)
                fleet.append(save_data)
            
            self._write_fleet(fleet)

    def remove_device(self, device_id: str) -> None:
        """Removes a device from the fleet."""
        with self._lock:
            with open(self.fleet_file, 'r') as f:
                fleet: List[Dict[str, Any]] = json.load(f)
            fleet = [d for d in fleet if d['id'] != device_id]
            self._write_fleet(fleet)

    def update_device_version(self, device_id: str, version_info: Dict[str, Any]) -> None:
        """Updates the version information for a device after flashing."""
        with self._lock:
            with open(self.fleet_file, 'r') as f:
                fleet: List[Dict[str, Any]] = json.load(f)
            for d in fleet:
                if d['id'] == device_id:
                    d['last_flashed'] = time.strftime("%Y-%m-%d %H:%M:%S")
                    d['flashed_version'] = version_info.get('version', 'unknown')
                    d['flashed_commit'] = version_info.get('commit', 'unknown')
                    break
            self._write_fleet(fleet)

    def update_device_live_version(self, device_id: str, live_version: str) -> None:
        """Updates the live running version for a device (from Moonraker query)."""
        with self._lock:
            with open(self.fleet_file, 'r') as f:
                fleet: List[Dict[str, Any]] = json.load(f)
            for d in fleet:
                if d['id'] == device_id:
                    d['live_version'] = live_version
                    break
            self._write_fleet(fleet)
