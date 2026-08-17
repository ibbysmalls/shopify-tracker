#!/usr/bin/env python3
"""
Shopify new-product tracker.

Usage:
  python3 tracker.py verify          # check which stores expose products.json
  python3 tracker.py run             # poll all verified stores once, notify on new items
  python3 tracker.py run --dry-run   # poll but print instead of sending Telegram messages

Environment variables (required for notifications):
  TELEGRAM_BOT_TOKEN   from @BotFather
  TELEGRAM_CHAT_ID     your numeric chat id (message @userinfobot to get it)

State is kept in seen.json next to this script. The first run seeds state
silently (no notification flood); subsequent runs notify on new product IDs
and on restocks (a known variant going from unavailable to available).

Designed to be run on a schedule: launchd/cron on a Mac, or GitHub Actions.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "stores.json")
STATE_PATH = os.path.join(BASE, "seen.json")

UA = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    # A real browser sends all of these. Some WAF rules reject requests that
    # claim a browser User-Agent while omitting them, which is cheap to fix
    # and occasionally the whole reason a store 403s.
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-CH-UA": '"Chromium";v="126", "Not.A/Brand";v="24"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"macOS"',
    "Connection": "keep-alive",
}


def http_get_json(url, timeout=20, retries=3):
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            last_err = e
            # Cloudflare bot protection. Retrying cannot help: the challenge
            # needs a JS engine and issues a cookie bound to IP + user agent,
            # so a datacenter runner will never pass it. Fail loudly instead.
            body = ""
            try:
                body = e.read(2000).decode("utf-8", errors="replace").lower()
            except Exception:
                pass
            if e.code in (403, 503) and "cloudflare" in body:
                raise RuntimeError(f"BLOCKED (Cloudflare {e.code})")
            if e.code in (429, 503) and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))  # back off: 2s, 4s
                continue
            raise
    raise last_err


def try_json(url, timeout=10):
    """Non-raising probe. Returns parsed JSON or None."""
    try:
        return http_get_json(url, timeout=timeout, retries=1)
    except Exception:
        return None


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def products_url(domain, limit, collection=None):
    if collection:
        handle = str(collection).strip().strip("/")
        return f"https://{domain}/collections/{handle}/products.json?limit={limit}"
    return f"https://{domain}/products.json?limit={limit}"


def store_collections(store):
    """Collection handles this store should poll, in config order."""
    cols = []
    if store.get("collection"):
        cols.append(store["collection"])
    for c in store.get("collections") or []:
        if c not in cols:
            cols.append(c)
    return cols


def variant_in_stock(variant):
    """True when a variant is sellable. Shopify storefront JSON exposes
    `available`; some payloads also include inventory_quantity."""
    qty = variant.get("inventory_quantity")
    try:
        qty = int(qty) if qty is not None else None
    except (TypeError, ValueError):
        qty = None
    if qty == 0 or variant.get("available") is False:
        return False
    if variant.get("available") is True:
        return True
    if qty is not None and qty > 0:
        return True
    return False


def snapshot_variants(product):
    """Map variant id -> {available, title} for restock comparisons."""
    snap = {}
    for v in product.get("variants") or []:
        vid = v.get("id")
        if vid is None:
            vid = v.get("title") or "default"
        snap[str(vid)] = {
            "available": variant_in_stock(v),
            "title": v.get("title") or str(vid),
        }
    return snap


def restocked_titles(product, prev_snap):
    """Variant titles that are in stock now and were missing or unavailable."""
    titles = []
    for vid, cur in snapshot_variants(product).items():
        if not cur["available"]:
            continue
        prev = (prev_snap or {}).get(vid)
        if prev is None or not prev.get("available"):
            titles.append(cur["title"])
    return titles


def window_seed_ids(catalog_products, prev_limit, limit):
    """IDs that are only visible because products_per_store grew.

    Shopify /products.json is newest-created first. Positions after the
    previously seeded window are older catalog items, not new drops.
    """
    try:
        prev_limit = int(prev_limit)
        limit = int(limit)
    except (TypeError, ValueError):
        return set()
    if limit <= prev_limit:
        return set()
    ids = [str(p["id"]) for p in catalog_products if "id" in p]
    return set(ids[prev_limit:])


def diff_store(products, seen_ids, prev_stock, seed_ids=None):
    """Compare one poll to persisted IDs and per-variant availability.

    seen_ids: previously known product IDs.
    prev_stock: product_id -> {variant_id: {available, title}}, or None if
    this store has never recorded variant availability (first run or upgrade).
    seed_ids: IDs to record silently (window-growth older catalog items).
    They are not "new", and first sight of their variant state does not
    emit a restock — same as the upgrade seed.

    Returns (events, current_ids, next_stock). events is a list of
    (kind, product, extra) where kind is "new" or "restock" and extra is
    restocked variant titles for restocks. First sight of a store, or of
    variant state, never emits restocks — that seeds silently so we don't
    spam on deploy.
    """
    seen = {str(i) for i in seen_ids}
    seed = {str(i) for i in (seed_ids or [])}
    current_ids = [str(p["id"]) for p in products if "id" in p]
    events = []
    next_stock = {} if prev_stock is None else dict(prev_stock)
    stock_ready = prev_stock is not None

    if not seen:
        next_stock = {str(p["id"]): snapshot_variants(p)
                      for p in products if "id" in p}
        return events, current_ids, next_stock

    for p in products:
        if "id" not in p:
            continue
        pid = str(p["id"])
        snap = snapshot_variants(p)
        if pid not in seen:
            if pid not in seed:
                events.append(("new", p, None))
        elif stock_ready and pid in prev_stock:
            titles = restocked_titles(p, prev_stock[pid])
            if titles:
                events.append(("restock", p, titles))
        next_stock[pid] = snap

    return events, current_ids, next_stock


def woo_products_url(domain, limit):
    return (f"https://{domain}/wp-json/wc/store/v1/products"
            f"?per_page={limit}&orderby=date&order=desc")


def woo_price(prices):
    """Woo returns prices as integer strings in minor units, with the exponent
    in currency_minor_unit: "26900" + minor_unit 2 -> "269.00"."""
    if not isinstance(prices, dict):
        return None
    raw = prices.get("price")
    if raw in (None, ""):
        return None
    try:
        minor = int(prices.get("currency_minor_unit", 2))
        return f"{int(raw) / (10 ** minor):.{minor}f}"
    except (TypeError, ValueError):
        return str(raw)


def normalize_woo(products, domain):
    """Reshape WooCommerce Store API products into the Shopify shape the rest
    of this script already understands. Adds _url, since Woo uses
    /product/slug/ while Shopify uses /products/slug."""
    out = []
    for p in products:
        price = woo_price(p.get("prices"))
        in_stock = bool(p.get("is_in_stock"))
        # Store API exposes variation ids and attribute values but not
        # per-variation stock, so availability falls back to product level.
        variants = []
        for v in p.get("variations") or []:
            label = " / ".join(str(a.get("value"))
                               for a in (v.get("attributes") or [])
                               if a.get("value"))
            variants.append({"title": label or str(v.get("id")),
                             "available": in_stock,
                             "price": price})
        if not variants:
            variants = [{"title": "Default", "available": in_stock, "price": price}]

        cats = p.get("categories") or []
        out.append({
            "id": p.get("id"),
            "title": p.get("name"),
            "handle": p.get("slug"),
            "vendor": "",
            "product_type": cats[0].get("name", "") if cats else "",
            "variants": variants,
            "_url": p.get("permalink") or f"https://{domain}/product/{p.get('slug')}/",
        })
    return out


def fetch_products(domain, limit, platform="shopify", collections=None,
                   catalog=True):
    """Return a list of products in Shopify shape, whatever the platform.

    Shopify stores may list collection handles; those are fetched (and, by
    default, the store-wide catalogue too) and merged by product id.
    """
    platform = (platform or "shopify").lower()
    if platform == "rss":
        items, why = probe_rss(domain, limit)
        if items is None:
            raise RuntimeError(why)
        return items, items
    if platform == "woocommerce":
        data = http_get_json(woo_products_url(domain, limit))
        if not isinstance(data, list):
            raise RuntimeError("unexpected shape from Woo Store API")
        items = normalize_woo(data, domain)
        return items, items
    return fetch_shopify_products(domain, limit, collections, catalog)


def fetch_shopify_products(domain, limit, collections=None, catalog=True):
    """Pull /products.json and any configured collection product lists.

    Returns (merged, catalog_products). catalog_products is the store-wide
    newest-created list (empty when the catalogue is not fetched), used to
    tell a real new drop from an older ID that only appeared because the
    poll window grew.
    """
    collections = list(collections or [])
    sources = list(collections)
    if catalog or not collections:
        sources.append(None)

    merged = {}
    catalog_products = []
    for collection in sources:
        data = http_get_json(products_url(domain, limit, collection))
        batch = data.get("products", [])
        if collection is None:
            catalog_products = batch
        for p in batch:
            pid = p.get("id")
            if pid is not None and pid not in merged:
                merged[pid] = p
    return list(merged.values()), catalog_products


def probe_json(url, timeout=10):
    """Single attempt. Returns (parsed_or_None, reason) so callers can tell a
    404 apart from a Cloudflare block apart from a timeout."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body), "ok"
        except json.JSONDecodeError:
            return None, "responded but not JSON"
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(2000).decode("utf-8", errors="replace").lower()
        except Exception:
            pass
        if e.code in (403, 503) and "cloudflare" in body:
            return None, f"blocked by Cloudflare ({e.code})"
        if e.code == 404:
            return None, "404, endpoint not present"
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, f"{type(e).__name__}"


