# Shopify New-Arrivals Tracker

Polls the public `products.json` endpoint of every store you follow and sends a Telegram message when a new product ID appears, or when a previously seen variant comes back in stock. No dependencies beyond Python 3 (standard library only).

## 1. Verify the store list

```bash
python3 tracker.py verify
```

This hits each domain in `stores.json` and tells you which ones respond. For any failures, open the store's page in the Shop app, tap through to its website, and correct the `domain` field. Then set `"verified": true` for every working store (or just run with `--all`).

Two stores need your eyes specifically:

- **Bloomr**: the name is ambiguous. Config currently points at bloomr.com (the UAE decor brand). If yours is a different Bloomr, fix the domain.
- **HINOYA**: their Shopify storefront may live on a different domain than hinoya.co.jp.
- **New Era / Culture Kings**: big brands sometimes run custom stacks; verify will tell you.

## 2. Set up Telegram (2 minutes)

1. Message **@BotFather** on Telegram → `/newbot` → pick a name. Copy the token.
2. Message **@userinfobot** → it replies with your numeric chat ID.
3. Send your new bot any message once (bots can't message you first).

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="123456789"
```

## 3. Test

```bash
python3 tracker.py run            # first run seeds state silently
python3 tracker.py run --dry-run  # later runs: prints what it would send
```

## 4. Filters (optional)

Edit `filters` in `stores.json`:

```json
"filters": {
  "include_keywords": ["fitted", "59fifty"],
  "exclude_keywords": ["youth", "toddler"],
  "notify_only_available": true
}
```

Empty `include_keywords` means notify on everything.

Stores that restock existing product pages (instead of creating new IDs) need
per-variant history, which is stored in `seen.json` under `_stock`. The first
run after an upgrade seeds that silently. To also poll a Shopify collection
such as Okayama Denim's new-restocks list:

```json
{
  "name": "Okayamadenim",
  "domain": "www.okayamadenim.com",
  "verified": true,
  "collections": ["new-restocks"]
}
```

That hits `/collections/<handle>/products.json` in addition to the store-wide
catalogue. Set `"catalog": false` to poll only the collection(s).

## 5. Schedule on the Mac Studio (launchd)

Save as `~/Library/LaunchAgents/com.tracker.shopify.plist`, fixing the two paths and credentials:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.tracker.shopify</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>/Users/YOURNAME/shopify-tracker/tracker.py</string>
    <string>run</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>TELEGRAM_BOT_TOKEN</key><string>123456:ABC...</string>
    <key>TELEGRAM_CHAT_ID</key><string>123456789</string>
  </dict>
  <key>StartInterval</key><integer>900</integer>
  <key>StandardOutPath</key><string>/tmp/shopify-tracker.log</string>
  <key>StandardErrorPath</key><string>/tmp/shopify-tracker.err</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.tracker.shopify.plist
```

900 seconds = every 15 minutes. Runs silently in the background, survives reboots.

## 6. Or run it free on GitHub Actions (works while the Mac sleeps)

Push this folder to a **private** repo, add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as repo secrets, and create `.github/workflows/track.yml`:

```yaml
name: track
on:
  schedule:
    - cron: "*/15 * * * *"
  workflow_dispatch:
permissions:
  contents: write
jobs:
  poll:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python3 tracker.py run
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
      - name: Persist seen state
        run: |
          git config user.name bot
          git config user.email bot@users.noreply.github.com
          git add seen.json && git commit -m "state" || true
          git push
```

Note: GitHub schedules aren't exact; expect 15 to 30 minute effective intervals.

## 7. Add or remove stores from Telegram

Send your bot a store link (share it from the browser or paste it) and the next
run validates it, adds it to stores.json, seeds it silently, and replies to
confirm. Send `remove domain.com` to drop a store. Only messages from your own
chat ID are honored. Requires the workflow to commit stores.json as well as
seen.json (see track.yml).

## 8. Quiet health notifications

Product-drop and restock alerts are unchanged. Health problems are grouped into
one message (at most 15 store names), rather than a failed/back pair per store:

- Individual fetch failures must last at least **1 hour and 3 failed polls**
- Empty catalogues lasting 24 hours and unverified stores join the same summary
- Ordinary health summaries repeat at most **once per 24 hours**, including when
  stores briefly recover or different stores start failing
- A majority of verified stores failing for **3 consecutive polling runs** can
  send one immediate widespread-outage escalation during that cooldown. This
  escalation is itself limited to once per 24 hours, so a normal alert followed
  by a new widespread outage can produce two messages in that window
- Recovery is silent and requires **3 successful non-empty polls**. Deferred
  stores do not count as failures or recoveries
- Cooldowns and incident history persist in `seen.json` under `_health`. Existing
  state migrates automatically; no reset or edits to `seen.json` are needed.
  Failed sends do not consume the cooldown, and dry runs do not send or consume
  the health-summary cooldown

Optional top-level `health` settings in `stores.json`:

```json
"health": {
  "fail_alerts_after": 3,
  "fail_alert_hours": 1,
  "recovery_polls": 3,
  "alert_cooldown_hours": 24,
  "empty_alert_hours": 24,
  "digest_every_days": 7,
  "quiet_flag_days": 14
}
```

The weekly quiet-store digest remains separate; set `digest_every_days` to 0 to
turn it off. Poll scheduling, retry behavior, filters and product notifications
are not affected by these settings. Health summaries reduce noise; they do not
resolve upstream HTTP 429 rate limiting. Run logs retain per-store errors.

Run offline regression tests without polling stores or sending Telegram messages:

```bash
python3 -m unittest -v
```

## Broad clothing feed

`clothing_policy` in `stores.json` admits garments from every brand at the
currently monitored stores. There is no brand whitelist, interest score,
preferred-fit threshold or reference-size rejection. Ordinary jeans, slim cuts,
black denim, standard truckers, shirts, hoodies and team apparel are eligible
when a new-listing, restock or meaningful price event occurs.

Socks and footwear are excluded by product type/title. Descriptions and
recommendations cannot reclassify an item. Known accessories/home goods and
unclassified merchandise are excluded from this clothing/hat feed. Garment
recognition does not rely on a fabric or preferred-brand mention in the body.
Stock checks remain required. Clothing bypasses store keyword/team/category
filters so those hat rules do not reject garments at the same stores.

Hats retain the complete existing store filters and New Era relevance gate.
New Era hats at every source still match the exact existing team/city terms
from New Era Cap. No team/city lists, stores, sources or hat formatting change.

New listings and catalogue-reported restocks retain the original ID/variant
history. All-brand garments now use the existing per-variant price references
in `_clothing_prices`; newly included products seed their prices silently.
Discounts must reach both the unchanged percentage and currency amount floor.
A simultaneous restock takes precedence over a price drop: one product/event
per poll. Hats, socks, footwear and other merchandise acquire no clothing price
events. Unchanged listings emit no event. No state reset, manual `seen.json`
edit or replay of previously rejected listings is needed.

One aggregate `Feed counts:` line reports garment, hat, sock/footwear and other
catalogue counts plus eligible/rejected event counts before the existing alert
cap. It contains integers only, no titles, identities, profile data or sizing
values. The cap and delivery/health behavior remain unchanged.

## Private clothing profile

Production reads `CLOTHING_FIT_PROFILE_JSON` directly from the Actions secret
of the same name. The workflow passes it only to the existing tracker step.
Install that secret separately before deployment; this local change does not
install or verify a deployed secret. For local use, `CLOTHING_CONTEXT_PATH` may
point to a private JSON file outside the checkout. An explicitly set environment
JSON wins over the path, including an empty or invalid value. The path is read
only when the environment variable is absent. No profile is written to state,
config, logs, caches or replay output.
Each tracker run prints one fixed startup status: `Clothing fit profile: loaded`,
`Clothing fit profile: absent`, or `Clothing fit profile: invalid, using fallback`.
Blank input or a missing selected file is absent; supplied malformed/unusable
JSON or another read failure is invalid. A valid selected local file is loaded.
This line contains no profile values, keys, counts, paths or exception details.

All values and labels below are **synthetic examples**, not personal defaults:

```json
{
  "fit": {
    "waist_range": [30, 32],
    "min_front_rise": 9.9,
    "min_thigh": 12.0,
    "min_knee": 7.0,
    "preferred_max_hem": 6.4,
    "min_acceptable_front_rise": 8.9,
    "min_acceptable_thigh": 11.2
  },
  "size_references": {
    "pants": ["S2", "31"],
    "jacket": ["J2"],
    "clothing": ["C2"]
  },
  "brand_size_references": {
    "Kapital": {"pants": [8], "jacket": [7], "clothing": [9]}
  },
  "owned_models": ["Synthetic-Owned-Model"]
}
```

References support multiple labels and numeric brand/category labels. All fit
numbers are positive inches; waist bounds are ordered. Unsupported/malformed
profiles are treated as absent without logging their contents. Fit and size
references are optional context for every garment and never change alert
eligibility, including when a profile is valid. Absent, empty or unusable
profiles simply omit that context. Ownership adds context/lower purchase
priority, never an exclusion or a dedupe key. The repository contains no personal wardrobe list or fit defaults.
Owned entries may also use `{"brand": "PBJ", "tokens": ["Synthetic-Model-A"]}`
so manufacturer aliases and intervening title words do not lose the annotation.

The current tracker has **no live product-page personal-size verifier**.
Catalogue API stock is an event signal and cannot prove personal size availability.
Clothing messages retain an explicit unverified inventory/size warning, including
in fallback mode. Measurement rows retain source, raw values, units and method;
chart conflicts are retained. Measurements do not prove inventory. Hats keep
their original API-derived stock wording. All garments use the explicit
inventory/size warning, including team apparel. RSS and WooCommerce retain
their existing limitations. No per-product crawling or rate
limit change is introduced. Recent production runs have reported HTTP 429 rate
limits. A nine-store replay is regression evidence and does not establish full
production reachability across the configured stores.
Marketplace adapters are not added.

Use `filters` for read-only catalogue previews. The legacy `run --dry-run` still
writes `seen.json`, so use the offline replay for event/dedupe comparisons:

```bash
python3 replay_policy.py captured_catalogues.json --state seen.json --baseline /path/to/unchanged/tracker.py
```

The capture is a JSON list of `{store, products, captured_at}` records. Replay
makes no network calls, sends no alerts and writes no state. Its CLI deliberately
does not load a private profile, so private context cannot be copied into review
artifacts. Tests may supply synthetic context directly. Replay distinguishes
conditional new-listing eligibility from actual events found against history.
Sources, crawling, schedules, delivery, health monitoring and hat filter
predicates remain unchanged. Actions environment-secret loading is exercised
with synthetic data in offline tests, without claiming deployed verification.


## Durable delivery recovery

Product events are queued before observation history advances. GitHub Actions
uses `delivery.json` on the dedicated `delivery-state` branch, through its existing
`contents: write` token. Each Telegram confirmation immediately checkpoints the
message ID there. This journal is independent of the end-of-run `seen.json`
commit, so a failed or cancelled runner cannot replay confirmed journal events.
Existing history and filter predicates are preserved. Per-product event sequences
and public stock/price observations are checkpointed atomically with queued events,
including polls with no eligible event. These durable observations reconcile stale
main history, preserving later genuine restocks and price drops while suppressing
unchanged-catalogue replay. Existing journals migrate without resetting receipts.

Failed sends remain queued even after the catalogue changes. Queued public
product data is rechecked against current store/filter rules before retrying;
rendered private profile context is never stored in the journal. Telegram 429
responses persist `retry_after` across runs and defer all sends until it expires.
Explicit rejections retry after at least a minute. A transport failure, interrupted
send, or lost acknowledgement is **ambiguous**: Telegram offers no idempotency
key or bot-message-history API. Such an event retries after at least 15 minutes
and duplicates remain possible on ambiguous Telegram outcomes, including repeated
ambiguous retries. This provides recoverable delivery, not exactly-once
semantics; successes from runs before this repair have no journal receipt and
cannot be retrospectively identified without Telegram readback.

Runs use a 240-second soft budget with bounded network calls, leaving room for
checkpointing before the existing six-minute job timeout. `Persist seen state`
also runs after failure/cancellation. Polling prioritizes the oldest store poll
timestamps, with configured order preserved for ties, so a budget-limited run
continues with previously deferred stores on the next regular dispatch. Minimum
interval checks and the external trigger cadence are unchanged.
Workflow concurrency remains serialized;
the journal additionally uses compare-and-swap and a 15-minute lease, refusing
concurrent writers. A killed runner's lease expires naturally. Local runs use
an atomic, fsynced journal and a process lock next to the script. Do not remove
or reset a delivery journal to resolve an error: doing so loses deduplication.

Offline coverage: `python3 -m unittest -v` includes success, failure, 429 cooldown,
restart/cancellation, lost main-history commits, concurrency and existing filter,
hat/team/city, restock and clothing-price regressions. No test sends live messages.

## Advertised clothing price references

Clothing prices retain independent references for each variant and compatible
currency/market context in `_clothing_prices` and the durable delivery journal.
A higher observation does not immediately replace the advertised reference.
Rearming requires both at least 24 hours since the first uninterrupted higher
observation and at least three successful higher observations in that same
context. The new reference is the lowest price in that higher sequence. A
return to the reference, invalid/missing price, missing variant/product, failed
fetch, or context change interrupts that sequence. Ordinary polling intervals,
store order, alert thresholds, hat/team/city filters and NEW/restock detection
are unchanged. Further meaningful reductions remain eligible.
When NEW/restock takes precedence, a lower observed price updates only that
variant's reference, including a small reduction already advertised by that
event. Higher observations still require the rearm guard.

References reserve queued reductions before sending, in the same checkpoint
as the event and stock/sequence observations. This prevents semantic repeats
under fresh event keys even when the main history commit is lost. A queued
price alert is rechecked immediately before delivery; equal, higher,
below-threshold, incompatible and unknown-context offers are suppressed.
Only eligible variant reductions appear in its message. Suppression retains
its original payload and pending/ambiguous status, without creating a receipt.
Confirmed receipts remain unchanged. A confirmation updates price metadata
atomically with the receipt. Failed checkpoints stop further writes and sends;
local journal replacement also fsyncs the containing directory.

Currency must be explicit in request configuration or catalogue data, agree
across sources, and use a recognized supported currency code. The display
fallback `$` supplies no currency evidence. Market/country fields, when
provided, are part of the context; conflicts or empty explicit fields are
quarantined. A new or changed context seeds silently before comparisons resume.
No exchange-rate inference or conversion is performed. Shopify payloads with
no currency evidence therefore produce no price alerts until explicit context
is available; their NEW/restock alerts remain eligible.

Legacy scalar price history and historical pre-send payloads migrate without
resetting state or modifying receipts. Legacy history lacks advertised-price
and context provenance: migration seeds conservatively, using the lower known
per-variant price when possible. Unknown-history/context seeding, and a queued
reduction that never successfully sends, can suppress a genuine price alert.
This deliberate tradeoff prevents inventing repeat offers. It never suppresses
NEW/restock events. Migration is idempotent, and newer durable metadata wins
over stale main state or older observation checkpoints. Migration does not
reconstruct deleted payloads from receipts or assume a product minimum applies
to every variant. Historical ambiguous Telegram outcomes remain ambiguous.
Whole observation records carry a revision so stale checkpoints cannot roll
back stock or event sequences while preserving newer prices. Rearming and
context reentry advance an offer generation; a superseded queued offer cannot
regain eligibility against a newer higher reference. Unversioned historical
offers after such a generation change are conservatively suppressed.
Legacy and newly discovered contexts without advertisement provenance remain
unproven until the first lower observation is silently absorbed, a lower
NEW/restock advertisement establishes a reference, or a fresh higher sequence
meets both rearm requirements. This avoids treating a surviving higher legacy
scalar as proof that an already advertised lower price is a new discount.
New queued price events must still own their exact per-variant reservation;
an event from a rejected stale observation cannot use a newer reference.
