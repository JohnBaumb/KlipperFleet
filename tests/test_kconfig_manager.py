import pytest
import os
from backend.kconfig_manager import KconfigManager

@pytest.fixture
def kconfig_mgr(tmp_path):
    # Create a mock Klipper structure
    klipper_dir = tmp_path / "klipper"
    src_dir = klipper_dir / "src"
    src_dir.mkdir(parents=True)
    
    # Create a minimal Kconfig file
    kconfig_content = """
mainmenu "Klipper Configuration"

config BOARD_MCU
    string "Micro-controller Architecture"
    default "stm32"

menu "Communication interface"
    config CANBUS_INTERFACE
        bool "CAN bus interface"
        default n

    config CANBUS_SPEED
        int "CAN bus speed"
        depends on CANBUS_INTERFACE
        default 1000000
endmenu
"""
    (src_dir / "Kconfig").write_text(kconfig_content)
    
    return KconfigManager(str(klipper_dir))

@pytest.mark.asyncio
async def test_load_kconfig(kconfig_mgr):
    await kconfig_mgr.load_kconfig()
    assert kconfig_mgr.kconf is not None
    assert "BOARD_MCU" in kconfig_mgr.kconf.syms

def test_get_menu_tree(kconfig_mgr):
    # get_menu_tree calls _load_kconfig_sync internally if needed
    tree = kconfig_mgr.get_menu_tree()
    
    assert len(tree) > 0
    # Check for BOARD_MCU
    mcu_node = next((n for n in tree if n['name'] == 'BOARD_MCU'), None)
    assert mcu_node is not None
    assert mcu_node['type'] == 'string'
    
    # Check for menu
    comm_menu = next((n for n in tree if n['type'] == 'menu'), None)
    assert comm_menu is not None
    assert len(comm_menu['children']) > 0

def test_set_value(kconfig_mgr):
    # set_value calls _load_kconfig_sync internally if needed
    kconfig_mgr.set_value("CANBUS_INTERFACE", "y")
    assert kconfig_mgr.kconf.syms["CANBUS_INTERFACE"].str_value == "y"
    
    # Check dependency
    assert kconfig_mgr.kconf.syms["CANBUS_SPEED"].visibility > 0

def test_save_config(kconfig_mgr, tmp_path):
    kconfig_mgr.set_value("BOARD_MCU", "rp2040")
    
    out_config = tmp_path / "test.config"
    kconfig_mgr.save_config(str(out_config))
    
    assert out_config.exists()
    content = out_config.read_text()
    assert 'CONFIG_BOARD_MCU="rp2040"' in content


class TestKalicoExtrasSupport:
    """Tests for Kalico (Danger Klipper) compatibility - find-firmware-extras.sh"""

    def test_extras_script_runs_when_present(self, tmp_path):
        """When find-firmware-extras.sh exists and src/extras/Kconfig is missing, run it."""
        klipper_dir = tmp_path / "klipper"
        src_dir = klipper_dir / "src"
        extras_dir = src_dir / "extras"
        scripts_dir = klipper_dir / "scripts"
        src_dir.mkdir(parents=True)
        extras_dir.mkdir(parents=True)
        scripts_dir.mkdir(parents=True)

        # Create a minimal Kconfig that sources extras
        kconfig_content = """
mainmenu "Klipper Configuration"
config TEST_SYM
    bool "Test"
    default y
source "src/extras/Kconfig"
"""
        (src_dir / "Kconfig").write_text(kconfig_content)

        # Create a mock find-firmware-extras.sh that creates an empty Kconfig
        script_content = '#!/bin/bash\necho -n "" > src/extras/Kconfig\n'
        script_path = scripts_dir / "find-firmware-extras.sh"
        script_path.write_text(script_content)
        script_path.chmod(0o755)

        mgr = KconfigManager(str(klipper_dir))
        # This should not crash - the script creates the missing file
        mgr._load_kconfig_sync()
        assert mgr.kconf is not None

    def test_extras_script_skipped_when_kconfig_exists(self, tmp_path):
        """When src/extras/Kconfig already exists, don't run the script."""
        klipper_dir = tmp_path / "klipper"
        src_dir = klipper_dir / "src"
        extras_dir = src_dir / "extras"
        scripts_dir = klipper_dir / "scripts"
        src_dir.mkdir(parents=True)
        extras_dir.mkdir(parents=True)
        scripts_dir.mkdir(parents=True)

        kconfig_content = """
mainmenu "Klipper Configuration"
config TEST_SYM
    bool "Test"
    default y
source "src/extras/Kconfig"
"""
        (src_dir / "Kconfig").write_text(kconfig_content)
        (extras_dir / "Kconfig").write_text("")  # Already exists

        # Script that would fail if run - proves we skip it
        script_path = scripts_dir / "find-firmware-extras.sh"
        script_path.write_text("#!/bin/bash\nexit 1\n")
        script_path.chmod(0o755)

        mgr = KconfigManager(str(klipper_dir))
        mgr._load_kconfig_sync()
        assert mgr.kconf is not None

    def test_no_extras_script_stock_klipper(self, tmp_path):
        """Stock Klipper has no find-firmware-extras.sh - should work fine."""
        klipper_dir = tmp_path / "klipper"
        src_dir = klipper_dir / "src"
        src_dir.mkdir(parents=True)

        kconfig_content = """
mainmenu "Klipper Configuration"
config TEST_SYM
    bool "Test"
    default y
"""
        (src_dir / "Kconfig").write_text(kconfig_content)

        mgr = KconfigManager(str(klipper_dir))
        mgr._load_kconfig_sync()
        assert mgr.kconf is not None

    def test_extras_fallback_on_script_failure(self, tmp_path):
        """If the script fails, create an empty Kconfig as fallback."""
        klipper_dir = tmp_path / "klipper"
        src_dir = klipper_dir / "src"
        extras_dir = src_dir / "extras"
        scripts_dir = klipper_dir / "scripts"
        src_dir.mkdir(parents=True)
        extras_dir.mkdir(parents=True)
        scripts_dir.mkdir(parents=True)

        kconfig_content = """
mainmenu "Klipper Configuration"
config TEST_SYM
    bool "Test"
    default y
source "src/extras/Kconfig"
"""
        (src_dir / "Kconfig").write_text(kconfig_content)

        # Script that always fails
        script_path = scripts_dir / "find-firmware-extras.sh"
        script_path.write_text("#!/bin/bash\nexit 1\n")
        script_path.chmod(0o755)

        mgr = KconfigManager(str(klipper_dir))
        # Should not crash - fallback creates the empty file
        mgr._load_kconfig_sync()
        assert mgr.kconf is not None
        assert (extras_dir / "Kconfig").exists()
