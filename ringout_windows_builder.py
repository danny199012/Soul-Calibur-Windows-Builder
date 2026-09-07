#!/usr/bin/env python3
"""
RingOut Windows Builder
=======================
Automates the full build of the SoulCalibur II static recompilation
(RingOut - https://github.com/danny199012/RingOut) on Windows.

What this script does:
  1. Checks / installs all required build tools (Git, CMake, Ninja, clang
     via llvm-mingw, Python 3, Vulkan SDK hint).
  2. Clones the RingOut repository (everything is vendored - no submodule init).
  3. Builds the ModernGekko runtime  ->  moderngekko-run.exe
  4. Builds the DolRecomp recompiler ->  dolrecomp.exe
  5. Compiles the Windows launcher   ->  RingOut.exe
  6. Assembles everything under  RingOut-windows  (or --out DIR).
  7. Runs first-time module setup if you supply a disc image with --iso.

Requirements (installed automatically where possible):
  - Windows 10 / 11, x86-64
  - Internet access on first run (to download tools)
  - ~6 GB free disk space
  - A Vulkan-capable GPU with up-to-date drivers

Usage:
  python ringout_windows_builder.py [--iso path\\to\\game.iso] [--out DIR]
                                    [--skip-deps] [--rebuild] [--jobs N]
"""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import textwrap
import urllib.request
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Release / download coordinates
# ---------------------------------------------------------------------------
REPO_URL      = "https://github.com/danny199012/RingOut"
REPO_DIR_NAME = "RingOut-src"

# llvm-mingw: self-contained clang + lld + ucrt headers for Windows
LLVM_MINGW_RELEASE_API   = "https://api.github.com/repos/mstorsjo/llvm-mingw/releases/latest"
LLVM_MINGW_ASSET_PATTERN = re.compile(r"llvm-mingw-\d+-ucrt-x86_64\.zip", re.IGNORECASE)

# Regex to strip ANSI colour codes from log file output
ANSI_RE = re.compile(r'\033\[[0-9;]*m')

# CMake portable zip (fallback if cmake not on PATH)
CMAKE_DOWNLOAD_URL = (
    "https://github.com/Kitware/CMake/releases/download/"
    "v3.29.6/cmake-3.29.6-windows-x86_64.zip"
)

# Ninja (fallback)
NINJA_RELEASE_API = "https://api.github.com/repos/ninja-build/ninja/releases/latest"

# Python embeddable (bundled inside the output package for module builds)
PYTHON_EMBED_VER = "3.11.9"
PYTHON_EMBED_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_EMBED_VER}/"
    f"python-{PYTHON_EMBED_VER}-embed-amd64.zip"
)

# ---------------------------------------------------------------------------
# Console colours
# ---------------------------------------------------------------------------
BOLD   = "\033[1m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
RESET  = "\033[0m"

def _enable_ansi():
    if sys.platform == "win32":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            pass

_enable_ansi()

def log(msg, colour=RESET): print(f"{colour}{msg}{RESET}", flush=True)
def step(title):
    log(f"\n{'='*60}", CYAN)
    log(f"  {title}", BOLD)
    log(f"{'='*60}", CYAN)
def ok(msg):    log(f"  [OK]  {msg}", GREEN)
def warn(msg):  log(f"  [!!]  {msg}", YELLOW)
def info(msg):  log(f"  [..]  {msg}")
def error(msg): log(f"  [ERR] {msg}", RED)
def die(msg):   error(msg); sys.exit(1)

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def run(cmd, *, cwd=None, env=None, check=True, capture=False, quiet=False):
    """Run a subprocess, streaming output unless capture=True."""
    if not quiet:
        shown = cmd if isinstance(cmd, str) else " ".join(str(c) for c in cmd)
        info("Running: " + shown)
    kwargs = dict(cwd=cwd, env=env)
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
        kwargs["text"]   = True
    result = subprocess.run(cmd, **kwargs)
    if check and result.returncode != 0:
        die(f"Command failed (exit {result.returncode})")
    return result

def which(name, extra_paths=None):
    """Find an executable on PATH (and optional extra dirs)."""
    dirs = list(os.environ.get("PATH", "").split(os.pathsep))
    if extra_paths:
        dirs = list(extra_paths) + dirs
    exts = [""] if sys.platform != "win32" else [".exe", ".cmd", ".bat", ""]
    for d in dirs:
        if not d:
            continue
        for ext in exts:
            p = Path(d) / (name + ext)
            if p.is_file():
                return str(p)
    return None

def download(url, dest_path, label=None):
    """Download a URL to a file with a simple progress indicator."""
    label = label or Path(dest_path).name
    info(f"Downloading {label} ...")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ringout-builder/1.0"})
        with urllib.request.urlopen(req) as resp, open(dest_path, "wb") as fh:
            total = int(resp.headers.get("Content-Length", 0))
            done  = 0
            chunk = 1 << 16
            while True:
                data = resp.read(chunk)
                if not data:
                    break
                fh.write(data)
                done += len(data)
                if total:
                    pct = done * 100 // total
                    print(f"\r    {pct:3d}% ({done//(1<<20)} MB / {total//(1<<20)} MB)",
                          end="", flush=True)
        print()
        ok(f"Downloaded {label}")
    except Exception as exc:
        die(f"Download failed for {url}: {exc}")

def extract_zip(zip_path, dest_dir, strip_components=0):
    """Extract a zip, optionally stripping top-level directory components."""
    info(f"Extracting {Path(zip_path).name} -> {dest_dir} ...")
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            parts = Path(member).parts
            if strip_components and len(parts) <= strip_components:
                continue
            rel = Path(*parts[strip_components:]) if strip_components else Path(member)
            target = Path(dest_dir) / rel
            if member.endswith("/") or member.endswith("\\"):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    ok(f"Extracted to {dest_dir}")

