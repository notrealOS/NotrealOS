# notreal.tube

One video hub for YouTube, TikTok, Instagram and Vimeo, with no accounts. The PC does the heavy
lifting, so videos start in about a second and the phone only draws lists and plays a stream.

> Not affiliated with YouTube, TikTok, Instagram or Vimeo.

## Features

- Subscription feed from YouTube channel RSS, refreshed on the PC every 20 minutes (Shorts off by default)
- One search across all four sites, or one site at a time; results that won't play are dropped
- Paste a link to play it
- Thumbnail and spinner the moment you tap, then a full buffer before it plays (no stalls)
- Follow YouTube channels and TikTok accounts, channel pages
- Audio only (keeps playing with the screen off), save offline, SponsorBlock skips, resume where you stopped
- Left-handed: the buttons you use most sit on the left

## How it works

`hub.py` resolves streams with yt-dlp ahead of time and pipes each video to the phone as one
fragmented MP4 made by `ffmpeg -c copy` (seeking asks for a new stream from that second). YouTube
files are fetched in 10 MB `range=` pieces, which YouTube doesn't throttle. Search for the sites
without a yt-dlp search goes through a private [SearXNG](https://github.com/searxng/searxng) with
`site:` queries.

At home the PC streams. When Tailscale can only reach the PC through a relay (mobile data), the app
runs the same `hub.py --local` on the phone (127.0.0.1:8098) and fetches straight from the site;
feed and search still come from the PC.

## Run

```sh
cp -r apps/notreal.tube ~/tube    # the code expects to live here
~/tube/nrtube
```

Needs Python 3, PyGObject, GTK 4, libadwaita, GStreamer (gst-plugins-good/bad, gst-libav,
gst-plugins-rs gtk4 sink), ffmpeg and yt-dlp on the phone (for the mobile-data mode), and the
[theme](../../shell/theme) module.

## Needs

The hub on your PC (see [pc/tube](../../pc/tube)).

## Configure

`~/.config/notreal/pc.json` → `host` (and `ssh` for `deploy-hub`, which copies `hub.py` to the PC)
