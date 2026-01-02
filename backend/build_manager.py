import subprocess
import os
import asyncio
import shutil
from typing import AsyncGenerator

class BuildManager:
    def __init__(self, klipper_dir: str, artifacts_dir: str):
        self.klipper_dir = klipper_dir
        self.artifacts_dir = artifacts_dir
        os.makedirs(self.artifacts_dir, exist_ok=True)

    async def run_build(self, config_path: str) -> AsyncGenerator[str, None]:
        """Runs the Klipper build process and yields output line by line."""
        profile_name = os.path.basename(config_path).replace(".config", "")
        
        # Klipper's Makefile doesn't handle spaces in KCONFIG_CONFIG well.
        # We copy the profile to the standard .config location in the klipper directory.
        tmp_config = os.path.join(self.klipper_dir, ".config")
        try:
            shutil.copy(config_path, tmp_config)
        except Exception as e:
            yield f"!!! Error copying config: {str(e)}\n"
            return

        # 1. Clean
        yield ">>> Cleaning build environment...\n"
        try:
            await self._run_command(["make", "clean"])
        except Exception as e:
            yield f"!!! Error during make clean: {str(e)}\n"
            return

        # 2. olddefconfig (ensure config is valid for current Klipper version)
        yield ">>> Validating configuration (olddefconfig)...\n"
        try:
            await self._run_command(["make", "olddefconfig"])
        except Exception as e:
            yield f"!!! Error during make olddefconfig: {str(e)}\n"
            return

        # 3. Build
        yield ">>> Starting build...\n"
        process = await asyncio.create_subprocess_exec(
            "make",
            cwd=self.klipper_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        while True:
            line = await process.stdout.readline()
            if not line:
                break
            yield line.decode()

        await process.wait()
        if process.returncode == 0:
            yield ">>> Build successful!\n"
            
            # Copy artifacts to persistent storage
            bin_src = os.path.join(self.klipper_dir, "out", "klipper.bin")
            elf_src = os.path.join(self.klipper_dir, "out", "klipper.elf")
            
            if os.path.exists(bin_src):
                shutil.copy(bin_src, os.path.join(self.artifacts_dir, f"{profile_name}.bin"))
                yield f">>> Saved artifact: {profile_name}.bin\n"
            if os.path.exists(elf_src):
                shutil.copy(elf_src, os.path.join(self.artifacts_dir, f"{profile_name}.elf"))
                yield f">>> Saved artifact: {profile_name}.elf\n"
        else:
            yield f">>> Build failed with return code {process.returncode}\n"

    async def _run_command(self, cmd: list, timeout: int = 60):
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.klipper_dir,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except:
                pass
            raise Exception(f"Command {' '.join(cmd)} timed out after {timeout}s")
