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

## Clothing relevance policy

`clothing_policy` in `stores.json` applies to garments identified by title or
product type. Existing store filters remain required. Ordinary clothing first
passes the 13-brand list; aliases live in `clothing_policy.py`. An explicit
outside manufacturer cannot be replaced by a preferred-brand title mention.
Configured retailer vendor labels may resolve the manufacturer from the title.
Brand, a new page, ordinary black denim, or scarcity alone is insufficient.
Distinctive fabrics, dyes, construction and collaborations are preferred.
Standard Type I/II/truckers require stronger distinction. Roomier text fit
signals still work without a private profile. When private measurement targets
are present, actual measurements outrank model-number assumptions. A wider hem
can remain a plausible tailoring candidate.

Garments matching an existing store team/city include list retain their legacy
eligibility and formatting, subject to the same exclusions, type and stock
filters. They bypass the new clothing whitelist and scoring. This currently
applies at Hat Club, Culture Kings US and New Era Cap. No include terms change;
ordinary non-team garments continue through the clothing policy.

New Era Cap is already a configured source. `New Era`, `New-Era`, `NEWERA` and
`neweracap` normalize to the same brand. `new_era_policy` requires New Era hats
at every source to match the exact existing include terms from New Era Cap.
Store exclusions, categories and stock filters still apply. Non-New-Era hat
eligibility is unchanged. No teams, city terms, stores or separate feeds are
added, and no separate city predicate is invented.

New listings and API-reported restocks retain the original ID/variant history.
Clothing also records per-variant price references in `_clothing_prices`;
upgrades seed silently. Discounts must reach both the configured percentage
and currency-specific amount floor. Unchanged listings and trivial discounts
emit no event. No state reset or manual `seen.json` edit is needed. With private
size references present, explicit restock/price-change labels must match a
category/brand reference or a measured fit match. A wrong-size restock does
not consume a simultaneous qualifying price drop. Team apparel retains its
legacy events and does not acquire new clothing price events.

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
profiles are treated as absent without logging their contents. Absent, empty,
or unusable profiles do not apply personal size gating or measurement fit
bonuses/penalties. Generic garment fit descriptions and useful discovery remain
active. Ownership adds context/lower purchase priority, never an exclusion or a
dedupe key. The repository contains no personal wardrobe list or fit defaults.
Owned entries may also use `{"brand": "PBJ", "tokens": ["Synthetic-Model-A"]}`
so manufacturer aliases and intervening title words do not lose the annotation.

The current tracker has **no live product-page personal-size verifier**.
Catalogue API stock is an event signal and cannot prove personal size availability.
Clothing messages retain an explicit unverified inventory/size warning, including
in fallback mode. Measurement rows retain source, raw values, units and method;
chart conflicts are retained. Measurements do not prove inventory. Legacy hats
and team apparel keep their original API-derived stock wording. RSS and
WooCommerce retain their existing limitations. No per-product crawling or rate
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
Sources, crawling, schedules, delivery, health monitoring and legacy filter
predicates remain unchanged. Actions environment-secret loading is exercised
with synthetic data in offline tests, without claiming deployed verification.
