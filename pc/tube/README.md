# notreal.tube on the PC

`hub.py` lives in [apps/notreal.tube](../../apps/notreal.tube) (the phone runs the same file for
mobile data). On the PC it keeps the feed warm, searches, resolves and streams on port 8099.

```sh
python -m venv ~/notrealOS/pc/tube/venv
~/notrealOS/pc/tube/venv/bin/pip install "yt-dlp[default]" deno    # deno solves YouTube's JS challenges

# private search engine for TikTok / Instagram / Vimeo results
cd ~/notrealOS/pc/tube
sed -i "s/CHANGE_ME/$(openssl rand -hex 24)/" searxng/settings.yml
docker compose up -d

cp ../systemd/videohub.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now videohub
tailscale serve --bg --tcp 8099 tcp://127.0.0.1:8099
```

Subscriptions are `~/.local/share/videohub/subs.json`, a list of
`{"src": "youtube", "id": "UC…", "name": "…"}` (or `"src": "tiktok", "id": "<handle>"`); the
app's FOLLOW button edits it.

Public search engines rate-limit repeated queries: `settings.yml` enables Google, Bing and Yahoo
next to the defaults, shortens SearXNG's suspensions to a few minutes, and the hub caches each
search for 10 minutes.
