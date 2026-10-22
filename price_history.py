"""Public per-variant price references, stored with durable observations.

References include queued advertisements: reserving a reduction before sending
prevents a second event key for the same offer. Only that reservation's owner
may compare against its previous reference. Receipts remain the outbox's concern.
"""
import copy
import json
from decimal import Decimal


VERSION = 1
META = '_advertised'
CURRENCIES = set('USD EUR GBP JPY CNY SGD HKD CAD AUD NZD KRW TWD CHF DKK SEK NOK INR IDR MYR THB PHP VND AED ZAR BRL MXN ILS TRY PLN CZK HUF BGN RON ISK RUB SAR'.split())


def context(product, variant, currency):
    # Explicit request configuration or catalogue evidence only; never the
    # display-symbol fallback or a guessed shop currency.
    store = currency if isinstance(currency, dict) else {'currency': currency}
    codes = [str(value).strip().upper() for value in
             (store.get('currency'), product.get('currency'), product.get('currency_code'),
              variant.get('currency'), variant.get('currency_code')) if value]
    if not codes or len(set(codes)) != 1 or codes[0] not in CURRENCIES:
        return None
    market = {}
    for key in ('market', 'market_id', 'country', 'country_code'):
        values = {str(source[key]).strip().upper() for source in (store, product, variant)
                  if source.get(key) is not None}
        if len(values) > 1 or '' in values:
            return None
        if values:
            market[key] = values.pop()
    return json.dumps([codes[0], market], sort_keys=True, separators=(',', ':'))


def meaningful(anchor, price, policy, key):
    currency = json.loads(key)[0]
    floor = Decimal(str(policy.get('price_drop_minimum', {}).get(currency, 20)))
    percent = Decimal(str(policy.get('price_drop_percent', 15))) / 100
    return anchor > price and anchor - price >= floor and (anchor - price) / anchor >= percent


def reset_higher(record):
    for field in ('higher_since', 'higher_count', 'higher_min', 'higher_last'):
        record.pop(field, None)


def interrupt(prices):
    result = copy.deepcopy(prices)
    for product in result.values():
        if META in product:
            product[META]['revision'] += 1
        for variant in product.get(META, {}).get('variants', {}).values():
            for record in variant.get('contexts', {}).values():
                reset_higher(record)
    return result


def observe(product, old, policy, currency, parse, now, skip=False, migration=False):
    current = copy.deepcopy(old)
    metadata = current.setdefault(META, {'version': VERSION, 'revision': 0, 'variants': {}})
    if metadata.get('version') != VERSION:
        raise ValueError('Unsupported advertised-price metadata')
    drops = []
    observed = set()
    variants = product.get('variants') or []
    identities = [str(v.get('id') or v.get('title') or 'default') for v in variants]
    for variant in variants:
        vid = str(variant.get('id') or variant.get('title') or 'default')
        observed.add(vid)
        price = parse(variant.get('price'))
        entry = metadata['variants'].setdefault(vid, {'active': None, 'contexts': {}})
        key = context(product, variant, currency)
        if price is None or price <= 0 or key is None or identities.count(vid) != 1:
            if price is not None and price > 0:
                current[vid] = str(price)
            for record in entry['contexts'].values():
                reset_higher(record)
            entry['active'] = None
            continue
        changed_context = entry['active'] != key
        if changed_context:
            for record in entry['contexts'].values():
                reset_higher(record)
        entry['active'] = key
        record = entry['contexts'].get(key)
        if record is None:
            # Legacy numbers have no advertised/context provenance. Seeding
            # may intentionally lose one genuine drop, never invent one.
            legacy = parse(old.get(vid)) if not old.get(META) else None
            baseline = min(price, legacy) if legacy is not None and legacy > 0 else price
            record = entry['contexts'][key] = {'baseline': str(baseline)}
            uncertain = migration or legacy is not None or vid in old.get(META, {}).get('variants', {})
            absorbed_lower = legacy is not None and legacy > 0 and price < legacy
            if uncertain and not absorbed_lower:
                record['unproven'] = True
        elif changed_context:
            record['generation'] = record.get('generation', 0) + 1
            record.pop('owner', None)
            if skip and price < parse(record['baseline']):
                record['baseline'] = str(price)
                record.pop('unproven', None)
        elif not changed_context:
            anchor = parse(record['baseline'])
            if price < anchor and (skip or record.get('unproven')):
                record['baseline'] = str(price)
                record.pop('unproven', None)
                record.pop('owner', None)
                reset_higher(record)
            elif price > anchor:
                if 'higher_since' not in record or now < record.get('higher_last', now):
                    reset_higher(record)
                    record.update(higher_since=now, higher_count=0, higher_min=str(price))
                # Retries at the same timestamp are not additional observations.
                if now != record.get('higher_last'):
                    record['higher_count'] += 1
                record['higher_last'] = now
                record['higher_min'] = str(min(price, parse(record['higher_min'])))
                if now - record['higher_since'] >= 86400 and record['higher_count'] >= 3:
                    record['baseline'] = record['higher_min']
                    record.pop('unproven', None)
                    record['generation'] = record.get('generation', 0) + 1
                    record.pop('owner', None)
                    reset_higher(record)
            else:
                reset_higher(record)
                if meaningful(anchor, price, policy, key):
                    record['baseline'] = str(price)
                    record.pop('owner', None)
                    if not skip:
                        drops.append({'variant': variant.get('title', vid), 'variant_id': vid,
                                      'from': str(anchor), 'to': str(price), 'context': key,
                                      'generation': record.get('generation', 0)})
        current[vid] = record['baseline']
        record['last_price'] = str(price)
    for vid, entry in metadata['variants'].items():
        if vid not in observed:
            entry['active'] = None
            for record in entry['contexts'].values():
                reset_higher(record)
    metadata['revision'] += 1
    return drops, current


