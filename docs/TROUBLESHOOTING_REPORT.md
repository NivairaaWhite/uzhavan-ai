# Uzhavan.AI — troubleshooting report

Scope: the whole repo (`backend/`, `mobile/`, `ai_model/`, launcher scripts, docs).
Method: ran the backend and its tests, then ran it against a **real PostgreSQL**
(the Docker stack uses Postgres, the unit tests use SQLite, and several bugs only
show on Postgres), reproduced each problem before fixing it, and re-ran after.

## What was verified, and how

| Area | Result |
|---|---|
| Backend tests (Python 3.11, same as the Dockerfile) | 25/25 passed before; **50/50 pass now** (25 original + 25 new regression tests, incl. migrations on real Postgres) |
| Alembic on Postgres | `upgrade head` OK; `downgrade base` then `upgrade head` **failed** before, passes now; `alembic check` shows no model/migration drift |
| Order race (12 concurrent buyers, 1 listing, real Postgres row locks) | exactly 1 order succeeds |
| Flutter app | **Not compiled** — no Flutter/Dart SDK reachable from the analysis environment. Dart files were syntax-parsed (all OK) and reviewed by hand. Run `flutter analyze` first. |
| Model training / TFLite export | **Not run** (no dataset, no GPU). Checked against the Ultralytics 8.3.28 source instead. |
| `start.bat` | Not run (no Windows). Fixed the structural causes listed below. |
| Docker stack | Not run (no Docker daemon). Compose file and scripts reviewed only. |

## Bugs fixed

### Security
1. **Anyone could register as `admin`** (`POST /auth/register` accepted `role: admin`)
   and then use admin-only endpoints (facility onboarding, logistics trigger).
   Now 403. Admins are created with `python -m app.create_admin <phone>`.
2. **Any logged-in user could "confirm payment" on any order**, and a valid
   signature could drag a `released` / `cancelled` order back to `escrow_held`.
   Now: buyer only, `PENDING` orders only, idempotent on retry.
3. **JWT secret placeholder was accepted in production.** `ENV=production` now
   refuses to start with a placeholder or < 32 char `JWT_SECRET_KEY`.
4. **Public marketplace leaked exact farm GPS coordinates and farmer ids.** The
   unauthenticated list now shows no farmer id and ~1 km-rounded coordinates
   (owners still see full precision on `/crop-listings/mine`).
5. **Offline-sync dedupe was not scoped to the user** — submitting another
   user's `offline_scan_uuid` returned *their* listing. Now per-user.

### Correctness (backend)
6. **Orders could be oversold** — stock was never reserved. Now reserved under a
   row lock; listing becomes `sold` at 0; unpaid orders expire after
   `PENDING_ORDER_TTL_MINUTES` (new Celery task) and return the stock.
7. **Razorpay errors were unhandled 500s** (provider down, or `confirm-payment`
   on an order with no payment session). Now 502 / 409 / 503 and stock is released.
8. **Pooling always labelled pools `district="Unknown"`** (it read a column that
   does not exist) and **one farmer with 3 listings formed a pool with
   themselves.** District now comes from member farmers; pools need N distinct farmers.
9. **Fair price was missing for every new listing until the next 06:30 run**, so
   buyers got "Listing has no price set yet" on anything scanned that day
   (the mobile app never sends a target price). Priced on creation now, hourly
   recompute (as the docs claimed), farmer's district preferred, case-insensitive.
10. **`/logistics/run` returned 500 when Redis was down** → 503 with a clear message.
11. **Invalid `listing_id` in `/post-harvest/recommend` → 500** on Postgres
    (foreign-key violation) → 404 (and ownership is checked).
12. **No latitude/longitude range validation** (e.g. 999 accepted) → 422.
13. **Phone numbers were compared as raw strings**, so `9876543210` and
    `+919876543210` were two accounts and login failed for the other spelling.
    Normalised; old accounts can still log in.
14. **IMPS payouts used the API key id as the debit account number.** Now a
    separate `RAZORPAY_X_ACCOUNT_NUMBER` setting, with a clear error when unset.
15. Post-harvest advisor counted cancelled orders as demand and matched district
    case-sensitively; scraper stored duplicate price rows on every re-run and one
    bad price cell ("NA") dropped a whole crop. All fixed.
