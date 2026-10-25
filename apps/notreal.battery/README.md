# notreal.battery

Where the battery went: you using the phone, or the phone on its own. `nrbatteryd.py` logs
charge, screen and sleep in segments (`~/.local/share/nrbattery/*.jsonl`); the app draws them.
Also switches the [battery saver](../../system/saver).

Run: `./nrbatteryd.py &` then `./nrbattery`
