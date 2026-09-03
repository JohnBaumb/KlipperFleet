"""Tests for the security and robustness fixes from the 2026-08 code review.

One check per fix, each written so it fails if the fix is reverted.
"""
import asyncio
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import backend.main as main_mod
from backend.flash_manager import FlashManager
from backend.process_utils import terminate


# --- Bootloader offset is read, not guessed -------------------------------


class TestFlashOffset:
    """A wrong DFU offset erases the bootloader, so never fall back to one."""

    def _with_config(self, monkeypatch, content):
        monkeypatch.setattr(
            main_mod, '_read_profile_config', lambda name: content
        )

    @pytest.mark.parametrize(
        'symbol,expected',
        [
            ('CONFIG_FLASH_START_2000=y', '0x08002000'),      # 8KiB
            ('CONFIG_STM32_FLASH_START_8000=y', '0x08008000'),  # 32KiB
            ('CONFIG_FLASH_START_20000=y', '0x08020000'),     # 128KiB
            ('CONFIG_FLASH_START_0=y', '0x08000000'),         # no bootloader
            # Offsets the old hardcoded table did not list. These used to fall
            # through to 0x08000000 and overwrite Katapult.
            ('CONFIG_STM32_FLASH_START_7000=y', '0x08007000'),  # 28KiB
            ('CONFIG_STM32_FLASH_START_C000=y', '0x0800c000'),  # 48KiB
            ('CONFIG_FLASH_START_5000=y', '0x08005000'),
        ],
    )
    def test_offset_parsed_from_symbol(self, monkeypatch, symbol, expected):
        self._with_config(monkeypatch, f'CONFIG_MACH_STM32=y\n{symbol}\n')
        assert main_mod.get_flash_offset('p') == expected

    def test_no_offset_declared_returns_none(self, monkeypatch):
        """No offset symbol must abort the flash, not default to the base address."""
        self._with_config(monkeypatch, 'CONFIG_MACH_STM32=y\n')
        assert main_mod.get_flash_offset('p') is None

    def test_missing_profile_returns_none(self, monkeypatch):
        self._with_config(monkeypatch, None)
        assert main_mod.get_flash_offset('nope') is None

    def test_unset_symbols_are_ignored(self, monkeypatch):
        """'# CONFIG_X is not set' lines must not be read as a selection."""
        self._with_config(
            monkeypatch,
            '# CONFIG_STM32_FLASH_START_20000 is not set\n'
            'CONFIG_STM32_FLASH_START_2000=y\n',
        )
        assert main_mod.get_flash_offset('p') == '0x08002000'


# --- Profile paths are validated at the chokepoint ------------------------


class TestProfilePathValidation:
    """The traversal guard has to live in the path builders, not per endpoint."""

    @pytest.mark.parametrize(
        'bad', ['../../etc/passwd', 'a/b', '..', 'x/../../y', '']
    )
    def test_traversal_rejected(self, bad):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            main_mod.profile_config_path(bad)
        assert exc.value.status_code == 400

        with pytest.raises(HTTPException):
            main_mod.artifact_path(bad, '.bin')

    def test_valid_name_builds_path_under_profiles_dir(self):
        path = main_mod.profile_config_path('my board v2')
        assert path == os.path.join(main_mod.PROFILES_DIR, 'my board v2.config')

    def test_resolve_firmware_path_rejects_traversal(self):
        """/flash used to reach this with an unvalidated profile name."""
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            main_mod.resolve_firmware_path('../../../etc/hosts', 'serial')


# --- Single-flight guard --------------------------------------------------


class TestHardwareClaim:
    """Two overlapping runs fight over the Klipper services and ~/klipper/.config."""

    def setup_method(self):
        main_mod._release_hardware()

    teardown_method = setup_method

    def test_second_claim_is_refused(self):
        from fastapi import HTTPException

        main_mod._claim_hardware('batch flash-all')
        with pytest.raises(HTTPException) as exc:
            main_mod._claim_hardware('flash /dev/ttyACM0')
        assert exc.value.status_code == 409
        assert 'flash-all' in exc.value.detail

    def test_release_allows_the_next_run(self):
        main_mod._claim_hardware('build a')
        main_mod._release_hardware()
        main_mod._claim_hardware('build b')  # must not raise


