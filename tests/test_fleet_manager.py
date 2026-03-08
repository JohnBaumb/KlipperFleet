import pytest
from backend.fleet_manager import FleetManager

@pytest.fixture
def fleet_mgr(tmp_path):
    return FleetManager(str(tmp_path))

@pytest.mark.asyncio
async def test_save_and_get_device(fleet_mgr):
    device = {
        "id": "test_id",
        "name": "Test Device",
        "method": "can",
        "profile": "test_profile"
    }
    await fleet_mgr.save_device(device)
    
    fleet = await fleet_mgr.get_fleet()
    assert len(fleet) == 1
    assert fleet[0]["id"] == "test_id"
    assert fleet[0]["name"] == "Test Device"

@pytest.mark.asyncio
async def test_update_device(fleet_mgr):
    device = {"id": "test_id", "name": "Old Name"}
    await fleet_mgr.save_device(device)
    
    updated_device = {"id": "test_id", "name": "New Name"}
    await fleet_mgr.save_device(updated_device)
    
    fleet = await fleet_mgr.get_fleet()
    assert len(fleet) == 1
    assert fleet[0]["name"] == "New Name"

@pytest.mark.asyncio
async def test_update_device_id(fleet_mgr):
    # Test changing the ID of a device using old_id
    device = {"id": "old_id", "name": "Test Device"}
    await fleet_mgr.save_device(device)
    
    updated_device = {"id": "new_id", "old_id": "old_id", "name": "Test Device"}
    await fleet_mgr.save_device(updated_device)
    
    fleet = await fleet_mgr.get_fleet()
    assert len(fleet) == 1
    assert fleet[0]["id"] == "new_id"
    assert "old_id" not in fleet[0]

@pytest.mark.asyncio
async def test_remove_device(fleet_mgr):
    device = {"id": "test_id", "name": "Test Device"}
    await fleet_mgr.save_device(device)
    assert len(await fleet_mgr.get_fleet()) == 1
    
    await fleet_mgr.remove_device("test_id")
    assert len(await fleet_mgr.get_fleet()) == 0
