# Water testers

OpenPoolPro can take water readings from digital testers and in-pool monitors
instead of having techs type them. The staff app runs in Safari on the iPhone,
and as far as we know Safari can't talk to Bluetooth devices, so every
integration goes through the vendor's cloud or through a file the vendor's app
exports. Nothing here needs a native app.

## Supported testers

| Tester | How readings arrive | Parameters used on the visit | Rough price |
|---|---|---|---|
| **PoolLab 2.0 / PoolWaterLAB** (Water-i.d.) | LabCOM cloud API (automatic) | FC, pH, TA, CYA (+ total/combined chlorine, hardness shown) | under $500 |
| **PoolLab 1.0** | LabCOM cloud, via the LabCOM phone app | same | lower |
| **PrimeLab 1.0 / 2.0 Pool Kit** | LabCOM cloud API (automatic) | same, more parameters | $1,499–$1,899 |
| **Ondilo ICO** (in-pool monitor) | Official Ondilo Customer API, OAuth | pH, salt (+ ORP, temperature shown) | homeowner device |
| **iopool EcO** (in-pool monitor) | Official iopool API, per-owner API key | pH (+ ORP, temperature shown) | homeowner device |
| **eXact iDip** | CSV file the app emails | whatever was tested | $200–$400 |
| **Hanna** meters that use the Hanna Lab app | CSV export | whatever was tested | varies |
| **LaMotte Spin Touch / ColorQ** | CSV from WaterLink Connect 2 (Windows, USB) | whatever was tested | ~$2,000+ |
| Any other tester that exports CSV | File import with column matching | whatever columns it has | — |

### Deliberately not supported

* **Blue Connect (Blueriiot), Sutro, WaterGuru.** They only work through
  unofficial, reverse-engineered APIs, and each one would need the
  *homeowner's* app password stored here. We won't hold customer passwords, and
  an unofficial API can break with no notice.
* **Flipr.** The company is in liquidation, so its cloud may go away.
* **Direct Bluetooth to a LaMotte Spin Touch** (the way Skimmer's native app
  does it). Safari can't do Bluetooth. Its results still come in through the
  WaterLink Connect 2 CSV export.

## Setup

Everything lives under **Settings → Chemistry → Water testers** (`/testers`, admin only).

### PoolLab / PoolWaterLAB / PrimeLab (LabCOM)

1. Sign in at labcom.cloud with the account the testers sync to, open user
   settings, and copy the API token.
2. Paste it under *PoolLab · PoolWaterLAB · PrimeLab* and press **Sync now**.
3. After the first sync each tester shows up under *Testers and who carries
   them*. Assign it to the tech who carries it.
4. Optional but recommended: in the LabCOM app, create one LabCOM account per
   customer and pick it before testing. Each account then appears under *LabCOM
   accounts not linked yet*. Link it to the pool once, and every future test
   from that account goes to that pool automatically.

Tests sync every 15 minutes. A tech pressing **Find tester readings** on a visit
also triggers a sync, limited to one every 45 seconds.

### Ondilo ICO

1. Set **Settings → System → Public base URL** if staff reach the app by a LAN
   address. Ondilo sends the browser back to that URL after sign-in.
2. Press **Connect Ondilo** and sign in with the company's Ondilo account.
3. Each homeowner shares their pool with that account's email from the ICO app.
4. Press **Load shared pools** and link each one to its pool.

Ondilo allows 30 API requests per hour. Pools are read at most hourly, 25 per
run; with more linked pools they're read in rotation.

### iopool

The homeowner opens the iopool app → *More → Settings → API key* and sends you
the key. Enter it with their pool under *iopool*. The key is checked with
iopool before it is saved, and it's stored encrypted. If one account has
several pools, the app asks which one you mean.

### File import (iDip, Hanna, LaMotte, anything CSV)

Any staff member can use **Import a file** (`/water-tests/import`), including
from the tester panel on a visit. The app guesses what each column is from its
heading, and the uploader confirms or corrects it. Both layouts are handled:
one row per test, and one row per parameter. Dates are read as US month/day
in the business time zone, and Fahrenheit temperatures are converted.
Re-importing the same file adds nothing.

## How a reading gets tied to the right pool

A reading never names one of our pools. It's tied to a pool in exactly one of
three explicit ways:

1. **Link.** A LabCOM account, Ondilo or iopool pool, or a customer name from
   an imported file has been linked to a pool. New readings from it go straight
   there.
2. **On the visit.** At the pool, the tech taps **Find tester readings**. The
   list only offers:
   * readings already tied to *this* pool, and
   * unassigned readings from a tester assigned to *this tech*, or a file *this
     tech* uploaded, taken in the last 4 hours.

   Readings tied to another pool never appear. If the tester's own name for the
   reading doesn't resemble this customer, a warning appears on it.
3. **Inbox.** Office staff (with *manage customers*) assign whatever is left at
   `/water-tests`. Name and address matches are shown as suggestions only. Tick
   *Always send … here* to create a link for the future.

Safeguards:

* Choosing a reading only fills in the form. The tech still reviews the numbers
  and presses **Mark done**.
* When the visit is saved, the server checks again that the reading belongs to
  this pool (or is from this tech's tester), hasn't been used on another visit,
  and was taken within 12 hours of the visit. If any check fails, the visit
  still saves the typed numbers, the reading is *not* credited, and the refusal
  goes in the audit log.
* A reading fills at most one visit.
* The visit shows where its numbers came from (source, tester serial, time), and
  whether the tech edited them before saving.
* A reading already on a visit can't be moved from the inbox. Reopen the visit
  to change it.
* Every link, assignment, ignore, and refusal is audit-logged.

Unused readings are deleted after 120 days. Readings used on a visit are kept.
