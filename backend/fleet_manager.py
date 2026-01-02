import json
import os
from typing import List, Dict, Any

class FleetManager:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.fleet_file = os.path.join(data_dir, "fleet.json")
        self._ensure_data_dir()

    def _ensure_data_dir(self):
        os.makedirs(self.data_dir, exist_ok=True)
        if not os.path.exists(self.fleet_file):
            with open(self.fleet_file, 'w') as f:
                json.dump([], f)

    def get_fleet(self) -> List[Dict[str, Any]]:
        """Returns the list of registered devices in the fleet."""
        with open(self.fleet_file, 'r') as f:
            return json.load(f)

    def save_device(self, device: Dict[str, Any]):
        """Adds or updates a device in the fleet."""
        fleet = self.get_fleet()
        # Check if device already exists by ID
        for i, d in enumerate(fleet):
            if d['id'] == device['id']:
                fleet[i] = device
                break
        else:
            fleet.append(device)
        
        with open(self.fleet_file, 'w') as f:
            json.dump(fleet, f, indent=4)

    def remove_device(self, device_id: str):
        """Removes a device from the fleet."""
        fleet = self.get_fleet()
        fleet = [d for d in fleet if d['id'] != device_id]
        with open(self.fleet_file, 'w') as f:
            json.dump(fleet, f, indent=4)
