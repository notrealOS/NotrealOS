# Themes

One switch restyles the whole phone: every notreal app, the shell wallpaper, the overview
widget, the joystick launcher, the swipe keyboard, the Phosh top bar and lock screen, the icons,
and the music player (a vinyl in the vapor themes, a cassette in the others).

| Theme | Look |
| --- | --- |
| `vapor` | Vaporwave: pink/purple gradients, glow |
| `vaporlight` | Vapor, pastel and light |
| `micro` | Clean light UI, small type |
| `microdark` | Micro in dark |
| `alien` | Green-on-black CRT terminal |
| `nasa` | Cassette futurism: putty whites, one orange, Michroma type, no outlines |

## Install

```sh
cp -r shell/theme ~/theme          # the apps watch ~/theme/active
shell/theme/get-fonts.sh           # Michroma, Space Mono, Share Tech Mono, VT323 (all OFL)
~/theme/nrtheme                    # list the themes
~/theme/nrtheme nasa               # switch
```

Apps, wallpaper, widget, joystick and keyboard change live. Phosh (top bar, lock screen,
app drawer) reads its GTK3 CSS only at startup, so it changes at the next login.

## Files

- `nrtheme`: the switcher
- `themes/<name>.css`: GTK4 app colours and fonts; `<name>.override.css`: per-theme fixes;
  `<name>.gtk3.css`: Phosh, built by `build_gtk3.py`
- `notareal_theme.py`: helper the apps import to load the active theme and follow changes
- `icons.py`: recolours `icon-src/` into `~/.local/share/icons` for each theme
- `lockbg.py`: draws the lock-screen picture; `lock/<name>.png` overrides it.
  The original vapor picture isn't ours to publish, so vapor and vaporlight draw their own
  background unless you add `lock/vapor.jpg` yourself.
