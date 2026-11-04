# RetroAchievements — SNESRecomp Tools bundle

**STATUS: WIRED (2026-11-02).** Conversions ship with
RetroAchievements on by default: login at launch, an in-game
login toast, and in-game achievement pop-ups, with the user's
filled `retroachievements.toml` staged beside the executable.

## What is here

- `ra_extras.cpp` — the SNES RA module (rc_client lifecycle,
  memory peek, toast queue).
- `ra_hooks.h` — the hook prototypes the generated `src/main.c`
  references.
- `ra_http.c/.h`, `ra-rcheevos/` — the family HTTP transport
  (OpenSSL on Linux, WinHTTP on Windows) and the vendored
  rcheevos, shared with the GB/NES tools.
- `retroachievements.toml.example` — the credentials template
  staged into every RA package.

## How it is wired

snesrecomp ships no RA code, so the converter wires the module
into each generated project (engine `_wire_ra`, in the sibling
GB/NES pattern):

- The generated `src/main.c`'s `SnesDesktopHostGame` hook struct
  gains four fields — `on_rom_loaded`, `on_reset`,
  `after_run_frame`, `on_shutdown` — pointing at the module.
- The project's CMakeLists gains a marked region that compiles
  `ra/ra_extras.cpp` + `ra/ra_http.c` + the vendored rcheevos
  (minus the libretro/raintegration sources) and links OpenSSL
  (Linux) / winhttp (Windows). `RA_DISABLE_POPUPS` is defined
  when the job's pop-ups option is off.
- Turning RA off strips both patches, restoring the generated
  files byte-for-byte. Asking for RA with a broken bundle fails
  the conversion loudly — a build never ships silently without
  the RA it was asked for.

## Design decisions

- **Toasts:** the desktop host exposes its own on-screen
  display — `snes_osd_push(msg, duration_ms)` in the runner,
  drawn over presented frames by both presenters. The module
  queues (title, body) pairs and pushes one line at a time from
  `after_run_frame`, 4 s per line, only while frames are live.
  No framebuffer painting was needed (the GB module paints its
  own because the GB runtime has no OSD at all). Hiding pop-ups
  (`RA_DISABLE_POPUPS`) hides the login toast too — the family
  semantics.
- **Identity/hash:** `on_rom_loaded` hands the module the
  host-verified ROM bytes; the hash is
  `rc_hash_generate_from_buffer` with console
  `RC_CONSOLE_SUPER_NINTENDO` (3), which skips a 512-byte
  copier header itself. (A file fallback via `rom.cfg` beside
  the executable covers hosts that withhold the bytes.)
  Super Mario World (USA) hashes to
  `cdd3c8c37322978ca8669b34bc89c804` — the official RA hash.
- **Memory peek:** reads go through the runner's bus read
  `snes_read(g_snes, ...)`, with rc_client's SNES address map
  translated to bus addresses: WRAM at 0x7E0000, cartridge RAM
  in the LoROM 0x700000 window (HiROM: banks 0x20–0x3F at
  0x6000), SA-1 I-RAM at 0x003000. HiROM is detected from the
  header checksum/complement score.
- **Frame pump:** lazy init on the first frame (the login is
  never attempted before the game is running); every
  `after_run_frame` delivers pending server responses, calls
  `rc_client_do_frame`, and pumps the toast queue. Server
  calls run on detached threads. `rc_client_reset` on game
  reset; `rc_client_destroy` on shutdown.
- **Credentials:** `retroachievements.toml` beside the
  executable — `username` + the **Connect API token** (the
  token emulators use; RetroArch keeps it as `cheevos_token`
  in retroarch.cfg). The Web API key from the site's Settings
  page does NOT work for client login.
- **Dev hook:** a `ra_test_toast.txt` beside the executable
  (two lines: title, body) fires one test toast at startup for
  verification. It is never staged into packages.

## Verified / not verified

Verified on Super Mario World (USA) through the converter:
official ROM hash in the log, login attempted as the toml's
user, login toast and test toast rendered in-game
(screenshots from the verification run). NOT yet verified: a
successful login and a real achievement unlock — the build
sandbox's route to retroachievements.org fails ("No
response"), the same known limit as the GB/NES modules; both
are expected to close on a normal connection.

RetroAchievements counts hardcore unlocks only from approved
clients; this client is unapproved, so unlocks earn Casual
(softcore) credit even with `hardcore = true`.
