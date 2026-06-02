# sportsbot - what to fill in (step by step)

A plain-English, click-by-click guide. You do **not** need to know any coding.
You will copy **two template files**, fill them in, then start the bot.

> ⚠️ **This bot places real bets with real money.** Take your time, double-check
> every value, and **start with small stake sizes** until you've watched a few
> bets place correctly.

**What this bot does:** it watches two Telegram tip groups - **Saiyan (AFL)** and
**Shook (NBA + MLB)** - and automatically places each tip on **your** bookmaker
accounts through HyperBot. It posts what it did into **your** Telegram channels.
Sports only - no racing.

You will end up editing **two files**, both in this folder:
- **`.env`** - your keys, tokens, channel ids, and stake sizes.
- **`sessions.yaml`** - the list of your bookmaker accounts.

You make those two by **copying** the `.env.example` and `sessions.example.yaml`
templates (Step 2). That way, when you update the bot later, your private files
are never touched.

---

## Before you start - you need these ready

**Software:**
- **Python 3.10 or newer** - https://www.python.org/downloads/. During install you
  **MUST tick "Add Python to PATH"** or the bot won't start.
- **Git** - https://git-scm.com/download/win (accept all the defaults). This is
  what lets you get the bot and later one-click update it.

**Accounts / access:**
- **HyperBot** - the separate app that actually places bets. Ask whoever gave you
  this bot for the installer + your **API key**, and have your bookmaker accounts
  logged in and their **sessions Started**. **sportsbot cannot place anything
  unless HyperBot is open with sessions Started.**
- A **Telegram account** (your normal one).
- A free **Groq** account (the AI that reads tips): https://console.groq.com
- Be a **member of both tip groups**, *Saiyan AFL* and *Shook Plays* - ask for
  invites. The account whose details you put in `.env` (Step 3) must be in both,
  or the bot sees no tips and gives no error.

---

## STEP 0 - Get the bot (one time)

Whoever set this up will give you a **repository link** (looks like
`https://github.com/SOMEONE/sportsbot`) and add you as a collaborator.

1. Make a folder for it, e.g. `C:\sportsbot`.
2. Open that folder in File Explorer, click the **address bar**, type `cmd`,
   press **Enter** (a black window opens there).
3. Type this (paste the link they gave you in place of THE-LINK):
   ```
   git clone THE-LINK .
   ```
   (the space-dot at the end means "into this folder".)
4. The **first time**, a GitHub login window may pop up - log in once and it
   remembers you.

> Later, to get new features, you just **double-click `update.bat`** - it pulls
> the latest code and **never touches your `.env` or `sessions.yaml`**.

---

## STEP 1 - Install the bot's requirements (one time)

In that same black window (or open one in the folder), type and press Enter:
```
python -m pip install -r requirements.txt
```
Wait until it finishes. If it says **"python is not recognized"**, Python isn't on
PATH - reinstall Python and **tick "Add Python to PATH"**, then try again.

---

## STEP 2 - Make your two settings files

In the folder you'll see `.env.example` and `sessions.example.yaml`. **Copy** each
to a new file WITHOUT the `.example`:

1. Right-click **`.env.example`** -> **Copy**, then right-click empty space ->
   **Paste**. Rename the copy to exactly **`.env`** (no `.example`, no `.txt`).
   - To see/rename these properly: in File Explorer turn on **View -> Show ->
     File name extensions** AND **Hidden items**.
2. Do the same for **`sessions.example.yaml`** -> rename the copy to exactly
   **`sessions.yaml`**.

From now on you edit **`.env`** and **`sessions.yaml`** (your private copies).
Open them with **Notepad** (right-click -> Open with -> Notepad). **Do NOT use
Word.** Each setting is `NAME=value` - type your value right after the `=`. The
help is on the `#` lines **above** each setting; **don't type notes after a value
on the same line** (it becomes part of the value and breaks the bot). Save with
**Ctrl+S**.

> The very first line, `SPORTSBOT_MODE=true`, must stay exactly as-is.

---

## STEP 3 - Telegram API ID + API HASH + phone (in `.env`)

1. Go to **https://my.telegram.org**, log in (it texts you a code).
2. Click **API development tools**, create an app (any title), and copy
   **api_id** (a number) and **api_hash** (a long string).

In **`.env`**:
- **`TELEGRAM_API_ID`** = the number (digits only).
- **`TELEGRAM_API_HASH`** = the hash.
- **`TELEGRAM_PHONE`** = your number, full international form, e.g.
  `TELEGRAM_PHONE=+61412345678`.

