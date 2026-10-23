# AureoClean — Desktop app (accounts module)

Automated restroom hygiene monitoring · STI College Caloocan

The rebuilt AureoClean desktop app. Included so far:

- **Sign-in and registration** for administrators and janitors, each with their own screens and permissions.
- **SMS sign-up**: after signing in, anyone can add a mobile number, confirm it with a texted code, and switch SMS alerts on or off.
- **Arduino IoT Cloud shortcuts**: an "Open" button for the monitoring dashboard (both roles), plus Arduino Cloud and Firebase Console (admins).
- **Desktop icon** for the Raspberry Pi menu and desktop.

Coming next: restroom alerts from the Realtime Database, then LLM recommendations, then sending those alerts by SMS.

```
PySide6 desktop app  ──HTTP (127.0.0.1:8765 only)──▶  FastAPI backend  ──▶  Firebase Auth + Firestore
   desktop/                                              backend/              (or a local SQLite file)
```

## How accounts work

| | Janitor | Administrator |
|---|---|---|
| Sign up | **Request access** with full name, username, email (and optional mobile). No password yet. | Needs a **one-time admin invite code** from an existing admin (can be locked to one email). |
| Approval | An admin approves → app shows a **one-time activation code** (8 chars, expires in 48 h, 5 wrong tries max). The admin gives it to the janitor in person. | The invite itself is the approval. |
| Finish | Janitor chooses **"I have an activation code"**, enters username + code, sets a password. Only now is the sign-in account created. | Creates the account straight away with the invite. |
| Sign in | Username **or** email + password. | Same. |
| Can do | Home (restroom alerts — placeholder until sensors are connected), open the monitoring dashboard, profile, **add/verify mobile number + SMS alerts on/off**, change password. | Review/approve/reject requests, re-issue codes, enable/disable users, **send a test SMS**, create/revoke admin invites, audit log, **Tools tab** (dashboard, Arduino Cloud, Firebase, SMS modem check), own SMS settings, change password. |

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

## Arduino IoT Cloud dashboard

The dashboard is **built into the app**: it's the first tab for both admins and janitors.
There is one shared dashboard view, so it stays loaded between sign-ins.

1. In Arduino Cloud open your dashboard and copy the link from the address bar.
2. Put it in `~/.config/aureoclean/aureoclean.env`: `AUREOCLEAN_DASHBOARD_URL=https://app.arduino.cc/dashboards/…`
3. Restart the app. Sign in to Arduino **once** inside the Dashboard tab — the sign-in is saved
   permanently (even Arduino's "session" cookie), so it survives sign-outs and restarts.

Use **A− / A+** to zoom the dashboard to fit the 7-inch screen (remembered), and **⟳ Reload** if it stalls.
Whoever is signed in to Arduino there decides what can be changed — a view-only Arduino account
shared with the dashboard is the safest choice.

Admins open Arduino Cloud and the Firebase Console from the **Tools** tab; those open in
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

**Check the modem first (recommended):** with AureoClean closed, run

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
5. Admin → **Tools** → **Check SMS modem** should say *registered* with a signal value; then
   **Users** → select a user with a verified number → **Send test SMS**.

> **⚠ SIM900A and the 2G shutdown.** The SIM900A only works on 2G. The NTC has
> ordered 2G/3G switched off nationwide by **31 Dec 2026** (PLDT/Smart has
> signalled its 2G may last longer, but don't rely on it). For anything beyond a
> short demo, use a 4G module such as a **SIM7600** or **A7670** — they speak the
> same AT commands, so only the wiring and power change, not the code.

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
  main.py          FastAPI routes + role checks
  service.py       all account + SMS rules (shared by both modes)
  sms.py           SMS senders: test mode (log) and GSM/LTE modem (AT commands)
  security.py      hashing, codes, validation, lockouts
  config.py        settings from ~/.config/aureoclean/aureoclean.env
  providers/       firebase.py (Auth + Firestore), local.py (SQLite)
desktop/
  app.py           window + navigation
  pages/           auth.py (sign-in, request, activate, admin register, status)
                   admin.py (admin console), janitor.py (janitor home)
  api.py, widgets.py, theme.py
tools/create_admin.py, tools/install_launcher.sh
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
- Google sign-in inside the dashboard relies on a browser-identity "quirk" (like the qutebrowser browser uses).
  If Google ever blocks it again, sign in to Arduino with email + password, or use a separate
  Arduino account (email + password) that the dashboard is shared with.
