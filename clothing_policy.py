"""Clothing-only relevance rules. No network, delivery or state writes.

Catalogue stock is an event signal, never proof of live size availability.
Personal ownership context may be supplied locally; it never changes eligibility.
"""

import html
from html.parser import HTMLParser
import re
import unicodedata

from private_clothing_profile import validate_profile


BRAND_ALIASES = {
    "Pure Blue Japan": ("pure blue japan", "purebluejapan", "pbj"),
    "ONI Denim": ("oni denim", "onidenim", "oni"),
    "Samurai Jeans": ("samurai jeans", "samurai denim", "samurai"),
    "Kapital": ("kapital",),
    "Omoto Denim": ("omoto denim", "omotodenim", "omoto"),
    "Studio D'Artisan": ("studio d artisan", "studio dartisan", "studiodartisan", "sda"),
    "Maru Sankaku Peke": ("maru sankaku peke", "marusankakupeke", "msp"),
    "Blue Blue Japan": ("blue blue japan", "bluebluejapan", "bbj"),
    "FDMTL": ("fdmtl", "fundamental agreement luxury"),
    "Rogue Territory": ("rogue territory", "rogueterritory", "rgt"),
    "Momotaro Jeans": ("momotaro jeans", "momotaro denim", "momotaro"),
    "Tanuki": ("tanuki",),
    "Graph Zero": ("graph zero", "graphzero"),
}


def normalized(text):
    text = unicodedata.normalize("NFKD", html.unescape(str(text or ""))).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def contains(text, phrase):
    return f" {normalized(phrase)} " in f" {normalized(text)} "


def canonical_brand(value):
    for brand, aliases in BRAND_ALIASES.items():
        if any(contains(value, alias) for alias in aliases):
            return brand
    if is_new_era_name(value):
        return "New Era"
    return None


def is_new_era_name(value):
    return any(contains(value, alias) for alias in ("new era", "newera", "new era cap", "neweracap"))


def product_brand(product, retailer_aliases=()):
    # Manufacturer/vendor and title only: recommendation copy in a description
    # must not turn another manufacturer's product into an approved brand.
    vendor = product.get("vendor") or ""
    brand = canonical_brand(vendor)
    if brand:
        return brand
    # An explicit outside manufacturer is authoritative. Only an empty vendor
    # or an explicitly configured retailer label may use the product title.
    compact = normalized(vendor).replace(" ", "")
    retailer = any(compact == normalized(alias).replace(" ", "") for alias in retailer_aliases)
    return canonical_brand(product.get("title")) if not compact or retailer else None


def product_text(product):
    tags = product.get("tags") or []
    if isinstance(tags, list):
        tags = " ".join(str(t) for t in tags)
    body = re.sub(r"<[^>]+>", " ", product.get("body_html") or "")
    return normalized(" ".join(str(product.get(k) or "") for k in
                               ("title", "product_type")) + " " + str(tags) + " " + body)


def title_collaboration(product):
    """Recognize named X/+ collaborations without treating dye pairs as collabs."""
    title = str(product.get("title") or "").replace("×", " x ")
    parts = re.split(r"\s+[xX]\s+|\+", title)
    if len(parts) < 2:
        return False
    dye_fabric = ("indigo", "sumi", "black", "white", "brown", "beige", "cotton",
                  "linen", "hemp", "wool", "silk", "denim", "sashiko", "kakishibu")
    for left, right in zip(parts, parts[1:]):
        a, b = normalized(left), normalized(right)
        if not a or not b or b.split()[0] in dye_fabric or b.split()[0].isdigit():
            continue
        if canonical_brand(left) in BRAND_ALIASES or canonical_brand(right) in BRAND_ALIASES:
            return True
    return False


