# AureoClean

Automated restroom hygiene monitoring · STI College Caloocan

- **One set of screens** for the Pi's 7-inch display and for phones/laptops on the same Wi-Fi
  (`webapp/`), built from the "AureoClean UI Redesign" design: navy sidebar on the Pi, bottom
  tab bar on phones, light animations.
- **Admins**: Home (live sensors, today at a glance) · Reports (filter, details, notes, charts,
  Excel/PDF) · People (sign-up requests, users, admin invites) · System (health, SMS modem,
  SIM promo, kiosk mode, Wi-Fi website, activity log) · My account.
- **Janitors**: My jobs (big cards: *I'll take it* → *Done*, same as texting OK/DONE) ·
  Report a problem (tap the restroom, tap what's wrong) · Live sensors · My account.
- **SMS**: alerts, two-way OK/DONE replies, escalation, SIM promo reminders (GSM modem).
- **Public pages** (GitHub Pages, same design): https://aureo-clean.github.io/aureoclean/ —
  `report/` (QR report page), `login/` (janitor SMS sign-up), `qr/posters.html` (printable QR posters).

```
Pi window (desktop/shell.py) ─┐
                              ├─HTTP─▶ AureoClean service (backend/, 127.0.0.1:8765) ─▶ Firebase / SQLite
Phones on the same Wi-Fi ─────┘          + the web app (webapp/)         └─▶ GSM modem (SMS)
   (optional listener on :8080, private network addresses only)
```

`./run_local.sh --classic` opens the previous Qt screens (also used automatically if the new
window can't start).

## How accounts work

| | Janitor | Administrator |
|---|---|---|
| Sign up | **Request access** with full name, username, email (and optional mobile). No password yet. | Needs a **one-time admin invite code** from an existing admin (can be locked to one email). |
| Approval | An admin approves → app shows a **one-time activation code** (8 chars, expires in 48 h, 5 wrong tries max). The admin gives it to the janitor in person. | The invite itself is the approval. |
| Finish | Janitor chooses **"I have an activation code"**, enters username + code, sets a password. Only now is the sign-in account created. | Creates the account straight away with the invite. |
| Sign in | Username **or** email + password. | Same. |
| Can do | **My jobs** (take / done), **Report a problem**, live sensors, my account (**mobile number + SMS alerts**, password, lock PIN). | Home, Reports + charts, People (approve/reject requests, re-issue codes, enable/disable/delete users, test SMS, admin invites), System (health, SMS modem, SIM promo, kiosk, Wi-Fi website, activity log), my account. |

**The first admin** is created on the Pi with `tools/create_admin.py, tools/install_launcher.sh` (you need
physical access to the Pi and its service-account key, so that is the trust root).

### Security built in
- Every admin-only action is checked **on the server**, not just hidden in the UI. Janitor tokens get `403` on admin routes.
- Passwords are never stored by AureoClean — Firebase Authentication holds them (local mode uses scrypt hashes).
- Activation codes and invites are stored only as hashes, are single-use, expire, and are guarded against guessing.
- 5 wrong passwords lock that username/email for 15 minutes.
- Usernames are reserved atomically, so two people can never get the same one.
- Disabling a user signs them out immediately and blocks sign-in; the last active admin can't be disabled.
- Every approval, rejection, activation, invite, sign-in, disable/enable and password change goes to the audit log.
- The API listens on `127.0.0.1` only and also rejects any non-loopback request.
- `firestore.rules` denies all direct client access to these collections.

## Setup on the Raspberry Pi

Use **Raspberry Pi OS 64-bit** (PySide6 needs aarch64).

```bash
cd ~/aureoclean            # wherever you unzipped it
./setup_pi.sh
```

That creates `.venv`, installs everything, and creates the config file
`~/.config/aureoclean/aureoclean.env`. Open it and fill in:

```
AUREOCLEAN_MODE=firebase
AUREOCLEAN_FIREBASE_PROJECT_ID=aureoclean-b7c2b
AUREOCLEAN_FIREBASE_WEB_API_KEY=<Firebase Console → Project settings → General → Web API key>
GOOGLE_APPLICATION_CREDENTIALS=~/.config/aureoclean/service-account.json
```

Put the service-account key at `~/.config/aureoclean/service-account.json`
(never inside the app folder, never in a ZIP).

In the Firebase Console make sure **Authentication → Sign-in method → Email/Password**
is enabled, and merge `firestore.rules` into your Firestore rules.

### Create the first admin

```bash
.venv/bin/python tools/create_admin.py, tools/install_launcher.sh
```

If the email **already has a Firebase Authentication account** (for example
`chagotsave@gmail.com` from earlier testing), link it instead — nothing is
deleted and its password stays the same:

```bash
.venv/bin/python tools/create_admin.py, tools/install_launcher.sh --link-existing
```

### Run

```bash
./run_local.sh                 # windowed
./run_local.sh --fullscreen    # kiosk-style on the Pi screen
```

Backend log: `logs/backend.log`.

### Desktop icon (instead of an .exe)

`.exe` files are Windows programs and can't run on the Raspberry Pi. The Pi's
equivalent is a desktop launcher:

```bash
./tools/install_launcher.sh               # icon on the desktop + in the menu
./tools/install_launcher.sh --autostart   # also open full-screen when the Pi boots
./tools/install_launcher.sh --remove      # undo
```

If the service fails to start when opened from the icon, the sign-in screen
shows the error (full details in `logs/backend.log`).

## Live sensors (Arduino IoT Cloud, no Arduino sign-in)

The latest value of every Arduino Cloud variable is shown as simple tiles on **Home**: for admins
and janitors, on the Pi and on phones over the Wi-Fi website. It updates every 10 s. Nobody signs
in to Arduino: the Pi reads the values itself with an **API key**.

Set up once (admin):
1. Open <https://app.arduino.cc/api-keys>, sign in, press **Create API key** and name it "AureoClean".
2. Copy the **Client ID** and **Client Secret** (the secret is shown only once; it's also in the PDF
   Arduino lets you download).
3. In AureoClean: **Home → Live sensors → Connect Arduino Cloud**, paste both, press **Connect**.
   Things kept in an organization space also need its **Organization ID**.

The key is stored only on the Pi in `~/.config/aureoclean/arduino.json` (readable by this user only), or
put it in the config file instead: `AUREOCLEAN_ARDUINO_CLIENT_ID=…`, `AUREOCLEAN_ARDUINO_CLIENT_SECRET=…`,
optional `AUREOCLEAN_ARDUINO_ORG=…`. Never commit it. **Arduino settings** (under the tiles) changes or
disconnects it. Values not updated for 15 minutes are greyed out ("last seen …"); yes/no values named
like smoke, leak, flood or alarm show **Yes** in red.

The full Arduino dashboard (with its own Arduino sign-in) is still one tap away on the Pi:
**Full Arduino dashboard** under the tiles. Put its link in the config file as
`AUREOCLEAN_DASHBOARD_URL=https://app.arduino.cc/dashboards/…`.

Admins open Arduino Cloud and the Firebase Console from **System → Kiosk & web → Websites**; those open in
**Google Chrome** (install once with `./tools/install_chrome.sh`). In Chrome, turn on
*Settings → On start-up → Continue where you left off* so websites keep you signed in after closing Chrome.

## SMS alerts

**How a janitor (or admin) turns them on:** *My profile* → **SMS alerts** → type
the mobile number (`0917 123 4567` is fine) → **Send verification code** → type
the 6-digit code from the text → **Verify number**. SMS alerts are then on; the
checkbox turns them off and on. Numbers are only used after they are verified.

Limits: one code per minute, 3 codes per 15 minutes, 5 wrong tries per code, codes expire in 10 minutes.

**Test mode (default, `AUREOCLEAN_SMS_MODE=log`):** nothing is sent. Codes and
messages are written to `logs/sms_outbox.log`, so you can test the whole flow
before the modem is wired:

```bash
tail -n 5 ~/aureoclean/logs/sms_outbox.log
```

**Turn the modem on from the app (easiest):** sign in as admin → **System → SMS** → **Find & use modem**.
AureoClean searches the serial ports and speeds, switches to the modem immediately and remembers it
(saved in `~/.config/aureoclean/sms.json`, which overrides `AUREOCLEAN_SMS_MODE` in the config file).
**Check signal** shows network, operator and signal; **Send test SMS** texts any number;
**Use test mode** switches back to writing texts to `logs/sms_outbox.log`.

**Or check the modem from the Terminal:** with AureoClean closed, run

```bash
.venv/bin/python tools/sms_test.py                      # port, speed, SIM, network, signal, SMS centre
.venv/bin/python tools/sms_test.py --send 09XXXXXXXXX   # ...and send a real test text
```

It finds the right speed by itself and prints the exact settings to put in the config file.

**Real modem (`AUREOCLEAN_SMS_MODE=modem`):**

1. `sudo raspi-config` → *Interface Options* → *Serial Port* → login shell **No**, serial hardware **Yes** → reboot.
2. Wiring: modem TX → Pi GPIO15 (RXD, pin 10), modem RX → Pi GPIO14 (TXD, pin 8), GND ↔ GND.
   The Pi's pins are **3.3 V** — if the module's TX is 5 V, use a level shifter or a voltage divider.
3. Power the modem from its own supply (SIM900A boards want ~5 V **2 A**; it draws spikes when sending). Never from the Pi's 3.3 V pin.
4. Insert a SIM with load, set `AUREOCLEAN_SMS_MODE=modem` (port defaults to `/dev/serial0`, 9600 baud), restart.
5. Admin → **System → SMS** → **Check signal** should say *registered* with a signal value; then
   **Users** → select a user with a verified number → **Send test SMS**.

> **⚠ SIM900A and the 2G shutdown.** The SIM900A only works on 2G. The NTC has
> ordered 2G/3G switched off nationwide by **31 Dec 2026** (PLDT/Smart has
> signalled its 2G may last longer, but don't rely on it). For anything beyond a
> short demo, use a 4G module such as a **SIM7600** or **A7670** — they speak the
> same AT commands, so only the wiring and power change, not the code.

## Public web pages (GitHub Pages, `docs/`)

| Page | Link | What it does |
|---|---|---|
| Home | `https://aureo-clean.github.io/aureoclean/` | Two buttons: report a problem, janitor sign-up |
| **Report** | `https://aureo-clean.github.io/aureoclean/report/` | Anyone reports a restroom problem (replaces Google Forms) |
| **Sign-up** | `https://aureo-clean.github.io/aureoclean/login/` | Janitors sign up for SMS alerts |

Both pages work on phones and computers, in light and dark mode, with a keyboard and with
screen readers. They can only *create* entries (`firestore.rules`); they can't read anything.

**Sign-up, one per device:** each phone/computer browser gets a random device ID that becomes the
sign-up's ID in the database, so a second sign-up from the same device is refused. After the Pi
imports it, only an anonymous "already used" marker remains. (Clearing the browser's data gives a
new ID — duplicate mobile numbers are still refused.) Approving a sign-up makes the person an
**SMS contact** and texts a welcome message.

**Report questions** (`docs/assets/issues.json` — one list shared by the page, the Pi and the rules):
1. Which restroom? (filled in automatically from the QR code)
2. What's the problem? (several: emergency, wet floor, dirty, smell, trash, no soap/tissue/paper
   towels, no water, clogged, flush, leak, faucet, door/lock, light, something else)
3. How urgent? (can wait / soon / urgent or unsafe)
4. How clean is it right now? (1–5, optional)
5. Anything else? (optional text)
6. Contact for follow-up (optional name + number)

The Pi imports reports every 20 s into the **Reports** tab. Fixed rules (no AI) decide what
happens: emergencies are *critical* and text everyone; "urgent" texts the janitors. Too many reports
from one device (6/hour) are dropped, except emergencies.

**QR codes:** `docs/qr/` has a general code, one per restroom (`report/?r=CODE` fills in the
restroom), a sign-up code, and printable A4 posters (`docs/qr/posters.html`). For a new restroom:
`.venv/bin/python tools/make_qr.py 4F-M "4th Floor · Male"` and add it to `docs/assets/config.js`.

**Ready for AI analysis:** `backend/analysis.py` turns reports into clean records (no personal data),
computes patterns (by restroom, issue, hour, weekday, cleanliness, time to fix, recurring problems)
and builds a ready-to-send prompt; `analyze(service, ask=...)` sends it to any LLM you plug in.
`.venv/bin/python tools/export_reports.py` writes JSONL, CSV, summary and prompt files to `exports/`.

Setup: GitHub repo → Settings → Pages → Deploy from a branch → `main` / `docs`. Then paste
`firestore.rules` into Firebase Console → Firestore → Rules → Publish.

## Reports (admin) and My jobs (janitors)

Public reports arrive automatically (QR code / report page), janitors can **Report a problem**,
and admins can add **New report** (type, priority, place, details, optionally text the janitors
or everyone). Open a report for its details and history, add notes, mark it in progress / done
or reopen it. **Charts** shows the last 7/30/90 days; **Excel** downloads the data and **PDF**
saves a printable summary (on the Pi both go to ~/Documents).

Janitors see open jobs as big cards. **I'll take it** and **Done** do the same as texting
`OK 12` / `DONE 12`; whichever comes first counts, and someone else's job shows as "X is on it".

## Admin invites by email

People → Invites → **Invite admin** emails a one-time code to the new administrator (and texts
it, if you add their mobile number). They register on the sign-in screen under **Admin invite
code**, on the Pi or on the Wi-Fi website, using the same email address.

Set up email once in **System → SMS & email → Email** with a Gmail account and a Gmail **app
password** (Google Account → Security → turn on 2-Step Verification → App passwords → create one
for "AureoClean"; copy the 16 letters). The app password is stored on the Pi only
(`~/.config/aureoclean/mail.json`, readable by you only). Without email set up, the code is
shown on screen to pass on yourself.

## Website on Wi-Fi

System → **Kiosk & web** → *Website on Wi-Fi* lets phones and laptops on the **same Wi-Fi** open
AureoClean (everything works the same as on the Pi). The addresses and a QR code are shown
there, e.g. `http://raspberrypi.local:8080`. It only accepts private network addresses and
limits sign-in attempts per device; it is never reachable from the internet unless someone
forwards the port. Note: it's plain `http`, like most devices on a local network, so use it on
a Wi-Fi you trust (some school networks also block phones from reaching other devices).

## System health (System → Health)

**System → Health** shows, with a coloured dot each:
internet, Firebase, the SMS modem and signal, the SIM promo, whether texts are going out, the website import
and SMS-reply jobs, and the Pi's temperature, free storage and memory. When
something needs attention System shows a **!** badge (checked every 2 minutes).

**SIM promo:** if the modem's SIM is on a promo (e.g. Globe unli calls & texts for
15 days), enter it in System → SMS → **Set promo** (name, days, day registered).
System health counts down the days, and the admins get a text the day before it
ends and again if it has ended. **Sending texts** turns red when texts start
failing — e.g. the promo ran out, or it doesn't cover the other network.

Optional: to also read the balance from the network, add the code you'd dial on
a phone to `~/.config/aureoclean/aureoclean.env` as
`AUREOCLEAN_SIM_BALANCE_USSD=...`; a **Check balance** button then appears.

## Kiosk mode (7" screen)

Kiosk mode is a switch: **System → Kiosk & web**. While it's on, the app is full screen, can't
be closed by accident, and locks after 5 minutes without a touch (unlock with a 4–6 digit PIN
from My account → **Screen-lock PIN**, or the password; five wrong tries sign out; **Lock** in
the sidebar locks right away). Keypad keys react on touch-down and the PIN is checked on the Pi without
waiting for Firebase, so unlocking is instant. To leave it: turn the switch off, or press **Exit kiosk mode…**
on the sign-in screen (an admin's username and password are needed). The app always starts in a
normal window. Change the lock time with `AUREOCLEAN_IDLE_LOCK_MINUTES` (0 = never).

Optional: `./tools/install_service.sh` runs the service (texts, replies, website reports) in the
background from boot and restarts it if it crashes, so closing the window doesn't stop alerts.
Add `--autostart` to also open the app at boot; `--remove` undoes it.

## Updating to a new version

Double-click **Update AureoClean** on the desktop. It closes the app, installs the
new version, installs new Python packages only if needed, and opens the app again.
Your settings, accounts, key and logs are never touched.

- **Without GitHub:** download the new `aureoclean_app.zip` into Downloads first; the updater picks the newest one.
- **With GitHub (easiest):** run once `./tools/connect_github.sh https://github.com/OWNER/REPO.git`.
  After that the update icon downloads changes by itself — nothing to download by hand.

Only run `./setup_pi.sh` again on a new Pi, or if an update tells you to.

## Try it without Firebase (local mode)

Set `AUREOCLEAN_MODE=local` in the config file. Accounts are stored in
`~/.local/share/aureoclean/aureoclean.db`. Handy for demos and for working on
the UI on a laptop. Switch back to `firebase` for the real system — the two
modes don't share data.

## Walkthrough to test the whole flow

1. Sign in as the admin you created.
2. Sign out → **Request janitor access** → fill in the form.
3. Sign in as admin → *Janitor requests* → select it → **Approve** → note the code.
4. Sign out → **I have an activation code** → username + code + new password.
5. Sign in as the janitor → you get the janitor home, not the admin console.
6. As admin, open *Audit log* to see every step recorded.

## Firestore collections

All prefixed `aureo` (change with `AUREOCLEAN_COLLECTION_PREFIX`), so they never
collide with the older `securityApp…` collections or the complaints/QR data.

| Collection | Holds |
|---|---|
| `aureoUsers/{uid}` | Profile: username, full name, email, phone, role, status, timestamps |
| `aureoUsernames/{username}` | Username → request or user (makes usernames unique; used for username sign-in) |
| `aureoRegistrationRequests/{id}` | Janitor requests and their status (`pending → approved → activated`, or `rejected`) |
| `aureoAdminInvites/{hash}` | Admin invites (only the hash of the code is stored) |
| `aureoAuditLogs/{id}` | Security events |

## Tests

```bash
.venv/bin/pip install httpx       # only needed for the HTTP tests
.venv/bin/python -m unittest discover -s . -p "test_*.py" -v
```

## Project layout

```
backend/
  main.py          API routes, role checks, web app files, Wi-Fi website guard
  service.py       all rules: accounts, reports, janitor jobs, SMS, promo
  web.py           Wi-Fi website listener, security headers, rate limits
  two_way.py       SMS replies (OK/DONE) and escalation
  health.py        System health checks
  arduino.py       live sensor values from Arduino Cloud (API key)
  analysis.py      report statistics (charts, Excel, LLM-ready data)
  sms.py           SMS senders: test mode (log) and GSM modem (AT commands)
  providers/       firebase.py (Auth + Firestore), local.py (SQLite)
webapp/            the screens (HTML/CSS/JS, no build step): js/main.js, admin.js, janitor.js, …
desktop/
  shell.py         the Pi window: shows webapp/, Arduino dashboard, kiosk, downloads/PDF
  app.py, pages/   the classic Qt screens (./run_local.sh --classic)
docs/              public GitHub Pages: report page, sign-up link, QR posters
tools/             update.sh, install_launcher.sh, install_service.sh, create_admin.py, …
tests/
```

## Known limits (for the next modules)
- ~~Sessions live in memory~~ — **done:** sessions are saved (hashed) in the database and survive restarts.
- ~~Admin email not verified~~ — **done (Firebase mode):** admins who register with an invite must open the
  verification link Firebase emails them before they can sign in ("Resend verification email" on the sign-in
  screen). The first admin made with `tools/create_admin.py` is trusted and skips this.
- Activation codes are handed over in person: a janitor has no verified number before activation.
  Could be added by asking for an optional mobile number at sign-up.
- Restroom alerts are a placeholder until the Realtime Database sensor paths are wired in.
- Live sensors only show values; alerts from sensor values (fixed rules) come in the next module.
- The Full Arduino dashboard still needs an Arduino sign-in on the Pi (the tiles on Home don't).