def rss_url(domain):
    return f"https://{domain}/?post_type=product&feed=rss2"


def probe_rss(domain, limit=20, timeout=15):
    """WordPress product feed. No price or stock, but title/link/date, and it
    survives where /wp-json/ is disabled or gated. Returns (items, reason)."""
    import xml.etree.ElementTree as ET
    try:
        req = urllib.request.Request(rss_url(domain), headers=dict(UA, Accept="application/rss+xml, text/xml"))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(2000).decode("utf-8", errors="replace").lower()
        except Exception:
            pass
        if e.code in (403, 503) and "cloudflare" in body:
            return None, f"blocked by Cloudflare ({e.code})"
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, f"{type(e).__name__}"

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None, "responded but not valid XML"

    items = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        title = (item.findtext("title") or "").strip()
        if not title or not link:
            continue
        items.append({
            "id": guid or link,
            "title": title,
            "handle": link.rstrip("/").rsplit("/", 1)[-1],
            "vendor": "",
            "product_type": "",
            "variants": [],
            "_url": link,
        })
        if len(items) >= limit:
            break
    if not items:
        return None, "feed had no product items"
    return items, "ok"



def detect_platform(domain, explain=False):
    """Probe a domain. Returns 'shopify', 'woocommerce', 'rss', or None.

    With explain=True returns (platform, reasons), where reasons records what
    each endpoint said. That keeps a Cloudflare block from being mistaken for
    a store that simply isn't on a supported platform.
    """
    reasons = []

    data, why = probe_json(products_url(domain, 1))
    if isinstance(data, dict) and "products" in data:
        return ("shopify", reasons) if explain else "shopify"
    reasons.append(f"shopify products.json: {why}")

    data, why = probe_json(woo_products_url(domain, 1))
    if isinstance(data, list):
        return ("woocommerce", reasons) if explain else "woocommerce"
    reasons.append(f"woo store api: {why}")

    items, why = probe_rss(domain, 1)
    if items:
        return ("rss", reasons) if explain else "rss"
    reasons.append(f"wp product feed: {why}")

    return (None, reasons) if explain else None


