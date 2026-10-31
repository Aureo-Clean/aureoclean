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

Use **Raspberry Pi OS 64-bit** (Bookworm or Trixie). Check with `uname -m` → `aarch64`.

```bash
sudo apt update && sudo apt install -y git
cd ~
git clone https://github.com/Aureo-Clean/aureoclean.git
cd aureoclean
./setup_pi.sh
```

`setup_pi.sh` installs everything, including **Qt WebEngine**, which the app's screens need. If pip has no
PySide6/WebEngine build for your Pi, it uses Raspberry Pi OS's own packages instead. If WebEngine still
can't be installed it stops with a message — without it the app could only show its old classic screens
(and it says so in a pop-up if that ever happens).

### Storage: Firebase always, with a secure local copy

`~/.config/aureoclean/aureoclean.env` → `AUREOCLEAN_MODE=firebase` (recommended) — fill in the Web API key,
put the service-account key at `~/.config/aureoclean/service-account.json`, and (for the sensors) the
Realtime Database address in `AUREOCLEAN_FIREBASE_DATABASE_URL`.

* The app reads and writes the **Pi's local copy** first (fast), and a background sync sends every change
  to Firebase and pulls changes made there — every 10 s while online. When the Wi-Fi drops, everything
  keeps working; changes wait in an outbox and go up by themselves when it's back.
* **Encrypted**: the local copy is encrypted (AES-256-GCM) with `~/.config/aureoclean/local.key` (owner-only).
  Back that key up with the service-account key — without it the local copy can't be read (Firebase still has everything).
* **Accounts**: signing in online also keeps a hashed copy on the Pi, so the same people can sign in offline.
  Accounts made **while offline** are marked `createdLocally`, their hashed credentials go to the separate
  `localCredentials` collection, and they're created in Firebase Authentication (same UID) once online.
  No password is ever stored in plain text.
* **In Firebase** the collections have plain names — `users`, `reports`, `deletedReports`, `janitorRequests`,
  `adminInvites`, `auditLog`, `counters`, `insights`, `localCredentials`. Data under the old names
  (`aureoUsers` …) is copied over on the first sync and left untouched (delete it in the console once
  you've checked). Times are real Firestore timestamps; every document has a `_meta` map (`updatedAt`,
  `device`, `offline`). Sign-in sessions stay on the Pi. Merge `firestore.rules`.
* **Sensors**: Arduino Cloud writes the boards' values to the Realtime Database; the Pi listens live and
  shows whichever value is newer (Firebase or the Arduino API), on the dashboard and to the smart alerts.
  It understands Arduino webhook pushes and a clean `sensors/<Thing>/<variable>` layout, and keeps a tidy
  summary at `sensorLatest/<thing>` = `{temperature, iaq, …, updatedAt}`.
* **System → Data** (admins): sync status, *Sync now*, and the local data itself — after re-entering your
  password, for 5 minutes, logged in the activity log; codes and password hashes are always hidden.
* `AUREOCLEAN_MODE=local` keeps everything on the Pi only (still encrypted).

### Deleting reports

Report → **Delete** (with a reason) moves it to the **deleted bin** (Reports → *Deleted*): hidden from every
list, chart and job, restorable for 30 days, then removed from the Pi and Firebase. *Delete forever* skips
the wait. Every step is in the activity log.

Turn on the serial port for the SMS modem: `sudo raspi-config` → *Interface Options* → *Serial Port* →
login shell **No**, serial hardware **Yes** → reboot.

### Create the first admin

```bash
.venv/bin/python tools/create_admin.py
```

If the email **already has a Firebase Authentication account** (for example
`chagotsave@gmail.com` from earlier testing), link it instead — nothing is
deleted and its password stays the same:

