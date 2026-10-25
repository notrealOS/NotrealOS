/* xkbguard (10-24): LD_PRELOAD shim for Chromium's Wayland launch crash.
 *
 * Chromium sometimes gets wl_keyboard.modifiers before it has an xkb_state (keyboard focus /
 * keymap race - this phone has many virtual keyboards: swipe OSK, voiced/circled/crtd uinput
 * clones) and calls xkb_state_update_mask(NULL, ...) -> SIGSEGV at 0x20 (all 31 browser crash
 * dumps since 10-11). Here a NULL state is simply ignored; the next keymap event creates it.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <stddef.h>

struct xkb_state;
typedef int (*update_mask_fn)(struct xkb_state *, uint32_t, uint32_t, uint32_t, uint32_t, uint32_t, uint32_t);
typedef int (*update_key_fn)(struct xkb_state *, uint32_t, int);

int xkb_state_update_mask(struct xkb_state *state, uint32_t dm, uint32_t lm, uint32_t km,
                          uint32_t dl, uint32_t ll, uint32_t kl)
{
    static update_mask_fn real;
    if (!state)
        return 0;
    if (!real)
        real = (update_mask_fn)dlsym(RTLD_NEXT, "xkb_state_update_mask");
    return real(state, dm, lm, km, dl, ll, kl);
}

int xkb_state_update_key(struct xkb_state *state, uint32_t key, int direction)
{
    static update_key_fn real;
    if (!state)
        return 0;
    if (!real)
        real = (update_key_fn)dlsym(RTLD_NEXT, "xkb_state_update_key");
    return real(state, key, direction);
}

/* 10-24 22:49: same path one call later - after update_mask(NULL) Chromium does
 * xkb_keymap_num_mods(xkb_state_get_keymap(NULL)) and loops over mod_index_is_active.
 * NULL keymap -> 0 mods, so Chromium skips the loop (cbz) and carries on. */
struct xkb_keymap;
typedef struct xkb_keymap *(*get_keymap_fn)(struct xkb_state *);
typedef uint32_t (*num_mods_fn)(struct xkb_keymap *);
typedef int (*mod_active_fn)(struct xkb_state *, uint32_t, int);

struct xkb_keymap *xkb_state_get_keymap(struct xkb_state *state)
{
    static get_keymap_fn real;
    if (!state)
        return NULL;
    if (!real)
        real = (get_keymap_fn)dlsym(RTLD_NEXT, "xkb_state_get_keymap");
    return real(state);
}

uint32_t xkb_keymap_num_mods(struct xkb_keymap *keymap)
{
    static num_mods_fn real;
    if (!keymap)
        return 0;
    if (!real)
        real = (num_mods_fn)dlsym(RTLD_NEXT, "xkb_keymap_num_mods");
    return real(keymap);
}

int xkb_state_mod_index_is_active(struct xkb_state *state, uint32_t idx, int type)
{
    static mod_active_fn real;
    if (!state)
        return 0;
    if (!real)
        real = (mod_active_fn)dlsym(RTLD_NEXT, "xkb_state_mod_index_is_active");
    return real(state, idx, type);
}