# ---------------------------------------------------------------- verify ----

def cmd_verify(cfg):
    ok, failed = [], []
    for s in cfg["stores"]:
        domain = s["domain"]
        platform = detect_platform(domain)
        if platform is None:
            s["verified"] = False
            failed.append((s["name"], domain, "no shopify or woocommerce endpoint"))
            print(f"  FAIL  {s['name']:<24} neither products.json nor wc/store responded")
            time.sleep(0.5)
            continue
        try:
            products, _ = fetch_products(domain, 1, platform)
            if isinstance(products, list):
                s["platform"] = platform
                s["verified"] = True
                ok.append((s["name"], domain, platform))
                print(f"  OK    {s['name']:<24} {platform}")
            else:
                s["verified"] = False
                failed.append((s["name"], domain, "unexpected shape"))
                print(f"  ????  {s['name']:<24} responded but not a product list")
        except Exception as e:
            s["verified"] = False
            failed.append((s["name"], domain, str(e)))
            print(f"  FAIL  {s['name']:<24} {e}")
        time.sleep(0.5)

    save_json(CONFIG_PATH, cfg)
    shopify = sum(1 for _, _, p in ok if p == "shopify")
    woo = sum(1 for _, _, p in ok if p == "woocommerce")
    print(f"\n{len(ok)} working ({shopify} shopify, {woo} woocommerce), "
          f"{len(failed)} failed.")
    if failed:
        print("Failed stores (check the domain via shop.app or the store site,")
        print("then correct 'domain' in stores.json):")
        for name, domain, err in failed:
            print(f"  - {name}: {domain}  ({err})")
    print("\nstores.json has been updated with the detected platform and")
    print("verified flags. Commit it to keep the results.")