```bash
.venv/bin/python tools/create_admin.py --link-existing
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

Your Arduino Cloud dashboards are shown inside the app, in AureoClean's style: on **Home** (admins and
janitors, on the Pi and on phones over the Wi-Fi website) and full screen with **Full view**. Nobody
signs in to Arduino: the Pi reads everything itself with an **API key**.

- **Tabs = your Arduino dashboard pages**, with the same names and in the same order (e.g. "Urinal",
  "Page 2", "Page 3" …). Multi-page dashboards are read through Arduino's v3 dashboards API; add a page
  or widgets in Arduino Cloud and they appear here within a minute (an empty page shows a short note).
  A page with an active alarm (smoke, leak …) gets a red dot on its tab. With several dashboards, a
  dashboard switcher sits next to the page tabs. With no dashboards yet, there's one page per thing.
- **Widgets** follow Arduino's: gauges, percentage rings, LED/status lights, on/off, sliders as level
  bars, values with a small 6-hour trend line, charts (last 6 hours), messages and sticky notes.
  Controls are shown read-only — change values in Arduino Cloud.
- **Interactive:** tap any tile for its details — a big chart for the last 1 h / 6 h / 24 h (touch or drag
  along it to read the value at that moment), lowest / average / highest, and when it last updated.
  Charts on the dashboard can be touched to read values too.
- **Controls:** switches, push buttons (hold), sliders, steppers and messenger boxes change the device,
  like in Arduino Cloud. Only variables that are *Read & Write* in Arduino Cloud can be changed; the Pi
  checks the type and the widget's min/max, and every change is logged (System → Log). Admins can always
  use them; janitors only after an admin turns on **Full view → Arduino settings → Janitors can use the
  controls**. Colour, map and schedule controls stay in Arduino Cloud.
- Values update in place every 5 s with gentle animations (the ring next to "Updated" counts down to
  the next refresh). If Arduino can't be reached, the last values stay on screen with a note.

Set up once (admin):
1. Open <https://app.arduino.cc/api-keys>, sign in, press **Create API key** and name it "AureoClean".
2. Copy the **Client ID** and **Client Secret** (the secret is shown only once; it's also in the PDF
   Arduino lets you download).
3. In AureoClean: **Home → Live sensors → Connect Arduino Cloud**, paste both, press **Connect**.
   Things kept in an organization space also need its **Organization ID**.

The key is stored only on the Pi in `~/.config/aureoclean/arduino.json` (readable by this user only), or
put it in the config file instead: `AUREOCLEAN_ARDUINO_CLIENT_ID=…`, `AUREOCLEAN_ARDUINO_CLIENT_SECRET=…`,
optional `AUREOCLEAN_ARDUINO_ORG=…`. Never commit it. **Full view → Arduino settings** changes or
disconnects it. Values not updated for 15 minutes are greyed out ("last seen …"); yes/no
values named like smoke, leak, flood or alarm show **Yes** in red (and **No** in green).

Arduino's own dashboard (with its own Arduino sign-in) is still there on the Pi: **Full view →
Arduino's own dashboard**. Put its link in the config file as
`AUREOCLEAN_DASHBOARD_URL=https://app.arduino.cc/dashboards/…`.

Admins open Arduino Cloud and the Firebase Console from **System → Kiosk & web → Websites**; those open in
**Google Chrome** (install once with `./tools/install_chrome.sh`). In Chrome, turn on
*Settings → On start-up → Continue where you left off* so websites keep you signed in after closing Chrome.

### Alerts page (admins)

**Alerts** in the menu shows everything that can raise a job or a text: *Happening now*, the **automatic
alerts** (built in, each can be switched off except fire/heat; *Limits & units* sets the limits and each
unit's restroom), **suggested alerts** (from the last 3 days of readings — e.g. a cubicle occupied 20+ min,
damp air that keeps coming back, a blocked ToF sensor; the AI adds its own ideas when it's set up — tap
**Add** and it becomes one of your alerts), **your alerts**, and the Arduino Cloud / SMS connection.
New suggestions also come every morning with the day's advice.

### Smart monitoring (trends → jobs and texts) and today's advice

Every ESP32 unit with the AureoClean variables (`temperature`, `iaq`, `iaqAccuracy`, `gasResistance`,
`sensorStatus`, `totalToFUsers`/`totalUses`, `totalFlushes`, `flushThreshold` …) is watched by the Pi every
5 seconds — fixed rules, no AI, no internet to the AI needed. Set each unit's restroom once:
**Alerts → Limits & units** (or Home → tap a unit → Smart settings).