def github_latest_asset(api_url, pattern):
    """Return (name, browser_download_url) for the first asset matching pattern."""
    req = urllib.request.Request(
        api_url,
        headers={"User-Agent": "ringout-builder/1.0",
                 "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    for asset in data.get("assets", []):
        if re.search(pattern, asset["name"]):
            return asset["name"], asset["browser_download_url"]
    raise RuntimeError(f"No asset matching {pattern!r} in {api_url}")

def ensure_dir(p):
    Path(p).mkdir(parents=True, exist_ok=True)
    return Path(p)

def rmtree(p):
    p = Path(p)
    if p.exists():
        shutil.rmtree(p, onerror=lambda f, path, e: (os.chmod(path, stat.S_IWRITE), f(path)))

# ---------------------------------------------------------------------------
# Tool tracking
# ---------------------------------------------------------------------------

class ToolSet:
    """Tracks paths to every required tool."""
    def __init__(self, tools_dir: Path):
        self.tools_dir = tools_dir
        self.cmake   = None
        self.ninja   = None
        self.clang   = None
        self.clangxx = None
        self.git     = None
        self.python  = None
        self.msvc_found    = False
        self.vs_generator  = None  # e.g. "Visual Studio 17 2022"

    def env(self, extra_path_dirs=None):
        """Build an os.environ copy that has all known tools on PATH."""
        e = os.environ.copy()
        prepend = []
        for attr in ("cmake", "ninja", "clang", "git", "python"):
            val = getattr(self, attr)
            if val:
                d = str(Path(val).parent)
                if d not in prepend:
                    prepend.append(d)
        if extra_path_dirs:
            prepend.extend(str(p) for p in extra_path_dirs)
        if prepend:
            e["PATH"] = os.pathsep.join(prepend) + os.pathsep + e.get("PATH", "")
        return e

# ---------------------------------------------------------------------------
# Dependency installation
# ---------------------------------------------------------------------------

def ensure_git(tools: ToolSet, dl_dir: Path, skip: bool):
    path = which("git")
    if path:
        tools.git = path
        ok(f"git: {path}")
        return
    if skip:
        die("git not found on PATH. Install Git for Windows: https://git-scm.com/downloads/win")
    warn("git not found - downloading portable MinGit ...")
    minigit_api = "https://api.github.com/repos/git-for-windows/git/releases/latest"
    try:
        name, url = github_latest_asset(minigit_api, r"MinGit-.*-64-bit\.zip")
    except Exception as exc:
        die(f"Could not find a MinGit release: {exc}")
    dest = dl_dir / "mingit.zip"
    download(url, dest, label=name)
    git_dir = tools.tools_dir / "mingit"
    extract_zip(dest, git_dir, strip_components=0)
    git_exe = git_dir / "cmd" / "git.exe"
    if not git_exe.exists():
        die(f"git.exe not found after extracting MinGit to {git_dir}")
    tools.git = str(git_exe)
    ok(f"git: {tools.git}")

def ensure_cmake(tools: ToolSet, dl_dir: Path, skip: bool):
    path = which("cmake")
    if path:
        tools.cmake = path
        ok(f"cmake: {path}")
        return
    if skip:
        die("cmake not found. Download from https://cmake.org/download/")
    warn("cmake not found - downloading portable CMake ...")
    dest = dl_dir / "cmake.zip"
    download(CMAKE_DOWNLOAD_URL, dest, label="cmake-3.29.6-windows-x86_64.zip")
    cmake_dir = tools.tools_dir / "cmake"
    extract_zip(dest, cmake_dir, strip_components=1)
    cmake_exe = cmake_dir / "bin" / "cmake.exe"
    if not cmake_exe.exists():
        die(f"cmake.exe not found after extraction at {cmake_dir}")
    tools.cmake = str(cmake_exe)
    ok(f"cmake: {tools.cmake}")

def ensure_ninja(tools: ToolSet, dl_dir: Path, skip: bool):
    path = which("ninja")
    if path:
        tools.ninja = path
        ok(f"ninja: {path}")
        return
    if skip:
        die("ninja not found. Download from https://github.com/ninja-build/ninja/releases")
    warn("ninja not found - downloading latest ...")
    try:
        name, url = github_latest_asset(NINJA_RELEASE_API, r"ninja-win\.zip")
    except Exception as exc:
        die(f"Could not find a Ninja release: {exc}")
    dest = dl_dir / "ninja.zip"
    download(url, dest, label=name)
    ninja_dir = tools.tools_dir / "ninja"
    ensure_dir(ninja_dir)
    extract_zip(dest, ninja_dir)
    ninja_exe = ninja_dir / "ninja.exe"
    if not ninja_exe.exists():
        die("ninja.exe not found after extraction")
    tools.ninja = str(ninja_exe)
    ok(f"ninja: {tools.ninja}")

def ensure_llvm_mingw(tools: ToolSet, dl_dir: Path, skip: bool):
    path = which("clang")
    if path:
        tools.clang   = path
        tools.clangxx = which("clang++") or path
        ok(f"clang: {path}")
        return
    if skip:
        die("clang not found on PATH. Install LLVM or llvm-mingw.")
    warn("clang not found - downloading llvm-mingw (clang + C runtime) ...")
    try:
        name, url = github_latest_asset(LLVM_MINGW_RELEASE_API, LLVM_MINGW_ASSET_PATTERN)
    except Exception as exc:
        die(f"Could not find an llvm-mingw release: {exc}")
    dest = dl_dir / "llvm-mingw.zip"
    download(url, dest, label=name)
    llvm_dir = tools.tools_dir / "llvm-mingw"
    extract_zip(dest, llvm_dir, strip_components=1)
    clang_exe = llvm_dir / "bin" / "clang.exe"
    if not clang_exe.exists():
        die(f"clang.exe not found after llvm-mingw extraction at {llvm_dir}")
    tools.clang   = str(clang_exe)
    tools.clangxx = str(llvm_dir / "bin" / "clang++.exe")
    ok(f"clang: {tools.clang}")

def ensure_python(tools: ToolSet, dl_dir: Path):
    """Find Python 3 on PATH; if absent download the embedded interpreter."""
    path = which("python3") or which("python")
    if path:
        r = subprocess.run([path, "--version"], capture_output=True, text=True)
        if "Python 3" in (r.stdout + r.stderr):
            tools.python = path
            ok(f"python: {path}")
            return
    warn("Python 3 not found on PATH - downloading embedded interpreter ...")
    dest = dl_dir / "python-embed.zip"
    download(PYTHON_EMBED_URL, dest, label=f"python-{PYTHON_EMBED_VER}-embed-amd64.zip")
    py_dir = tools.tools_dir / "python"
    ensure_dir(py_dir)
    extract_zip(dest, py_dir)
    py_exe = py_dir / "python.exe"
    if not py_exe.exists():
        die(f"python.exe not found after extraction at {py_dir}")
    tools.python = str(py_exe)
    ok(f"python: {tools.python}")

def check_vulkan():
    """Warn if Vulkan SDK is absent (build works, runtime needs a Vulkan driver)."""
    sdk = os.environ.get("VULKAN_SDK", "")
    if sdk and Path(sdk).exists():
        ok(f"Vulkan SDK: {sdk}")
        return
    for candidate in [Path(r"C:\VulkanSDK"),
                      Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "VulkanSDK"]:
        if candidate.exists():
            subdirs = sorted(candidate.iterdir())
            if subdirs:
                sdk = str(subdirs[-1])
                os.environ["VULKAN_SDK"] = sdk
                ok(f"Vulkan SDK (auto-detected): {sdk}")
                return
    warn(
        "Vulkan SDK not found. The runtime needs a Vulkan GPU driver at runtime.\n"
        "  Make sure your GPU drivers include Vulkan support.\n"
        "  Optional SDK for development: https://vulkan.lunarg.com/sdk/home#windows"
    )

def ensure_msvc(tools: ToolSet, dl_dir: Path, skip: bool):
    """Detect Visual Studio / MSVC, preferring VS 2022 (version 17).

    The Dolphin runtime (moderngekko-run.exe) MUST be built with MSVC on Windows.
    llvm-mingw fails on POSIX functions like wcwidth() that MSVC provides.

    VS 2022 (MSVC 14.3x, ~19.40) is the version the original RingOut CI used
    (windows-2022 runner). VS 2026 Preview (MSVC 19.51, toolset 14.51) has
    tighter C++ conformance that breaks the vendored Dolphin code â€” it rejects
    implicit std::string_view -> std::string conversions (C2440) that VS 2022
    silently allowed. So we prefer VS 2022 and only use a newer version as a
    last resort (with a warning).
    """
    vswhere = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")

    def _vswhere_find(version_range):
        """Query vswhere for a VS install in the given version range. Returns
        (install_path, version_major) or (None, None)."""
        if not vswhere.exists():
            return None, None
        r = subprocess.run(
            [str(vswhere), "-latest", "-products", "*",
             "-version", version_range,
             "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
             "-property", "installationPath"],
            capture_output=True, text=True)
        if r.returncode != 0 or not r.stdout.strip():
            return None, None
        vs_path = r.stdout.strip().splitlines()[0].strip()
        r2 = subprocess.run(
            [str(vswhere), "-latest", "-products", "*",
             "-version", version_range,
             "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
             "-property", "installationVersion"],
            capture_output=True, text=True)
        ver_str = r2.stdout.strip() if r2.returncode == 0 else ""
        major = int(ver_str.split('.')[0]) if ver_str.split('.')[0].isdigit() else 17
        return vs_path, major

    def _set_generator(major):
        if major == 17:
            tools.vs_generator = "Visual Studio 17 2022"
        elif major == 16:
            tools.vs_generator = "Visual Studio 16 2019"
        else:
            tools.vs_generator = f"Visual Studio {major}"
        tools.msvc_found = True

    # 1) Prefer VS 2022 (version 17.x) â€” the version the original CI used.
    #    ModernGekko requires MSVC 19.32+ (VS 2022 17.2.3+), and the vendored
    #    Dolphin code has implicit conversions that VS 2026+ rejects.
    vs_path, major = _vswhere_find("[17.0,18.0)")
    if vs_path:
        _set_generator(major)
        ok(f"Visual Studio 2022 found: {vs_path}")
        ok(f"Using generator: {tools.vs_generator}")
        return

    # 2) VS 2019 (version 16) is too old â€” ModernGekko requires MSVC 19.32+
    #    and VS 2019 ships MSVC 19.29. Skip it and fall through to download.

    # 3) Check for VS 2026+ (version 18+) â€” usable but may have C2440 errors.
    #    Only use this if the user explicitly passed --skip-deps.
    if skip:
        vs_path, major = _vswhere_find("[18.0,)")
        if vs_path:
            _set_generator(major)
            warn(f"Only Visual Studio {major} was found (not VS 2022).")
            warn(f"VS 2019 is too old (MSVC 19.29 < 19.32 required).")
            warn(f"VS {major} may have C2440 string_view->string errors.")
            warn(f"Installing VS 2022 Build Tools is recommended.")
            ok(f"Using generator: {tools.vs_generator} (best available)")
            return

    # 4) Check for cl.exe on PATH (Developer Command Prompt)
    cl = which("cl")
    if cl:
        ok(f"MSVC (cl.exe) found on PATH: {cl}")
        tools.msvc_found = True
        tools.vs_generator = "Visual Studio 17 2022"
        return

    if skip:
        warn("MSVC not found. The runtime requires Visual Studio 2022 Build Tools.\n"
             "  Download from: https://visualstudio.microsoft.com/downloads/\n"
             "  Select 'Build Tools for Visual Studio 2022' and install the\n"
             "  'Desktop development with C++' workload.")
        return

    # 5) Download and install VS 2022 Build Tools
    warn("VS 2022 not found - downloading Visual Studio 2022 Build Tools ...")
    warn("This is a large install (~2-3 GB) and may take 10-20 minutes.")
    url = "https://aka.ms/vs/17/release/vs_buildtools.exe"
    dest = dl_dir / "vs_buildtools.exe"
    download(url, dest, label="vs_buildtools.exe")

    info("Installing Visual Studio 2022 Build Tools (C++ workload) ...")
    result = subprocess.run(
        [str(dest), "--quiet", "--wait", "--norestart",
         "--add", "Microsoft.VisualStudio.Workload.VCTools",
         "--includeRecommended"],
        timeout=2400)  # up to 40 minutes
    if result.returncode != 0:
        warn(f"VS Build Tools installer returned code {result.returncode}")
        warn("You may need to install manually from https://visualstudio.microsoft.com/downloads/")
        return

    ok("Visual Studio 2022 Build Tools installed.")

    # Re-detect VS 2022
    vs_path, major = _vswhere_find("[17.0,18.0)")
    if vs_path:
        _set_generator(major)
        ok(f"Using generator: {tools.vs_generator}")

# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

def clone_or_update_repo(repo_dir: Path, tools: ToolSet):
    env = tools.env()
    # Windows has a 260-character path limit by default. The vendored Dolphin
    # tree contains deeply-nested SPIRV-Cross shader files whose names exceed
    # this (e.g. overlapping-bindings.msl31.argument.argument-tier-1...comp).
    # Without core.longpaths, git clone fails with "Filename too long" on ~6
    # files, then aborts the checkout with exit 128.
    run([tools.git, "config", "--global", "core.longpaths", "true"],
        env=env, quiet=True)
    if (repo_dir / ".git").exists():
        ok(f"Repository already exists at {repo_dir} - pulling latest ...")
        run([tools.git, "-C", str(repo_dir), "pull", "--ff-only"], env=env)
    else:
        info(f"Cloning {REPO_URL} into {repo_dir} ...")
        # No submodules - repo README says everything is vendored as plain files.
        # -c core.longpaths=true is belt-and-braces alongside the global setting
        # above, in case the user's gitrc overrides it.
        run([tools.git, "clone", "--depth=1",
             "-c", "core.longpaths=true",
             REPO_URL, str(repo_dir)], env=env)
    ok("Repository ready.")

# ---------------------------------------------------------------------------
# CMake build helpers
# ---------------------------------------------------------------------------

def cmake_configure(src: Path, build: Path, generator, extra_defs: dict,
                    tools: ToolSet, rebuild: bool):
    """Configure with Ninja + clang (for DolRecomp, launcher, module)."""
    if rebuild:
        rmtree(build)
    ensure_dir(build)
    env = tools.env()
    cmd = [
        tools.cmake,
        "-S", str(src),
        "-B", str(build),
        "-G", generator,
        f"-DCMAKE_MAKE_PROGRAM={tools.ninja}",
        f"-DCMAKE_C_COMPILER={tools.clang}",
        f"-DCMAKE_CXX_COMPILER={tools.clangxx}",
        "-DCMAKE_BUILD_TYPE=Release",
    ]
    for k, v in extra_defs.items():
        cmd.append(f"-D{k}={v}")
    run(cmd, env=env)

def cmake_configure_msvc(src: Path, build: Path, vs_generator: str,
                          extra_defs: dict, tools: ToolSet, rebuild: bool):
    """Configure with Visual Studio generator + MSVC (for the Dolphin runtime).

    The VS generator finds MSVC automatically â€” do NOT pass CMAKE_C_COMPILER
    or CMAKE_MAKE_PROGRAM. -A x64 selects 64-bit.
    """
    if rebuild:
        rmtree(build)
    ensure_dir(build)
    env = tools.env()
    cmd = [
        tools.cmake,
        "-S", str(src),
        "-B", str(build),
        "-G", vs_generator,
        "-A", "x64",
    ]
    for k, v in extra_defs.items():
        cmd.append(f"-D{k}={v}")
    run(cmd, env=env)

def cmake_build(build: Path, target: str, tools: ToolSet, jobs: int):
    env = tools.env()
    cmd = [tools.cmake, "--build", str(build), "--config", "Release",
           "--target", target, f"-j{jobs}"]
    run(cmd, env=env)

# ---------------------------------------------------------------------------
# Stage 1 - ModernGekko runtime
# ---------------------------------------------------------------------------

def build_moderngekko(repo: Path, build_root: Path, tools: ToolSet,
                      rebuild: bool, jobs: int) -> tuple[Path, Path, Path]:
    step("Building ModernGekko runtime + launcher + port tool")
    src   = repo / "ModernGekko"
    build = build_root / "moderngekko-build"

    defs = {
        "MODERNGEKKO_ENABLE_DOLPHIN_RUNTIME": "ON",
        "ENABLE_QT":             "OFF",
        "ENABLE_NOGUI":          "OFF",
        "ENABLE_TESTS":          "OFF",
        "ENABLE_ANALYTICS":      "OFF",
        "ENABLE_AUTOUPDATE":     "OFF",
        "USE_RETRO_ACHIEVEMENTS": "OFF",
        "USE_DISCORD_PRESENCE":   "OFF",
        "USE_MGBA":              "OFF",
        "USE_UPNP":              "OFF",
        "ENCODE_FRAMEDUMPS":     "OFF",
        "ENABLE_LLVM":           "OFF",
        # The vendored Dolphin tree has two MSVC compatibility issues:
        #
        # 1. C++20 __VA_OPT__ in macros (HookableEvent.h, ChunkFile.h, etc.)
        #    requires the conforming preprocessor: /Zc:preprocessor
        #
        # 2. Implicit std::string_view -> std::string conversions
        #    (StringUtil.h lines 259, 264) which newer MSVC (19.44+)
        #    rejects under strict conformance (/permissive-).
        #    Adding /permissive (without minus) relaxes this so the
        #    conversion is allowed, as it was with older MSVC.
        #
        # Both flags are needed: /Zc:preprocessor for __VA_OPT__, and
        # /permissive for the string_view->string conversion.
        "CMAKE_CXX_FLAGS":       "/Zc:preprocessor /wd4067 /wd4804 /wd4805",
        "CMAKE_C_FLAGS":         "/Zc:preprocessor /std:c11",
        "CMAKE_CXX_STANDARD":    "20",
    }

    if tools.msvc_found and tools.vs_generator:
        info("Using MSVC (Visual Studio generator) for the Dolphin runtime")
        cmake_configure_msvc(src, build, tools.vs_generator, defs, tools, rebuild)
    else:
        die("MSVC is required to build the Dolphin runtime on Windows.\n"
            "llvm-mingw cannot build it (wcwidth and other POSIX functions\n"
            "are missing). Install Visual Studio Build Tools and re-run.\n"
            "  https://visualstudio.microsoft.com/downloads/")
    cmake_build(build, "moderngekko-run", tools, jobs)
    cmake_build(build, "moderngekko-launcher", tools, jobs)
    cmake_build(build, "moderngekko-port", tools, jobs)

    # Locate the produced binaries (VS generator places them in Release/).
    def _find(build_dir: Path, name: str) -> Path:
        for candidate in [build_dir / "Release" / name,
                          build_dir / name,
                          build_dir / "Source" / "Core" / name,
                          build_dir / "Binaries" / name]:
            if candidate.exists():
                return candidate
        hits = list(build_dir.rglob(name))
        if hits:
            return hits[0]
        die(f"{name} not found after build")

    runtime_exe   = _find(build, "moderngekko-run.exe")
    launcher_exe  = _find(build, "RingOut.exe")   # OUTPUT_NAME is RingOut
    port_exe      = _find(build, "moderngekko-port.exe")

    ok(f"Runtime built:    {runtime_exe}")
    ok(f"Launcher built:   {launcher_exe}  (the C++ GUI launcher)")
    ok(f"Port tool built:  {port_exe}")
    return runtime_exe, launcher_exe, port_exe

# ---------------------------------------------------------------------------
# Stage 2 - DolRecomp recompiler
# ---------------------------------------------------------------------------

def build_dolrecomp(repo: Path, build_root: Path, tools: ToolSet,
                    rebuild: bool, jobs: int) -> Path:
    step("Building DolRecomp recompiler (dolrecomp.exe)")
    src   = repo / "DolRecomp"
    build = build_root / "dolrecomp-build"

    defs = {"DOLRECOMP_ENABLE_LLVM": "OFF"}
    info("Using llvm-mingw clang + Ninja for DolRecomp (provides POSIX headers)")
    cmake_configure(src, build, "Ninja", defs, tools, rebuild)
    cmake_build(build, "dolrecomp", tools, jobs)

    exe = None
    for candidate in [build / "dolrecomp.exe", build / "Release" / "dolrecomp.exe"]:
        if candidate.exists():
            exe = candidate
            break
    if exe is None:
        hits = list(build.rglob("dolrecomp.exe"))
        if hits:
            exe = hits[0]
        else:
            die("dolrecomp.exe not found after build")

    ok(f"Recompiler built: {exe}")
    return exe


def build_launcher(repo: Path, build_root: Path, tools: ToolSet):
    # The C++ GUI launcher (moderngekko-launcher) is built by
    # build_moderngekko() as part of the same CMake configure, and its output
    # is named RingOut.exe there. Nothing extra needs to be compiled here.
    step("C++ launcher built with the runtime (RingOut.exe)")
    build = build_root / "moderngekko-build"
    exe = None
    for candidate in [build / "Release" / "RingOut.exe", build / "RingOut.exe"]:
        if candidate.exists():
            exe = candidate
            break
    if exe is None:
        hits = list(build.rglob("RingOut.exe"))
        if hits:
            exe = hits[0]
    if exe is None:
        warn("RingOut.exe not found in moderngekko-build")
        return None
    ok(f"Launcher: {exe}")
    return exe

# ---------------------------------------------------------------------------
# Stage 4 - Assemble the output package
# ---------------------------------------------------------------------------

# Windows system DLLs that must never be bundled (they come from the OS). Mirrors
# the allowlist the official package script uses for import resolution.
_WINDOWS_SYSTEM_DLLS = {
    "advapi32.dll", "avrt.dll", "bcrypt.dll", "cfgmgr32.dll", "comctl32.dll",
    "comdlg32.dll", "crypt32.dll", "cryptui.dll", "d3d11.dll", "d3d12.dll",
    "d3dcompiler_47.dll", "dbghelp.dll", "dnsapi.dll", "dwmapi.dll",
    "dxgi.dll", "gdi32.dll", "hid.dll", "imm32.dll", "iphlpapi.dll",
    "kernel32.dll", "ksuser.dll", "mf.dll", "mfplat.dll", "mfreadwrite.dll",
    "mfuuid.dll", "mmdevapi.dll", "mpr.dll", "msvcrt.dll", "netapi32.dll",
    "normaliz.dll", "ntdll.dll", "ole32.dll", "oleacc.dll", "oleaut32.dll",
    "opengl32.dll", "powrprof.dll", "propsys.dll", "psapi.dll", "qwave.dll",
    "rpcrt4.dll", "secur32.dll", "setupapi.dll", "shcore.dll", "shell32.dll",
    "shlwapi.dll", "strmiids.dll", "ucrtbase.dll", "user32.dll", "userenv.dll",
    "usp10.dll", "uxtheme.dll", "version.dll", "vulkan-1.dll", "winhttp.dll",
    "wininet.dll", "winmm.dll", "winspool.drv", "wintrust.dll", "ws2_32.dll",
    "wtsapi32.dll",
}


def _copy_runtime_dlls(out_dir: Path, subdir: str, exe):
    """Copy non-system DLLs that sit next to a freshly built exe into the
    package location for that executable. This approximates the official
    release's PE-import resolution without needing objdump: for this project
    every non-system DLL the exes need is produced as a sibling in the build
    output directory (or an SDK bin/ dir like the MSVC runtime DLLs)."""
    if not exe or not Path(exe).exists():
        return
    src_dir = Path(exe).parent
    if not src_dir.is_dir():
        return
    dest = out_dir / subdir
    ensure_dir(dest)
    copied = 0
    for dll in sorted(src_dir.glob("*.dll")):
        if dll.name.lower() in _WINDOWS_SYSTEM_DLLS:
            continue
        try:
            shutil.copy2(dll, dest / dll.name)
            copied += 1
        except OSError:
            pass
    if copied:
        ok(f"Copied {copied} runtime DLL(s) to {subdir or '.'}/")


def assemble_package(
    repo:        Path,
    out_dir:     Path,
    runtime_exe: Path,
    dolrecomp_exe: Path,
    launcher_exe,
    port_exe,
    build_root:  Path,
    tools:       ToolSet,
):
    step("Assembling Windows package")
    windows_dist = (repo / "attic" / "windows" / "dist" /
                    "RingOut-1.0-dist-windows")

    # Directory layout matching the official ell release:
    #   <out_dir>/
    #     RingOut.exe          C++ GUI launcher (double-click to play)
    #     setup.ps1            recompiles a disc image on this machine
    #     bin/  moderngekko-run.exe
    #     bin/Sys/             Dolphin DSP/fonts/config resources (required)
    #     fonts/ art/          launcher fonts + character art
    #     tools/ dolrecomp.exe, moderngekko-port.exe
    #     module-src/  build recipe + DolRecomp headers
    #     shaders/     bundled post-processing filters
    #     userdata/GameSettings/GRSEAF.ini  cheat codes
    #     toolchain/   bundled clang + cmake + ninja + python (for module builds)

    ensure_dir(out_dir / "bin")
    ensure_dir(out_dir / "tools")
    ensure_dir(out_dir / "fonts")
    ensure_dir(out_dir / "art")
    ensure_dir(out_dir / "userdata" / "GameSettings")
    tc = out_dir / "toolchain"
    ensure_dir(tc / "bin")
    ensure_dir(tc / "python")

    # --- binaries ---
    shutil.copy2(runtime_exe,   out_dir / "bin" / "moderngekko-run.exe")
    dolrecomp_dst = out_dir / "tools" / "dolrecomp.exe"
    shutil.copy2(dolrecomp_exe, dolrecomp_dst)
    if port_exe and Path(port_exe).exists():
        shutil.copy2(port_exe, out_dir / "tools" / "moderngekko-port.exe")
        ok("Copied moderngekko-port.exe")

    # dolrecomp imports libwinpthread-1.dll (pthread_create/join/mutex_*).
    # Windows DLL search order checks the executable's directory first; without
    # it there the CRT init loop hits STATUS_CTRL_C_EXIT (-1073741515).
    # tools.clang is a plain string, so wrap it in Path to get its parent dir.
    tc_bin = Path(tools.clang).parent if tools.clang else None
    if tc_bin:
        pth = tc_bin / "libwinpthread-1.dll"
        if pth.exists():
            shutil.copy2(pth, out_dir / "tools" / "libwinpthread-1.dll")
            ok("Copied libwinpthread-1.dll for dolrecomp.")

    ok("Copied binaries.")

    # --- bin/Sys: Dolphin resources (DSP, fonts, config). CMake's POST_BUILD
    # copied Data/Sys next to moderngekko-run.exe; stage it as bin/Sys.
    sys_src = Path(runtime_exe).parent / "Sys"
    if sys_src.is_dir():
        shutil.copytree(sys_src, out_dir / "bin" / "Sys", dirs_exist_ok=True)
        ok("Copied bin/Sys (Dolphin resources).")
    else:
        warn(f"bin/Sys not found next to runtime ({sys_src}) - runtime may "
             "fail to find DSP/fonts/config.")

    # --- auto-copy runtime DLLs needed by the built executables ---
    # The official release resolves each PE's imports and ships the non-system
    # DLLs. Approximate that by copying any non-system DLL that sits next to
    # the freshly built executables into the same relative package location.
    _copy_runtime_dlls(out_dir, "bin",     runtime_exe)
    _copy_runtime_dlls(out_dir, "tools",   dolrecomp_exe)
    _copy_runtime_dlls(out_dir, "tools",   port_exe)
    _copy_runtime_dlls(out_dir, "",        launcher_exe)

    # --- launcher + its fonts/art payload ---
    if launcher_exe and Path(launcher_exe).exists():
        shutil.copy2(launcher_exe, out_dir / "RingOut.exe")
        ok("Copied RingOut.exe")
        # CMake's POST_BUILD copied fonts/ and art/ next to the launcher exe.
        for sub in ("fonts", "art"):
            src = Path(launcher_exe).parent / sub
            if src.is_dir():
                shutil.copytree(src, out_dir / sub, dirs_exist_ok=True)
                ok(f"Copied {sub}/")
            else:
                warn(f"{sub}/ not found next to launcher ({src}).")
    else:
        cmd_text = (
            "@echo off\r\n"
            "rem Ring Out - Windows launcher\r\n"
            'powershell.exe -NoProfile -ExecutionPolicy Bypass'
            ' -File "%~dp0RingOut.ps1" %*\r\n'
        )
        (out_dir / "RingOut.cmd").write_text(cmd_text, encoding="ascii")
        warn("RingOut.exe not built - using RingOut.cmd as entry point.")

    # --- scripts from the windows attic ---
    for name in ("setup.ps1", "RingOut.ps1", "RingOut.cmd"):
        src = windows_dist / name
        if src.exists():
            shutil.copy2(src, out_dir / name)

    # --- module-src (build recipe + DolRecomp headers) ---
    ms_src = repo / "dist" / "RingOut-1.0-dist" / "module-src"
    ms_dst = out_dir / "module-src"
    if ms_src.exists():
        if ms_dst.exists():
            rmtree(ms_dst)
        shutil.copytree(ms_src, ms_dst)
        ok("Copied module-src.")

    # --- shaders ---
    sh_src = repo / "dist" / "RingOut-1.0-dist" / "shaders"
    if sh_src.exists():
        shutil.copytree(sh_src, out_dir / "shaders", dirs_exist_ok=True)
        ok("Copied shaders.")

    # --- cheat codes ---
    ini_src = repo / "work" / "mg_userdir" / "GameSettings" / "GRSEAF.ini"
    if ini_src.exists():
        shutil.copy2(ini_src,
                     out_dir / "userdata" / "GameSettings" / "GRSEAF.ini")
        ok("Copied cheat codes (GRSEAF.ini).")

    # --- credits / readme ---
    for name in ("CREDITS.txt", "README.txt"):
        src = repo / "dist" / "RingOut-1.0-dist" / name
        if src.exists():
            shutil.copy2(src, out_dir / name)

    # --- bundled toolchain (clang, cmake, ninja, python) ---
    _bundle_toolchain(tools, out_dir)

    ok(f"Package assembled at: {out_dir}")


def _bundle_toolchain(tools: ToolSet, out_dir: Path):
    """Copy the minimal toolchain needed by setup.ps1 into toolchain/."""
    tc = out_dir / "toolchain"

    # clang / clang++ / lld
    if tools.clang:
        clang_bin = Path(tools.clang).parent
        llvm_root = clang_bin.parent
        if (llvm_root / "bin").exists() and (llvm_root / "lib").exists():
            info("Bundling llvm-mingw toolchain ...")
            for sub in ("bin", "lib", "include", "x86_64-w64-mingw32"):
                src = llvm_root / sub
                if src.exists():
                    shutil.copytree(src, tc / sub, dirs_exist_ok=True)
            ok("Bundled llvm-mingw.")
        else:
            shutil.copy2(tools.clang, tc / "bin" / "clang.exe")
            if tools.clangxx and Path(tools.clangxx).exists():
                shutil.copy2(tools.clangxx, tc / "bin" / "clang++.exe")
            warn("System clang copied - module builds may need extra libs.")

    # cmake
    if tools.cmake:
        cmake_bin  = Path(tools.cmake).parent
        cmake_root = cmake_bin.parent
        share = cmake_root / "share"
        if share.exists():
            shutil.copytree(cmake_bin, tc / "bin", dirs_exist_ok=True)
            shutil.copytree(share, tc / "share", dirs_exist_ok=True)
        else:
            shutil.copy2(tools.cmake, tc / "bin" / "cmake.exe")
        ok("Bundled cmake.")

    # ninja
    if tools.ninja:
        shutil.copy2(tools.ninja, tc / "bin" / "ninja.exe")
        ok("Bundled ninja.")

    # python embedded
    if tools.python:
        py_bin = Path(tools.python)
        py_dir = py_bin.parent
        has_stdlib = any(py_dir.glob("python3*.zip"))
        if has_stdlib:
            py_dst = tc / "python"
            ensure_dir(py_dst)
            for item in py_dir.iterdir():
                dst = py_dst / item.name
                if item.is_dir():
                    shutil.copytree(item, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dst)
            ok("Bundled Python (embedded).")
        else:
            shutil.copy2(tools.python, tc / "python" / "python.exe")
            warn("Copied python.exe only (not embedded build).")

# ---------------------------------------------------------------------------
# Stage 5 - Optional: run first-time module setup
# ---------------------------------------------------------------------------

def run_setup(out_dir: Path, iso_path: str, tools: ToolSet):
    step("Running first-time module setup (recompiling your disc)")
    setup_ps1 = out_dir / "setup.ps1"
    if not setup_ps1.exists():
        warn(f"setup.ps1 not found in {out_dir}. Skipping module build.")
        return

    iso = Path(iso_path).resolve()
    if not iso.exists():
        die(f"Disc image not found: {iso}")

    info("This step takes several minutes - compiling 535,000+ PowerPC"
         " instructions to C ...")
    ps_cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
              "-File", str(setup_ps1), str(iso)]
    result = subprocess.run(ps_cmd, cwd=str(out_dir))
    if result.returncode != 0:
        die("setup.ps1 failed. See the output above for details.")

    # Check that a module was produced
    dlls = list((out_dir / "bin").glob("g*_recomp.dll"))
    if dlls:
        ok(f"Module built: {dlls[0].name}")
    else:
        warn("Module DLL not found in bin/ - check setup output above.")

# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------

def print_summary(out_dir: Path, has_iso: bool):
    step("Build complete!")
    exe = out_dir / "RingOut.exe"
    cmd = out_dir / "RingOut.cmd"
    launch = "Double-click  RingOut.exe" if exe.exists() else \
             ("Run  RingOut.cmd" if cmd.exists() else "(check out_dir)")

    log(textwrap.dedent(f"""
    +---------------------------------------------------------+
    |              RingOut Windows build finished              |
    +---------------------------------------------------------+

    Output folder:
      {out_dir}

    To play:
      {launch}
    """), GREEN if has_iso else YELLOW)

    if not has_iso:
        log(textwrap.dedent(f"""
    You did NOT supply a disc image (--iso), so the game module was NOT built.

    On first launch RingOut.exe will ask you to select your GameCube disc image.
    Alternatively, run setup manually:
      powershell -ExecutionPolicy Bypass -File "{out_dir}\\setup.ps1" "C:\\path\\to\\game.iso"
        """), YELLOW)

    log("Controls (in-game):", CYAN)
    log("  Escape = Settings menu | Alt+Enter = fullscreen | F1-F8 = load state")
    log("  Shift+F1-F8 = save state | Alt+W = widescreen toggle\n")

# ---------------------------------------------------------------------------
# Argument parsing + main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Build RingOut (SoulCalibur II static recomp) for Windows",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__,
    )
    p.add_argument("--iso",         metavar="PATH",
                   help="Path to your SC2 GameCube disc image (.iso / .wbfs)")
    p.add_argument("--out",         metavar="DIR", default=None,
                   help="Output directory (default: RingOut-windows beside this script)")
    p.add_argument("--skip-deps",   action="store_true",
                   help="Assume all build tools are on PATH; skip downloads")
    p.add_argument("--rebuild",     action="store_true",
                   help="Delete cached build directories and rebuild")
    p.add_argument("--no-launcher", action="store_true",
                   help="Skip building the native RingOut.exe launcher")
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 4,
                   metavar="N", help="Parallel compile jobs")
    p.add_argument("--log",  metavar="FILE", default=None,
                   help="Also write all output to this log file (plain text, no ANSI codes)")
    return p.parse_args()

