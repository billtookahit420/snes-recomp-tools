#!/usr/bin/env python3
"""SNESRecomp Tools engine — UI-free core.

Converts Super Nintendo / Super Famicom ROMs to native PC executables
with snesrecomp (RetroPortingToolKit; Matthew Stanley / mstan): probe
(SNES cartridge header) -> scaffold (the framework's own
tools/new_project/setup_project.sh, which seeds the analysis config,
pins the framework + recomp-ui submodules, fetches box art and
generates C with the Rust analyzer) -> build (Linux / Windows) ->
verify -> package. Driven by the CLI:

    engine.py toolbox
    engine.py probe --rom game.sfc
    engine.py run --job job.json

Job JSON keys:
    rom          path to the .sfc / .smc file
    title        display title (default: probed display name)
    output_dir   where packages + the project folder land
    targets      ["linux"], ["windows"] or both (default both)
    jobs         parallel build jobs (default 2)
    players      launcher seats 1-8 (default: 2)
    force        build even if the probe flags needs-review
    regen        delete and re-scaffold the generated project
    netplay      wire the framework's recomp-net delay-sync + rollback
                 into the project (default false). EXPERIMENTAL: the
                 scaffold wires it, but two-instance gameplay has not
                 been verified end-to-end for converter-built games —
                 see README.md. Never described as working until it is
                 seen working.
    ra           include RetroAchievements (default true): the
                 bundled module (bundle/ra/) is copied into the
                 generated project, src/main.c is patched to fill
                 the desktop host's hook fields with it, and a
                 managed CMake region compiles it into the target.
                 Login toast + unlock pop-ups ride the host's own
                 OSD (snes_osd_push). If the bundle is missing the
                 run fails loudly — a package never silently ships
                 without the RA it was asked for.
    ra_popups    in-game achievement pop-ups + login toast (default
                 true; false compiles RA_DISABLE_POPUPS)
    ra_toml      path to a filled retroachievements.toml to ship
                 beside the exe (plus the .example, always)
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
FRAMEWORK_CACHE = APP_DIR / "framework-cache"
ART_CACHE = APP_DIR / "art-cache"
RA_BUNDLE = APP_DIR / "bundle" / "ra"
TOOLS_DIR = Path(os.environ.get(
    "SNES_TOOLS_DIR", APP_DIR / "tools"))

SNESRECOMP_GIT = "https://github.com/RetroPortingToolKit/snesrecomp.git"

# libretro-thumbnails, GitHub mirror first (the canonical host has
# been unreachable from some networks for whole evenings), canonical
# second — same dual-host policy as the other converter apps.
BOXART_HOSTS = [
    "https://raw.githubusercontent.com/libretro-thumbnails/Nintendo_-_Super_Nintendo_Entertainment_System/master/Named_Boxarts/",
    "https://thumbnails.libretro.com/Nintendo%20-%20Super%20Nintendo%20Entertainment%20System/Named_Boxarts/",
]


def log_line(log, msg):
    if log:
        log(msg)
    else:
        print(msg, flush=True)


def _run(cmd, log, cwd=None, timeout=None, env=None):
    log_line(log, "$ " + " ".join(str(c) for c in cmd))
    proc = subprocess.Popen([str(c) for c in cmd], cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    try:
        for line in proc.stdout:
            log_line(log, line.rstrip())
    finally:
        proc.wait(timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): "
                           f"{' '.join(str(c) for c in cmd)}")
    return proc.returncode


# --------------------------------------------------------------------------
# Rust toolchain (the analyzer is a Rust binary; rustup under
# ~/workspace/tools survives VM restarts, /usr packages do not)
# --------------------------------------------------------------------------

def _rust_env():
    """Environment for subprocesses, with the discovered tool
    dirs reachable: cargo for the analyzer, and the tools dir
    itself (portable ninja) plus its MinGW bin for the builds.
    Without this, a launch environment whose PATH lacks the
    tools dir configures fine in the toolbox check (which
    searches TOOLS_DIR explicitly) but then fails at
    `cmake -G Ninja` with "unable to find a build program" —
    caught by the clean-room run."""
    env = dict(os.environ)
    candidates = []
    if env.get("CARGO_HOME"):
        candidates.append(Path(env["CARGO_HOME"]) / "bin")
    candidates.append(TOOLS_DIR / "cargo" / "bin")
    candidates.append(APP_DIR / "tools" / "cargo" / "bin")
    candidates.append(Path.home() / "workspace" / "tools" / "cargo"
                      / "bin")
    candidates.append(Path.home() / ".cargo" / "bin")
    path = env.get("PATH", "")
    for cand in candidates:
        if (cand / "cargo").exists() and str(cand) not in path.split(os.pathsep):
            path = str(cand) + os.pathsep + path
    for cand, marker in [(TOOLS_DIR, "ninja"),
                         (TOOLS_DIR / "mingw64" / "bin",
                          "x86_64-w64-mingw32-gcc")]:
        if (cand / marker).exists() and str(cand) not in path.split(os.pathsep):
            path = str(cand) + os.pathsep + path
    env["PATH"] = path
    for base in (TOOLS_DIR, APP_DIR / "tools",
                 Path.home() / "workspace" / "tools"):
        if "RUSTUP_HOME" not in env and (base / "rustup").is_dir():
            env["RUSTUP_HOME"] = str(base / "rustup")
        if "CARGO_HOME" not in env and (base / "cargo").is_dir():
            env["CARGO_HOME"] = str(base / "cargo")
    return env


def _cargo_version(env):
    try:
        out = subprocess.run(["cargo", "--version"], capture_output=True,
                             text=True, timeout=30, env=env)
        if out.returncode == 0:
            m = re.search(r"cargo (\d+)\.(\d+)", out.stdout)
            if m:
                return (int(m.group(1)), int(m.group(2))), out.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _download(url, dest, log):
    """Fetch url to dest (curl when present — it honours the
    proxy env on every platform — else urllib). Atomic via a
    .part rename so a half-download never looks complete."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    if shutil.which("curl"):
        _run(["curl", "-fL", "--retry", "3", "-o", str(part), url], log,
             timeout=1200)
    else:
        log_line(log, f"downloading {url}")
        with urllib.request.urlopen(url, timeout=120) as resp, \
                open(part, "wb") as fh:
            shutil.copyfileobj(resp, fh)
    part.replace(dest)


