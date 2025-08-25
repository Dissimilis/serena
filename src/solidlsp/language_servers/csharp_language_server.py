# src/solidlsp/language_servers/csharp_language_server.py
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional, Tuple, List

# Serena / SolidLSP internals
# NOTE: keep imports lightweight to avoid circulars; names match existing codebase.
try:
    from solidlsp.ls_handler import LanguageServerHandler, LanguageServerException
except Exception:  # pragma: no cover
    # Fallback for static analysis / partial execution outside Serena
    class LanguageServerException(Exception):
        pass

    class LanguageServerHandler:
        pass

log = logging.getLogger("solidlsp")

# -----------------------
# User-tweakable settings
# -----------------------
# Roslyn LS package + version (defaults to publicly available, net9-capable train)
ROSLYN_LS_VERSION = os.getenv("SERENA_ROSLYN_LS_VERSION", "5.0.0-1.25277.114").strip()
# Prefer nuget.org direct package download to avoid Azure DevOps auth flakes
NUGET_PACKAGE_BASE = os.getenv(
    "SERENA_NUGET_BASE",
    "https://www.nuget.org/api/v2/package"
).rstrip("/")

# Controls the design-time warmup that brings up the MSBuild host
ENABLE_MSBUILD_WARMUP = os.getenv("SERENA_ROSLYN_MSBUILD_WARMUP", "1") not in ("0", "false", "False")
# Path discovery preference for warmup; if both sln and csproj exist, sln wins
WARMUP_TIMEOUT_SEC = int(os.getenv("SERENA_ROSLYN_WARMUP_TIMEOUT_SEC", "180"))
# Extra time we allow the very first heavy LSP request (symbols, etc.)
FIRST_REQUEST_EXTRA_TIMEOUT_SEC = int(os.getenv("SERENA_ROSLYN_FIRST_REQ_TIMEOUT_SEC", "120"))
# If Roslyn logs the specific NamedPipe timeout once, we restart + retry one time
RETRY_ON_NAMEDPIPE_TIMEOUT = os.getenv("SERENA_ROSLYN_RETRY_ON_PIPE_TIMEOUT", "1") not in ("0","false","False")

# Let dotnet roll forward so LS built against net8 runs on net9 SDKs and vice-versa
DOTNET_ROLL_FORWARD = os.getenv("SERENA_DOTNET_ROLL_FORWARD", "LatestMajor")

# Internal constants / paths
_THIS_DIR = Path(__file__).resolve().parent
_STATIC_ROOT = _THIS_DIR / "static" / "CSharpLanguageServer"
_DOWNLOADS_ROOT = _STATIC_ROOT / "downloads"
_EXTRACT_ROOT = _STATIC_ROOT / "roslyn-ls"

# Package IDs per-OS
_PKG_ID_BY_OS = {
    "Windows": "Microsoft.CodeAnalysis.LanguageServer.win-x64",
    "Linux":   "Microsoft.CodeAnalysis.LanguageServer.linux-x64",
    "Darwin":  "Microsoft.CodeAnalysis.LanguageServer.osx-arm64" if platform.machine().lower() in ("arm64","aarch64") else "Microsoft.CodeAnalysis.LanguageServer.osx-x64",
}

# Regex we watch for in Roslyn stderr to detect the specific BuildHostPipe timeout
_NAMEDPIPE_TIMEOUT_RE = re.compile(
    r"System\.TimeoutException: The operation has timed out.*NamedPipeClientStream\.ConnectInternal",
    re.DOTALL
)