---

## STEP 4 - HyperBot API key (in `.env`)

- **`HYPERBOT_API_KEY`** = the key whoever gave you this bot provided. Keep it
  private. Leave **`HYPERBOT_BASE_URL`** exactly as-is.

---

## STEP 5 - Groq API key, free (in `.env`)

1. https://console.groq.com -> sign up (free, no card) -> **API Keys -> Create
   API Key** -> copy it (starts `gsk_`).
- **`GROQ_API_KEY`** = that key.

---

## STEP 6 - Your notification bot + channels (in `.env`)

1. In Telegram, message **@BotFather**, send **`/newbot`**, give it a name and a
   username ending in `bot`. Copy the **token** (includes a `:`).
   - **`NOTIFY_BOT_TOKEN`** = that token.
2. Make ONE Telegram **Group** for the bot's messages, and **add your new bot to
   it**. (One group for everything is easiest to start.)
3. Get the group's id: search **@getidsbot** in Telegram, **forward any message
   from your group** to it - it replies with a **negative** id (e.g.
   `-1001234567890`). Copy it exactly, including the minus sign.
4. Put that same negative id into all five lines:
   `NOTIFY_SUCCESS_CHAT_ID`, `NOTIFY_MANUAL_CHAT_ID`, `NOTIFY_MAINTENANCE_CHAT_ID`,
   `NOTIFY_CRITICAL_CHAT_ID`, `NOTIFY_CHAT_ID`.

> Every one of these must be a **negative group id**. If wrong, the bot's messages
> silently go nowhere.

---

## STEP 7 - Stake sizes (in `.env`)

- **`SAIYAN_UNIT_SIZE`** = AUD per unit for **AFL** (Saiyan).
- **`SHOOK_UNIT_SIZE`** = AUD per unit for **NBA + MLB** (Shook).
- **`MAX_UNITS`** = leave at `3.0`. **`LINE_TOLERANCE`** = leave at `1.0`.
- **`MLB_FLAT_STAKE`** = Shook's MLB is bet at a FLAT dollar amount (his main MLB
  play, "2+ HRRBI", is auto-placed as a 2-leg multi). **Leave this at `1`** until
  you've watched it place one MLB bet correctly, **then** raise it.

> 💰 Start small (e.g. $50-$100 units, MLB at $1) until you trust it.

---

## STEP 8 - Optional: your own test channel (in `.env`)

A safe way to prove the bot works before trusting real tips:
1. Make another Telegram **Group**, and add the same account from Step 3.
2. Get its id with @getidsbot (negative number).
3. Put it in **`SPORTSBOT_TEST_CHANNEL_ID`**.
Now any tip **you post** in that group is placed at just **$1**. Leave blank to
disable.

---

## STEP 9 - Find your HyperBot session ids, then fill the account lists

A **session id** is a number HyperBot gives each bookmaker account you've logged
in. You need them in both files.

### 9a. Don't know them? Run the bot once to print them
1. Make sure **HyperBot is running**, accounts logged in, **sessions Started**.
2. Fill in at least Step 3 (`TELEGRAM_*`) and Step 4 (`HYPERBOT_API_KEY`), save.
3. **Double-click `run.bat`**. First run: Telegram texts you a **login code** -
   type it into the window. If you have Two-Step Verification, it then asks for
   your password (you won't see the characters - that's normal).
4. In the window (or `logs\tipbot.log`) you'll see lines like:
   ```
   [foreign] sportsbet - session 11111 - your.account
   ```
   **The number after `session` is the id.** Write each one down.
   > 🟢 It's NORMAL on this run-once step to see accounts as `[foreign]` and even
   > a red CRITICAL "NO owned sessions" message - it goes away after you fill in
   > `sessions.yaml` (Step 10) and restart.
5. Close the window (or Ctrl+C).

### 9b. Fill the account lists in `.env`
Type ids **comma-separated, no spaces**. Leave a list **blank** to send that bet
type to your **manual** channel instead.
- **`AFL_SESSION_PRIORITY`** - accounts for **AFL singles** (Saiyan), best first.
- **`AFL_SGM_SESSION_PRIORITY`** - accounts for **AFL multis**.
- **`NBA_SESSION_PRIORITY`** - accounts for **NBA singles** (Shook).
- **`NBA_SGM_SESSION_PRIORITY`** - accounts for **NBA multis**.
- **`MLB_SGM_SESSION_PRIORITY`** - accounts for **Shook's 2+ HRRBI multi** (the
  main MLB play). Put your MLB accounts here.