def category(product):
    # Routing uses title/type only, so a hat's description mentioning a denim
    # jacket, or a clothing page recommending caps, cannot change the path.
    ptype = normalized(product.get("product_type"))
    text = normalized(str(product.get("title") or "") + " " + ptype)
    text = text.replace("cap sleeve", "sleeve")
    # An explicit garment type outranks incidental words such as "cap sleeve"
    # in its title. Generic fabric/apparel categories still defer to hat names.
    for kind, words in (
            ("pants", ("jeans", "jean", "pants", "pant", "trousers", "shorts", "bottoms", "leggings")),
            ("jacket", ("jacket", "jackets", "coat", "coats", "outerwear", "blazer", "haori", "happi")),
            ("clothing", ("shirt", "shirts", "t shirt", "tee", "tees", "tops", "sweats", "sweater",
                           "sweaters", "sweatshirt", "sweatshirts", "hoodie", "hoodies", "cardigan",
                           "henley", "pullover", "polo", "vest", "dress", "skirt", "knitwear"))):
        if any(contains(ptype, word) for word in words):
            return kind
    if ptype == "fitted" or any(contains(ptype, word) for word in ("headwear", "hat", "hats", "cap", "caps", "beanie")):
        return "hat"
    if any(contains(text, word) for word in (
            "hat", "hats", "cap", "caps", "59fifty", "59forty", "9fifty",
            "9forty", "39thirty", "9twenty", "9seventy", "beanie", "bucket hat", "snapback",
            "headwear", "fedora", "beret", "boonie", "trilby", "visor", "5 panel", "6 panel")):
        return "hat"
    if any(contains(text, word) for word in ("jeans", "jean", "pants", "pant",
                                            "trousers", "trouser", "shorts", "bottoms")):
        return "pants"
    if any(contains(text, word) for word in ("jacket", "jackets", "coat", "coats",
            "outerwear", "coverall", "coveralls", "haori", "happi", "blazer", "blazers")):
        return "jacket"
    if any(contains(text, word) for word in ("shirt", "shirts", "tee", "tees",
            "t shirt", "sweater", "sweatshirt", "hoodie", "vest", "cardigan",
            "clothing", "apparel", "denim", "overalls", "dress", "skirt", "knitwear",
            "top", "tops", "henley", "henleys", "pullover", "pullovers", "crewneck",
            "crewnecks", "jumper", "jumpers", "sweatpants", "sweaters", "sweatshirts",
            "hoodies", "vests", "cardigans", "knit", "knits", "jersey", "jerseys",
            "windbreaker", "windbreakers", "jumpsuit", "dresses", "skirts", "sweats", "polo", "polos")):
        return "clothing"
    return None


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables, self.rows, self.row, self.cell = [], None, None, None
        self.leads, self.context = [], ""

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.leads.append(self.context[-150:])
            self.context = ""
            self.rows = []
        elif tag == "tr" and self.rows is not None:
            self.row = []
        elif tag in ("th", "td") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.rows is None:
            self.context = (self.context + " " + data)[-150:]
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("th", "td") and self.cell is not None:
            self.row.append(" ".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == "table" and self.rows is not None:
            self.tables.append(self.rows)
            self.rows = None


MEASUREMENT_NAMES = {
    "waist": ("waist",), "rear_rise": ("rear rise", "back rise"),
    "front_rise": ("front rise", "rise"), "thigh": ("thigh", "upper thigh"),
    "knee": ("knee",), "hem": ("hem", "leg opening", "opening"),
    "inseam": ("inseam", "inside leg"), "chest": ("chest", "pit to pit"),
    "shoulder": ("shoulder", "shoulders"), "sleeve": ("sleeve", "sleeve length"),
    "body_length": ("body length", "centre back", "center back", "back length", "length"),
}


def measurement_name(value):
    for name, aliases in MEASUREMENT_NAMES.items():
        if any(contains(value, alias) for alias in aliases):
            return name
    return None


def measurement_value(value):
    # Ambiguous ranges/fractions are retained as raw evidence but not guessed.
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(?:"|in|inches|cm)?\s*', value, re.I)
    return float(match.group(1)) if match else None


def size_label(value):
    text = normalized(value)
    text = re.sub(r"\bw(\d{2})\b", r"\1", text)
    for alias in ("extra large", "x large", "xlarge", "1xl"):
        text = re.sub(r"\b" + alias + r"\b", "xl", text)
    return text


def measurements(product, source_url=None):
    """Capture labelled HTML size tables in both common orientations.

    Only explicit table/body unit labels are converted. Unknown units and
    differing measurement methods stay visible and are not used as fit proof.
    """
    body = product.get("body_html") or ""
    parser = _Tables()
    parser.feed(body)
    evidence = []
    unit_text = normalized(body)
    has_cm = contains(unit_text, "cm") or contains(unit_text, "centimeters")
    has_in = contains(unit_text, "inches") or contains(unit_text, "inch")
    default_unit = "cm" if has_cm and not has_in else "in" if has_in and not has_cm else None
    for index, rows in enumerate(parser.tables):
        rows = [row for row in rows if row]
        if len(rows) < 2:
            continue
        headers = rows[0]
        table_unit_text = normalized(" ".join(" ".join(r) for r in rows))
        table_cm = contains(table_unit_text, "cm")
        table_in = contains(table_unit_text, "inches") or contains(table_unit_text, "inch")
        lead = normalized(parser.leads[index])
        lead_cm = contains(lead, "cm") or contains(lead, "centimeters")
        lead_in = contains(lead, "inches") or contains(lead, "inch")
        lead_unit = "cm" if lead_cm and not lead_in else "in" if lead_in and not lead_cm else None
        unit = (None if table_cm and table_in else "cm" if table_cm else
                "in" if table_in else lead_unit or default_unit)
        records = []
        if sum(measurement_name(h) is not None for h in headers[1:]) >= 2:
            for row in rows[1:]:
                raw = {measurement_name(h): v for h, v in zip(headers[1:], row[1:])
                       if measurement_name(h)}
                labels = {measurement_name(h): h for h in headers[1:] if measurement_name(h)}
                if row:
                    records.append((row[0], raw, labels))
        elif contains(headers[0], "size") or contains(headers[0], "measurement") or measurement_name(rows[1][0]):
            for col, size in enumerate(headers[1:], 1):
                raw = {measurement_name(row[0]): row[col] for row in rows[1:]
                       if len(row) > col and measurement_name(row[0])}
                labels = {measurement_name(row[0]): row[0] for row in rows[1:]
                          if len(row) > col and measurement_name(row[0])}
                records.append((size, raw, labels))
        for size, raw, labels in records:
            values = {name: measurement_value(v) for name, v in raw.items()}
            inch = {name: v / 2.54 if unit == "cm" else v for name, v in values.items()
                    if v is not None and unit is not None}
            # A flat waist measurement is readily distinguishable from a full
            # circumference in adult clothing. Keep the method in the evidence.
            flat_waist = "waist" in inch and inch["waist"] < 25
            if flat_waist:
                inch["waist"] *= 2
            evidence.append({"size": size, "raw": raw, "labels": labels, "unit": unit,
                             "inches": inch, "source": source_url,
                             "table": index, "waist_method": "flat doubled" if flat_waist else "as listed"})
    return evidence


def reference_labels(profile, kind, brand):
    """Category defaults plus optional brand-specific labels; no personal defaults."""
    labels = list(profile.get("size_references", {}).get(kind, []))
    for name, categories in profile.get("brand_size_references", {}).items():
        if canonical_brand(name) == brand:
            labels += categories.get(kind, [])
    return {size_label(label) for label in labels}


def fit_context(product, policy, evidence, profile=None):
    profile = profile or {}
    kind = category(product)
    targets = profile.get("fit", {})
    brand = product_brand(product, policy.get("retailer_vendor_aliases", []))
    refs = reference_labels(profile, kind, brand)
    bounds = targets.get("waist_range") if kind == "pants" else None
    fits = [m for m in evidence if bounds and
            bounds[0] <= m["inches"].get("waist", 0) <= bounds[1]]
    fits += [m for m in evidence if size_label(m["size"]) in refs and m not in fits
             and (kind != "pants" or "waist" not in m["inches"] or not bounds)]
    # Conflicting charts must not be collapsed to a single convenient row.
    conflict = any(a["size"] == b["size"] and any(
        abs(a["inches"][key] - b["inches"][key]) > 0.35
        for key in a["inches"].keys() & b["inches"].keys())
        for i, a in enumerate(fits) for b in fits[i + 1:])
    selected = fits[0]["inches"] if fits and not conflict and len({m["size"] for m in fits}) == 1 else {}
    upper_checks = [selected[field] >= targets[target]
                    for field, target in (("front_rise", "min_front_rise"),
                                          ("thigh", "min_thigh"), ("knee", "min_knee"))
                    if field in selected and target in targets]
    narrow = any(selected[field] < targets[target]
                 for field, target in (("thigh", "min_acceptable_thigh"),
                                       ("front_rise", "min_acceptable_front_rise"))
                 if field in selected and target in targets)
    roomy = kind == "pants" and sum(upper_checks) >= 2 and not narrow
    return {"roomy": roomy, "narrow": bool(narrow), "selected": selected,
            "conflict": conflict, "evidence": evidence, "matching_rows": fits}


def evaluate(product, policy, event_kind=None, extra=None, source_url=None, context=None):
    """Return eligibility and explainable context; ownership never gates it."""
    context = validate_profile(context)
    kind = category(product)
    brand = product_brand(product, policy.get("retailer_vendor_aliases", []))
    result = {"applies": bool(policy.get("enabled") and kind in ("pants", "jacket", "clothing")),
              "keep": True, "reasons": [], "brand": brand, "category": kind,
              "owned": False, "measurements": [], "size_available": None}
    if not result["applies"]:
        return result
    if brand not in policy.get("core_brands", []):
        result.update(keep=False, reasons=["brand outside clothing whitelist"])
        return result
    if event_kind is not None and event_kind not in ("new", "restock", "price_drop", "product_update"):
        result.update(keep=False, reasons=["no meaningful listing/availability/price change"])
        return result
    text = product_text(product)
    signals = {}
    for group, words in policy.get("interest", {}).items():
        signals[group] = [word for word in words if contains(text, word)]
    fabric = bool(signals.get("textures"))
    exceptional = bool(signals.get("exceptional_textures"))
    dye = bool(signals.get("dyes"))
    construction = bool(signals.get("construction"))
    collaboration = bool(signals.get("collaborations")) or title_collaboration(product)
    if title_collaboration(product):
        signals.setdefault("collaborations", []).append("named title collaboration")
    rare = bool(signals.get("rarity"))
    evidence = measurements(product, source_url)
    fit = fit_context(product, policy, evidence, context)
    result["measurements"] = evidence
    result["fit"] = fit
    score = (3 if fabric else 0) + (2 if dye else 0) + (3 if construction else 0) + (3 if collaboration else 0)
    if rare and score:
        score += 1
    roomy_words = any(contains(text, s) for s in ("relaxed straight", "relaxed taper", "wide taper",
                      "wide straight", "wide leg", "loose straight", "roomy", "high rise", "wide cut", "mild taper"))
    if kind == "pants":
        if fit["roomy"]:
            score += 2
        elif roomy_words:
            score += 1
        slim = any(contains(text, s) for s in ("skinny", "slim", "low rise", "aggressive taper", "narrow thigh"))
        pbj_019 = brand == "Pure Blue Japan" and contains(product.get("title"), "019")
        if (slim or pbj_019 or fit["narrow"]) and not fit["roomy"]:
            score -= 2
        if fit["narrow"]:
            result["reasons"].append("measured narrow thigh or low rise; lower-leg tailoring does not resolve upper-block fit")
        hem = fit["selected"].get("hem")
        max_hem = context.get("fit", {}).get("preferred_max_hem")
        if hem is not None and max_hem is not None and hem > max_hem:
            if fit["roomy"] and (fabric or dye or construction):
                result["reasons"].append("roomy upper block; wider hem is a potential professional tapering candidate")
            else:
                result["reasons"].append("wide hem needs upper-block measurements and tailoring assessment")
        if brand == "Pure Blue Japan" and contains(product.get("title"), "002") and (fabric or dye) and not fit["narrow"]:
            result["reasons"].append("002 may be a tailoring base; model number is not fit proof")
        ordinary_black = any(contains(text, s) for s in ("black", "black black", "sumi"))
        if ordinary_black and not (exceptional or dye or construction or collaboration or (rare and fabric) or fit["roomy"]):
            score -= 2
    if kind == "jacket":
        standard = any(contains(text, s) for s in ("type i", "type 1", "type ii", "type 2", "trucker"))
        if standard and not (exceptional or construction or collaboration or (fabric and dye) or (rare and fabric)):
            score -= 2
            result["reasons"].append("standard trucker silhouette requires an unusually special treatment")
        if any(contains(text, s) for s in ("chore", "coverall", "coveralls", "haori", "happi", "blazer", "work jacket")) and score:
            score += 1
    if event_kind == "price_drop" and (fabric or dye or construction or collaboration):
        # A material deal can make an overlapping but interesting piece worth
        # seeing again. A discounted ordinary garment still fails relevance.
        score += 2
    if event_kind in ("restock", "price_drop") and extra:
        refs = reference_labels(context, kind, brand)
        refs.update(size_label(m["size"]) for m in fit["matching_rows"])
        # Default/unknown size labels can support discovery, but no size claim.
        labels = [d["variant"] for d in extra] if event_kind == "price_drop" else extra
        sized = [v for v in labels if normalized(v) not in {"default", "default title", "one size"}]
        if refs and sized and not any(any(contains(size_label(v), ref) for ref in refs) for v in sized):
            result.update(keep=False, reasons=["reported change does not include reference size or a measured fit match"])
            return result
    threshold = policy.get("min_interest_score", 3)
    result["score"] = score
    result["signals"] = {group: hits for group, hits in signals.items() if hits}
    result["keep"] = score >= threshold
    result["reasons"].append("distinctive clothing merits discovery" if result["keep"] else
                             "insufficient distinction/fit beyond brand, novelty or scarcity")
    if fit["conflict"]:
        result["reasons"].append("conflicting measurement charts retained; fit not asserted")
    # Caller may provide private model tokens. The repository contains no
    # wardrobe list; a matching owned model only adds context to the alert.
    result["owned"] = any(
        contains(product.get("title"), model) if isinstance(model, str) else
        canonical_brand(model["brand"]) == brand and all(
            contains(product.get("title"), token) for token in model["tokens"])
        for model in context.get("owned_models", []))
    if result["owned"]:
        result["reasons"].append("owned model: discovery context, lower purchase priority")
    return result
