import os
import asyncio
import shutil
import json
from typing import AsyncGenerator, Dict, Any, Optional
from asyncio.subprocess import Process

class BuildManager:
    def __init__(self, klipper_dir: str, artifacts_dir: str) -> None:
        self.klipper_dir: str = klipper_dir
        self.artifacts_dir: str = artifacts_dir
        self._last_build_info: Dict[str, Dict[str, Any]] = {}
        os.makedirs(self.artifacts_dir, exist_ok=True)

    async def get_klipper_version(self) -> Dict[str, str]:
        """Gets the current Klipper git version info."""
        version_info: Dict[str, str] = {"version": "unknown", "commit": "unknown", "date": "unknown"}
        try:
            # Get the short commit hash
            process: Process = await asyncio.create_subprocess_exec(
                "git", "describe", "--always", "--tags", "--dirty",
                cwd=self.klipper_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await process.communicate()
            if process.returncode == 0:
                version_info["version"] = stdout.decode().strip()
            
            # Get the full commit hash
            process = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "HEAD",
                cwd=self.klipper_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await process.communicate()
            if process.returncode == 0:
                version_info["commit"] = stdout.decode().strip()[:12]
            
            # Get the commit date
            process = await asyncio.create_subprocess_exec(
                "git", "log", "-1", "--format=%ci",
                cwd=self.klipper_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await process.communicate()
            if process.returncode == 0:
                version_info["date"] = stdout.decode().strip()
                
        except Exception as e:
            print(f"Error getting Klipper version: {e}")
        return version_info

    def get_last_build_info(self, profile: str) -> Optional[Dict[str, Any]]:
        """Returns the build info for the last successful build of a profile."""
        return self._last_build_info.get(profile)

    async def run_build(self, config_path: str) -> AsyncGenerator[str, None]:
        """Runs the Klipper build process and yields output line by line."""
        profile_name: str = os.path.basename(config_path).replace(".config", "")
        
        # Klipper's Makefile doesn't handle spaces in KCONFIG_CONFIG well.
        # We copy the profile to the standard .config location in the klipper directory.
        tmp_config: str = os.path.join(self.klipper_dir, ".config")
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
        process: Process = await asyncio.create_subprocess_exec(
            "make",
            cwd=self.klipper_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        while True:
            if process.stdout:
                line: bytes = await process.stdout.readline()
                if not line:
                    break
                yield line.decode()
            else:
                break

        await process.wait()
        if process.returncode == 0:
            yield ">>> Build successful!\n"
            
            # Get version info for this build
            version_info = await self.get_klipper_version()
            yield f">>> Klipper version: {version_info['version']} ({version_info['commit']})\n"
            
            # Copy artifacts to persistent storage
            bin_src: str = os.path.join(self.klipper_dir, "out", "klipper.bin")
            elf_src: str = os.path.join(self.klipper_dir, "out", "klipper.elf")
            
            if os.path.exists(bin_src):
                shutil.copy(bin_src, os.path.join(self.artifacts_dir, f"{profile_name}.bin"))
                yield f">>> Saved artifact: {profile_name}.bin\n"
            if os.path.exists(elf_src):
                shutil.copy(elf_src, os.path.join(self.artifacts_dir, f"{profile_name}.elf"))
                yield f">>> Saved artifact: {profile_name}.elf\n"
            
            # Store build info for later retrieval
            import time
            self._last_build_info[profile_name] = {
                "version": version_info["version"],
                "commit": version_info["commit"],
                "date": version_info["date"],
                "built_at": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            
            # Save build info to a JSON file for persistence
            build_info_path = os.path.join(self.artifacts_dir, f"{profile_name}.build_info.json")
            with open(build_info_path, "w") as f:
                json.dump(self._last_build_info[profile_name], f, indent=2)
        else:
            yield f">>> Build failed with return code {process.returncode}\n"

    async def _run_command(self, cmd: list, timeout: int = 60) -> None:
        try:
            process: Process = await asyncio.create_subprocess_exec(
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