# ----------------------------------------------------- telegram commands ----

import re

URL_RE = re.compile(r"(?:https?://)?((?:[\w-]+\.)+[a-z]{2,})(?:/\S*)?", re.I)


def telegram_api(method, params):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}?" + urllib.parse.urlencode(params)
    try:
        return http_get_json(url)
    except Exception as e:
        print(f"[warn] telegram {method} failed: {e}", file=sys.stderr)
        return None


def derive_name(domain):
    core = domain.replace("www.", "").split(".")[0]
    return core.replace("-", " ").title()


def process_telegram_commands(cfg, state):
    """Read messages sent to the bot; add/remove stores accordingly.

    Send the bot a store URL (or bare domain) to add it.
    Send 'remove <domain>' to remove one.
    Only messages from TELEGRAM_CHAT_ID are honored.
    """
    my_chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not my_chat:
        return False

    offset = state.get("_tg_offset", 0)
    resp = telegram_api("getUpdates", {"offset": offset + 1, "timeout": 0})
    if not resp or not resp.get("ok"):
        return False

    changed = False
    for upd in resp.get("result", []):
        state["_tg_offset"] = max(state.get("_tg_offset", 0), upd["update_id"])
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(my_chat):
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue

        existing = {s["domain"].replace("www.", ""): s for s in cfg["stores"]}

        if text.lower().startswith("remove"):
            m = URL_RE.search(text)
            target = m.group(1).replace("www.", "") if m else None
            if target and target in existing:
                cfg["stores"] = [s for s in cfg["stores"]
                                 if s["domain"].replace("www.", "") != target]
                changed = True
                send_telegram(f"➖ Removed {existing[target]['name']} ({target})")
            else:
                send_telegram(f"Couldn't find that store in the list: {text}")
            continue

        m = URL_RE.search(text)
        if not m:
            continue  # ordinary chatter, ignore
        domain = m.group(1)
        bare = domain.replace("www.", "")
        if bare in existing:
            send_telegram(f"Already tracking {existing[bare]['name']} ({bare})")
            continue

        # Validate: what platform is it? Try as given, then with www.
        working = None
        platform = None
        reasons = []
        for candidate in (domain, f"www.{bare}"):
            platform, reasons = detect_platform(candidate, explain=True)
            if platform:
                working = candidate
                break

        if working:
            name = derive_name(working)
            cfg["stores"].append(
                {"name": name, "domain": working,
                 "platform": platform, "verified": True})
            changed = True
            send_telegram(f"➕ Added {name} ({working}, {platform}). "
                          f"Seeding now; notifications start next run.")
        else:
            detail = "\n".join(f"  \u2022 {r}" for r in reasons)
            send_telegram(f"\u26a0\ufe0f Couldn't add {bare}.\n{detail}\n\n"
                          f"'blocked by Cloudflare' means the store works but "
                          f"refuses datacenter IPs. '404' means it isn't on a "
                          f"supported platform.")

    return changed