def ensure_rust(log):
    """Return an env with a working cargo >= 1.85, installing
    Rust via rustup into the app's own tools dir when it is
    missing or too old — no admin rights needed, so a first
    conversion on a fresh machine just works (field report
    2026-11-03: a whole queue failed because Rust was absent
    and the app only *mentioned* it in the toolbox notes)."""
    env = _rust_env()
    ver, ver_str = _cargo_version(env)
    if ver and ver >= (1, 85):
        log_line(log, f"rust: {ver_str}")
        return env
    log_line(log, "Rust (cargo >= 1.85) not found — installing it "
                  "with rustup into the app's tools folder; this is "
                  "a one-time download…")
    if sys.platform.startswith("win"):
        url = ("https://static.rust-lang.org/rustup/dist/"
               "x86_64-pc-windows-msvc/rustup-init.exe")
        init = TOOLS_DIR / "rustup-init.exe"
    else:
        url = ("https://static.rust-lang.org/rustup/dist/"
               "x86_64-unknown-linux-gnu/rustup-init")
        init = TOOLS_DIR / "rustup-init"
    _download(url, init, log)
    if not sys.platform.startswith("win"):
        init.chmod(0o755)
    inst_env = dict(os.environ)
    inst_env["RUSTUP_HOME"] = str(TOOLS_DIR / "rustup")
    inst_env["CARGO_HOME"] = str(TOOLS_DIR / "cargo")
    _run([str(init), "-y", "--no-modify-path", "--profile",
          "minimal", "--default-toolchain", "stable"], log,
         timeout=1800, env=inst_env)
    env = _rust_env()
    ver, ver_str = _cargo_version(env)
    if not (ver and ver >= (1, 85)):
        raise RuntimeError(
            "Rust auto-install finished but cargo >= 1.85 is still "
            "not reachable — install Rust manually from "
            "https://rustup.rs and re-run.")
    log_line(log, f"rust installed: {ver_str}")
    return env


def ensure_system_tools(log, targets):
    """Best-effort auto-install of the system build tools a
    conversion needs (cmake, ninja, C/C++ compilers, git,
    pkg-config, MinGW for Windows packages) plus — for the Linux
    target — the system development libraries the SDL3 build
    probes with pkg-config (see LINUX_BUILD_LIBS). User-level
    installs (Rust) happen in ensure_rust; these need apt, so
    they install only when passwordless sudo is available —
    otherwise the exact, complete command is logged for the
    user to run once (the GUI's "Install missing packages"
    button covers the same set via pkexec)."""
    env = _rust_env()
    path = env.get("PATH", "")
    want = ["cmake", "ninja", "gcc", "g++", "git", "pkg-config"]
    if "windows" in targets:
        want += ["x86_64-w64-mingw32-gcc", "x86_64-w64-mingw32-g++"]
    missing = [t for t in want
               if not shutil.which(t, path=path)]
    lib_pkgs = missing_linux_build_libs(env) \
        if "linux" in targets else []
    if not missing and not lib_pkgs:
        return
    pkgs = sorted({APT_PACKAGES[t] for t in missing} | set(lib_pkgs))
    if missing:
        log_line(log, "missing build tools: " + ", ".join(missing))
    if lib_pkgs:
        log_line(log, "missing Linux build libraries: "
                      + ", ".join(lib_pkgs))
    apt = shutil.which("apt-get")
    sudo_ok = False
    if apt and shutil.which("sudo"):
        try:
            sudo_ok = subprocess.run(
                ["sudo", "-n", "true"], capture_output=True,
                timeout=15).returncode == 0
        except Exception:  # noqa: BLE001
            sudo_ok = False
    if apt and sudo_ok:
        log_line(log, "installing them with apt (one-time)…")
        _run(["sudo", "-n", "apt-get", "install", "-y",
              "--no-install-recommends"] + pkgs, log, timeout=1800)
        still = [t for t in missing if not shutil.which(t, path=path)]
        still += missing_linux_build_libs(env) \
            if "linux" in targets else []
        if still:
            log_line(log, "still missing after apt: "
                          + ", ".join(sorted(set(still))))
    else:
        log_line(log, "install them once with:  sudo apt install "
                      + " ".join(pkgs)
                      + "  (or use the app's \"Install missing "
                        "packages\" button; the app installs them "
                        "itself when passwordless sudo is "
                        "available)")


# --------------------------------------------------------------------------
# Probe — mirrors the framework's own tools/new_project/probe_rom.py
# (same header logic, same coprocessor table) so the converter's
# verdict is the framework's verdict, not a second opinion.
# --------------------------------------------------------------------------

REGIONS = {
    0x00: ("JPN", "Japan"), 0x01: ("USA", "North America"),
    0x02: ("EUR", "Europe"), 0x03: ("SWE", "Sweden / Scandinavia"),
    0x04: ("FIN", "Finland"), 0x05: ("DNK", "Denmark"),
    0x06: ("FRA", "France"), 0x07: ("NLD", "Netherlands"),
    0x08: ("ESP", "Spain"), 0x09: ("DEU", "Germany"),
    0x0A: ("ITA", "Italy"), 0x0B: ("CHN", "China"),
    0x0C: ("IDN", "Indonesia"), 0x0D: ("KOR", "South Korea"),
    0x0F: ("CAN", "Canada"), 0x10: ("BRA", "Brazil"),
    0x11: ("AUS", "Australia"),
}

# Cartridge-type byte -> coprocessor, copied from the framework's
# probe_rom.py COPROCESSORS table.
COPROCESSORS = {
    0x00: "none", 0x01: "none", 0x02: "none",
    0x03: "DSP", 0x04: "DSP", 0x05: "DSP",
    0x13: "SuperFX", 0x14: "SuperFX", 0x15: "SuperFX", 0x1A: "SuperFX",
    0x25: "OBC1",
    0x32: "SA-1", 0x34: "SA-1", 0x35: "SA-1", 0x36: "SA-1",
    0x43: "S-DD1", 0x45: "S-DD1",
    0x55: "S-RTC",
    0xE3: "Super Game Boy",
    0xF3: "CX4",
    0xF5: "SPC7110 / ST018",
    0xF6: "ST010 / ST011",
    0xF9: "SPC7110",
}

# What the framework's mapper/chip layer actually implements (README
# compatibility table, checked 2026-11-02): standard LoROM/HiROM,
# Cx4, Super FX, DSP-1, SA-1. Everything else is flagged up front
# instead of failing partway through a build.
SUPPORTED_CHIPS = {"none", "CX4", "SuperFX", "SA-1"}


