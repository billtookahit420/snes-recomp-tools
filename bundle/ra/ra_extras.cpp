/* ra_extras.cpp — RetroAchievements for SNESRecomp games.
 *
 * Plugs into the game through the desktop host's per-title hooks
 * (runner/src/desktop/host_main.h): the engine patches the
 * generated project's src/main.c to fill SnesDesktopHostGame's
 * on_rom_loaded / on_reset / after_run_frame / on_shutdown fields
 * with the functions at the bottom of this file, and compiles this
 * TU into the game target via a managed CMake region. All RA work
 * happens on the host's main loop thread: init is lazy (first
 * after_run_frame, when g_snes exists and the ROM is loaded), and
 * server responses are delivered there too.
 *
 * Ported from the GBRecomp RA module (same config format, same
 * server plumbing, same callback-polarity fix); the SNES
 * differences:
 *
 *  - Identity: the exact ROM image the host read and verified,
 *    handed over by on_rom_loaded, hashed with rcheevos' SNES
 *    algorithm (rc_hash_generate_from_buffer,
 *    RC_CONSOLE_SUPER_NINTENDO = 3; the algorithm itself skips a
 *    512-byte copier header when one is present). Fallback, if the
 *    hook never fired: rom.cfg beside the exe, hashed from file.
 *  - Memory: the live machine via the runner's snes_read() bus
 *    read on the global g_snes. rc_client's SNES address space
 *    (rc_consoleinfo.c) is offset-based, so the peek translates:
 *      0x000000-0x01FFFF  System RAM  -> bus 0x7E0000 + addr
 *      0x020000-0x09FFFF  Cartridge RAM -> LoROM: bus 0x700000+off
 *                                          HiROM: banks 0x20-0x3F,
 *                                          0x6000-0x7FFF window
 *      0x0A0000-0x0A07FF  SA-1 I-RAM  -> bus 0x003000 + off
 *  - Pop-ups: the desktop host owns an OSD with a timed-toast API,
 *    snes_osd_push() (runner/src/snes_osd.h) — the same channel the
 *    host's own "Slot N saved" toasts use, composited over the
 *    presented frame by both presenters. The OSD holds ONE line
 *    (64 chars) at a time, so a two-line pop-up (title, then body)
 *    is queued and pushed back-to-back from after_run_frame.
 *  - Login: retroachievements.toml next to the exe (username +
 *    Connect api_token). Absent file = RA off, game runs normally.
 *
 * Hardcore note: rc_client is told the toml's hardcore flag, but
 * RetroAchievements counts hardcore only from approved clients; a
 * recomp client is an unknown emulator, so unlocks register as
 * Casual/softcore. Never spoof another client's identity.
 */
#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <filesystem>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "rc_client.h"
#include "rc_consoles.h"
#include "rc_hash.h"
#include "ra_http.h"
#include "ra_hooks.h"

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#else
#include <unistd.h>
#include <limits.h>
#endif

/* Runner symbols the module uses, declared here rather than by
 * including the runner headers: snes.h / common_cpu_infra.h carry
 * no extern "C" guards of their own in a C++ TU, and the host API
 * header drags in SDL. These are plain C symbols in the runner
 * objects linked into the same executable. */
struct Snes;
extern "C" {
uint8_t snes_read(Snes *snes, uint32_t adr);
extern Snes *g_snes;
void snes_osd_push(const char *msg, int duration_ms);
bool snesrecomp_host_frame_is_live(void);
}