# --- Batch action whitelist ----------------------------------------------


class TestBatchActionValidation:
    def test_known_actions_cover_the_ui_buttons(self):
        assert set(main_mod.BATCH_ACTIONS) == {
            'build',
            'flash-ready',
            'flash-all',
            'build-flash-ready',
            'build-flash-all',
        }

    def test_typo_is_not_silently_accepted(self):
        """'flash-redy' matched no substring and used to no-op with a task_id."""
        assert 'flash-redy' not in main_mod.BATCH_ACTIONS


# --- Custom make command comes from the fleet, never the request ----------


class TestCustomMakeCommandSource:
    def test_reads_command_from_fleet_entry(self):
        fleet = [
            {'id': 'dev-a', 'custom_make_command': 'make -j2 all'},
            {'id': 'dev-b', 'custom_make_command': None},
        ]
        with patch.object(
            main_mod.fleet_mgr, 'get_fleet', AsyncMock(return_value=fleet)
        ):
            got = asyncio.get_event_loop().run_until_complete(
                main_mod._custom_make_command_for('dev-a')
            )
        assert got == 'make -j2 all'

    def test_unknown_device_yields_no_command(self):
        with patch.object(
            main_mod.fleet_mgr, 'get_fleet', AsyncMock(return_value=[])
        ):
            got = asyncio.get_event_loop().run_until_complete(
                main_mod._custom_make_command_for('ghost')
            )
        assert got is None

    def test_no_device_id_yields_no_command(self):
        got = asyncio.get_event_loop().run_until_complete(
            main_mod._custom_make_command_for(None)
        )
        assert got is None

    def test_build_endpoint_takes_no_command_parameter(self):
        """The shell command must not be reachable from the request at all."""
        import inspect

        params = inspect.signature(main_mod.build_profile).parameters
        assert 'custom_make_command' not in params
        assert set(params) == {'profile', 'device_id'}

    def test_build_endpoint_is_post_only(self):
        """A GET that runs bash is triggerable from any page via <img>."""
        routes = [
            r for r in main_mod.app.routes
            if getattr(r, 'path', None) == '/build/{profile}'
        ]
        assert routes, 'build route missing'
        assert routes[0].methods == {'POST'}

    def test_batch_endpoint_is_post_only(self):
        routes = [
            r for r in main_mod.app.routes
            if getattr(r, 'path', None) == '/batch/{action}'
        ]
        assert routes, 'batch route missing'
        assert routes[0].methods == {'POST'}


# --- Incremental task logs -----------------------------------------------


class TestTaskStatusSince:
    def _task(self, lines):
        store = main_mod.TaskStore()
        store.create_task('t1')
        for line in lines:
            store.add_log('t1', line)
        return store

    def _get(self, store, since):
        with patch.object(main_mod, 'task_store', store):
            return asyncio.get_event_loop().run_until_complete(
                main_mod.get_task_status('t1', since=since)
            )

    def test_returns_only_new_lines(self):
        store = self._task(['a\n', 'b\n', 'c\n'])
        assert self._get(store, 0)['logs'] == ['a\n', 'b\n', 'c\n']
        assert self._get(store, 2)['logs'] == ['c\n']
        assert self._get(store, 3)['logs'] == []

    def test_since_beyond_end_is_clamped(self):
        store = self._task(['a\n'])
        assert self._get(store, 99)['logs'] == []

    def test_negative_since_is_clamped(self):
        store = self._task(['a\n'])
        assert self._get(store, -5)['logs'] == ['a\n']

    def test_other_task_fields_survive(self):
        store = self._task(['a\n'])
        result = self._get(store, 0)
        assert result['status'] == 'running'
        assert result['completed'] is False


