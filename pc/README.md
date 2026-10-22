# The PC side

Services the phone uses over Tailscale. Read [docs/pc-link.md](../docs/pc-link.md) first for how
it fits together. Everything here was run on an Arch-based PC (CachyOS) with an RTX 5060 Ti 16 GB,
but any Linux PC with Docker and systemd works; the LLM needs a GPU with ~16 GB of VRAM (or
use a smaller model).

Clone this repo to `~/notrealOS` on the PC: the systemd units point there.

| Folder | Service | Port |
| --- | --- | --- |
| `llm/` | `nrbrain.py` AI gateway, `nrtts.py` Kokoro voice | 8097, 8100 |
| `matrix/` | Synapse + mautrix bridges (Docker) | 8008 |
| `music/` | Navidrome (Docker), `spin_anim.py` animated covers | 4533, 4535 |
| `feed/` | Redlib (Docker), `api.py` JSON for notreal.feed | 8095, 8096 |
| `tube/` | video hub for notreal.tube, SearXNG (Docker) | 8099 |
| `systemd/` | user units for the Python services | |

## AI: `llm/`

```sh
# llama.cpp (CUDA) and whisper.cpp, built in ~/llm
git clone https://github.com/ggml-org/llama.cpp ~/llm/llama.cpp
cmake -S ~/llm/llama.cpp -B ~/llm/llama.cpp/build -DGGML_CUDA=ON && cmake --build ~/llm/llama.cpp/build -j
git clone https://github.com/ggml-org/whisper.cpp ~/llm/whisper.cpp
cmake -S ~/llm/whisper.cpp -B ~/llm/whisper.cpp/build && cmake --build ~/llm/whisper.cpp/build -j

# models into ~/llm/models
#   Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf   (unsloth, Hugging Face)
#   ggml-small-q5_1.bin               (whisper.cpp models)

# Kokoro TTS in its own venv
python -m venv ~/llm/ttsvenv && ~/llm/ttsvenv/bin/pip install kokoro-onnx soundfile
```

Set your Tailscale IP in `systemd/nrbrain.service` (`NRBRAIN_HOST`), then:

```sh
cp systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nrbrain nrtts nrwhisper
loginctl enable-linger $USER     # keep them running without a login
```

`nrbrain` loads the model on the first request and unloads it after 10 idle minutes. If ComfyUI
is running on `:8188`, it is asked to free its VRAM first.

## Chat: `matrix/`

```sh
cd matrix && cp .env.example .env    # set PGPASS
docker compose run --rm synapse generate   # then edit synapse/homeserver.yaml: Postgres, your server name
docker compose up -d
```

Each bridge needs its own `config.yaml` and registration file; follow the
[mautrix docs](https://docs.mau.fi/bridges/) for WhatsApp, Meta (Messenger), Instagram and
Google Voice. Expose Synapse with `tailscale serve --bg https+insecure://127.0.0.1:8008`, then sign
in to notreal.chat with `https://<pc>.<tailnet>.ts.net`.

## Music: `music/`

```sh
cd music && docker compose up -d     # set your music path in docker-compose.yml first
```

`spin_anim.py` reads `~/.config/spin-anim.json`:

```json
{ "server": "http://127.0.0.1:4533", "user": "…", "token": "…", "salt": "…", "bind": "127.0.0.1", "port": 4535 }
```

(token = md5(password + salt), the Subsonic API login.) Needs `ffmpeg`.

```sh
systemctl --user enable --now spin-anim
```

## Feed: `feed/`

```sh
cd feed && docker compose up -d      # builds Redlib from source
systemctl --user enable --now reddit-api
```

## Video: `tube/`

See [tube/README.md](tube/README.md): a venv with yt-dlp, SearXNG in Docker, then
`systemctl --user enable --now videohub`.

## Expose to the tailnet only

```sh
for p in 4533 4535 8095 8096 8099; do tailscale serve --bg --tcp $p tcp://127.0.0.1:$p; done
```

Nothing here binds to `0.0.0.0`: from your home network or the internet these ports are closed.
