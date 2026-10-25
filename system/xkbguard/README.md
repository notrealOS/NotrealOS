# xkbguard

LD_PRELOAD shim for a Chromium Wayland crash at launch: with several virtual keyboards, Chromium
can get keyboard modifiers before it has a keymap and calls libxkbcommon with NULL. The shim
makes those calls safe.

Build: `cc -shared -fPIC -O2 -o xkbguard.so xkbguard.c -ldl`, then start Chromium with
`LD_PRELOAD=/path/to/xkbguard.so`.