# --- DFU target must be unambiguous --------------------------------------


class TestDfuTargeting:
    @pytest.mark.asyncio
    async def test_refuses_when_several_devices_and_none_match(self):
        """dfu-util selects by VID:PID; with two boards it picks one at random."""
        fm = FlashManager('/tmp/klipper', '/tmp/katapult')
        devs = [
            {'id': 'AAA111', 'serial': 'AAA111'},
            {'id': 'BBB222', 'serial': 'BBB222'},
        ]
        with patch.object(
            fm, 'discover_dfu_devices', AsyncMock(return_value=devs)
        ), patch.object(fm, '_run_flash_command') as run:
            out = ''.join(
                [line async for line in fm.flash_dfu('CCC333', '/tmp/fw.bin')]
            )
        assert 'Refusing to flash' in out
        assert '>>> Flashing successful!' not in out
        run.assert_not_called()

    @pytest.mark.asyncio
    async def test_allows_a_single_device(self):
        """One board in DFU is unambiguous even if the stored ID is stale."""
        fm = FlashManager('/tmp/klipper', '/tmp/katapult')
        devs = [{'id': 'AAA111', 'serial': 'AAA111'}]

        async def fake_run(cmd, **kwargs):
            yield '>>> Flashing successful!\n'

        with patch.object(
            fm, 'discover_dfu_devices', AsyncMock(return_value=devs)
        ), patch.object(fm, '_run_flash_command', fake_run):
            out = ''.join(
                [
                    line
                    async for line in fm.flash_dfu(
                        'STALE_ID', '/tmp/fw.bin', leave=False
                    )
                ]
            )
        assert 'Refusing to flash' not in out
        assert 'Flash operation complete' in out

    @pytest.mark.asyncio
    async def test_allows_several_devices_when_one_matches(self):
        fm = FlashManager('/tmp/klipper', '/tmp/katapult')
        devs = [
            {'id': 'AAA111', 'serial': 'AAA111'},
            {'id': 'BBB222', 'serial': 'BBB222'},
        ]

        async def fake_run(cmd, **kwargs):
            yield '>>> Flashing successful!\n'

        with patch.object(
            fm, 'discover_dfu_devices', AsyncMock(return_value=devs)
        ), patch.object(fm, '_run_flash_command', fake_run):
            out = ''.join(
                [
                    line
                    async for line in fm.flash_dfu(
                        'BBB222', '/tmp/fw.bin', leave=False
                    )
                ]
            )
        assert 'Refusing to flash' not in out

    @pytest.mark.asyncio
    async def test_strict_resolve_does_not_rename_the_target(self):
        """The single-device fallback must not fire when we had an expectation."""
        fm = FlashManager('/tmp/klipper', '/tmp/katapult')
        devs = [{'id': 'OTHER_BOARD', 'serial': 'OTHER_BOARD'}]
        with patch.object(
            fm, 'discover_dfu_devices', AsyncMock(return_value=devs)
        ):
            strict = await fm.resolve_dfu_id(
                '/dev/serial/by-id/usb-Klipper_stm32-if00',
                known_dfu_id='EXPECTED',
                strict=True,
            )
            loose = await fm.resolve_dfu_id(
                '/dev/serial/by-id/usb-Klipper_stm32-if00',
                known_dfu_id='EXPECTED',
                strict=False,
            )
        assert strict == '/dev/serial/by-id/usb-Klipper_stm32-if00'
        assert loose == 'OTHER_BOARD'