def _header_score(data, base, expected_low_nibble):
    """Mirror of snes65816._header_score."""
    if base < 0 or base + 0x40 > len(data):
        return -1
    score = 0
    if (data[base + 0x15] & 0x0F) == expected_low_nibble:
        score += 4
    reset = data[base + 0x3C] | (data[base + 0x3D] << 8)
    if reset >= 0x8000 and reset != 0xFFFF:
        score += 2
    complement = data[base + 0x1C] | (data[base + 0x1D] << 8)
    checksum = data[base + 0x1E] | (data[base + 0x1F] << 8)
    if (checksum ^ complement) == 0xFFFF:
        score += 2
    return score


def _detect_mapping(data):
    """Mirror of snes65816.detect_rom_mapping."""
    if len(data) >= 0x8000 and data[0x7FD5] == 0x23 \
            and data[0x7FD6] in (0x34, 0x35):
        return "sa1"
    lorom_score = _header_score(data, 0x7FC0, 0)
    hirom_score = _header_score(data, 0xFFC0, 1)
    if hirom_score > lorom_score:
        return "hirom"
    if len(data) > 0x400000:
        return "sdd1_exlorom"
    return "lorom"


def _clean_title(raw_title):
    text = raw_title.decode("ascii", errors="replace")
    text = "".join(ch if 0x20 <= ord(ch) < 0x7F else " " for ch in text)
    return " ".join(text.split())


def _clean_filename_title(stem):
    text = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", stem)
    text = text.replace("_", " ")
    return " ".join(text.split()).strip(" -")


def _display_name(header_title, stem):
    from_file = _clean_filename_title(stem)
    if from_file and (" " in from_file or "-" in from_file):
        return from_file
    if not header_title:
        return from_file or stem
    if header_title.isupper() and len(header_title) > 3:
        return header_title.title()
    return header_title


def _slug_title(display):
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", display.title())
    return cleaned or "SnesGame"


