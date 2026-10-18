# notreal.share

Send files to a chat or attach them to a new mail.

> No screenshot: its list is your own chats.

## Features

- From the file manager (Open With → Share, or Scripts → Share) or from our apps (`nrshare FILE...`)
- MAIL first, then your chats newest first (SMS can't carry files, so they're left out)

## Run

```sh
cp -r apps/notreal.share ~/share    # the code expects to live here
~/share/nrshare
```

Needs Python 3, PyGObject, GTK 4, libadwaita and the [theme](../../shell/theme) module on `PYTHONPATH` (or `notareal_theme.py` symlinked into `~/.local/lib/python3.*/site-packages/`).

## Needs

notreal.chat and/or notreal.mail
