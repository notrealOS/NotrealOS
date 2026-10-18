# notreal.mail

Swipeable mail for Gmail and Microsoft 365.

<p><img src="../../docs/screenshots/mail.png" width="260" alt="notreal.mail: mail"></p>

## Features

- Swipe right to **snooze**, left to **trash** (with Undo)
- INBOX / SNOOZED / SENT / SPAM / TRASH tabs
- Gmail over IMAP with an app password (kept in the keyring); Microsoft 365 and Outlook via GNOME Online Accounts + Microsoft Graph
- Unified inbox across accounts, tinted per account
- Attachments, save-as, translate, share files from notreal.share
- `vapormaild` checks every 15 minutes and sends notifications

## Run

```sh
cp -r apps/notreal.mail ~/vapormail    # the code expects to live here
~/vapormail/vapormail
```

Needs Python 3, PyGObject, GTK 4, libadwaita and the [theme](../../shell/theme) module on `PYTHONPATH` (or `notareal_theme.py` symlinked into `~/.local/lib/python3.*/site-packages/`).

## Needs

libadwaita, WebKitGTK 6 (to show HTML mail), libsecret, GNOME Online Accounts for Microsoft accounts