def report_health(state, ok_names, failed, skipped, empty, new_by_store,
                  dry_run, hcfg=None, deferred=None):
    """Alert on Telegram when a store's health *changes*, not every run, and
    send a periodic digest of which stores have gone quiet.

    Alerts (things that are probably broken):
      - failed `fail_alerts_after` consecutive runs
      - responded with an empty catalogue for `empty_alert_hours`
      - deactivated by verify (verified:false), flagged immediately
    Every alert closes with a recovery message so nothing stays open.

    Digest (things that are merely suspicious): every `digest_every_days`,
    a summary listing stores with no new listings in `quiet_flag_days`.
    Deliberately not an alert, because a quiet store is usually just quiet.
    """
    hcfg = hcfg or {}
    threshold = hcfg.get("fail_alerts_after", 3)
    empty_hours = hcfg.get("empty_alert_hours", 24)
    digest_days = hcfg.get("digest_every_days", 7)
    quiet_days = hcfg.get("quiet_flag_days", 14)

    now = int(time.time())
    health = state.setdefault("_health", {})
    fails = health.setdefault("fails", {})
    alerted = set(health.setdefault("alerted", []))
    empty_since = health.setdefault("empty_since", {})
    last_new = health.setdefault("last_new", {})
    period_new = health.get("period_new", 0)
    messages = []
    empty_set = set(empty)

    # Anything that polled cleanly resets its failure counter.
    for name in ok_names:
        fails.pop(name, None)
        if name in empty_set:
            empty_since.setdefault(name, now)
        else:
            empty_since.pop(name, None)
            if name in alerted:
                alerted.discard(name)
                messages.append(f"✅ {name} is back. Polling normally again.")
        n = new_by_store.get(name, 0)
        if n:
            last_new[name] = now
            period_new += n
        last_new.setdefault(name, now)

    # Responding but returning nothing, for long enough that it isn't a blip.
    for name, since in list(empty_since.items()):
        if now - since >= empty_hours * 3600 and name not in alerted:
            alerted.add(name)
            messages.append(
                f"🟠 {name} has returned an empty catalogue for "
                f"{(now - since) // 3600}h. Its endpoint may be gated.")

    # Repeated hard failures cross the threshold and open an alert.
    for name in failed:
        fails[name] = fails.get(name, 0) + 1
        if fails[name] >= threshold and name not in alerted:
            alerted.add(name)
            messages.append(
                f"🔴 {name} has failed {fails[name]} runs in a row. "
                f"No drops from this store are being caught.")

    # Deactivated stores are silent holes, so flag them straight away.
    for name in skipped:
        if name not in alerted:
            alerted.add(name)
            messages.append(
                f"⚠️ {name} is set verified:false and is not being polled. "
                f"Re-run verify, or fix its domain in stores.json.")

    # Periodic digest of quiet stores.
    last_digest = health.get("last_digest")
    if digest_days and last_digest is None:
        health["last_digest"] = now          # seed, don't fire on first run
    elif digest_days and now - last_digest >= digest_days * 86400:
        quiet = sorted((((now - t) // 86400), n) for n, t in last_new.items())
        quiet.reverse()
        lines = [f"📊 Tracker digest, last {digest_days} days",
                 f"{len(last_new)} stores polling, {period_new} new listings."]
        flagged = [(d, n) for d, n in quiet if d >= quiet_days]
        if flagged:
            lines.append("")
            lines.append(f"Nothing new in {quiet_days}+ days:")
            lines += [f"  {d}d  {n}" for d, n in flagged[:15]]
            if len(flagged) > 15:
                lines.append(f"  ...and {len(flagged) - 15} more")
            lines.append("")
            lines.append("Usually just a quiet store. Worth a spot check if "
                         "you know they've been dropping.")
        else:
            lines.append(f"Every store has listed something within {quiet_days} days.")
        messages.append("\n".join(lines))
        health["last_digest"] = now
        period_new = 0

    # Prune bookkeeping so it doesn't grow with stores you've removed. Deferred
    # stores are alive and merely not due, so their history must survive.
    live = set(ok_names) | set(failed) | set(skipped) | set(deferred or [])
    health["alerted"] = sorted(n for n in alerted if n in live)
    health["fails"] = {k: v for k, v in fails.items() if k in live}
    health["empty_since"] = {k: v for k, v in empty_since.items() if k in live}
    health["last_new"] = {k: v for k, v in last_new.items() if k in live}
    health["period_new"] = period_new

    for msg in messages:
        if dry_run:
            print("---\n" + msg)
        else:
            try:
                send_telegram(msg)
                time.sleep(1)
            except Exception as e:
                print(f"[warn] health alert send failed: {e}", file=sys.stderr)

    return messages


def store_interval(store, poll_cfg):
    """Minimum seconds between polls for this store. Per-store override wins,
    then a per-platform default, then 0 (poll every run)."""
    if "min_interval_seconds" in store:
        return int(store["min_interval_seconds"])
    per_platform = poll_cfg.get("min_interval_seconds", {})
    if isinstance(per_platform, dict):
        return int(per_platform.get(store.get("platform", "shopify"), 0))
    return int(per_platform or 0)


# ------------------------------------------------------------------- run ----

def passes_filters(product, filters):
    title = (product.get("title") or "").lower()
    ptype = (product.get("product_type") or "").lower()

    inc = [k.lower() for k in filters.get("include_keywords", [])]
    if inc and not any(k in title for k in inc):
        return False

    exc = [k.lower() for k in filters.get("exclude_keywords", [])]
    if any(k in title for k in exc):
        return False

    inc_types = [t.lower() for t in filters.get("include_product_types", [])]
    if inc_types and ptype not in inc_types:
        return False

    if filters.get("notify_only_available", False):
        variants = product.get("variants", [])
        if variants and not any(v.get("available", True) for v in variants):
            return False

    return True


def format_message(store_name, domain, product, restocked=None):
    title = product.get("title", "Untitled")
    handle = product.get("handle", "")
    # Woo products carry _url because their permalink shape differs.
    url = product.get("_url") or f"https://{domain}/products/{handle}"
    prices = sorted({v.get("price") for v in product.get("variants", []) if v.get("price")})
    price = prices[0] if prices else None
    vendor = product.get("vendor", "")
    badge = "♻️" if restocked else "🆕"
    lines = [f"{badge} {store_name}", title]
    if vendor and vendor.lower() not in title.lower():
        lines.append(vendor)
    if price:
        lines.append(f"${price}" if not str(price).startswith("$") else str(price))
    if restocked:
        lines.append("Restocked: " + ", ".join(restocked))
    lines.append(url)
    return "\n".join(lines)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "false",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def cmd_run(cfg, dry_run=False, poll_all=False):
    state = load_json(STATE_PATH, {})

    if not dry_run:
        try:
            cfg_changed = process_telegram_commands(cfg, state)
            if cfg_changed:
                save_json(CONFIG_PATH, cfg)
        except Exception as e:
            print(f"[warn] telegram command processing failed: {e}", file=sys.stderr)

    filters = cfg.get("filters", {})
    poll_cfg = cfg.get("poll", {})
    limit = poll_cfg.get("products_per_store", 20)
    # Missing/invalid means the implicit default this repo used before
    # _poll_limit existed. Needed so 20 → 50 does not treat older catalog
    # IDs as new.
    try:
        prev_limit = int(state.get("_poll_limit", 20))
    except (TypeError, ValueError):
        prev_limit = 20
    # Slack so a store due at exactly N seconds isn't pushed to the next
    # trigger by a fraction of a second, which would slowly drift its cadence.
    grace = poll_cfg.get("interval_grace_seconds", 60)
    last_poll = state.setdefault("_last_poll", {})
    first_run_stores = 0
    notified = 0
    timings = []   # (seconds, name, status)
    run_start = time.time()

    total_configured = len(cfg["stores"])
    skipped_unverified = []
    polled_ok = 0
    ok_names = []
    failed = []
    empty = []
    deferred = []
    new_by_store = {}

    for s in cfg["stores"]:
        name, domain = s["name"], s["domain"]
        if not poll_all and not s.get("verified", False):
            skipped_unverified.append(name)
            continue

        # Not due yet: Woo stores poll less often than Shopify ones.
        interval = store_interval(s, poll_cfg)
        since_last = time.time() - last_poll.get(domain, 0)
        if interval and since_last < interval - grace:
            deferred.append(name)
            continue
        last_poll[domain] = int(time.time())

        t0 = time.time()
        try:
            products, catalog_products = fetch_products(
                domain, limit, s.get("platform", "shopify"),
                collections=store_collections(s),
                catalog=s.get("catalog", True))
            timings.append((time.time() - t0, name, "ok"))
            polled_ok += 1
            ok_names.append(name)
            if not products:
                empty.append(name)
        except Exception as e:
            timings.append((time.time() - t0, name, f"FAILED: {e}"))
            failed.append(name)
            print(f"[warn] {name}: fetch failed after {time.time()-t0:.1f}s: {e}",
                  file=sys.stderr)
            continue

        seen = list(state.get(domain, []))
        stock_all = state.setdefault("_stock", {})
        prev_stock = stock_all.get(domain)  # None until variant state is seeded
        seed_ids = window_seed_ids(catalog_products, prev_limit, limit)
        events, current_ids, next_stock = diff_store(
            products, seen, prev_stock, seed_ids=seed_ids)

        if not seen:
            # First time seeing this store: seed IDs and stock silently.
            state[domain] = current_ids
            stock_all[domain] = next_stock
            new_by_store[name] = 0
            first_run_stores += 1
            continue

        new_by_store[name] = len(events)
        for kind, p, extra in events:
            if not passes_filters(p, filters):
                continue
            msg = format_message(name, domain, p,
                                 restocked=extra if kind == "restock" else None)
            if dry_run:
                print("---\n" + msg)
            else:
                try:
                    send_telegram(msg)
                    notified += 1
                    time.sleep(1)  # be gentle with Telegram rate limits
                except Exception as e:
                    print(f"[warn] Telegram send failed: {e}", file=sys.stderr)

        # Keep a rolling window of known IDs so state doesn't grow forever.
        state[domain] = list(dict.fromkeys(current_ids + seen))[:500]
        kept = set(state[domain])
        stock_all[domain] = {pid: snap for pid, snap in next_stock.items()
                             if pid in kept}
        time.sleep(1.5)  # be gentle with the stores too

    # Prune poll timestamps and stock maps for stores that have been removed.
    live_domains = {s["domain"] for s in cfg["stores"]}
    state["_last_poll"] = {k: v for k, v in last_poll.items() if k in live_domains}
    if "_stock" in state:
        state["_stock"] = {d: snaps for d, snaps in state["_stock"].items()
                           if d in live_domains}
    state["_poll_limit"] = limit

    report_health(state, ok_names, failed, skipped_unverified, empty,
                  new_by_store, dry_run, cfg.get("health", {}), deferred)

    save_json(STATE_PATH, state)
    total = time.time() - run_start
    print(f"Done in {total:.1f}s. Seeded {first_run_stores} store(s), "
          f"sent {notified} notification(s).")

    # Census heartbeat: proof every store was actually reached.
    print(f"Census: {polled_ok}/{total_configured} polled ok, "
          f"{len(failed)} failed, {len(empty)} returned empty, "
          f"{len(deferred)} not due, "
          f"{len(skipped_unverified)} skipped (unverified).")
    if failed:
        print(f"  FAILED: {', '.join(failed)}")
    if empty:
        print(f"  EMPTY (responded but no products): {', '.join(empty)}")
    if skipped_unverified:
        print(f"  SKIPPED (verified:false): {', '.join(skipped_unverified)}")

    # Timing report: anything slow or failed, slowest first.
    problems = sorted(
        [t for t in timings if t[0] >= 2.0 or t[2] != "ok"],
        key=lambda t: t[0], reverse=True)
    if problems:
        print("\nSlow or failing stores (>=2s or errored), slowest first:")
        for secs, name, status in problems:
            tag = "" if status == "ok" else f"  [{status}]"
            print(f"  {secs:6.1f}s  {name}{tag}")
    else:
        print("All stores responded in under 2s.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["verify", "run"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="poll every store, including unverified ones")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit(f"Missing or invalid config: {CONFIG_PATH}")

    if args.command == "verify":
        cmd_verify(cfg)
    else:
        cmd_run(cfg, dry_run=args.dry_run, poll_all=args.all)


if __name__ == "__main__":
    main()
