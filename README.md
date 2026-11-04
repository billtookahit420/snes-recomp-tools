# SNESRecomp Tools

Convert your own Super Nintendo / Super Famicom ROM dumps into native
PC games — no emulator. Each game's 65816 code is statically
recompiled to a real executable with the recomp-ui launcher, box art,
save states and rewind, using the **snesrecomp** framework
(RetroPortingToolKit).

## Running it

```sh
./snes-recomp-tools.sh        # GUI (first run sets up a Python venv)
./snes-recomp-tools.sh --cli toolbox
./snes-recomp-tools.sh --cli probe --rom "Super Mario World (USA).sfc"
./snes-recomp-tools.sh --cli run --job job.json
```

Add `.sfc` / `.smc` files (or a folder), check the probe table, press
**Run Queue**. Each game is packaged as `<Game>-Linux.zip` and
`<Game>-Windows.zip` in the output folder, with the converted project
kept beside them as `<Game>SNESRecomp/`. **No ROM is ever included in
a package** — the player puts their own dump of the exact cartridge
next to the executable, and the launcher verifies its digest before
playing.

## What you need on the machine

git, Python 3.11+, CMake, Ninja, a C/C++ toolchain, and **Rust
(cargo ≥ 1.85)** — the framework's analyzer (`recompiler-rs`) is a
Rust binary that the first conversion builds. SDL is downloaded and
built by the framework itself; no system SDL package is required.
The Windows target cross-builds with MinGW-w64. **You do not have
to install any of this by hand:** on the first conversion the app
installs Rust itself with rustup (into its own `tools/` folder, no
admin rights needed) and installs the apt build packages too when
passwordless sudo is available — the Toolbox line in the app shows
what is present and names anything left for you to install.

First run also clones the framework into `framework-cache/` (the app
ships without it): snesrecomp itself plus, per game project, the
recomp-ui launcher submodule the scaffolder pins.

## Supported cartridges, and "needs review"

Implemented by the framework: standard **LoROM / HiROM** (incl.
FastROM), **Cx4**, **Super FX**, **DSP-1**, **SA-1**. The probe flags
a ROM *needs review* — before any build starts — when it carries
something the framework does not implement: DSP cartridges whose chip
might be DSP-2/3/4 (the header cannot distinguish them from DSP-1),
S-DD1 (experimental, title-gated), SPC7110, ST010/ST011, ST018,
OBC-1, S-RTC, Super Game Boy cartridges, ExLoROM / ExHiROM mappings,
an unrecognized cartridge type, or a header checksum that does not
verify. Probing Duane's 730-ROM set: **709 pass, 21 flagged** — see
`~/workspace/snes-collection/needs-review-list.md` (in the collection
folder, not this repo). A job can be forced past the flag; expect the
unimplemented hardware to misbehave.

Passing the probe is not a playability promise: this framework ports
per title, so a converted game still wants its first areas played
before a long session.

## Feature status — read before repeating any of it

- **Cover art — fetched and shipped.** The scaffolder fetches the
  launcher box art from libretro-thumbnails by the ROM's CRC32
  (No-Intro name) and the build stages it as
  `assets/img/boxart.tga`; this app additionally fetches by title
  (GitHub mirror first, canonical host second, retries, name
  variants) for the package's `artwork/<Title>/front.png` and the
  GUI thumbnail. Both fetches proven on Super Mario World. (The
  SNES launcher's main page does not display the art in the proof
  screenshots — it is staged for the launcher's art surfaces; the
  rendered-in-launcher claim is deliberately not made until it is
  seen on screen.)
- **Netplay — wired; sessions start; a connected match is NOT yet
  demonstrated.** The Settings checkbox passes the scaffolder's
  `--netplay` (recomp-net; the rollback engine is the default
  session mode, delay-sync the fallback). A netplay build of Super
  Mario World was run as two loopback instances: both entered
  their rollback LAN sessions with the expected bind/peer sockets
  and sat in the peer-wait barrier — but the converter's build
  sandbox refuses outgoing UDP at the OS level (the kernel returns
  EPERM for the netplay HELLO/READY packets themselves, proven
  under strace), so no packet can ever be exchanged there and the
  barrier timed out on both sides. That is an environment limit,
  not a build defect, but until a synchronized match has actually
  been seen on a normal machine, netplay ships labelled
  experimental and defaults to off. To try it on real machines:
  same build + same ROM on both sides, then either use the
  pre-boot lobby, or direct-IP from the shell —
  `SNES_NETPLAY=1 SNES_NET_SLOT=0 SNES_NET_BIND=host:7777
  SNES_NET_PEER=peer:7778` on one side and slot 1 with the ports
  swapped on the other. A session refuses save states, pause
  menus and input scripts: launch both sides cold.
- **RetroAchievements — included (on by default).** The framework
  ships no RA code, so the converter wires its own module into
  each generated project (host lifecycle hooks + the framework's
  own on-screen-display channel for the toasts; design notes in
  `bundle/ra/README.md`). The game logs in at launch with the
  `retroachievements.toml` beside the executable — the converter
  ships a filled copy when given one — shows a login toast
  in-game, and pops achievements in-game as they unlock
  (uncheck "RA pop-ups" to silence pop-ups AND the login toast).
  Verified on Super Mario World: the official RA ROM hash, the
  login attempt, and the login/test toasts rendering in-game.
  Still unverified: a successful login and a real unlock from
  the converter's build sandbox (its route to
  retroachievements.org fails; expected to close on a normal
  connection). NOTE: RetroAchievements counts hardcore unlocks
  only from approved clients; an unapproved client earns Casual
  (softcore) credit even with hardcore = true in the toml.

## Package layout

```
<Game>/
  <Game>            (or <Game>.exe)   the game, launcher included
  ...               whatever the build staged beside the exe
                    (launcher assets incl. box art, mods catalog)
  artwork/<Title>/front.png
  README.txt        controls + per-feature status for that build
```

## Credits

Built with **snesrecomp** by **Matthew Stanley (mstan)** /
**RetroPortingToolkit** (retroportingtoolkit.com;
github.com/mstan, github.com/RetroPortingToolKit). Launcher:
recomp-ui (MIT, © 2026 Matthew Stanley). snesrecomp license:
PolyForm Noncommercial 1.0.0. The runner statically links components
under their own licenses — LakeSnes / snesrev (MIT) and ares-derived
coprocessor cores (ISC); see `THIRD_PARTY_ATTRIBUTION.md` in the
framework checkout for the full notices.

## Status of this app

First working version (2026-11-02): engine + GUI + toolbox, SMW
proof conversion boot-verified on Linux. Windows packages are
cross-built with MinGW and labelled compile-verified until one is
run on a real Windows machine. Not published — the converter family
publishes together, later, on the owner's word.