16. Farmers had no way to see orders on their listings → added `GET /orders/received`.

### Migrations
17. **`alembic downgrade base` left the Postgres enum types behind**, so the next
    `upgrade head` failed with `type "userrole" already exists`. Downgrade now drops them.

### Mobile / AI contract
18. **Synced scans were never marked as synced.** The app matches the server's
    reply on `offline_scan_uuid`, but the server never returned it, so the queue
    never cleared and every sync re-uploaded everything. Server now echoes it.
19. **The model's defect labels (`defect_rotten`, …) were sent as the grade** and
    the server rejects them with 422 for the **whole batch** — one bad scan would
    block all others forever. Mapped to `rejected`; unsendable rows are skipped and reported.
20. **The TFLite code only understood a classifier output `[1, N]`, but the repo's own
    pipeline trains a YOLOv8 *detection* model** (`[1, 4+N, anchors]`), which would
    fail at the first scan. The app now reads the real output shape and handles both.
    It also trims label lines (CRLF `labels.txt` broke every label), applies EXIF
    rotation, and reports *why* a model failed to load.
21. **Location permission was never requested**, so on a fresh install every scan
    was saved at latitude 0 / longitude 0 (and the post-harvest screen queried
    facilities near the equator). Shared `LocationService` now asks, and the app
    refuses to save a scan without a real fix. Quantity ≤ 0 is also refused.
22. Pending-sync badge never refreshed (service was not a `ChangeNotifier`); query
    parameters were not URL-encoded; an expired JWT produced a generic error forever
    (now clears the token; a sign-out button was added); camera-permission failure
    left an endless spinner; the marketplace dropdown lacked 3 of the 6 crops;
    removed unused `hive_generator`/`build_runner` dev dependencies (a common
    `pub get` version-solver conflict); the "Logistics Pools" tile did nothing.
23. `train_yolo.py` passed `augment=True`, which is not training augmentation in
    Ultralytics (ignored while training) — removed, comment explains the real knobs.

### Windows launcher
24. **`start.bat` / `stop.bat` had Unix (LF) line endings** and non-ASCII dashes. `cmd.exe`
    mis-parses labels and `goto` in LF-only files and prints garbage for the dashes —
    plausibly behind the earlier "Docker engine offline" start failures. Rewritten
    as ASCII + CRLF, with the Docker-wait loop moved into a subroutine instead of a
    `goto` into a parenthesised block. `.gitattributes` keeps `.bat` CRLF and `.sh` LF.

## Not fixed — needs your decision

- **Escrow is never released.** No endpoint moves an order past `escrow_held`
  (no delivery confirmation / dispute / release), and `create_route_transfer` is
  called nowhere. Farmers cannot be paid via the API yet.
- **Pooled stock can't be sold** — pooled listings leave the marketplace and nothing links a buyer to a pool.
- **Pools aren't capacity-bounded** (DBSCAN can chain beyond the radius; no truck capacity).
- **Advisor capacity is not decremented** although the model docstrings say it is
  (left alone on purpose: decrementing on every query would drain storage forever).
- **`kyc.py`, `bhashini.py`, `routing.py` and `voice_service.dart` are not wired** to any endpoint/screen.
- **No sign-up screen** in the app; i18n JSON files are not loaded by any code.
- Postgres (5432) and Redis (6379) are published on the host with the dev password
  in `docker-compose.yml` — fine on a laptop, restrict before exposing the machine.
- `create_order` is `async` but uses synchronous DB calls (blocks the event loop
  under load); acceptable for now, convert to a threadpool or async driver before scale.
- A payment captured after its order expired needs a webhook/refund path.

## Things to do on your side

1. `cd mobile && flutter create . && flutter pub get && flutter analyze` — and see
   `mobile/README.md` for the Android permissions / `10.0.2.2` emulator address.
2. If `flutter analyze` flags `DropdownButtonFormField(initialValue:)`, your Flutter is
   older than that parameter — switch those three lines (scan, marketplace and post-harvest screens) back to `value:`.
3. If your Docker volume was created by an older version, nothing needs migrating —
   no table or column changed; only new Python modules and one migration `downgrade`.
