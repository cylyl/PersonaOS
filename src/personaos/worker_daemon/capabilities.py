"""System capability discovery for remote worker compute nodes.

Per ADR 0019 & ADR 0034:
Scans hardware resources (CPU cores, RAM architecture) and available tools/runtimes.
"""

from __future__ import annotations

import os
import platform
import shutil
from typing import Any


class SystemCapabilityScanner:
    """Discovers host hardware and software capabilities."""

    @staticmethod
    def scan() -> dict[str, Any]:
        """Performs a comprehensive scan of host capabilities."""
        cpu_cores = os.cpu_count() or 1
        cpu_arch = platform.machine()
        system_os = platform.system().lower()

        # Memory detection
        memory_gb = 1.0
        try:
            if hasattr(os, "sysconf"):
                pages = os.sysconf("SC_PHYS_PAGES")
                page_size = os.sysconf("SC_PAGE_SIZE")
                total_bytes = pages * page_size
                memory_gb = round(total_bytes / (1024**3), 2)
        except Exception:
            pass

        # Toolchain and runtime checks
        known_tools = ["docker", "kubectl", "git", "curl", "openclaw", "python3"]
        available_tools = [tool for tool in known_tools if shutil.which(tool) is not None]

        runtimes = ["native_process"]
        if "docker" in available_tools:
            runtimes.append("docker")
        if "openclaw" in available_tools:
            runtimes.append("openclaw_acp")

        return {
            "cpu_cores": cpu_cores,
            "cpu_arch": cpu_arch,
            "system_os": system_os,
            "memory_gb": memory_gb,
            "available_tools": available_tools,
            "supported_runtimes": runtimes,
        }