namespace {

/* ------------------------------------------------------------------ */
/* Config: retroachievements.toml next to the exe                      */
/* ------------------------------------------------------------------ */

struct RaConfig {
    std::string username;
    std::string api_token;
    bool hardcore = false;
    bool present = false;
};

std::string ra_exe_dir() {
#ifdef _WIN32
    char path[MAX_PATH];
    DWORD n = GetModuleFileNameA(NULL, path, (DWORD)sizeof(path));
    if (n == 0 || n >= sizeof(path)) return std::string();
    std::string s(path);
    size_t slash = s.find_last_of("\\/");
    return (slash == std::string::npos) ? std::string() : s.substr(0, slash);
#else
    char path[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", path, sizeof(path) - 1);
    if (n <= 0) return std::string();
    path[n] = '\0';
    std::string s(path);
    size_t slash = s.find_last_of('/');
    return (slash == std::string::npos) ? std::string() : s.substr(0, slash);
#endif
}

std::string ra_trim(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && (s[a] == ' ' || s[a] == '\t' || s[a] == '\r' || s[a] == '\n')) a++;
    while (b > a && (s[b - 1] == ' ' || s[b - 1] == '\t' || s[b - 1] == '\r' || s[b - 1] == '\n')) b--;
    return s.substr(a, b - a);
}

std::string ra_unquote(const std::string& s) {
    if (s.size() >= 2 && s.front() == '"' && s.back() == '"')
        return s.substr(1, s.size() - 2);
    if (s.size() >= 2 && s.front() == '\'' && s.back() == '\'')
        return s.substr(1, s.size() - 2);
    return s;
}

RaConfig ra_read_config(const std::string& exe_dir) {
    RaConfig cfg;
    std::string path = exe_dir + "/retroachievements.toml";
    FILE* f = fopen(path.c_str(), "r");
    char line[1024];
    if (!f) return cfg;
    cfg.present = true;
    while (fgets(line, sizeof(line), f)) {
        std::string s = ra_trim(line);
        size_t eq;
        std::string key, val;
        if (s.empty() || s[0] == '#') continue;
        eq = s.find('=');
        if (eq == std::string::npos) continue;
        key = ra_trim(s.substr(0, eq));
        val = ra_unquote(ra_trim(s.substr(eq + 1)));
        if (key == "username") cfg.username = val;
        else if (key == "api_token") cfg.api_token = val;
        else if (key == "hardcore") cfg.hardcore = (val == "true" || val == "1");
    }
    fclose(f);
    return cfg;
}

/* ------------------------------------------------------------------ */
/* ROM identity: the image on_rom_loaded handed over (raw file       */
/* bytes; rc_hash_snes skips a copier header itself). The mapping    */
/* guess drives the cartridge-RAM peek translation.                  */
/* ------------------------------------------------------------------ */

std::vector<uint8_t> g_rom;
bool g_hirom = false;

bool ra_detect_hirom(const uint8_t* rom, size_t size) {
    size_t off = (size % 1024 == 512) ? 512 : 0;
    if (size < off + 0x8000) return false;
    auto score = [&](size_t base, int low_nibble) {
        if (base + 0x40 > size) return -1;
        int s = 0;
        if ((rom[base + 0x15] & 0x0F) == low_nibble) s += 4;
        unsigned reset = rom[base + 0x3C] | (rom[base + 0x3D] << 8);
        if (reset >= 0x8000 && reset != 0xFFFF) s += 2;
        unsigned comp = rom[base + 0x1C] | (rom[base + 0x1D] << 8);
        unsigned chk = rom[base + 0x1E] | (rom[base + 0x1F] << 8);
        if ((chk ^ comp) == 0xFFFF) s += 2;
        return s;
    };
    return score(off + 0xFFC0, 1) > score(off + 0x7FC0, 0);
}

/* Fallback identity path if on_rom_loaded never fired: rom.cfg
 * beside the exe (the host's own ROM cache), hashed from file. */
std::string ra_rom_cfg_path(const std::string& exe_dir) {
    std::string cfg = exe_dir + "/rom.cfg";
    FILE* f = fopen(cfg.c_str(), "r");
    if (!f) return std::string();
    char line[4096];
    std::string p;
    if (fgets(line, sizeof(line), f)) p = ra_trim(line);
    fclose(f);
    if (p.empty()) return std::string();
    FILE* rf = fopen(p.c_str(), "rb");
    if (rf) { fclose(rf); return p; }
    std::string joined = exe_dir + "/" + p;
    rf = fopen(joined.c_str(), "rb");
    if (rf) { fclose(rf); return joined; }
    return std::string();
}

/* ------------------------------------------------------------------ */
/* Memory peek: live machine via the runner's bus read, with the     */
/* rc_client SNES address translation (see the file header).         */
/* ------------------------------------------------------------------ */

uint32_t ra_bus_for(uint32_t addr) {
    if (addr < 0x020000u) return 0x7E0000u + addr;             /* WRAM */
    if (addr < 0x0A0000u) {                                    /* cart RAM */
        uint32_t off = addr - 0x020000u;
        if (g_hirom) {
            uint32_t bank = 0x20u + ((off >> 13) & 0x1Fu);
            return (bank << 16) | (0x6000u + (off & 0x1FFFu));
        }
        return 0x700000u + off;                                /* LoROM SRAM */
    }
    if (addr < 0x0A0800u) return 0x003000u + (addr - 0x0A0000u); /* SA-1 I-RAM */
    return 0x7E0000u + (addr & 0x1FFFFu);
}

uint32_t ra_read_memory(uint32_t address, uint8_t* buffer, uint32_t num_bytes,
                        rc_client_t* /*client*/) {
    if (!buffer || num_bytes == 0 || !g_snes) return 0;
    for (uint32_t i = 0; i < num_bytes; i++)
        buffer[i] = snes_read(g_snes, ra_bus_for(address + i));
    return num_bytes;
}

/* ------------------------------------------------------------------ */
/* Server calls: detached thread + game-thread delivery                */
/* ------------------------------------------------------------------ */

struct RaPending {
    rc_client_server_callback_t callback = nullptr;
    void* callback_data = nullptr;
    char* body = nullptr;
    size_t body_len = 0;
    int http_status = 0;
};

std::mutex g_pending_mutex;
std::vector<RaPending> g_pending;

void ra_server_call(const rc_api_request_t* request,
                    rc_client_server_callback_t callback, void* callback_data,
                    rc_client_t* /*client*/) {
    std::string url = request->url ? request->url : "";
    std::string post = request->post_data ? request->post_data : "";
    std::string ctype = request->content_type ? request->content_type : "";
    std::thread([url, post, ctype, callback, callback_data]() {
        char* body = nullptr;
        size_t body_len = 0;
        int status = 0;
        RaPending p;
        if (ra_http_post(url.c_str(), post.c_str(), ctype.c_str(),
                         &body, &body_len, &status) != 0) {
            body = nullptr;
            body_len = 0;
            status = 0;
        }
        p.callback = callback;
        p.callback_data = callback_data;
        p.body = body;
        p.body_len = body_len;
        p.http_status = status;
        {
            std::lock_guard<std::mutex> lock(g_pending_mutex);
            g_pending.push_back(p);
        }
    }).detach();
}

void ra_deliver_pending() {
    std::vector<RaPending> due;
    {
        std::lock_guard<std::mutex> lock(g_pending_mutex);
        due.swap(g_pending);
    }
    for (auto& p : due) {
        rc_api_server_response_t resp;
        memset(&resp, 0, sizeof(resp));
        resp.body = p.body ? p.body : "";
        resp.body_length = p.body_len;
        resp.http_status_code = p.http_status;
        if (p.callback) p.callback(&resp, p.callback_data);
        free(p.body);
    }
}

/* ------------------------------------------------------------------ */
/* Toasts: the host OSD's snes_osd_push is a single 64-char line     */
/* with one slot, so a pop-up (title, then body) is a small queue    */
/* pumped from after_run_frame. Only pushed on live frames — a       */
/* rollback replay or run-ahead speculation must not re-toast.       */
/* ------------------------------------------------------------------ */

std::deque<std::pair<std::string, int>> g_toast_queue;
std::chrono::steady_clock::time_point g_toast_until{};

void ra_toast_enqueue(const std::string& line, int duration_ms) {
#ifdef RA_DISABLE_POPUPS
    /* Recomp Tools option: pop-ups hidden at build time. This also
       hides the login toast, which uses the same channel. */
    (void)line; (void)duration_ms;
    return;
#else
    if (line.empty()) return;
    g_toast_queue.emplace_back(line.substr(0, 63), duration_ms);
#endif
}

void ra_toast_show(const std::string& title, const std::string& body) {
    ra_toast_enqueue(title, 4000);
    if (!body.empty() && body != title) ra_toast_enqueue(body, 4000);
}

void ra_toast_pump() {
    if (g_toast_queue.empty()) return;
    if (std::chrono::steady_clock::now() < g_toast_until) return;
    if (!snesrecomp_host_frame_is_live()) return;
    auto [line, ms] = g_toast_queue.front();
    g_toast_queue.pop_front();
    snes_osd_push(line.c_str(), ms);
    g_toast_until = std::chrono::steady_clock::now() +
                    std::chrono::milliseconds(ms > 200 ? ms - 100 : ms);
}

/* ------------------------------------------------------------------ */
/* Events + async callbacks                                            */
/* ------------------------------------------------------------------ */

void ra_event_handler(const rc_client_event_t* event, rc_client_t* /*client*/) {
    if (!event) return;
    switch (event->type) {
        case RC_CLIENT_EVENT_ACHIEVEMENT_TRIGGERED:
            if (event->achievement) {
                fprintf(stdout,
                        "[RA] Achievement unlocked: %s (%u points) - %s\n",
                        event->achievement->title ? event->achievement->title : "?",
                        event->achievement->points,
                        event->achievement->description
                            ? event->achievement->description
                            : "");
                fflush(stdout);
                ra_toast_show(
                    event->achievement->title ? event->achievement->title : "?",
                    event->achievement->description
                        ? event->achievement->description
                        : "");
            }
            break;
        case RC_CLIENT_EVENT_GAME_COMPLETED:
            fprintf(stdout, "[RA] All achievements for this game earned!\n");
            fflush(stdout);
            break;
        case RC_CLIENT_EVENT_SERVER_ERROR:
            if (event->server_error) {
                fprintf(stderr, "[RA] Server error (%s): %s\n",
                        event->server_error->api ? event->server_error->api : "?",
                        event->server_error->error_message
                            ? event->server_error->error_message
                            : "?");
            }
            break;
        case RC_CLIENT_EVENT_DISCONNECTED:
            fprintf(stderr, "[RA] Disconnected: unlocks will queue until reconnect.\n");
            break;
        case RC_CLIENT_EVENT_RECONNECTED:
            fprintf(stdout, "[RA] Reconnected: queued unlocks submitted.\n");
            fflush(stdout);
            break;
        default:
            break;
    }
}

void ra_login_callback(int result, const char* error_message,
                       rc_client_t* /*client*/, void* /*userdata*/) {
    /* rcheevos reports RC_OK (0) on success, nonzero on failure. */
    if (result == 0) {
        fprintf(stdout, "[RA] Logged in.\n");
        ra_toast_show("RetroAchievements", "Logged in");
    } else {
        fprintf(stdout, "[RA] Login failed: %s\n",
                error_message ? error_message : "unknown error");
        ra_toast_show("RetroAchievements", "Login failed: check toml");
    }
    fflush(stdout);
}

void ra_load_callback(int result, const char* error_message,
                      rc_client_t* /*client*/, void* /*userdata*/) {
    if (result == 0) {
        fprintf(stdout, "[RA] Game achievements loaded.\n");
    } else {
        fprintf(stderr, "[RA] Could not load game achievements: %s\n",
                error_message ? error_message : "unknown error");
    }
    fflush(stdout);
}

/* ------------------------------------------------------------------ */
/* Init (lazy, first after_run_frame) + the host hooks                 */
/* ------------------------------------------------------------------ */

rc_client_t* g_client = nullptr;
bool g_init_done = false;

void ra_init() {
    std::string exe_dir = ra_exe_dir();
    /* Dev-only test hook (NOT shipped in packages): if
     * ra_test_toast.txt sits next to the exe, pop its two lines as
     * a toast so the pop-up channel can be verified without a real
     * RA unlock. */
    {
        std::string test_path = exe_dir + "/ra_test_toast.txt";
        FILE* tf = fopen(test_path.c_str(), "r");
        if (tf) {
            char tline[256] = "";
            char bline[512] = "";
            if (fgets(tline, sizeof(tline), tf)) {
                std::string t = ra_trim(tline);
                std::string b;
                if (fgets(bline, sizeof(bline), tf)) b = ra_trim(bline);
                if (t.empty()) t = "Test Achievement";
                if (b.empty()) b = "In-game pop-ups are working!";
                ra_toast_show(t, b);
                fprintf(stdout, "[RA] test toast armed: %s\n", t.c_str());
                fflush(stdout);
            }
            fclose(tf);
        }
    }
    RaConfig cfg = ra_read_config(exe_dir);
    if (!cfg.present) {
        fprintf(stdout,
                "[RA] Disabled: no retroachievements.toml next to the exe.\n"
                "[RA] To enable: create retroachievements.toml with your\n"
                "[RA] username and RetroAchievements API token, then restart.\n");
        fflush(stdout);
        return;
    }
    if (cfg.username.empty() || cfg.api_token.empty()) {
        fprintf(stderr,
                "[RA] Disabled: retroachievements.toml is missing username "
                "or api_token.\n");
        return;
    }
    char hash[33];
    memset(hash, 0, sizeof(hash));
    if (!g_rom.empty()) {
        if (!rc_hash_generate_from_buffer(hash, RC_CONSOLE_SUPER_NINTENDO,
                                          g_rom.data(), g_rom.size())) {
            fprintf(stderr, "[RA] Disabled: could not hash the loaded ROM\n");
            return;
        }
    } else {
        std::string rom = ra_rom_cfg_path(exe_dir);
        if (rom.empty()) {
            fprintf(stderr, "[RA] Disabled: no ROM image from the host "
                            "and no rom.cfg beside the exe.\n");
            return;
        }
        if (!rc_hash_generate_from_file(hash, RC_CONSOLE_SUPER_NINTENDO,
                                        rom.c_str())) {
            fprintf(stderr, "[RA] Disabled: could not hash ROM %s\n",
                    rom.c_str());
            return;
        }
    }
    fprintf(stdout, "[RA] ROM hash: %s\n", hash);

    g_client = rc_client_create(ra_read_memory, ra_server_call);
    if (!g_client) {
        fprintf(stderr, "[RA] Disabled: rc_client_create failed.\n");
        return;
    }
    rc_client_set_event_handler(g_client, ra_event_handler);
    rc_client_set_hardcore_enabled(g_client, cfg.hardcore ? 1 : 0);
    fprintf(stdout, "[RA] Logging in as %s (hardcore %s)...\n",
            cfg.username.c_str(), cfg.hardcore ? "on" : "off");
    fflush(stdout);
    rc_client_begin_login_with_token(g_client, cfg.username.c_str(),
                                     cfg.api_token.c_str(), ra_login_callback,
                                     nullptr);
    rc_client_begin_load_game(g_client, hash, ra_load_callback, nullptr);
}

} /* namespace */

/* ------------------------------------------------------------------ */
/* The host hooks (declared in ra_hooks.h; the engine patches them   */
/* into the generated project's SnesDesktopHostGame).                */
/* ------------------------------------------------------------------ */

extern "C" void snes_ra_on_rom_loaded(const uint8_t *rom, size_t size) {
    if (rom && size > 0) {
        g_rom.assign(rom, rom + size);
        g_hirom = ra_detect_hirom(rom, size);
    }
}

extern "C" void snes_ra_on_reset(void) {
    if (g_client) rc_client_reset(g_client);
}

extern "C" void snes_ra_after_run_frame(const SnesDesktopHostFrameStats *) {
    if (!g_init_done) {
        g_init_done = true;
        ra_init();
    }
    if (g_client) ra_deliver_pending();
    if (g_client) rc_client_do_frame(g_client);
    ra_toast_pump();
}

extern "C" void snes_ra_on_shutdown(void) {
    if (g_client) {
        rc_client_destroy(g_client);
        g_client = nullptr;
    }
}