def _install_log_tee(log_path):
    """Redirect stdout + stderr through a Tee that also writes to a log file."""
    class _Tee:
        def __init__(self, *streams):
            self.streams = streams
        def write(self, text):
            for s in self.streams:
                s.write(text)
        def flush(self):
            for s in self.streams:
                try:
                    s.flush()
                except Exception:
                    pass
    log_dir = Path(log_path).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    lf = open(log_path, 'w', encoding='utf-8')
    # Strip ANSI colour codes before writing to the log file
    class _StripAnsi:
        def __init__(self, fh):
            self.fh = fh
        def write(self, text):
            self.fh.write(ANSI_RE.sub('', text))
        def flush(self):
            self.fh.flush()
    stripped = _StripAnsi(lf)
    sys.stdout = _Tee(sys.stdout, stripped)
    sys.stderr = _Tee(sys.stderr, stripped)

def main():
    args = parse_args()

    if sys.platform != "win32":
        die("This script targets Windows only.\n"
            "On Linux/Mac use  ./setup.sh  from the dist/ directory instead.")

    # Optional log file (also used by the GUI)
    if args.log:
        _install_log_tee(args.log)

    log(r"""
  ____  _             ___        _
 |  _ \(_)_ __   __ _/ _ \ _   _| |_
 | |_) | | '_ \ / _` | | | | | | | __|
 |  _ <| | | | | (_| | |_| | |_| | |_
 |_| \_\_|_| |_|\__, |\___/ \__,_|\__|
                |___/  Windows Builder
    """, CYAN)

    # Paths
    script_dir = Path(__file__).resolve().parent
    work_dir   = script_dir / "_ringout_build"
    ensure_dir(work_dir)

    out_dir    = Path(args.out).resolve() if args.out else script_dir / "RingOut-windows"
    tools_dir  = work_dir / "tools"
    dl_dir     = work_dir / "downloads"
    repo_dir   = work_dir / REPO_DIR_NAME
    build_root = work_dir / "build"

    ensure_dir(tools_dir)
    ensure_dir(dl_dir)
    ensure_dir(build_root)
    ensure_dir(out_dir)

    tools = ToolSet(tools_dir)

    # --- dependencies ---
    step("Checking / installing dependencies")
    ensure_git(tools,        dl_dir, args.skip_deps)
    ensure_cmake(tools,      dl_dir, args.skip_deps)
    ensure_ninja(tools,      dl_dir, args.skip_deps)
    ensure_llvm_mingw(tools, dl_dir, args.skip_deps)
    ensure_python(tools,     dl_dir)
    check_vulkan()
    ensure_msvc(tools,        dl_dir, args.skip_deps)

    # --- source ---
    step("Fetching RingOut source")
    clone_or_update_repo(repo_dir, tools)

    # --- builds ---
    runtime_exe, launcher_exe, port_exe = \
        build_moderngekko(repo_dir, build_root, tools, args.rebuild, args.jobs)
    dolrecomp_exe = build_dolrecomp(repo_dir, build_root, tools, args.rebuild, args.jobs)
    if args.no_launcher:
        launcher_exe = None

    # --- assemble ---
    assemble_package(repo_dir, out_dir, runtime_exe, dolrecomp_exe,
                     launcher_exe, port_exe, build_root, tools)

    # --- optional disc setup ---
    if args.iso:
        run_setup(out_dir, args.iso, tools)

    # --- done ---
    print_summary(out_dir, has_iso=bool(args.iso))

if __name__ == "__main__":
    main()