class TestDfuLockReentrancy:
    """flash_dfu holds _dfu_lock, and asyncio.Lock is not reentrant.

    The retry path re-discovered devices through the public (locking) method,
    so any DFU flash that needed a second attempt deadlocked against itself
    and left the lock held for the life of the process.
    """

    def _primed(self):
        fm = FlashManager('/tmp/klipper', '/tmp/katapult')
        fm._dfu_cache = [{'id': 'AAA111', 'serial': 'AAA111'}]
        fm._dfu_cache_time = asyncio.get_event_loop().time()
        return fm

    @pytest.mark.asyncio
    async def test_locked_variant_works_while_holding_the_lock(self):
        fm = self._primed()
        async with fm._dfu_lock:
            got = await asyncio.wait_for(
                fm._discover_dfu_devices_locked(), timeout=1.0
            )
        assert got == [{'id': 'AAA111', 'serial': 'AAA111'}]

    @pytest.mark.asyncio
    async def test_public_variant_still_deadlocks_under_the_lock(self):
        """Documents why the _locked variant has to exist."""
        fm = self._primed()
        async with fm._dfu_lock:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(fm.discover_dfu_devices(), timeout=0.2)

    @pytest.mark.asyncio
    async def test_flash_retry_completes_instead_of_hanging(self):
        fm = self._primed()
        attempts = []

        async def flaky_run(cmd, **kwargs):
            attempts.append(cmd)
            if len(attempts) == 1:
                yield '>>> Flashing failed with return code 74\n'
            else:
                yield '>>> Flashing successful!\n'

        with patch.object(
            fm,
            'discover_dfu_devices',
            AsyncMock(return_value=[{'id': 'AAA111', 'serial': 'AAA111'}]),
        ), patch.object(fm, '_run_flash_command', flaky_run), patch(
            'asyncio.sleep', AsyncMock()
        ):
            out = ''.join(
                [
                    line
                    async for line in fm.flash_dfu(
                        'AAA111', '/tmp/fw.bin', leave=False
                    )
                ]
            )

        assert len(attempts) == 2, 'retry did not run'
        assert 'Flash operation complete' in out
        assert not fm._dfu_lock.locked(), 'lock leaked after the retry'


# --- Subprocess cleanup ---------------------------------------------------


class TestTerminate:
    @pytest.mark.asyncio
    async def test_running_process_is_killed_and_reaped(self):
        proc = MagicMock()
        proc.returncode = None
        proc.wait = AsyncMock(return_value=-9)
        proc.kill = MagicMock()
        await terminate(proc)
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_finished_process_is_left_alone(self):
        """Called twice (loop end + finally), the second call must be a no-op."""
        proc = MagicMock()
        proc.returncode = 0
        proc.kill = MagicMock()
        await terminate(proc)
        proc.kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_is_safe(self):
        await terminate(None)

    @pytest.mark.asyncio
    async def test_already_exited_race_is_swallowed(self):
        proc = MagicMock()
        proc.returncode = None
        proc.kill = MagicMock(side_effect=ProcessLookupError)
        await terminate(proc)  # must not raise


# --- Sudoers scope --------------------------------------------------------


class TestSudoersScope:
    """The generated rules must not amount to a root shell."""

    def _rules(self, tmp_path):
        import importlib.util

        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'install_scripts',
            'setup_sudoers.py',
        )
        spec = importlib.util.spec_from_file_location('setup_sudoers', script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_cp_rule_is_pinned_to_the_artifacts_dir(self, tmp_path):
        mod = self._rules(tmp_path)
        src = open(
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'install_scripts',
                'setup_sudoers.py',
            ),
            encoding='utf-8',
        ).read()
        # `cp * /usr/local/bin/klipper_mcu` staged any file as a root-run binary.
        assert '("cp", "* /usr/local/bin/klipper_mcu")' not in src
        assert 'artifacts' in src
        assert mod._home_dir('definitely-not-a-user') == (
            '/home/definitely-not-a-user'
        )

    def test_fuser_rule_is_pinned_to_the_mcu_binary(self):
        src = open(
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'install_scripts',
                'setup_sudoers.py',
            ),
            encoding='utf-8',
        ).read()
        # `fuser *` allowed killing any process on the box as root.
        assert '("fuser", "*")' not in src
        assert '("fuser", "-k /usr/local/bin/klipper_mcu")' in src