| Trend | What happens |
|---|---|
| ≥ 50 °C for 30 s · rising 8 °C in 2 min (and ≥ 38 °C) · heat + sudden bad air together | **Emergency**: texts everyone active today (signed in or replied by SMS today, plus SMS-only contacts), at any hour |
| Above 35 °C for 10 min | job; 20 min → texts the janitors |
| BSEC IAQ above 150 for 10 min (raw gas 40 % below this hour's normal while IAQ calibrates) | job (says whether it followed heavy use or the unit was barely used); 20 min, or IAQ > 250 for 5 min → texts the janitors |
| Uses since the last cleaning ≥ 120 (ToF count) | "Cleaning due" job; 1.5× → texts the janitors. Marking a cleaning job done, or **Cleaned it** on the unit, starts the count again |
| Uses keep coming but `totalFlushes` doesn't move | "Auto-flush may be broken" job |
| BME680 error status for 5 min · no data for 10 min | job for the admin |

* **Quiet hours 7 PM–5 AM**: no texts except emergencies; held texts go out at 5 AM if the job is still open,
  and only emergencies escalate at night. (This applies to every alert, not just the sensors.)
* One job per problem; it can move up (job → text janitors → emergency), never down. A report people already
  made about the same thing in that restroom gets a "Sensors agree" note instead of a second job.
  Back to normal for 2 min → a note; marked done but still happening → a new job after 30 min.
* It learns each unit's normal per hour of the day and counts uses per hour (first from Arduino's last 3 days).
* **Today's plan** (Home, admins and janitors): when quiet hours end, the AI (if set up) writes the janitors'
  plan — clean before the busy hours, when the air usually gets bad, what's due — and advice for the admins,
  sometimes with a suggested setting an admin can apply with one tap. Without the AI, the same patterns are
  turned into advice by fixed rules. The AI never decides alerts.
* All limits are in **Smart settings**; saved on the Pi in `~/.config/aureoclean/smart.json` (learned data:
  `smart_state.json`). **System → Health** shows "Smart monitoring" and offline units.

### Sensor alerts (sensor limits → reports and texts)

A sensor value over its limit becomes a report and texts people — fixed rules on the Pi, no AI.
Set one up as admin: tap a value on **Live sensors** (e.g. *Temperature*) → **Set an alert…**, or
**Sensor alerts → Add alert**. Choose:

| | Example |
|---|---|
| When | *goes above* / *goes below* a limit, or *turns on* / *turns off* (yes/no values) |
| Limit | `35` °C · BME680 gas resistance *below* `40000` Ω (it drops when the air gets worse) · IAQ *above* `200` |
| For at least | right away … 1 hour (ignores short spikes) |
| Then | **Emergency** — report + text everyone · **Urgent** — report + text the janitors (admins after 10 min) · **Report only** — no text |
| Restroom | where the sensor is (goes in the report and the text) |

AureoClean suggests a first limit from what the value measures (temperature, humidity, BME680 gas/IAQ,
ammonia, CO₂, smoke, leaks, soap/tissue levels); the detail chart shows the limit as a red dashed line, so
watch it for a day and adjust. The Pi checks every 15 seconds, even with no screen open.

* One report per problem: the text looks like
  `AureoClean SENSOR ALERT #14: Temperature too high: 38.4 C at 2nd Floor - Male. Limit above 35 C. Reply OK 14 or DONE 14`
  — janitors reply **OK 14 / DONE 14** as usual.
* While the report is open, going over the limit again only adds a note. Back to normal for 2 minutes →
  a "Back to normal" note (the janitor still marks it done). Marked done but still over → a new report after 30 minutes.
* Readings older than 3 hours (device off?) never trigger or clear an alert. At most 12 sensor reports an hour
  (emergencies always go through). Tiles over a limit turn amber, red with the report number once the alert went out.
* Rules are kept on the Pi in `~/.config/aureoclean/sensor_alerts.json`; changes are in the audit log.
  **System → Health** shows "Sensor alerts" (and fails if Arduino Cloud can't be read).
* Texts need the GSM modem (not test mode) and janitors with a verified number (*My profile → SMS alerts*).
* **Sensor alerts** shows, for every alert, *why* it has or hasn't fired yet ("Normal: now 31 °C",
  "Over the limit for 1 min — a report after 5 min", "the last reading is 4 h old…"), and whether the checker is
  running. **Test** makes a report marked TEST and sends the texts now, to check reports and SMS end to end.

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

**AI analysis:** `backend/analysis.py` turns reports into clean records (no personal data),
computes patterns (by restroom, issue, hour, weekday, cleanliness, time to fix, recurring problems)
and builds the prompt that **AI insights** (below) sends.
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

## AI insights (advice only)

