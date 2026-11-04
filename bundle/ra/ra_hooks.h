/* ra_hooks.h — RetroAchievements hooks for SNESRecomp games.
 *
 * The engine patches the generated project's src/main.c to include
 * this header and to fill three fields of the SnesDesktopHostGame
 * struct with these functions (plus on_reset). The definitions live
 * in ra_extras.cpp, which the engine's managed CMake region compiles
 * into the game target when a job asks for RetroAchievements.
 *
 * The signatures match host_main.h's hook fields exactly; the stats
 * struct is only ever passed through, never inspected, so a forward
 * declaration is enough here.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

struct SnesDesktopHostFrameStats;

#ifdef __cplusplus
extern "C" {
#endif

/* SnesDesktopHostGame.on_rom_loaded: the exact ROM image the host
 * read and verified, before SnesInit. */
void snes_ra_on_rom_loaded(const uint8_t *rom, size_t size);

/* SnesDesktopHostGame.on_reset: guest reset / save-state load. */
void snes_ra_on_reset(void);

/* SnesDesktopHostGame.after_run_frame: the per-frame RA pump
 * (server-response delivery + rc_client_do_frame + toast pump). */
void snes_ra_after_run_frame(const struct SnesDesktopHostFrameStats *stats);

/* SnesDesktopHostGame.on_shutdown: tear the client down. */
void snes_ra_on_shutdown(void);

#ifdef __cplusplus
}
#endif