def reserve(prices, extra, event_id):
    for drop in extra:
        record = prices[META]['variants'][drop['variant_id']]['contexts'][drop['context']]
        record['owner'] = event_id


def eligible(prices, payload, event_id, policy, currency, parse):
    """Recheck even historical pre-send payloads; unknown identity fails closed."""
    metadata = prices.get(META, {})
    product = payload['product']
    variants = product.get('variants') or []
    accepted = []
    for drop in payload.get('extra') or []:
        matches = [v for v in variants if
                   (str(v.get('id') or v.get('title') or 'default') == drop['variant_id']
                    if 'variant_id' in drop else v.get('title') == drop.get('variant'))]
        if len(matches) != 1:
            continue
        variant = matches[0]
        vid = str(variant.get('id') or variant.get('title') or 'default')
        key = context(product, variant, currency)
        entry = metadata.get('variants', {}).get(vid, {})
        if key is None or entry.get('active') != key:
            continue
        record = entry.get('contexts', {}).get(key, {})
        if 'generation' in drop and record.get('owner') != event_id:
            continue
        if record.get('unproven'):
            continue
        if drop.get('generation', 0) != record.get('generation', 0):
            continue
        anchor = parse(drop.get('from') if record.get('owner') == event_id else record.get('baseline'))
        price = parse(drop.get('to'))
        if parse(variant.get('price')) != price:
            continue
        if record.get('owner') == event_id and parse(record.get('last_price')) != price:
            continue
        if anchor is not None and price is not None and price > 0 and meaningful(anchor, price, policy, key):
            accepted.append(drop)
    return accepted


def confirmed(prices, payload, event_id, policy, currency, parse):
    result = copy.deepcopy(prices)
    for drop in eligible(result, payload, event_id, policy, currency, parse):
        matches = [v for v in payload['product']['variants'] if
                   (str(v.get('id') or v.get('title') or 'default') == drop['variant_id']
                    if 'variant_id' in drop else v.get('title') == drop.get('variant'))]
        variant = matches[0]
        vid = str(variant.get('id') or variant.get('title') or 'default')
        key = context(payload['product'], variant, currency)
        record = result[META]['variants'][vid]['contexts'][key]
        record['baseline'] = str(min(parse(record['baseline']), parse(drop['to'])))
        record.pop('owner', None)
        reset_higher(record)
    if META in result:
        result[META]['revision'] += 1
    return result
