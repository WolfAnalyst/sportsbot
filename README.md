# sportsbot - setup guide

This bot watches two Telegram tip groups - **Saiyan (AFL)** and **Shook
(NBA + MLB)** - reads each tip, and automatically places it on **your** bookmaker
accounts through **HyperBot**. It posts what it did into **your** Telegram
channels.

It only does **sports** (AFL, NBA, MLB). No racing.

You set it up **once**. After that you just leave it running, and one-click
**`update.bat`** whenever there's a new feature.

> 👉 **New here? Follow [`SETUP_STEPS.md`](SETUP_STEPS.md)** - a click-by-click,
> plain-English walkthrough. This README is just the overview.

---

## What you need

1. **Python 3.10+** (https://www.python.org/downloads/ - tick *"Add Python to
   PATH"*).
2. **Git** (https://git-scm.com/download/win - accept the defaults). Used to get
   the bot and to one-click update it.
3. **HyperBot** installed and running, bookmaker accounts logged in and **sessions
   Started** (ask whoever gave you this bot for the installer + your API key). The
   bot can't place anything if HyperBot isn't running with live sessions.
4. A **Telegram account** (your normal one).
5. A free **Groq** account (the AI that reads tips): https://console.groq.com
6. **Membership of the two tip groups**: *Saiyan AFL* and *Shook Plays*.

---

## Setup (once, in order) - details in SETUP_STEPS.md

1. **Get the bot:** install Git, then `git clone THE-LINK .` into a folder (the
   link + access are given to you).
2. **Install requirements:** `python -m pip install -r requirements.txt`
3. **Make your settings files:** copy `.env.example` -> `.env` and
   `sessions.example.yaml` -> `sessions.yaml`, then fill them in (Telegram /
   HyperBot / Groq keys, your notify channels, stake sizes, your HyperBot session
   ids). `SPORTSBOT_MODE=true` stays as-is.
4. **Start it:** double-click `run.bat`. First run, type the Telegram login code.
   You should see a startup message, "Listening for tips...", and a "started"
   message in your status channel.

---

## What gets auto-placed

- **Saiyan** - AFL (head-to-head, lines, totals, player disposals/goals/etc.,
  and AFL multis).
- **Shook** - NBA (points/rebounds/assists/etc. + multis) and **MLB**. Shook's
  main MLB play, **"2+ HRRBI"**, is auto-placed as a **2-leg same-player multi**
  (1+ and 2+) for better odds, at the flat `MLB_FLAT_STAKE` amount. Other MLB
  singles go to your **manual** channel by default.
- Anything the bot can't place automatically (event not up yet, odds drifted,
  unknown player, etc.) is posted to your **manual** channel so you can place it
  by hand.

---

## Day-to-day

- **Keep HyperBot running** with sessions Started, or bets can't be placed.
- **Updating:** double-click **`update.bat`** to get new features. Your `.env`,
  `sessions.yaml` and login are **never touched** (they're private to you).
- **If you change `.env` or `sessions.yaml`, restart the bot** (close the window,
  run `run.bat` again) - it only reads them at startup.
- Stop it: close the window or press `Ctrl+C`.

---

## Notes / safety

- This is **real money**. Start with **small unit sizes**, and keep
  **`MLB_FLAT_STAKE=1`** until you've seen MLB place correctly.
- Leave `RECONCILE_AMBIGUOUS` and `RECONCILE_SPILL` set to **false** unless told
  otherwise (advanced safety net that needs tuning).
- If tips stop arriving from a group, you may have been removed, or the group's id
  changed - check you're still a member, or ask for help.
- Logs are in the `logs/` folder if you ever need to show someone what happened.