def probe_rom(rom_path):
    """Parse a SNES cartridge header. Instant; no framework needed.
    Digests are of the normalized image (512-byte copier header
    stripped), matching No-Intro and the framework's probe."""
    p = Path(rom_path)
    raw_file = p.read_bytes()
    if len(raw_file) < 0x8000:
        raise ValueError("not a SNES ROM (smaller than one bank)")
    had_header = len(raw_file) % 1024 == 512
    data = raw_file[512:] if had_header else raw_file
    mapping = _detect_mapping(data)
    base = 0x7FC0 if mapping in ("lorom", "sdd1_exlorom") else 0xFFC0
    if mapping == "sa1":
        base = 0x7FC0
    def byte(off):
        return data[base + off] if base + off < len(data) else 0
    def u16(off):
        return byte(off) | (byte(off + 1) << 8)
    header_title = _clean_title(data[base:base + 21])
    cart_type = byte(0x16)
    rom_size_byte = byte(0x17)
    ram_size_byte = byte(0x18)
    region_byte = byte(0x19)
    region_code, region_name = REGIONS.get(region_byte, ("UNK", "unknown"))
    checksum = u16(0x1E)
    complement = u16(0x1C)
    display = _display_name(header_title, p.stem)
    probe = {
        "rom": str(p),
        "title": p.stem,
        "display_name": display,
        "project_name": _slug_title(display) + "SNESRecomp",
        "header_title": header_title,
        "mapping": mapping,
        "map_mode": byte(0x15),
        "cart_type": cart_type,
        "coprocessor": COPROCESSORS.get(cart_type, "unknown"),
        "rom_size_kb": (1 << rom_size_byte) if 0 < rom_size_byte < 24 else 0,
        "sram_size_kb": (1 << ram_size_byte) if 0 < ram_size_byte < 24 else 0,
        "region_byte": region_byte,
        "region": region_code,
        "region_name": region_name,
        "checksum": "%04x" % checksum,
        "checksum_valid": (checksum ^ complement) == 0xFFFF,
        "reset_vector": "%04x" % u16(0x3C),
        "size": len(raw_file),
        "normalized_size": len(data),
        "had_copier_header": had_header,
        "crc32": "%08x" % (zlib.crc32(data) & 0xFFFFFFFF),
        "md5": hashlib.md5(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    probe["needs_review"], probe["review_reasons"] = _review_reasons(probe)
    return probe


def _review_reasons(probe):
    """Flag, up front, the variants the framework refuses or has not
    implemented, so a conversion never dies halfway through a build.
    Returns (needs_review, [reasons]). Probe-level only: passing says
    the cartridge type is implemented, not that the game has been
    validated end-to-end (every title still needs its own analysis
    and play check — that is the framework's model)."""
    reasons = []
    chip = probe["coprocessor"]
    mapping = probe["mapping"]
    if mapping == "sdd1_exlorom":
        reasons.append("ExLoROM mapping: the framework's mapper layer "
                       "does not support ExLoROM (the S-DD1 MMC path is "
                       "experimental and title-gated to Star Ocean)")
    if mapping == "hirom" and probe["normalized_size"] > 0x400000:
        reasons.append("image larger than 4 MiB with a HiROM header: "
                       "that is ExHiROM, which the framework's mapper "
                       "layer does not support yet")
    if chip == "DSP":
        reasons.append("DSP cartridge: the framework implements DSP-1 "
                       "only (validated on Super Mario Kart); DSP-2, "
                       "DSP-3 and DSP-4 are not supported, and the "
                       "header byte alone does not say which DSP a "
                       "cartridge carries — verify the title before "
                       "building")
    elif chip == "S-DD1":
        reasons.append("S-DD1 chip: support is experimental and "
                       "title-gated (Star Ocean validation); treat as "
                       "unsupported for a general conversion")
    elif chip not in SUPPORTED_CHIPS:
        reasons.append(f"coprocessor '{chip}' (cartridge type "
                       f"0x{probe['cart_type']:02X}) is not implemented "
                       "by the framework's mapper layer")
    if not probe["checksum_valid"]:
        reasons.append("header checksum/complement mismatch — the "
                       "image may be modified, over-dumped, or not a "
                       "plain SNES ROM (the framework's own probe "
                       "warns on this too)")
    return bool(reasons), reasons


# --------------------------------------------------------------------------
# Cover art (libretro-thumbnails, both hosts, retries — same policy as
# the other converter apps, plus No-Intro name variants)
# --------------------------------------------------------------------------

def _fetch_bytes_retry(url, tries=3, timeout=20):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "snes-recomp-tools"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise  # a missing name is definitive; retrying can't help
            last = e
            time.sleep(1.5 * (i + 1))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def _valid_cover(data):
    if not data or len(data) < 8000:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        import struct
        if len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return w >= 200 and h >= 200
        return True
    return data[:3] == b"\xff\xd8\xff"


def _cover_name_variants(title):
    """No-Intro names for SNES art sometimes differ from a dump's file
    name in the region group or revision tag; try the plausible forms
    before concluding the art is absent (the GB Tetris DX lesson:
    check tag variants before declaring art missing)."""
    variants = [title]
    no_brackets = re.sub(r"\s*\[[^\]]*\]", "", title).strip()
    if no_brackets != title:
        variants.append(no_brackets)
    norm = re.sub(r"\((JUE|JU|UE|U|J)\)", "(World)", no_brackets)
    norm = re.sub(r"\(V(\d+)\.(\d+)\)", r"(Rev \2)", norm)
    if norm != no_brackets:
        variants.append(norm)
    core = title.split(" (")[0].strip()
    if core and core not in variants:
        variants.append(core)
    seen, out = set(), []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def fetch_cover(title, log=None):
    """Box art for a game title (the queue/job title, usually the
    ROM's file stem, which is the No-Intro name). Returns the cached
    PNG path or None."""
    ART_CACHE.mkdir(parents=True, exist_ok=True)
    dest = ART_CACHE / (title + ".png")
    if dest.exists() and _valid_cover(dest.read_bytes()):
        return dest
    for name in _cover_name_variants(title):
        url_name = urllib.parse.quote(name + ".png")
        for host in BOXART_HOSTS:
            try:
                data = _fetch_bytes_retry(host + url_name)
            except Exception as e:  # noqa: BLE001
                log_line(log, f"cover: {host.split('/')[2]} failed ({e})")
                continue
            if _valid_cover(data):
                dest.write_bytes(data)
                log_line(log, f"cover: fetched {name}.png "
                              f"({len(data)} bytes)")
                return dest
            log_line(log, f"cover: {host.split('/')[2]} returned an "
                          f"invalid image for {name}.png")
    log_line(log, f"cover: no box art found for {title}")
    return None


# --------------------------------------------------------------------------
# Toolbox
# --------------------------------------------------------------------------

APT_PACKAGES = {
    "cmake": "cmake",
    "ninja": "ninja-build",
    "gcc": "build-essential",
    "g++": "build-essential",
    "git": "git",
    "pkg-config": "pkg-config",
    "x86_64-w64-mingw32-gcc": "gcc-mingw-w64-x86-64",
    "x86_64-w64-mingw32-g++": "g++-mingw-w64-x86-64",
}

# pkg-config module -> Ubuntu dev package, for the Linux-native
# build. The framework fetches and builds SDL3 itself, but SDL3's
# CMake probes the system for these development libraries (X11 +
# extensions, xkbcommon, DRM/GBM, EGL/GL, audio, udev, dbus,
# Wayland, libdecor). When one is missing the Linux configure dies
# minutes in under a wall of CMake output — the 2026-11-05 field
# failure, where only the Windows zip appeared because the Windows
# build is self-contained and the Linux failure was invisible.
# They are checked up front so a gap is a list of named packages,
# not a dead build.
LINUX_BUILD_LIBS = [
    ("x11", "libx11-dev"),
    ("xext", "libxext-dev"),
    ("xrandr", "libxrandr-dev"),
    ("xcursor", "libxcursor-dev"),
    ("xi", "libxi-dev"),
    ("xfixes", "libxfixes-dev"),
    ("xscrnsaver", "libxss-dev"),
    ("xtst", "libxtst-dev"),
    ("xinerama", "libxinerama-dev"),
    ("xxf86vm", "libxxf86vm-dev"),
    ("xkbcommon", "libxkbcommon-dev"),
    ("xkbcommon-x11", "libxkbcommon-x11-dev"),
    ("libdrm", "libdrm-dev"),
    ("gbm", "libgbm-dev"),
    ("egl", "libegl-dev"),
    ("gl", "libgl-dev"),
    ("alsa", "libasound2-dev"),
    ("libpulse", "libpulse-dev"),
    ("libudev", "libudev-dev"),
    ("dbus-1", "libdbus-1-dev"),
    ("wayland-client", "libwayland-dev"),
    ("libdecor-0", "libdecor-0-dev"),
]


def _pkg_config_exists(name, env=None):
    pc = shutil.which("pkg-config",
                      path=(env or os.environ).get("PATH", ""))
    if not pc:
        return False
    try:
        return subprocess.run([pc, "--exists", name],
                              capture_output=True,
                              timeout=15).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def linux_build_lib_status(env=None):
    """[(pkg-config name, apt package, found)] for the system
    libraries the Linux-native build needs. Empty on non-Linux
    hosts (the checks are meaningless there)."""
    if sys.platform.startswith("win"):
        return []
    return [(pc_name, apt, _pkg_config_exists(pc_name, env))
            for pc_name, apt in LINUX_BUILD_LIBS]


def missing_linux_build_libs(env=None):
    """apt packages for the Linux-native build libraries missing
    on this host ([] when complete, or not a Linux host)."""
    return sorted({apt for _, apt, found in linux_build_lib_status(env)
                   if not found})


def toolbox():
    checks = []
    for tool in ["cmake", "ninja", "gcc", "g++", "git", "pkg-config",
                 "x86_64-w64-mingw32-gcc", "x86_64-w64-mingw32-g++"]:
        checks.append({"tool": tool, "found": shutil.which(tool) is not None,
                       "apt": APT_PACKAGES[tool]})
    # System libraries the Linux-native build links against
    # (pkg-config probes; skipped entirely on a Windows host).
    # Their apt packages join missing_apt, so the GUI's
    # "Install missing packages" button (pkexec) covers them.
    for pc_name, apt, found in linux_build_lib_status():
        checks.append({"tool": f"pkg-config {pc_name} (Linux build)",
                       "found": found, "apt": apt})
    env = _rust_env()
    ver, ver_str = _cargo_version(env)
    rust_ok = bool(ver and ver >= (1, 85))
    checks.append({"tool": f"cargo/rustc >= 1.85 for the analyzer "
                           f"({ver_str or 'not found'})",
                   "found": rust_ok, "apt": None})
    py_ok = sys.version_info >= (3, 11)
    checks.append({"tool": f"Python >= 3.11 (have {sys.version.split()[0]})",
                   "found": py_ok, "apt": "python3"})
    try:
        import PIL  # noqa: F401
        pil = True
    except ImportError:
        pil = False
    checks.append({"tool": "Pillow (GUI cover thumbnails)", "found": pil,
                   "apt": "python3-pil"})
    missing = sorted({c["apt"] for c in checks
                      if not c["found"] and c["apt"]})
    notes = []
    if not rust_ok:
        notes.append("Rust is required to build the framework's "
                     "analyzer (recompiler-rs) — no action needed: "
                     "the app installs it automatically with rustup "
                     "into its own tools folder on the first "
                     "conversion.")
    lib_missing = missing_linux_build_libs()
    if lib_missing:
        notes.append("The Linux build also needs system "
                     "development libraries for the SDL3 build "
                     "(X11 extensions, xkbcommon, DRM/GBM, "
                     "EGL/GL, ALSA, PulseAudio, udev, dbus, "
                     "Wayland, libdecor). Missing: "
                     + ", ".join(lib_missing)
                     + " — \"Install missing packages\" installs "
                       "them with one password prompt.")
    return {"checks": checks, "missing_apt": missing,
            "ready": not missing and rust_ok, "notes": notes}


# --------------------------------------------------------------------------
# Framework
# --------------------------------------------------------------------------

def _framework_candidates():
    cands = []
    if os.environ.get("SNESRECOMP_DIR"):
        cands.append(Path(os.environ["SNESRECOMP_DIR"]))
    cands.append(FRAMEWORK_CACHE / "snesrecomp")
    cands.append(Path.home() / "workspace" / "snes-probe")
    return cands


def ensure_framework(log):
    """Locate (or fetch) the snesrecomp checkout the scaffolder runs
    from. The scaffolder pins THIS checkout's commit into each new
    project as a submodule and renders templates from it, so the
    checkout must be complete with its own submodules."""
    for cand in _framework_candidates():
        if (cand / "tools" / "new_project" / "setup_project.sh").exists() \
                and (cand / "recompiler-rs" / "Cargo.toml").exists():
            log_line(log, f"framework: {cand}")
            return cand
    dest = FRAMEWORK_CACHE / "snesrecomp"
    if dest.exists():
        shutil.rmtree(dest)
    log_line(log, "fetching snesrecomp framework…")
    _run(["git", "clone", "--recursive", SNESRECOMP_GIT, str(dest)],
         log, timeout=1800)
    return dest


# --------------------------------------------------------------------------
# RetroAchievements wiring
#
# The framework has no RA code, so the converter carries the module
# (bundle/ra/, ported from the GB tool's bundle) and wires it into
# the generated project the same way the GB engine wires its own:
#
#  - bundle/ra is copied to <project>/ra/.
#  - src/main.c (the title's desktop-host shim) is patched to
#    include ra_hooks.h and fill SnesDesktopHostGame's
#    on_rom_loaded / on_reset / after_run_frame / on_shutdown
#    fields with the module's hooks. The module inits lazily on
#    the first after_run_frame, pumps rc_client there, reads guest
#    memory via the runner's snes_read(g_snes, ...) bus read, and
#    shows the login toast + unlock pop-ups through the host OSD's
#    snes_osd_push — the channel the host's own toasts use.
#  - CMakeLists.txt gets a managed region (marker-guarded,
#    rewritten on every scaffold) compiling ra_extras.cpp +
#    ra_http.c + the vendored rcheevos into the game target.
#
# Both patches are idempotent and fail loudly if the template
# anchors move; flipping the job's ra flag off strips them, so an
# RA build can be cleanly unmade on a reused project.
# --------------------------------------------------------------------------

RA_CMAKE_BEGIN = "# >>> SNESRecomp Tools: RetroAchievements >>>"
RA_CMAKE_END = "# <<< SNESRecomp Tools: RetroAchievements <<<"

RA_CMAKE_TMPL = """{begin}
# Managed by the SNESRecomp Tools engine (bundle/ra). Do not edit
# by hand; the region is rewritten on every scaffold.
if(EXISTS "${{CMAKE_CURRENT_SOURCE_DIR}}/ra/ra_extras.cpp")
    file(GLOB_RECURSE SNES_RA_RCHEEVOS_SOURCES
        "${{CMAKE_CURRENT_SOURCE_DIR}}/ra/ra-rcheevos/src/*.c")
    list(FILTER SNES_RA_RCHEEVOS_SOURCES EXCLUDE REGEX "rc_libretro\\\\.c$")
    list(FILTER SNES_RA_RCHEEVOS_SOURCES EXCLUDE REGEX "rc_client_raintegration\\\\.c$")
    target_sources({exe} PRIVATE
        "${{CMAKE_CURRENT_SOURCE_DIR}}/ra/ra_extras.cpp"
        "${{CMAKE_CURRENT_SOURCE_DIR}}/ra/ra_http.c"
        ${{SNES_RA_RCHEEVOS_SOURCES}})
    target_include_directories({exe} PRIVATE
        "${{CMAKE_CURRENT_SOURCE_DIR}}/ra/ra-rcheevos/include"
        "${{CMAKE_CURRENT_SOURCE_DIR}}/ra")
    if(WIN32)
        target_link_libraries({exe} PRIVATE winhttp)
    else()
        find_package(OpenSSL REQUIRED)
        target_link_libraries({exe} PRIVATE OpenSSL::SSL OpenSSL::Crypto)
    endif()
{popups_def}endif()
{end}
"""

RA_MAIN_MARKERS = ("SNESRT_RA_INC", "SNESRT_RA_HOOKS")


def _ra_bundle_ok():
    return ((RA_BUNDLE / "ra_extras.cpp").exists()
            and (RA_BUNDLE / "ra_hooks.h").exists()
            and (RA_BUNDLE / "ra_http.c").exists()
            and (RA_BUNDLE / "ra-rcheevos" / "include"
                 / "rc_client.h").exists())


def _set_cmake_ra_region(proj, exe_name, popups, enable):
    cmakelists = proj / "CMakeLists.txt"
    text = cmakelists.read_text()
    stripped = re.sub(
        r"(?s)" + re.escape(RA_CMAKE_BEGIN) + r".*?"
        + re.escape(RA_CMAKE_END) + r"\n?", "", text)
    if enable:
        popups_def = "" if popups else \
            f"    target_compile_definitions({exe_name} PRIVATE RA_DISABLE_POPUPS=1)\n"
        region = RA_CMAKE_TMPL.format(
            begin=RA_CMAKE_BEGIN, end=RA_CMAKE_END, exe=exe_name,
            popups_def=popups_def)
        stripped = stripped.rstrip("\n") + "\n\n" + region
    if stripped != text:
        cmakelists.write_text(stripped)


def _patch_main_ra(proj, enable, log):
    main_path = proj / "src" / "main.c"
    original = main_path.read_text()
    lines = [ln for ln in original.split("\n")
             if not any(m in ln for m in RA_MAIN_MARKERS)]
    text = "\n".join(lines)
    if enable:
        text, n = re.subn(r'(?m)^(#include "host_main\.h")$',
                          r'\1\n#include "ra_hooks.h" /* SNESRT_RA_INC */',
                          text, count=1)
        if n != 1:
            raise RuntimeError("src/main.c has no #include \"host_main.h\" "
                               "anchor for the RA hook include")
        hooks = (r"\1\n"
                 r"    .on_rom_loaded       = snes_ra_on_rom_loaded,"
                 r" /* SNESRT_RA_HOOKS */\n"
                 r"    .on_reset            = snes_ra_on_reset,"
                 r" /* SNESRT_RA_HOOKS */\n"
                 r"    .after_run_frame     = snes_ra_after_run_frame,"
                 r" /* SNESRT_RA_HOOKS */\n"
                 r"    .on_shutdown         = snes_ra_on_shutdown,"
                 r" /* SNESRT_RA_HOOKS */")
        text, n = re.subn(r'(?m)^(\s*\.game_info\s*=.*)$', hooks,
                          text, count=1)
        if n != 1:
            raise RuntimeError("src/main.c has no .game_info anchor "
                               "for the RA hook fields")
        log_line(log, "patched src/main.c: RetroAchievements hooks")
    if text != original:
        main_path.write_text(text)


def _wire_ra(job, proj, proj_name, enable, log):
    """Copy the RA bundle into the project and patch src/main.c +
    CMakeLists. With enable=False the patches are stripped, so
    flipping the job's ra flag off cleanly unmakes an RA build."""
    if enable:
        dest = proj / "ra"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(RA_BUNDLE, dest)
        log_line(log, "RA bundle copied into the project (bundle/ra)")
    _set_cmake_ra_region(proj, proj_name,
                         popups=job.get("ra_popups", True), enable=enable)
    _patch_main_ra(proj, enable, log)


# --------------------------------------------------------------------------
# Project scaffold / build / verify / package
# --------------------------------------------------------------------------

TOOLCHAIN_MINGW = """set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_C_COMPILER x86_64-w64-mingw32-gcc)
set(CMAKE_CXX_COMPILER x86_64-w64-mingw32-g++)
set(CMAKE_RC_COMPILER x86_64-w64-mingw32-windres)
set(CMAKE_FIND_ROOT_PATH /usr/x86_64-w64-mingw32)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
"""

README_TMPL = """{title} — native PC recompilation (SNESRecomp)

HOW TO PLAY
  Put your own ROM dump next to {exe} (the exact .sfc/.smc this
  build was made from — the launcher checks its digest and only
  accepts that ROM; no ROM is included). Run {exe}: the launcher
  opens first, press PLAY.

CONTROLS
  Keyboard and gamepads are configured in the launcher. The
  in-game menu (save states, rewind, display and audio options)
  is described on the launcher's Help / settings pages.

FEATURE STATUS FOR THIS BUILD
  Cover art: {cover_status}
  Online netplay: {netplay_status}
  RetroAchievements: {ra_status}

CREDITS
  Built with snesrecomp by Matthew Stanley (mstan) /
  RetroPortingToolkit (retroportingtoolkit.com;
  github.com/mstan, github.com/RetroPortingToolKit).
  Launcher: recomp-ui (MIT, (c) 2026 Matthew Stanley).
  snesrecomp license: PolyForm Noncommercial 1.0.0. The runner
  statically links components under their own licenses; see
  THIRD_PARTY_ATTRIBUTION.md in the converter's framework for
  the full notices (LakeSnes / snesrev, MIT; ares-derived
  coprocessor cores, ISC).
"""

NETPLAY_ON = ("WIRED BY THE SCAFFOLD (--netplay: recomp-net, "
              "rollback sessions). Both players need the same "
              "build and the same ROM. Status: sessions start "
              "(verified two-instance on loopback), but a "
              "connected, synchronized match has NOT been "
              "demonstrated yet — the converter's build sandbox "
              "blocks outgoing UDP, so the two instances there "
              "could never exchange a packet. Treat netplay as "
              "experimental until a synced match has been seen "
              "on real machines. Direct-IP play without the "
              "lobby: SNES_NETPLAY=1 with SNES_NET_SLOT=0/1 and "
              "a SNES_NET_BIND / SNES_NET_PEER host:port pair; "
              "launch both sides cold (sessions refuse save "
              "states, pause menus and input scripts).")
NETPLAY_OFF = ("off for this build (the converter was run without "
               "the netplay option).")
COVER_OK = "included (launcher box art + artwork folder)."
COVER_MISSING = "not found in the libretro thumbnail sets for this title."

RA_ON = """included. The game logs in at launch and shows a
  "RetroAchievements / Logged in" toast in-game; achievements
  pop up in-game as they unlock. Put retroachievements.toml
  next to the executable — a filled copy ships in this package
  when the converter was given one; otherwise copy
  retroachievements.toml.example, fill in your username and
  Connect API token, and restart. NOTE: RetroAchievements
  counts hardcore unlocks only from approved clients; this
  build is an unknown emulator to it, so unlocks register as
  Casual/softcore."""

RA_OFF = """not included in this build (the converter was run
  with RetroAchievements off)."""


MARKER_NAME = ".snesrecomp-tools.json"


def _norm_stem(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _stem_of(name):
    s = _norm_stem(name)
    return s[:-len("snesrecomp")] if s.endswith("snesrecomp") else s


def _own_stems(probe, rom_stem):
    """Normalized directory-name stems that may belong to THIS
    game: the scaffolder slugs either the display title or the
    raw ROM filename (it keeps a title-cased region group, so
    "Super Mario World (USA)" can become SuperMarioWorldUsa…),
    and the probe's predicted name derives from the display
    title. Any other game's stem is a different set — ActRaiser
    and ActRaiser 2 stay distinct because stems compare whole."""
    stems = {_stem_of(_slug_title(probe["display_name"])),
             _stem_of(probe["project_name"])}
    if rom_stem:
        stems.add(_stem_of(_slug_title(rom_stem)))
    return {s for s in stems if s}


def _gen_populated(proj):
    """Generated C actually present — the check the build itself
    makes. (A src/gen holding only subdirectories or a manifest
    still fails the build with 'src/gen is empty'.)"""
    gen = proj / "src" / "gen"
    return gen.is_dir() and any(gen.glob("*.c"))


def _is_own_project(d, probe, rom_stem):
    if not d.name.endswith("SNESRecomp"):
        return False
    if _stem_of(d.name) not in _own_stems(probe, rom_stem):
        return False
    marker = d / MARKER_NAME
    if marker.exists():
        try:
            info = json.loads(marker.read_text())
            return info.get("crc32") == probe["crc32"]
        except Exception:  # noqa: BLE001
            return False
    return True


def _write_marker(proj, probe):
    try:
        (proj / MARKER_NAME).write_text(json.dumps(
            {"crc32": probe["crc32"], "title": probe["display_name"]},
            indent=2))
    except Exception:  # noqa: BLE001
        pass


def _find_project(out_dir, probe, rom_stem=None):
    """Locate THIS game's generated project under out_dir — and
    only this game's. The engine used to fall back to "the single
    *SNESRecomp dir with a populated src/gen", which in a shared
    output folder handed EVERY queued game the first converted
    game's project (field failure 2026-11-03: a whole queue
    'reused' Castlevania Dracula X's project and failed on its
    empty src/gen). Discover by identity instead: directory name
    must slug-match this game, a marker written at scaffold time
    must name this ROM's crc32 when present, and src/gen must
    hold generated C."""
    hits = [d for d in sorted(out_dir.glob("*SNESRecomp"))
            if d.is_dir() and _is_own_project(d, probe, rom_stem)
            and _gen_populated(d)]
    if not hits:
        return None
    named = out_dir / probe["project_name"]
    return named if named in hits else hits[0]


def scaffold_project(job, probe, fw_dir, log):
    out_dir = Path(job["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    title = job.get("title") or probe["display_name"]
    rom_stem = Path(job["rom"]).stem
    if job.get("regen"):
        for d in sorted(out_dir.glob("*SNESRecomp")):
            if d.is_dir():
                shutil.rmtree(d)
    proj = _find_project(out_dir, probe, rom_stem)
    if proj is not None:
        log_line(log, f"reusing generated project: {proj}")
        (proj / "tc-mingw.cmake").write_text(TOOLCHAIN_MINGW)
        _write_marker(proj, probe)
        return proj, title, proj.name
    # Drop this game's own stale projects (e.g. a scaffold whose
    # generation died halfway and left an empty src/gen) so the
    # scaffolder starts clean; other games' projects are never
    # touched.
    for d in sorted(out_dir.glob("*SNESRecomp")):
        if d.is_dir() and _is_own_project(d, probe, rom_stem):
            shutil.rmtree(d)
    before = {d.name for d in out_dir.glob("*SNESRecomp")
              if d.is_dir()}
    cmd = ["sh", "tools/new_project/setup_project.sh",
           "--rom", str(job["rom"]),
           "--dir", str(out_dir),
           "--name", title,
           "--yes",
           "--players", str(int(job.get("players", 2))),
           "--recomp-ui",
           "--netplay" if job.get("netplay") else "--no-netplay",
           "--fetch-boxart",
           "--no-github",
           "--generate", "--no-build"]
    _run(cmd, log, cwd=fw_dir, timeout=3600, env=_rust_env())
    proj = _find_project(out_dir, probe, rom_stem)
    if proj is None:
        # Last resort, still identity-safe: a directory the
        # scaffolder created during THIS run (name prediction can
        # miss an unusual title) with generated C in it.
        fresh = [d for d in sorted(out_dir.glob("*SNESRecomp"))
                 if d.is_dir() and d.name not in before
                 and _gen_populated(d)]
        if len(fresh) == 1:
            proj = fresh[0]
    if proj is None:
        raise RuntimeError("scaffolder finished but no project with "
                           "generated C was found under "
                           f"{out_dir} — generation failed")
    (proj / "tc-mingw.cmake").write_text(TOOLCHAIN_MINGW)
    _write_marker(proj, probe)
    return proj, title, proj.name


def build_target(proj, proj_name, target, jobs, log):
    if target == "linux":
        bdir = proj / "build-linux"
        cmd = ["cmake", "-S", str(proj), "-B", str(bdir), "-G", "Ninja",
               "-DCMAKE_BUILD_TYPE=Release",
               # SDL3 otherwise links -ljack whenever pkg-config
               # smells JACK, and machines with a half-installed
               # JACK (runtime lib, no dev symlink) fail the final
               # link — seen on a clean machine 2026-11-03. The
               # games use Pulse/PipeWire/ALSA; JACK is not missed.
               "-DSDL_JACK=OFF"]
        env = _rust_env()
    else:
        bdir = proj / "build-win"
        cmd = ["cmake", "-S", str(proj), "-B", str(bdir), "-G", "Ninja",
               "-DCMAKE_BUILD_TYPE=Release",
               "-DSDL_JACK=OFF",
               "-DCMAKE_TOOLCHAIN_FILE=" + str(proj / "tc-mingw.cmake")]
        env = _rust_env()
    _run(cmd, log, timeout=1800, env=env)
    _run(["cmake", "--build", str(bdir), "-j", str(jobs)], log,
         timeout=3600, env=env)
    exe = bdir / (proj_name + (".exe" if target == "windows" else ""))
    return bdir, exe


def verify_build(bdir, exe, proj):
    if not exe.exists() or exe.stat().st_size < 1_000_000:
        return {"ok": False,
                "reason": f"binary missing or too small: {exe}"}
    gen = proj / "src" / "gen"
    gen_c = list(gen.glob("*.c")) if gen.is_dir() else []
    if not gen_c:
        return {"ok": False, "reason": "no generated C in src/gen"}
    return {"ok": True, "size": exe.stat().st_size,
            "generated_c_files": len(gen_c)}


def package(job, probe, title, proj_name, target, exe, bdir, log):
    out_dir = Path(job["output_dir"])
    camel = proj_name[:-len("SNESRecomp")] if \
        proj_name.endswith("SNESRecomp") else proj_name
    stage = out_dir / (camel + "-pkg-" + target)
    if stage.exists():
        shutil.rmtree(stage)
    game_dir = stage / camel
    game_dir.mkdir(parents=True)
    exe_name = camel + (".exe" if target == "windows" else "")
    shutil.copy2(exe, game_dir / exe_name)
    # What the build stages beside the executable for the runtime:
    # the assets/ tree (launcher art incl. the staged box art) and
    # the mods/ catalog, plus any shared libraries. Everything else
    # in the build dir is CMake intermediates or the _deps tree
    # (100+ MB of SDL sources) and must NOT ship. rom.cfg is a
    # machine-local cache naming this machine's ROM path — the
    # player drops their ROM beside the exe instead, so it is
    # deliberately not packaged.
    for name in ("assets", "mods"):
        src = bdir / name
        if src.is_dir():
            shutil.copytree(src, game_dir / name)
        else:
            log_line(log, f"WARNING: build staged no {name}/ beside "
                          f"the exe; launcher may show placeholder art")
    for sibling in sorted(bdir.iterdir()):
        if sibling.is_file() and sibling.suffix in (".so", ".dll") \
                and sibling.name != exe.name:
            shutil.copy2(sibling, game_dir / sibling.name)
    cover = fetch_cover(job.get("title") or probe["title"], log=log)
    cover_ok = cover is not None
    if cover_ok:
        art_dir = game_dir / "artwork" / (job.get("title")
                                          or probe["title"])
        art_dir.mkdir(parents=True)
        shutil.copy2(cover, art_dir / "front.png")
    ra_on = bool(job.get("ra", True))
    if ra_on:
        if job.get("ra_toml") and Path(job["ra_toml"]).exists():
            shutil.copy2(job["ra_toml"],
                         game_dir / "retroachievements.toml")
        else:
            log_line(log, "RA on but no ra_toml given: package ships "
                          "the .example only (RA stays off until the "
                          "player fills one in)")
        example = RA_BUNDLE / "retroachievements.toml.example"
        if example.exists():
            shutil.copy2(example,
                         game_dir / "retroachievements.toml.example")
    (game_dir / "README.txt").write_text(README_TMPL.format(
        title=title, exe=exe_name,
        cover_status=COVER_OK if cover_ok else COVER_MISSING,
        netplay_status=NETPLAY_ON if job.get("netplay") else NETPLAY_OFF,
        ra_status=RA_ON if ra_on else RA_OFF))
    zip_path = out_dir / (camel + ("-Windows.zip" if target == "windows"
                                   else "-Linux.zip"))
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(game_dir.rglob("*")):
            if f.is_file():
                info = zipfile.ZipInfo(str(f.relative_to(stage)))
                info.compress_type = zipfile.ZIP_DEFLATED
                if f.name == exe_name:
                    info.external_attr = 0o755 << 16
                z.writestr(info, f.read_bytes())
    shutil.rmtree(stage)
    log_line(log, f"package: {zip_path} ({zip_path.stat().st_size} bytes)")
    return zip_path


def run_job(job, log=None):
    """Run one conversion job and report exactly what happened.

    Result contract (the GUI relies on this — a target failure
    must never be invisible):
      status "done"         every requested target packaged;
                            result["failures"] is empty
      status "partial"      some targets packaged, some failed;
                            result["failures"] maps each failed
                            target to its reason
      status "needs_review" the probe flagged the ROM; no build ran
      raises RuntimeError   every requested target failed (the
                            failures are in the exception message)
    """
    probe = probe_rom(job["rom"])
    log_line(log, f"probe: \"{probe['header_title']}\" {probe['mapping']} "
                  f"chip {probe['coprocessor']} ROM {probe['rom_size_kb']}K "
                  f"SRAM {probe['sram_size_kb']}K {probe['region']} "
                  f"crc32 {probe['crc32']}")
    if probe["needs_review"] and not job.get("force"):
        for reason in probe["review_reasons"]:
            log_line(log, "needs-review: " + reason)
        log_line(log, "flagged needs-review; set force to try anyway")
        return {"status": "needs_review", "probe": probe}
    ra_on = bool(job.get("ra", True))
    if ra_on and not _ra_bundle_ok():
        raise RuntimeError(
            "RetroAchievements requested but the RA bundle is "
            "missing or incomplete (bundle/ra/) — refusing to "
            "ship a package that silently lacks it")
    if ra_on:
        log_line(log, "RetroAchievements: on (bundle/ra)")
    ensure_system_tools(log, job.get("targets", ["linux", "windows"]))
    ensure_rust(log)
    fw_dir = ensure_framework(log)
    proj, title, proj_name = scaffold_project(job, probe, fw_dir, log)
    _wire_ra(job, proj, proj_name, ra_on, log)
    results = {}
    failures = {}
    for target in job.get("targets", ["linux", "windows"]):
        try:
            if target == "linux":
                # Preflight: the native build needs system dev
                # libraries (pkg-config probes). Fail fast with
                # the package names instead of dying minutes
                # into CMake — and never let the other target's
                # success make this failure invisible.
                lib_pkgs = missing_linux_build_libs(_rust_env())
                if lib_pkgs:
                    raise RuntimeError(
                        "missing Linux build libraries: "
                        + ", ".join(lib_pkgs)
                        + " — use the app's \"Install missing "
                          "packages\" button (one password "
                          "prompt), or run: sudo apt install "
                        + " ".join(lib_pkgs))
            log_line(log, f"building {target}…")
            bdir, exe = build_target(proj, proj_name, target,
                                     int(job.get("jobs", 2)), log)
            v = verify_build(bdir, exe, proj)
            if not v["ok"]:
                raise RuntimeError(f"verify failed: {v['reason']}")
            log_line(log, f"verify {target}: ok ({v['size']} bytes, "
                          f"{v['generated_c_files']} generated C files)")
            results[target] = str(package(job, probe, title, proj_name,
                                          target, exe, bdir, log))
        except Exception as e:  # noqa: BLE001
            failures[target] = str(e)
            log_line(log, f"ERROR: {target} target failed: {e}")
    if not results:
        raise RuntimeError(f"all targets failed: {failures}")
    status = "partial" if failures else "done"
    if failures:
        log_line(log, "PARTIAL result: packaged "
                      + ", ".join(sorted(results)) + "; FAILED "
                      + ", ".join(sorted(failures))
                      + " — this job is NOT fully done")
    return {"status": status, "probe": probe, "packages": results,
            "failures": failures}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "toolbox":
        print(json.dumps(toolbox(), indent=2))
        return 0
    if cmd == "probe":
        rom = argv[argv.index("--rom") + 1]
        print(json.dumps(probe_rom(rom), indent=2))
        return 0
    if cmd == "run":
        job_path = argv[argv.index("--job") + 1]
        job = json.loads(Path(job_path).read_text())
        result = run_job(job)
        print(json.dumps({"RESULT": result["status"], **result}, indent=2))
        return 0 if result["status"] == "done" else 1
    print("unknown command: " + cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