**Reports → AI insights.** An AI model reads the last 30 days of reports and the live sensor readings
(latest values plus each sensor's last-6-hour minimum, maximum, average and trend) and writes:
a one-line summary, **What to do** (ranked Do first / Soon / When you can, with where, when and why),
patterns, sensor notes, a supply plan, repairs and notes about the data. **Ask about your restrooms**
answers a question from the same data ("Which restroom needs attention first today?"). **PDF** saves it.
Janitors see the top 3 suggestions as **Suggested today** on My jobs.

- **Set up once:** pick a provider and paste its key; it's tested before it's saved.
  - **Google Gemini** — free key, no card: <https://aistudio.google.com/app/apikey>. Leave **Model** empty:
    AureoClean asks Google which models the key can use and picks the newest regular Flash model.
    Free-tier data may be used by Google to improve its products.
  - **Claude (Anthropic)** — paid key from <https://console.anthropic.com/settings/keys> (default `claude-haiku-5-5`).
  - **Other (OpenAI-compatible)** — e.g. a model on the Pi with Ollama (`http://127.0.0.1:11434/v1`,
    no key, slow: minutes per run), Groq, OpenRouter.
- **Retired models fix themselves:** providers retire models (Google turned off `gemini-2.5-flash` in
  October 2026). When the model is gone, AureoClean looks up the newest one, saves it and carries on.
- **Kiosk mode:** outside websites (like AI Studio) show as a QR code to open on your phone. Setting up
  the key from a phone (Wi-Fi website) is easiest — paste it there once.
- **Every morning** (AI settings, default 6:00 AM) it makes fresh insights by itself; System health shows
  if that fails. One run at most every 2 minutes and 30 questions an hour, to protect the key.
- **Privacy:** report text is sent with phone numbers and e-mails removed; reporter names, numbers and
  device IDs are never included. The key stays on the Pi (`~/.config/aureoclean/llm.json`, mode 600) or
  in the config file (`AUREOCLEAN_LLM_PROVIDER`, `AUREOCLEAN_LLM_API_KEY`, optional `AUREOCLEAN_LLM_MODEL`,
  `AUREOCLEAN_LLM_URL`).
- **Safety:** the AI only suggests. It never creates or changes reports and never decides or sends alerts —
  emergencies are handled by the fixed alert rules the moment a report arrives. The screens label
  everything as AI suggestions to check before acting.

### AI in reporting (advice only)

- **AI tip on each report:** new reports get a tip in the background (summary, likely cause, steps,
  what to bring, about how long, safety, who else may be needed, and any repeat pattern from earlier
  reports at the same restroom). Admins see it on the report page (**New tip** to redo it); janitors see
  "Bring: …" on the job card. Up to 25 automatic tips a day (switch in AI settings); more on request.
- **Say it in your own words:** in *Report a problem* and *New report*, type e.g. "baradong inidoro sa
  2nd floor male" and **Fill in for me** picks the restroom, the problems and a clean note. The person
  checks and sends. The AI never selects **Emergency**: if it sounds dangerous it says so, and the person
  decides (Emergency still texts everyone by the fixed rule).
- **Earlier here:** each report page lists earlier reports from the same restroom (worked out without AI).
- **Written summary:** *Charts* → **Write AI summary** writes a short report for the 7/30/90 days shown
  (what went well, what to watch, next steps); it's included in the PDF.
- **Reports page:** the latest AI headline, linking to AI insights.

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
waiting for Firebase, so unlocking is instant.

In kiosk mode the window covers the Pi's own on-screen keyboard, so AureoClean shows its **own
keyboard** whenever a text box is tapped (letters, ?123 symbols, a number pad for PINs and phone
numbers, hold ⌫ to keep deleting, ⌄ to hide it; tap the box again to bring it back). Outside kiosk mode
the Pi's normal keyboard is used. Typing on a real keyboard hides it. To leave it: turn the switch off, or press **Exit kiosk mode…**
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
  arduino.py       Arduino Cloud dashboards, values and history (API key)
  sensor_alerts.py sensor limits → reports and texts (fixed rules, checked every 15 s)
  smart.py         smart monitoring: trends from the ESP32 units → jobs, texts, quiet hours
  advice.py        today's plan for janitors and admins (AI, or fixed rules without it)
  sensor_feed.py   live sensor values from the Firebase Realtime Database
  firestore_format.py  how documents look in Firestore (timestamps)
  localcrypt.py    encryption of the local copy
  providers/synced.py  Firebase ⇄ local copy sync, offline accounts
  alert_ideas.py   suggested alerts from the 3-day trends (and the AI), added with one tap
  llm.py           AI providers (Gemini, Claude, OpenAI-compatible / Ollama)
  insights.py      AI insights and questions (advice only), daily run
  report_ai.py     AI tips on reports, filling the report form, written period summaries
  analysis.py      report statistics (charts, Excel, LLM-ready data)
  sms.py           SMS senders: test mode (log) and GSM modem (AT commands)
  providers/       firebase.py (Auth + Firestore), local.py (SQLite)
webapp/            the screens (HTML/CSS/JS, no build step): js/main.js, admin.js, janitor.js, sensors.js, insights.js, keyboard.js, …
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