- **`MLB_SESSION_PRIORITY`** - other MLB singles. **Leave BLANK** to send them to
  manual (safest) unless you want them auto-placed.
- **`SGM_BLACKLIST_SESSIONS`** - accounts to never use for multis (or blank).

> ⚠️ Every id you list here MUST also have a block in `sessions.yaml` (Step 10),
> or that bet goes to your manual channel.

---

## STEP 10 - Fill in `sessions.yaml` (your accounts)

Open **`sessions.yaml`** in Notepad. There's one example block at `"12345":`.

> ⚠️ This file is fussy about spacing - **exactly 2 spaces per indent, NEVER a
> Tab**. Safest: **copy the whole example block and change only a few values**.

For **each** account: copy the example block, then change:
- the id line `"12345":` -> your real session id (keep the quotes + colon),
- `name` -> any label, `bookmaker` -> lowercase (`sportsbet`, `bet365`, ...),
- leave `boost_eligible: false`. The liability caps are a safe start; lower them
  to match your real limits if you know them.

> ✅ Every id in your `.env` priority lists must appear here as a `"id":` block.

Save **`sessions.yaml`** (Ctrl+S).

---

## How to start it

1. Make sure **HyperBot is running** with sessions Started.
2. **Double-click `run.bat`**. First run only: type the Telegram login code.
3. Leave the window open. To stop: close it or press **Ctrl+C**.

> If you change `.env` or `sessions.yaml`, **close the window and run `run.bat`
> again** - the bot only reads them at startup.

---

## How to UPDATE the bot (when new features are added)

**Double-click `update.bat`.** It downloads the latest code and installs anything
new. Your **`.env`, `sessions.yaml` and login are NOT touched** - they're private
to you. Then start the bot again with `run.bat`.

> If `update.bat` says a new setting was added, open `.env.example`, find the new
> line, and copy it into your `.env`.

---

## How to know it worked

- The window prints `Startup sessions: N total (N owned, ...)`. After Step 10 your
  accounts should show as **owned** (not `[foreign]`). **0 owned = no bets can be
  placed** - check HyperBot.
- A **"started" message appears in your status group** (proves your bot token +
  group id are right).
- On a real tip you'll get a **"placed"** message, or a **"place by hand"** message
  if it couldn't place automatically.
- Try Step 8's test channel: post a tip there, expect a ~$1 placement.

---

## If it doesn't work

- **"TELEGRAM_API_ID must be an integer"** -> that line is blank or has letters,
  or you typed a note after the value. Put the number only, save, restart.
- **"... must be a number"** (a stake size) -> that line has something that isn't a
  number (often a note typed after the value). Fix, save, restart.
- **No login code / login fails** -> check `TELEGRAM_PHONE` is `+61...` and
  API_ID/HASH are from the same app. To redo login, delete the hidden
  `tipbot_session.session` file and start again.
- **Window closes instantly / "stopped. Press any key"** -> it hit an error; the
  text **just above** that line says what's wrong (also in `logs\tipbot.log`).
- **No "started" message** -> wrong bot token / group id, or the bot isn't in the
  group. Re-check Step 6.
- **`sessions.yaml load failed`** -> a spacing error (usually a Tab). Re-copy the
  example block; 2 spaces per indent, never Tab.
- **Lists 0 sessions** -> HyperBot isn't running / accounts not Started.
- **Tips go to "manual" not auto-placed** -> an id is in `.env` but missing from
  `sessions.yaml`, or that priority list is blank. Make them match, restart.
- **No tips arriving** -> you may have been removed from *Saiyan AFL* / *Shook
  Plays*, or you're logged in as a different Telegram account than the one in
  those groups.
- **`update.bat` fails** -> make sure Git is installed (Step 0). If it mentions
  local changes, you may have edited a code file by hand - send the window to
  whoever set up the bot. Your `.env`/`sessions.yaml` are safe.
- **Anything else** -> send `logs\tipbot.log` to whoever set up the bot.

> 💰 Reminder: real money. Start small, watch the first few bets, keep
> `RECONCILE_SPILL=false`, and keep `MLB_FLAT_STAKE=1` until you've seen MLB place.
>
> Note: the two tip groups (*Saiyan AFL* / *Shook Plays*) are built into the bot.
> If you're ever told to read a **different** group, ask whoever set it up.
