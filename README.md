# Playo

```text
           d8b
           88P
          d88
?88,.d88b,888   d888b8b  ?88   d8P  d8888b
`?88'  ?88?88  d8P' ?88  d88   88  d8P' ?88
  88b  d8P 88b 88b  ,88b ?8(  d88  88b  d88
  888888P'  88b`?88P'`88b`?88P'?8b `?8888P'
  88P'                          )88
 d88                           ,d8P
 ?8P                        `?888P'
```

**Your musics from Telegram.** A full-screen terminal music player that turns
any Telegram channel into your music library: every audio post becomes a
library entry (even before it is downloaded), songs stream while they
download, and synced lyrics scroll along with the music.

> Windows only right now (keyboard input uses the Win32 console API).

## Features

- **Full-screen dashboard** — library list + live lyrics + now-playing bar,
  everything on one screen, fully keyboard-driven.
- **Stream on demand** — picking a cloud track starts playing in seconds
  while the rest downloads in the background (no waiting for the full file).
- **Synced lyrics** — fetched from [LRCLIB](https://lrclib.net), line-by-line
  karaoke-style highlighting; falls back to plain lyrics.
- **Live catalog** — new channel posts appear in the library automatically
  while the app is open (bot watch or account polling).
- **Smart search** — press `Tab`, type a few letters, results filter as you
  type (spaces ignored, so `song4` finds `Song 4`).
- **Shuffle play** — `z` enables shuffle and starts a random track at once.
- **Sort & filter** — sort by title / artist / duration / recency; filter
  stays applied until you clear it.
- **Everything saved** — volume, sort mode, key bindings and settings persist
  across restarts (`~/.playo/config.json`).

## Quick start

### 1. One-time setup

Grab two values from Telegram first:

- **api_id** and **api_hash** — open
  [my.telegram.org](https://my.telegram.org) → *API development tools* →
  create an app (any name) → copy the two values.
- *(optional)* **bot_token** — from [@BotFather](https://t.me/BotFather) if
  you plan to use a bot as the channel listener. Without it, Playo uses your
  personal account (it will ask for your phone + Telegram code **once**).

Then run the setup wizard — it checks Python, installs every missing
dependency and saves your settings:

```bat
python setup.py
```

or on Windows, simply double-click **`setup.bat`**.

### 2. Run

```bat
python -m playo
```

If you prefer an installed command:

```bat
pip install -e .
playo
```

The first run indexes the channel (audio posts only — nothing is downloaded)
and then opens the dashboard.

## Keys

| Key | Action |
| --- | --- |
| `Tab` | arm search — type to filter, `Esc` clears |
| `↑` `↓` `PgUp` `PgDn` | move the selection |
| `Enter` | play the selected track |
| `Space` | play / pause |
| `n` / `p` | next / previous track |
| `z` | **shuffle play** — shuffle on + random track now |
| `s` | toggle shuffle |
| `y` | cycle sort: title → artist → duration → recent |
| `←` `→` | seek ∓5 s (or previous/next lyric line — change in settings) |
| `Ctrl`+`←` `→` | seek ∓30 s |
| `+` `−` | volume |
| `Del` / `m` | mute |
| `v` / `F4` | show / hide lyrics |
| `r` / `F8` | rescan the download folder |
| `F3` | settings (volume, sort, seek mode, channel, sync now, …) |
| `Esc` `Esc` | quit |

## Settings overlay (`F3`)

Highlights:

- **sync now** — re-index the *entire* channel history. The automatic
  background sync is incremental (only new posts); if some posts were ever
  missed, this full pass fills the gap.
- **seek mode** — `seconds` (←/→ = ±5 s) or `lyric` (←/→ jump between lyric
  lines).
- **channel / download folder / bot token** — editable inline.
- **user session** — switch between bot mode and personal-account mode.

## Where things live

| Path | What |
| --- | --- |
| `~/.playo/config.json` | settings |
| `~/.playo/catalog.json` | channel index (msg id ↔ title/artist/size) |
| `~/.playo/error.log` | error log |
| download folder (default `C:\Users\<username>\Music`) | the actual audio files |

## How it works

```text
Telegram channel ──► telethon (bot or personal account)
                        │
                        ▼
        catalog: every audio post, no downloads
                        │
                        ▼  pick a track
        .part file grows ──► mixer plays a separate .play copy
                        │     (hops to a fresh copy before the edge)
                        ▼
        download completes ──► file promoted, playback continues
                        │
                        ▼
        LRCLIB lyrics ──► synced line highlighting
```

- **Bot mode** — a bot with admin access listens live for new posts but
  *cannot* read channel history (Telegram restriction). Use it for
  fire-and-forget channels.
- **Personal-account mode** — indexes the full history with your own account
  (one-time phone + code login). Recommended.
- Only **one** Telegram session runs at a time; downloads use a separate
  session file so a live watch is never interrupted.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `ApiIdInvalidError` | the api_id/api_hash pair is wrong — re-run `python setup.py` |
| code never arrives | same as above; Telegram refuses to send codes for bad credentials |
| `Channel not found in dialogs` | open/join the channel once in Telegram with that account, then sync again |
| track stuck on `⇣` | it is cloud-only; press `Enter` to download/stream it |
| lyrics show `(no lyrics found)` | LRCLIB has no match for that title/artist |

## Development

```bat
git clone <repo>
cd playo
python setup.py        # deps + config
python -m playo
```

| File | Purpose |
| --- | --- |
| `playo/cli.py` | app core: catalog, playback order, streaming glue, REPL |
| `playo/tui.py` | the full-screen dashboard (render + key handling) |
| `playo/player.py` | pygame mixer wrapper (seek, streaming hops) |
| `playo/library.py` | folder scan + tag reading (mutagen) |
| `playo/lyrics.py` | LRCLIB fetch + LRC parsing |
| `playo/telegram_sync.py` | Telethon: index, watch, download, login |
| `setup.py` | dependency check/install + first-run wizard |

## License

MIT