class CSharpLanguageServer(LanguageServerHandler):
    """
    Microsoft Roslyn Language Server (plain Roslyn) adapter with:
      * NuGet.org-backed download (net9-friendly)
      * MSBuild design-time warmup to avoid BuildHost pipe connection timeouts
      * Single automatic retry on NamedPipe timeout
    """

    def __init__(self, project_root: Path, **kwargs):
        super().__init__()
        self.project_root = Path(project_root).resolve()
        self._proc: Optional[subprocess.Popen] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._saw_namedpipe_timeout: bool = False
        self._first_request_deadline: Optional[float] = None
        _STATIC_ROOT.mkdir(parents=True, exist_ok=True)
        _DOWNLOADS_ROOT.mkdir(parents=True, exist_ok=True)
        _EXTRACT_ROOT.mkdir(parents=True, exist_ok=True)

    # -------------
    # Public API-ish
    # -------------

    def start(self) -> None:
        self._ensure_dotnet()
        self._ensure_language_server_binary()
        if ENABLE_MSBUILD_WARMUP:
            self._prewarm_msbuild_host()
        self._spawn_roslyn_ls()
        self._first_request_deadline = time.time() + FIRST_REQUEST_EXTRA_TIMEOUT_SEC

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        self._proc = None

    # Example hook SolidLSP calls around LSP requests; we extend first-call timeout
    def request(self, method: str, params: dict, timeout: Optional[float] = None):
        timeout = self._maybe_extend_first_timeout(timeout)
        try:
            return self._forward_lsp_request(method, params, timeout=timeout)
        except LanguageServerException as e:
            if RETRY_ON_NAMEDPIPE_TIMEOUT and self._saw_namedpipe_timeout:
                log.warning("Roslyn reported NamedPipe timeout once; restarting language server and retrying.")
                self._saw_namedpipe_timeout = False
                self._restart_after_pipe_timeout()
                # Try again (once)
                timeout = self._maybe_extend_first_timeout(timeout)
                return self._forward_lsp_request(method, params, timeout=timeout)
            raise

    # ----------------
    # Internal helpers
    # ----------------

    def _maybe_extend_first_timeout(self, timeout: Optional[float]) -> Optional[float]:
        if self._first_request_deadline is None:
            return timeout
        remain = max(0.0, self._first_request_deadline - time.time())
        if remain <= 0:
            self._first_request_deadline = None
            return timeout
        if timeout is None:
            return remain
        return max(timeout, remain)

    def _forward_lsp_request(self, method: str, params: dict, timeout: Optional[float] = None):
        """
        Delegate to SolidLSP base; here we just keep the signature.
        This method name is what the base class actually calls; we hook around it above.
        """
        # The actual base class provides the transport; call super if available.
        if hasattr(super(), "request"):
            return super().request(method, params, timeout=timeout)  # type: ignore[misc]
        raise LanguageServerException("Underlying LSP transport not available in this context.")

    def _ensure_dotnet(self) -> None:
        try:
            out = subprocess.run(["dotnet", "--info"], capture_output=True, text=True, check=True)
            log.info("Found system dotnet:\n%s", _first_lines(out.stdout, 15))
        except Exception as e:  # pragma: no cover
            raise LanguageServerException("Required .NET SDK/runtime not found on PATH") from e

    def _ensure_language_server_binary(self) -> None:
        os_name = platform.system()
        pkg_id = _PKG_ID_BY_OS.get(os_name)
        if not pkg_id:
            raise LanguageServerException(f"Unsupported OS for Roslyn LS: {os_name}")

        target_dir = _EXTRACT_ROOT / f"{pkg_id}-{ROSLYN_LS_VERSION}"
        ls_exe, ls_dll = target_dir / "Microsoft.CodeAnalysis.LanguageServer.exe", target_dir / "Microsoft.CodeAnalysis.LanguageServer.dll"
        if ls_exe.exists() or ls_dll.exists():
            return

        # Download
        nupkg = self._download_nuget_package(pkg_id, ROSLYN_LS_VERSION)
        # Extract
        with zipfile.ZipFile(nupkg, "r") as zf:
            # Prefer "tools/" or "content/" layouts; extract all under a dedicated folder
            target_dir.mkdir(parents=True, exist_ok=True)
            zf.extractall(target_dir)

        # Some packages place binaries under subfolders like "tools/" or "bincore/"
        # Try to surface the main executable/dll into target_dir root for simpler launching
        candidate = self._find_ls_binary(target_dir)
        if candidate and candidate.parent != target_dir:
            for name in ("Microsoft.CodeAnalysis.LanguageServer.exe", "Microsoft.CodeAnalysis.LanguageServer.dll"):
                src = candidate.parent / name
                if src.exists():
                    shutil.copy2(src, target_dir / name)

        if not (ls_exe.exists() or ls_dll.exists()):
            raise LanguageServerException(
                f"Roslyn LS package extracted but binaries not found in {target_dir}"
            )

    def _download_nuget_package(self, package_id: str, version: str) -> Path:
        url = f"{NUGET_PACKAGE_BASE}/{package_id}/{version}"
        out = _DOWNLOADS_ROOT / f"{package_id}-{version}.nupkg"
        if out.exists() and out.stat().st_size > 0:
            return out
        log.info("Downloading %s %s from %s ...", package_id, version, url)
        try:
            with urllib.request.urlopen(url) as resp, open(out, "wb") as fh:
                shutil.copyfileobj(resp, fh)
        except Exception as e:
            raise LanguageServerException(f"Failed to download {package_id} {version} from nuget.org") from e
        return out

    def _find_ls_binary(self, root: Path) -> Optional[Path]:
        # search for exe first (win), then dll for dotnet exec
        for name in ("Microsoft.CodeAnalysis.LanguageServer.exe", "Microsoft.CodeAnalysis.LanguageServer.dll"):
            found = next(root.rglob(name), None)
            if found:
                return found
        return None

    def _prewarm_msbuild_host(self) -> None:
        """
        Kick MSBuild design-time build before Roslyn LS initializes.
        This starts the Roslyn MSBuild build host so the LS doesn't hit the 10s pipe timeout.
        """
        solution, csproj = self._discover_solution_or_project()
        target = solution or csproj
        if not target:
            log.info("No .sln or .csproj found under %s; skipping MSBuild warmup.", self.project_root)
            return

        # Best-effort: ensure servers are in a clean state, then run a fast design-time build
        with contextlib.suppress(Exception):
            subprocess.run(["dotnet", "build-server", "shutdown", "--msbuild", "--vbcscompiler"],
                           cwd=self.project_root, capture_output=True, text=True, timeout=30)

        # Design-time build properties (mirrors IDE behavior; fast & dependency-only)
        props = [
            "/nologo",
            "/v:minimal",
            "/t:ResolveReferences",
            "/p:DesignTimeBuild=true",
            "/p:SkipCompilerExecution=true",
            "/p:DisableRarCache=false",
        ]
        cmd = ["dotnet", "msbuild", str(target)] + props

        log.info("Prewarming MSBuild host: %s", " ".join(cmd))
        try:
            subprocess.run(cmd, cwd=self.project_root, check=False, timeout=WARMUP_TIMEOUT_SEC,
                           capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            log.warning("MSBuild warmup exceeded timeout (%ss); continuing anyway.", WARMUP_TIMEOUT_SEC)
        except Exception as e:
            log.warning("MSBuild warmup failed (non-fatal): %s", e)

    def _discover_solution_or_project(self) -> Tuple[Optional[Path], Optional[Path]]:
        # Prefer nearest .sln; else nearest .csproj
        slns = sorted(self.project_root.rglob("*.sln"), key=lambda p: len(p.parts))
        projs = sorted(self.project_root.rglob("*.csproj"), key=lambda p: len(p.parts))
        return (slns[0] if slns else None, projs[0] if projs else None)

    def _spawn_roslyn_ls(self) -> None:
        env = os.environ.copy()
        # Make Roslyn happier on modern SDK stacks
        env.setdefault("DOTNET_ROLL_FORWARD", DOTNET_ROLL_FORWARD)
        env.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")
        env.setdefault("DOTNET_NOLOGO", "1")
        env.setdefault("MSBUILDDISABLENODEREUSE", "1")  # keep build server churn low

        bin_dir = _EXTRACT_ROOT / f"{_PKG_ID_BY_OS[platform.system()]}-{ROSLYN_LS_VERSION}"
        exe_path = bin_dir / "Microsoft.CodeAnalysis.LanguageServer.exe"
        dll_path = bin_dir / "Microsoft.CodeAnalysis.LanguageServer.dll"

        if exe_path.exists():
            cmd = [str(exe_path)]
        elif dll_path.exists():
            cmd = ["dotnet", str(dll_path)]
        else:
            raise LanguageServerException("Roslyn LS binary not found after extraction.")

        # Roslyn LS uses a VS Code–style pipe handshake; SolidLSP has the adapter for this server.
        # We spawn and attach readers to stderr to watch for the NamedPipe timeout signature.
        log.info("Starting Roslyn LS: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(self.project_root),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr_watchdog, name="roslyn-stderr-watchdog", daemon=True
        )
        self._stderr_thread.start()

        # Handshake into SolidLSP transport (implemented upstream).
        if hasattr(super(), "_attach_process_streams"):
            super()._attach_process_streams(self._proc.stdin, self._proc.stdout)  # type: ignore[attr-defined]

    def _read_stderr_watchdog(self) -> None:
        if not self._proc or not self._proc.stderr:
            return
        for line in self._proc.stderr:
            try:
                # Stream LS logs through to our own logger at debug level
                m = line.rstrip("\r\n")
                if m:
                    log.debug("RoslynLS[stderr]: %s", m)
                if _NAMEDPIPE_TIMEOUT_RE.search(m):
                    self._saw_namedpipe_timeout = True
            except Exception:
                pass

    def _restart_after_pipe_timeout(self) -> None:
        self.stop()
        # One more attempt: re-warm in case LS crashed early
        if ENABLE_MSBUILD_WARMUP:
            self._prewarm_msbuild_host()
        self._spawn_roslyn_ls()
        # Give the "first request" a fresh grace period again
        self._first_request_deadline = time.time() + FIRST_REQUEST_EXTRA_TIMEOUT_SEC


# -----------------
# Small util helpers
# -----------------

def _first_lines(s: str, n: int) -> str:
    out = io.StringIO()
    for i, line in enumerate(s.splitlines()):
        if i >= n:
            out.write("...\n")
            break
        out.write(line + "\n")
    return out.getvalue()
