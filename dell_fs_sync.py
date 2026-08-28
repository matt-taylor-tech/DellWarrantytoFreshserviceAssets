#!/usr/bin/env python3
"""Sync Dell TechDirect warranty coverage into Freshservice asset fields.

Answers the question Freshservice cannot: not just WHEN warranty ends, but WHAT is
covered. In particular whether the asset carries Accidental Damage Protection. A
cracked screen or a spill is only covered under ADP, never under standard
ProSupport or Basic. A technician who cannot see that quotes the wrong thing.

Reads Dell. Writes only the configured custom fields on matching assets. Non-Dell
assets are never touched. Dry-run by default; pass --apply to write.

Field keys are resolved by LABEL at runtime, so this works in any Freshservice
tenant without editing code. Freshservice builds keys like
`warranty_tier_21001234567`, where the numeric suffix is that tenant's asset type
id, so a hardcoded key would only ever work in one helpdesk.

Credentials come from the environment, never from the command line:
    DELL_CLIENT_ID          Dell TechDirect API client id
    DELL_CLIENT_SECRET      Dell TechDirect API client secret
    FRESHSERVICE_DOMAIN     e.g. yourcompany.freshservice.com  (no scheme)
    FRESHSERVICE_API_KEY    an agent API key with asset write permission

Everything else lives in config.json. See config.example.json.

Usage:
    python3 dell_fs_sync.py                 # dry run, prints what would change
    python3 dell_fs_sync.py --apply         # write to Freshservice
    python3 dell_fs_sync.py --selftest      # offline checks, no credentials needed
"""
import argparse
import base64
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DELL_TOKEN_URL = "https://apigtwb2c.us.dell.com/auth/oauth/v2/token"
DELL_ENTITLEMENTS_URL = "https://apigtwb2c.us.dell.com/PROD/sbil/eapi/v5/asset-entitlements"
DELL_BATCH = 100          # Dell accepts about 100 service tags per call
DETAIL_MAX = 240          # Freshservice text fields are not unbounded

DEFAULT_CONFIG = {
    "asset_types": ["Laptop", "Desktop", "Server", "Windows Server", "Monitor",
                    "Dock", "Tablet"],
    "product_match": ["latitude", "optiplex", "precision", "xps", "poweredge",
                      "monitor", "dock", "thunderbolt", "dell"],
    "require_product_match": False,
    "service_tag_pattern": "^[A-Za-z0-9]{7}$",
    "skip_asset_states": ["Retired", "Disposed"],
    "field_labels": {
        "warranty_tier": ["warranty tier"],
        "adp": ["accidental damage protection", "adp covered", "adp"],
        "detail": ["dell coverage detail", "coverage detail"],
        "warranty_expiry": ["warranty expiry date", "warranty expiry"],
        "acquisition": ["acquisition date"],
        "warranty_months": ["warranty"],
    },
    "tiers": [
        ["ProSupport Plus", "prosupport plus|prosupport flex"],
        ["ProSupport", "prosupport"],
        ["Premium Support", "premium support"],
        ["Basic Onsite (NBD)", "next business day|basic onsite|basic hardware"],
        # Monitors and docks come back as "Advanced Exchange Support". Without
        # this they classified as Unknown, and Unknown is never written, so
        # every monitor and dock synced with a blank tier.
        ["Advanced Exchange", "advanced exchange"],
        ["Return to Depot", "depot|carry[- ]in|mail[- ]in"],
    ],
    "max_writes": 500,
}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

class ConfigError(Exception):
    """A config file that cannot be used as written."""


def _merge(base, override):
    """Merge one level of nesting, rather than replacing whole objects.

    A flat dict.update meant a config naming only `warranty_tier` under
    `field_labels` deleted every other slot. That is exactly what
    config.example.json did: it lists four slots, so copying it as documented
    silently switched off the acquisition-date and warranty-months fields.
    Set a slot to [] to disable it deliberately.
    """
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            nested = dict(out[key])
            nested.update(value)
            out[key] = nested
        else:
            out[key] = value
    return out


def validate_config(cfg):
    """Reject a broken config with a sentence instead of a traceback.

    This runs unattended on a schedule, where a stack trace buried in a job log
    is the only symptom anyone ever sees.
    """
    problems = []

    for key in ("asset_types", "product_match", "tiers", "skip_asset_states"):
        if not isinstance(cfg.get(key), list):
            problems.append("%s must be a list" % key)

    labels = cfg.get("field_labels")
    if not isinstance(labels, dict):
        problems.append("field_labels must be an object")
    else:
        for slot, names in labels.items():
            if not isinstance(names, list):
                problems.append("field_labels.%s must be a list of labels" % slot)

    try:
        re.compile(cfg.get("service_tag_pattern") or "")
    except re.error as exc:
        problems.append("service_tag_pattern is not a valid regex: %s" % exc)

    if isinstance(cfg.get("tiers"), list):
        for i, entry in enumerate(cfg["tiers"]):
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                problems.append("tiers[%d] must be a [label, pattern] pair" % i)
                continue
            try:
                re.compile(entry[1])
            except (re.error, TypeError) as exc:
                problems.append("tiers[%d] pattern is not a valid regex: %s" % (i, exc))

    cap = cfg.get("max_writes")
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
        problems.append("max_writes must be a non-negative integer")

    if problems:
        raise ConfigError("config problems:\n  " + "\n  ".join(problems))
    return cfg


def _normalise_labels(cfg):
    """Compare field labels without regard to case or padding.

    `resolve_field_keys` lower-cases the label Freshservice reports, but nothing
    lower-cased the label from config. So `"Warranty Tier"` in config.json never
    matched anything, the field was quietly skipped, and the only symptom was one
    WARN line in a job log.
    """
    labels = cfg.get("field_labels")
    if isinstance(labels, dict):
        cfg["field_labels"] = {
            slot: [str(name).strip().lower() for name in names]
            for slot, names in labels.items()
        }
    return cfg


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            try:
                user = json.load(fh)
            except ValueError as exc:
                raise ConfigError("%s is not valid JSON: %s" % (path, exc))
        if not isinstance(user, dict):
            raise ConfigError("%s must contain a JSON object" % path)
        # `_comment` keys document the example file; they are not settings.
        cfg = _merge(cfg, {k: v for k, v in user.items() if not k.startswith("_")})
    return _normalise_labels(validate_config(cfg))


def require_env(*names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        sys.exit("missing environment variable(s): " + ", ".join(missing))
    return [os.environ[n] for n in names]


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

class HttpError(Exception):
    def __init__(self, status, body):
        super().__init__("HTTP %s: %s" % (status, body[:300]))
        self.status = status
        self.body = body


def _request(req, attempts=4):
    """Retry on 429 and 5xx, honouring Retry-After. A full pass makes hundreds of
    calls, and one transient 502 used to abort mid-run and leave assets half
    updated."""
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read().decode("utf-8")
                if not raw:
                    return {}
                try:
                    return json.loads(raw)
                except ValueError:
                    # An HTML error page or a captive portal, served with a 200.
                    raise HttpError(resp.getcode(), raw)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if (exc.code != 429 and exc.code < 500) or i == attempts - 1:
                raise HttpError(exc.code, body)
            try:
                delay = min(float(exc.headers.get("Retry-After")), 60.0)
            except (AttributeError, TypeError, ValueError):
                delay = 2 * (i + 1)
            time.sleep(delay)
        except urllib.error.URLError:
            if i == attempts - 1:
                raise
            time.sleep(2 * (i + 1))
    return {}


# --------------------------------------------------------------------------
# Dell
# --------------------------------------------------------------------------

def dell_token(client_id, client_secret):
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(
        DELL_TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    return _request(req)["access_token"]


def dell_entitlements(token, tags):
    """Fetch entitlements for up to DELL_BATCH service tags."""
    url = DELL_ENTITLEMENTS_URL + "?" + urllib.parse.urlencode(
        {"servicetags": ",".join(tags)})
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
    })
    out = _request(req)
    return out if isinstance(out, list) else [out]


class Dell:
    """Holds the Dell token and replaces it when it expires.

    A Dell token lasts about an hour. A full pass over a large estate, plus any
    retries, can outlive one. The token used to be fetched once for the whole
    run, so a 401 part way through aborted everything and left the estate half
    updated.

    `_request` deliberately does not retry a 401 itself. A 401 from a wrong
    client secret must fail at once rather than loop, so only this class, which
    can do something about it, retries.
    """

    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.refreshes = 0

    def _new_token(self):
        self.token = dell_token(self.client_id, self.client_secret)
        return self.token

    def entitlements(self, tags):
        if not self.token:
            self._new_token()
        try:
            return dell_entitlements(self.token, tags)
        except HttpError as exc:
            if exc.status != 401:
                raise
            self.refreshes += 1
            self._new_token()
            return dell_entitlements(self.token, tags)


# --------------------------------------------------------------------------
# Freshservice
# --------------------------------------------------------------------------

class Freshservice:
    def __init__(self, domain, api_key):
        self.domain = domain.strip().rstrip("/")
        self.auth = "Basic " + base64.b64encode((api_key + ":X").encode()).decode()

    def _call(self, method, path, body=None):
        url = "https://%s/api/v2%s" % (self.domain, path)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": self.auth}
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        return _request(req)

    def asset_types(self):
        out, page = [], 1
        while True:
            got = self._call("GET", "/asset_types?per_page=100&page=%d" % page).get("asset_types", [])
            out += got
            if len(got) < 100:
                return out
            page += 1

    def assets(self, asset_type_ids):
        """All assets of the given types, with type_fields included."""
        wanted, out, page = set(asset_type_ids), [], 1
        while True:
            got = self._call("GET", "/assets?include=type_fields&per_page=100&page=%d" % page).get("assets", [])
            out += [a for a in got if a.get("asset_type_id") in wanted]
            if len(got) < 100:
                return out
            page += 1

    def fields(self, asset_type_id):
        """Every type_field on this asset type, flattened out of its sections.

        The whole field record, not only the key, because the pre-flight checks
        below also need the label and the dropdown choices.
        """
        raw = self._call("GET", "/asset_types/%s/fields" % asset_type_id)
        sections = raw.get("asset_type_fields") or raw.get("fields") or []
        out = []
        for section in sections:
            out += section.get("fields") or []
        return out

    def update_asset(self, display_id, type_fields):
        return self._call("PUT", "/assets/%s" % display_id,
                          {"asset": {"type_fields": type_fields}})


# --------------------------------------------------------------------------
# field resolution
# --------------------------------------------------------------------------

def resolve_field_keys(fields, label_map):
    """Map our slot names to this tenant's type_field keys, matched by label."""
    keys = {}
    for field in fields:
        label = (field.get("label") or "").strip().lower()
        for slot, names in label_map.items():
            if label in names:
                keys[slot] = field.get("name")
    return keys


def field_choices(field):
    """The dropdown choices for a field, or None when it is not a dropdown.

    Freshservice reports `choices` in more than one shape, so normalise it
    rather than assume a list of strings.
    """
    raw = field.get("choices")
    if not raw:
        return None
    if isinstance(raw, dict):
        return [str(key) for key in raw]
    out = []
    for item in raw:
        if isinstance(item, dict):
            for key in ("value", "name", "label"):
                if item.get(key) is not None:
                    out.append(str(item[key]))
                    break
        elif isinstance(item, (list, tuple)):
            # Freshservice returns most dropdowns as a [value, label] pair,
            # sometimes [value, id]. Falling through to str() below turned the
            # whole pair into "['Yes', 'Yes']", which matches nothing, so every
            # defined choice read as missing and the pre-flight warned on every
            # dropdown of every run. A check that always fires is a check
            # nobody reads, and it would have hidden a real missing choice.
            if item:
                out.append(str(item[0]))
        else:
            out.append(str(item))
    return out or None


# What each slot writes. Used to spot a field that cannot hold it.
SLOT_KINDS = {
    "warranty_tier": "text",
    "adp": "text",
    "detail": "text",
    "warranty_expiry": "date",
    "acquisition": "date",
    "warranty_months": "number",
}


def field_kind(field):
    """text, date or number, or None when the type is not one we recognise.

    None means no opinion, so an unfamiliar Freshservice type raises no
    warning rather than a wrong one.
    """
    kind = str(field.get("field_type") or "").lower()
    if "date" in kind:
        return "date"
    if "number" in kind or "decimal" in kind:
        return "number"
    if "text" in kind or "paragraph" in kind or "dropdown" in kind:
        return "text"
    return None


def check_field_kinds(fields, keys):
    """Report a resolved field that cannot hold the value we would write.

    The default label for `warranty_months` is just "warranty", which is
    generic enough to land on a date or a text field in some tenants. Writing a
    month count into a date field fails on every asset, one at a time.
    """
    by_name = {f.get("name"): f for f in fields}
    problems = []
    for slot in sorted(SLOT_KINDS):
        field = by_name.get(keys.get(slot))
        if not field:
            continue
        kind = field_kind(field)
        if kind and kind != SLOT_KINDS[slot]:
            problems.append(
                "%s is a %s field, but %s writes a %s"
                % (field.get("label") or field.get("name"), kind, slot,
                   SLOT_KINDS[slot]))
    return problems


def check_choices(fields, keys, expected):
    """Report values we would write that the dropdown will not accept.

    Freshservice answers 400 for a dropdown value that is not one of the defined
    choices. Without this check, the first --apply run is where you find out. The
    field metadata already lists the choices, so a dry run can say so instead.

    A field with no choices is not a dropdown, so it is left alone.
    """
    by_name = {f.get("name"): f for f in fields}
    problems = []
    for slot in sorted(expected):
        field = by_name.get(keys.get(slot))
        if not field:
            continue
        choices = field_choices(field)
        if choices is None:
            continue
        allowed = {str(c).strip().lower() for c in choices}
        gaps = [v for v in expected[slot] if str(v).strip().lower() not in allowed]
        if gaps:
            # One line per field, not per value. A tenant that defined two of
            # the five tiers should read one warning, not four.
            problems.append(
                "%s is missing choice(s): %s  (it has: %s)"
                % (field.get("label") or field.get("name"),
                   ", ".join(gaps), ", ".join(sorted(choices))))
    return problems


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

# "Complete Care" is Dell's name for the accidental damage product, and it is
# not always spelled out alongside "Accidental Damage". A false negative here is
# the expensive direction: it tells a technician a cracked screen is not covered.
ADP_RE = re.compile(r"accidental damage|complete care|\badp\b", re.I)
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def unchanged(current, wanted):
    """True when Freshservice already holds `wanted`. Dates compare on the date
    part only, because Freshservice returns them with a time component."""
    a = "" if current is None else str(current)
    b = "" if wanted is None else str(wanted)
    if ISO_DATE_RE.match(a) and ISO_DATE_RE.match(b):
        return a[:10] == b[:10]
    return a == b


def classify(obj, tiers, today=None):
    """Turn one Dell asset-entitlement record into the values we write.

    The Expired rule is deliberate and was a real defect once. Marking anything
    whose active coverage description is unrecognised as "Expired" put the word
    Expired next to a future warranty end date, and a technician quoted a paid
    repair on a covered machine. Unrecognised but still active coverage stays
    Unknown, and Unknown is never written.
    """
    today = today or datetime.date.today().isoformat()
    ents = obj.get("entitlements") or []

    def desc(e):
        return str(e.get("serviceLevelDescription") or "")

    active = [desc(e) for e in ents if (e.get("endDate") or "")[:10] >= today]
    every = [desc(e) for e in ents]

    tier = "Unknown"
    for label, pattern in tiers:
        if any(re.search(pattern, d, re.I) for d in active):
            tier = label
            break
    if tier == "Unknown" and ents and not active:
        tier = "Expired"

    if any(ADP_RE.search(d) for d in active):
        adp = "Yes"
    else:
        adp = "No" if ents else "Unknown"

    ends = [(e.get("endDate") or "")[:10] for e in ents if e.get("endDate")]
    warranty_end = max(ends) if ends else None

    ship = (obj.get("shipDate") or "")[:10] or None
    months = None
    if ship and warranty_end:
        try:
            start = datetime.date.fromisoformat(ship)
            end = datetime.date.fromisoformat(warranty_end)
            months = max(0, round((end - start).days / 30.4375))
        except ValueError:
            months = None

    joined = "; ".join(sorted({d for d in every if d}))
    detail = joined if len(joined) <= DETAIL_MAX else joined[:DETAIL_MAX].rsplit("; ", 1)[0]

    return {
        "tier": tier,
        "adp": adp,
        "warranty_end": warranty_end,
        "ship": ship,
        "warranty_months": months,
        "detail": detail,
        "has_data": bool(ents),
        "product_line": obj.get("productLineDescription") or obj.get("systemDescription") or "",
    }


SERIAL_KEY_RE = re.compile(r"^(serial_number|service_tag)")
ASSET_STATE_KEY_RE = re.compile(r"^asset_state")
PRODUCT_KEY_RE = re.compile(r"^(product|model|asset_model|manufacturer|vendor)")


def asset_serial(asset):
    """The service tag, from whichever type_field this tenant keeps it in."""
    for key, value in (asset.get("type_fields") or {}).items():
        if SERIAL_KEY_RE.match(key) and value:
            return str(value).strip()
    return str(asset.get("serial_number") or "").strip()


def asset_state(asset):
    """The asset state, or "" when this tenant does not set one.

    Freshservice keys it `asset_state_<asset type id>`, so it is found by
    prefix like the serial is.
    """
    for key, value in (asset.get("type_fields") or {}).items():
        if ASSET_STATE_KEY_RE.match(key) and value:
            return str(value).strip()
    return ""


def asset_product_text(asset):
    """Every part of the Freshservice record that might name the model.

    The asset `name` is usually a hostname (LT-JSMITH-01), which names no
    product at all. Reading the product and model type_fields as well is what
    makes the Dell filter and the collision guard below actually fire; on the
    name alone they almost never did.
    """
    parts = [asset.get("name"), asset.get("description")]
    for key, value in (asset.get("type_fields") or {}).items():
        if PRODUCT_KEY_RE.match(key):
            parts.append(value)
    return " ".join(str(p) for p in parts if p)


def looks_dell(product_name, patterns):
    low = (product_name or "").lower()
    return any(str(p).lower() in low for p in patterns)


FAMILIES = ("latitude", "optiplex", "precision", "xps", "inspiron", "vostro",
            "poweredge", "wyse", "venue", "ultrasharp", "chromebook")
DOCK_RE = re.compile(r"\b(wd|hd|ud|tb)\d+", re.I)
# Dell monitor model codes: E2425HM, P2725HE, U2723QE, S2721DGF. Freshservice
# names them "Dell Pro 24 Monitor - E2425HM" and Dell answers "DELL PRO 24
# E2425HM", so the model code is the only part both sides share.
# The trailing (?![0-9]) rather than \b: Freshservice suffixes its names
# ("E2425HM_1"), and _ is a word character, so \b never matched. Refusing a
# following digit also stops a plain serial like P1234567 from matching.
MONITOR_RE = re.compile(r"\b([epsu]\d{4})(?![0-9])", re.I)


def product_family(name):
    """Coarse family token for a product string, or None if unrecognisable.

    None means "cannot tell", which the collision guard treats as permission to
    proceed. So a pattern that fails to recognise a product is safe, and a
    pattern that recognises the wrong thing is not.
    """
    low = (name or "").lower()
    for fam in FAMILIES:
        if fam in low:
            return fam
    match = DOCK_RE.search(low)
    if match:
        return match.group(0).lower()
    match = MONITOR_RE.search(low)
    return match.group(1).lower() if match else None


def same_machine(fs_product, dell_product_line):
    """Guard against a service-tag collision.

    A seven-character serial from another manufacturer can be a real Dell
    service tag. Dell then answers with a completely different machine's
    warranty, and writing it would put one asset's coverage onto another.

    Only block when both sides name a family we recognise AND they disagree.
    An unknown family on either side is not evidence of a collision, so it is
    allowed through rather than silently skipping half the estate.
    """
    a = product_family(fs_product)
    b = product_family(dell_product_line)
    if a and b:
        return a == b
    return True


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def build_updates(asset, result, keys):
    """Only the fields that would actually change. Unknown is never written."""
    current = asset.get("type_fields") or {}
    wanted = {}
    if result["tier"] != "Unknown" and keys.get("warranty_tier"):
        wanted[keys["warranty_tier"]] = result["tier"]
    if result["adp"] != "Unknown" and keys.get("adp"):
        wanted[keys["adp"]] = result["adp"]
    if result["detail"] and keys.get("detail"):
        wanted[keys["detail"]] = result["detail"]
    if result["warranty_end"] and keys.get("warranty_expiry"):
        wanted[keys["warranty_expiry"]] = result["warranty_end"]
    if result.get("ship") and keys.get("acquisition"):
        wanted[keys["acquisition"]] = result["ship"]
    if result.get("warranty_months") is not None and keys.get("warranty_months"):
        wanted[keys["warranty_months"]] = result["warranty_months"]
    return {k: v for k, v in wanted.items() if not unchanged(current.get(k), v)}


def split_writes(updates, keys, expiry_value=None):
    """Split one asset's updates so Dell's exact expiry date survives.

    Freshservice computes Warranty Expiry Date itself whenever Acquisition Date
    or Warranty length is filled in or changed. Sent in one call, that computed
    date lands on top of the exact end date Dell gave us. The computed one is
    the acquisition date plus a whole number of months, so it can be about two
    weeks out, and a technician reading it two weeks late quotes a paid repair
    on a covered machine.

    So the two trigger fields go in the first call, and the expiry date goes in
    a second call that changes neither of them. Freshservice does not recompute
    for that one, so the exact date stays.

    The expiry is re-asserted even when Freshservice already holds the right
    value, because the first call would otherwise recompute it away and the
    field would flap between runs.

    Returns one payload, or two in the order they must be sent.
    """
    expiry_key = keys.get("warranty_expiry")
    triggers = [k for k in (keys.get("acquisition"), keys.get("warranty_months"))
                if k and k in updates]
    if not expiry_key or not triggers:
        return [updates]
    expiry = updates.get(expiry_key, expiry_value)
    if not expiry:
        # Nothing to protect. Freshservice may compute whatever it likes.
        return [updates]
    first = {k: v for k, v in updates.items() if k != expiry_key}
    return [first, {expiry_key: expiry}]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--apply", action="store_true",
                    help="write to Freshservice (default is a dry run)")
    ap.add_argument("--limit", type=int, default=0, help="process at most N assets")
    ap.add_argument("--only", action="append", metavar="TAG",
                    help="sync only this service tag; repeatable. Use it for "
                         "the first live write, on a machine you can check by "
                         "eye. Unlike --limit it does not pick an arbitrary "
                         "asset.")
    ap.add_argument("--max-writes", type=int, default=None,
                    help="abort after this many writes (0 = no cap)")
    ap.add_argument("--selftest", action="store_true",
                    help="run offline checks and exit; needs no credentials")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    cfg = load_config(args.config)
    tiers = [(label, pattern) for label, pattern in cfg["tiers"]]
    tag_re = re.compile(cfg["service_tag_pattern"])
    max_writes = cfg["max_writes"] if args.max_writes is None else args.max_writes

    dell_id, dell_secret, fs_domain, fs_key = require_env(
        "DELL_CLIENT_ID", "DELL_CLIENT_SECRET",
        "FRESHSERVICE_DOMAIN", "FRESHSERVICE_API_KEY")

    fs = Freshservice(fs_domain, fs_key)

    type_ids = {t["id"]: t["name"] for t in fs.asset_types()
                if t["name"] in cfg["asset_types"]}
    if not type_ids:
        sys.exit("no asset types matched %s" % cfg["asset_types"])
    print("asset types: " + ", ".join(sorted(type_ids.values())))

    # Values we would write into a dropdown. "Unknown" is not here because
    # build_updates never writes it.
    expected_values = {
        "warranty_tier": sorted({label for label, _ in tiers} | {"Expired"}),
        "adp": ["No", "Yes"],
    }

    keys_by_type = {}
    for tid in type_ids:
        fields = fs.fields(tid)
        keys = resolve_field_keys(fields, cfg["field_labels"])
        keys_by_type[tid] = keys
        missing = [s for s, names in cfg["field_labels"].items()
                   if names and s not in keys]
        if missing:
            print("  WARN %s is missing field(s): %s"
                  % (type_ids[tid], ", ".join(missing)))
        # Reported before any write, so a dry run catches both. Freshservice
        # rejects an undefined dropdown value, or a value of the wrong shape,
        # with a 400 on that asset.
        for problem in (check_choices(fields, keys, expected_values)
                        + check_field_kinds(fields, keys)):
            print("  WARN %s: %s" % (type_ids[tid], problem))

    if not any(keys_by_type.values()):
        # Otherwise every asset falls through build_updates with nothing to
        # write, and the summary reads "already correct: 1200". That looks like
        # a clean run and is really a complete no-op.
        sys.exit("no configured field label matched any field on %s.\n"
                 "Nothing could be written. Compare field_labels in your config "
                 "with the field labels in Freshservice."
                 % ", ".join(sorted(type_ids.values())))

    require_match = bool(cfg.get("require_product_match"))
    skip_states = {str(s).strip().lower() for s in cfg.get("skip_asset_states") or []}
    only = {str(t).strip().upper() for t in (args.only or []) if str(t).strip()}

    candidates, non_dell, retired = [], 0, 0
    no_tag, no_tag_examples = 0, []
    for asset in fs.assets(type_ids):
        serial = asset_serial(asset)
        if not serial or not tag_re.match(serial):
            # Counted, not silent. This is the filter that would quietly drop a
            # whole product type if its serials are not recorded as service
            # tags, and a silent drop looks identical to "we have none".
            no_tag += 1
            if serial and len(no_tag_examples) < 3:
                no_tag_examples.append(
                    "%s=%s" % (type_ids.get(asset.get("asset_type_id"), "?"),
                               serial))
            continue
        if only and serial.upper() not in only:
            continue
        # A retired or disposed asset needs no warranty data, and syncing it
        # spends a write and fills the log.
        if asset_state(asset).lower() in skip_states:
            retired += 1
            continue
        if require_match and not looks_dell(asset_product_text(asset), cfg["product_match"]):
            non_dell += 1
            continue
        candidates.append((serial.upper(), asset))

    if only:
        unmatched = sorted(only - {t for t, _ in candidates})
        if unmatched:
            # Silence here would read as "nothing to do" when the real answer
            # is that the tag is not in Freshservice under a scanned type.
            print("  WARN --only tag(s) matched no asset: %s"
                  % ", ".join(unmatched))
    if args.limit:
        candidates = candidates[:args.limit]

    notes = []
    if non_dell:
        notes.append("%d skipped by product_match" % non_dell)
    if retired:
        notes.append("%d skipped by asset state" % retired)
    if no_tag:
        notes.append("%d had no service tag" % no_tag)
    print("candidate Dell assets: %d%s"
          % (len(candidates), ("  (%s)" % ", ".join(notes)) if notes else ""))
    if no_tag_examples:
        print("  serials that did not look like a service tag, for example: %s"
              % ", ".join(no_tag_examples))

    # A breakdown by asset type, so you can see that servers, monitors and docks
    # really are in the run rather than assume it.
    by_type = {}
    for _, asset in candidates:
        name = type_ids.get(asset.get("asset_type_id"), "?")
        by_type[name] = by_type.get(name, 0) + 1
    if by_type:
        print("  by asset type: %s"
              % ", ".join("%s %d" % (n, by_type[n]) for n in sorted(by_type)))
    if not candidates:
        return 0

    dell = Dell(dell_id, dell_secret)
    by_tag = {}
    # One asset per tag is the norm, but a duplicated asset record would
    # otherwise spend the same Dell lookup twice.
    tags = list(dict.fromkeys(t for t, _ in candidates))
    for i in range(0, len(tags), DELL_BATCH):
        batch = tags[i:i + DELL_BATCH]
        for rec in dell.entitlements(batch):
            tag = (rec.get("serviceTag") or "").upper()
            if tag:
                by_tag[tag] = rec
        print("  Dell: %d/%d tags" % (min(i + DELL_BATCH, len(tags)), len(tags)))

    changed = skipped = written = split = 0
    capped = False
    no_data, mismatched, failed = [], [], []
    for tag, asset in candidates:
        rec = by_tag.get(tag)
        if not rec:
            no_data.append(tag)
            continue
        result = classify(rec, tiers)
        if not result["has_data"]:
            no_data.append(tag)
            continue
        if not same_machine(asset_product_text(asset), result["product_line"]):
            mismatched.append(tag)
            continue
        keys = keys_by_type.get(asset["asset_type_id"], {})
        updates = build_updates(asset, result, keys)
        if not updates:
            skipped += 1
            continue
        if args.apply and max_writes and written >= max_writes:
            # Before the print, not after it: the old order announced a change
            # it then declined to make, and counted it in the summary.
            print("  stopping: hit --max-writes %d" % max_writes)
            capped = True
            break
        changed += 1
        label = asset.get("name") or tag
        print("  %s (%s): %s" % (label, tag,
                                 ", ".join("%s=%s" % (k, v) for k, v in updates.items())))
        payloads = split_writes(updates, keys, result["warranty_end"])
        if len(payloads) > 1:
            split += 1
        if args.apply:
            sent = 0
            try:
                for payload in payloads:
                    fs.update_asset(asset["display_id"], payload)
                    sent += 1
                written += 1
            except (HttpError, urllib.error.URLError) as exc:
                # One asset Freshservice will not accept must not abandon the
                # rest of the estate, and must not skip the summary. A dropdown
                # value that is not a defined choice fails with a 400 on that
                # asset alone.
                failed.append(label)
                print("    FAILED %s: %s" % (label, exc))
                if sent:
                    # It matters which half landed. The expiry date goes last,
                    # so this asset now holds the date Freshservice computed.
                    print("    NOTE %s was partly written; re-run to finish it"
                          % label)

    print("\nsummary")
    print("  would change : %d" % changed)
    print("  already correct: %d" % skipped)
    print("  no Dell data : %d" % len(no_data))
    print("  tag mismatch : %d%s" % (len(mismatched),
          ("  " + ", ".join(mismatched[:5])) if mismatched else ""))
    print("  written      : %d%s" % (written, "" if args.apply else "  (dry run)"))
    print("  write failed : %d%s" % (len(failed),
          ("  " + ", ".join(failed[:5])) if failed else ""))
    if split:
        print("  split writes : %d  (the expiry date goes in a second call, so"
              " Freshservice cannot compute over it)" % split)
    if dell.refreshes:
        print("  NOTE: the Dell token expired mid-run and was renewed %d time(s)"
              % dell.refreshes)
    if capped:
        print("  NOTE: stopped at the write cap; re-run to continue")
    return 1 if failed else 0


# --------------------------------------------------------------------------
# offline checks
# --------------------------------------------------------------------------

def selftest():
    tiers = [(l, p) for l, p in DEFAULT_CONFIG["tiers"]]
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append("%s: got %r want %r" % (name, got, want))
        print("  %s %s" % ("PASS" if got == want else "FAIL", name))

    future, past = "2099-01-01", "2000-01-01"

    r = classify({"entitlements": [
        {"serviceLevelDescription": "ProSupport Plus", "endDate": future}]}, tiers)
    check("prosupport plus recognised", r["tier"], "ProSupport Plus")
    check("no adp when not mentioned", r["adp"], "No")

    r = classify({"entitlements": [
        {"serviceLevelDescription": "ProSupport", "endDate": future},
        {"serviceLevelDescription": "Accidental Damage Service", "endDate": future}]}, tiers)
    check("adp detected", r["adp"], "Yes")
    check("tier still prosupport", r["tier"], "ProSupport")

    r = classify({"entitlements": [
        {"serviceLevelDescription": "ProSupport", "endDate": past}]}, tiers)
    check("all ended means expired", r["tier"], "Expired")

    r = classify({"entitlements": [
        {"serviceLevelDescription": "Some New Dell Plan", "endDate": future}]}, tiers)
    check("unrecognised but active is not expired", r["tier"], "Unknown")

    r = classify({"entitlements": []}, tiers)
    check("no entitlements means unknown adp", r["adp"], "Unknown")
    check("no entitlements means no data", r["has_data"], False)

    r = classify({"entitlements": [
        {"serviceLevelDescription": "ProSupport", "endDate": "2027-05-01"},
        {"serviceLevelDescription": "Basic", "endDate": "2029-05-01"}]}, tiers)
    check("latest end date wins", r["warranty_end"], "2029-05-01")

    check("dates compare on date part", unchanged("2029-05-01T00:00:00Z", "2029-05-01"), True)
    check("different dates differ", unchanged("2029-05-01", "2029-05-02"), False)
    check("none is empty", unchanged(None, ""), True)

    upd = build_updates({"type_fields": {"wt": "ProSupport"}},
                        {"tier": "ProSupport", "adp": "Unknown", "detail": "",
                         "warranty_end": None},
                        {"warranty_tier": "wt", "adp": "adp"})
    check("no write when already correct", upd, {})

    check("dock family from model", product_family("Dell WD19 Dock"), "wd19")
    check("laptop family", product_family("Latitude 5540"), "latitude")
    check("unknown family is none", product_family("Acme Widget"), None)
    check("guard allows same family", same_machine("Latitude 5540", "Latitude 7440"), True)
    check("guard blocks tag collision", same_machine("Latitude 5540", "OptiPlex 7010"), False)
    check("guard allows unknown family", same_machine("Acme Widget", "Latitude"), True)

    upd = build_updates({"type_fields": {}},
                        {"tier": "Unknown", "adp": "Unknown", "detail": "",
                         "warranty_end": None},
                        {"warranty_tier": "wt", "adp": "adp"})
    check("unknown is never written", upd, {})

    # A config naming one field_labels slot must not delete the rest. Copying
    # config.example.json used to switch off two fields without a word.
    merged = _merge(DEFAULT_CONFIG, {"field_labels": {"warranty_tier": ["tier"]}})
    check("partial field_labels keeps other slots",
          sorted(merged["field_labels"]), sorted(DEFAULT_CONFIG["field_labels"]))
    check("partial field_labels overrides its own slot",
          merged["field_labels"]["warranty_tier"], ["tier"])
    check("top level values still replace",
          _merge(DEFAULT_CONFIG, {"max_writes": 7})["max_writes"], 7)

    def rejects(cfg_override):
        try:
            validate_config(_merge(DEFAULT_CONFIG, cfg_override))
            return False
        except ConfigError:
            return True

    check("bad tier regex is rejected", rejects({"tiers": [["X", "(unclosed"]]}), True)
    check("malformed tier pair is rejected", rejects({"tiers": [["X"]]}), True)
    check("negative max_writes is rejected", rejects({"max_writes": -1}), True)
    check("default config is valid", rejects({}), False)

    # The Freshservice asset name is normally a hostname, so the product and
    # model type_fields are where the model name actually lives.
    asset = {"name": "LT-JSMITH-01",
             "type_fields": {"product_5": "Latitude 5540", "serial_number_5": "ABC1234"}}
    check("serial read from type_fields", asset_serial(asset), "ABC1234")
    check("product text reaches the model field",
          product_family(asset_product_text(asset)), "latitude")
    check("guard blocks collision via model field",
          same_machine(asset_product_text(asset), "OptiPlex 7010"), False)
    check("hostname alone names no family", product_family("LT-JSMITH-01"), None)

    upd = build_updates({"type_fields": {}},
                        {"tier": "Unknown", "adp": "Unknown", "detail": "",
                         "warranty_end": None, "warranty_months": 0},
                        {"warranty_months": "wm"})
    check("zero months is a value, not a blank", upd, {"wm": 0})

    # A label typed with capitals in config.json used to match nothing at all.
    cfg = _normalise_labels(_merge(
        DEFAULT_CONFIG, {"field_labels": {"warranty_tier": ["  Warranty Tier "]}}))
    check("config labels are lower-cased",
          cfg["field_labels"]["warranty_tier"], ["warranty tier"])

    # Freshservice keys carry the asset type id, so fields are found by label.
    FIELDS = [
        {"name": "warranty_tier_2100", "label": "Warranty Tier",
         "choices": ["ProSupport Plus", "ProSupport", "Expired"]},
        {"name": "adp_2100", "label": "Accidental Damage Protection",
         "choices": [{"value": "Yes"}, {"value": "No"}]},
        {"name": "detail_2100", "label": "Dell Coverage Detail"},
    ]
    keys = resolve_field_keys(FIELDS, cfg["field_labels"])
    check("key resolved by label", keys.get("warranty_tier"), "warranty_tier_2100")

    check("choices as strings", field_choices(FIELDS[0]),
          ["ProSupport Plus", "ProSupport", "Expired"])
    check("choices as objects", field_choices(FIELDS[1]), ["Yes", "No"])
    check("a text field has no choices", field_choices(FIELDS[2]), None)

    # The shape a real tenant returns. Custom dropdowns come back as
    # [value, label] and the built-in ones as [value, id]. Both were read as
    # one opaque string before, so the pre-flight warned on every dropdown of
    # every run and could not have reported a genuine missing choice.
    check("choices as value/label pairs",
          field_choices({"name": "x", "choices": [["Yes", "Yes"], ["No", "No"]]}),
          ["Yes", "No"])
    check("choices as value/id pairs",
          field_choices({"name": "x", "choices": [["In Stock", 4], ["Retired", 5]]}),
          ["In Stock", "Retired"])
    check("an empty pair is skipped",
          field_choices({"name": "x", "choices": [[], ["Yes", "Yes"]]}), ["Yes"])

    PAIRED = [{"name": "warranty_tier_2100", "label": "Warranty Tier",
               "choices": [["ProSupport", "ProSupport"], ["Expired", "Expired"]]}]
    PAIRED_KEYS = {"warranty_tier": "warranty_tier_2100"}
    check("paired choices that cover the values do not warn",
          check_choices(PAIRED, PAIRED_KEYS,
                        {"warranty_tier": ["ProSupport", "Expired"]}), [])
    check("paired choices still report a real gap",
          len(check_choices(PAIRED, PAIRED_KEYS,
                            {"warranty_tier": ["ProSupport", "Return to Depot"]})), 1)

    full = resolve_field_keys(FIELDS, DEFAULT_CONFIG["field_labels"])
    check("all three fields resolve", sorted(full),
          ["adp", "detail", "warranty_tier"])

    check("valid choices raise nothing",
          check_choices(FIELDS, full, {"warranty_tier": ["ProSupport", "Expired"],
                                       "adp": ["Yes", "No"]}), [])

    problems = check_choices(FIELDS, full,
                             {"warranty_tier": ["Premium Support", "Return to Depot"]})
    check("missing choices give one line per field", len(problems), 1)
    check("the report names the label",
          bool(problems) and problems[0].startswith("Warranty Tier is missing"),
          True)
    check("the report names every missing value",
          bool(problems) and "Premium Support, Return to Depot" in problems[0], True)

    # A text field takes free text, so it must not be checked against choices.
    check("text field is not choice-checked",
          check_choices(FIELDS, full, {"detail": ["anything at all"]}), [])

    # An unresolved slot cannot be checked, and must not invent a warning.
    check("unresolved slot raises nothing",
          check_choices(FIELDS, {}, {"warranty_tier": ["Nope"]}), [])

    # A month count written into a date field fails on every asset.
    KINDS = [
        {"name": "wm", "label": "Warranty", "field_type": "custom_date"},
        {"name": "wt", "label": "Warranty Tier", "field_type": "custom_dropdown"},
        {"name": "wx", "label": "Warranty Expiry Date", "field_type": "custom_date"},
    ]
    check("a date field reads as a date", field_kind(KINDS[0]), "date")
    check("a dropdown reads as text", field_kind(KINDS[1]), "text")
    check("a number field reads as a number",
          field_kind({"field_type": "custom_number"}), "number")
    check("an unknown type has no opinion",
          field_kind({"field_type": "custom_lookup"}), None)
    check("a missing type has no opinion", field_kind({}), None)

    problems = check_field_kinds(KINDS, {"warranty_months": "wm"})
    check("months into a date field is reported", len(problems), 1)
    check("the report names the field",
          bool(problems) and problems[0].startswith("Warranty is a date field"),
          True)
    check("a matching kind is not reported",
          check_field_kinds(KINDS, {"warranty_expiry": "wx",
                                    "warranty_tier": "wt"}), [])
    check("an unresolved slot has no kind warning",
          check_field_kinds(KINDS, {}), [])

    # The state is keyed asset_state_<asset type id>, like the serial is.
    check("asset state read from type_fields",
          asset_state({"type_fields": {"asset_state_5": " Retired "}}), "Retired")
    check("no state field gives an empty string", asset_state({}), "")
    check("a blank state gives an empty string",
          asset_state({"type_fields": {"asset_state_5": ""}}), "")

    check("skip_asset_states must be a list",
          rejects({"skip_asset_states": "Retired"}), True)
    check("retired and disposed are skipped by default",
          DEFAULT_CONFIG["skip_asset_states"], ["Retired", "Disposed"])

    # Freshservice computes the expiry from acquisition date and warranty
    # length whenever either is filled in or changed. So those two must not
    # travel in the same call as the exact date from Dell.
    KEYS = {"warranty_tier": "wt", "warranty_expiry": "wx",
            "acquisition": "acq", "warranty_months": "wm"}

    check("acquisition and expiry are split",
          split_writes({"wx": "2027-05-01", "acq": "2023-01-15"}, KEYS),
          [{"acq": "2023-01-15"}, {"wx": "2027-05-01"}])

    check("months and expiry are split",
          split_writes({"wx": "2027-05-01", "wm": 52}, KEYS),
          [{"wm": 52}, {"wx": "2027-05-01"}])

    check("the expiry goes last",
          split_writes({"wx": "2027-05-01", "acq": "2023-01-15"}, KEYS)[-1],
          {"wx": "2027-05-01"})

    check("other fields ride in the first call",
          split_writes({"wt": "ProSupport", "wx": "2027-05-01",
                        "acq": "2023-01-15"}, KEYS)[0],
          {"wt": "ProSupport", "acq": "2023-01-15"})

    # No trigger field means no recompute, so one call is enough.
    check("expiry alone is one call",
          split_writes({"wx": "2027-05-01"}, KEYS), [{"wx": "2027-05-01"}])
    check("a tier change alone is one call",
          split_writes({"wt": "ProSupport"}, KEYS), [{"wt": "ProSupport"}])

    # The expiry is re-asserted even when Freshservice already holds it,
    # otherwise the first call recomputes it away and the field flaps.
    check("an unchanged expiry is still re-asserted",
          split_writes({"acq": "2023-01-15"}, KEYS, "2027-05-01"),
          [{"acq": "2023-01-15"}, {"wx": "2027-05-01"}])

    # Nothing to protect, so no second call.
    check("no expiry value means one call",
          split_writes({"acq": "2023-01-15"}, KEYS, None),
          [{"acq": "2023-01-15"}])
    check("no expiry field means one call",
          split_writes({"acq": "2023-01-15"}, {"acquisition": "acq"},
                       "2027-05-01"),
          [{"acq": "2023-01-15"}])

    # The coverage descriptions below are Dell's own product wording, observed
    # from the API, so the classifier is pinned to real responses rather than
    # to guesses. Service tags, asset tags and asset names are INVENTED. Never
    # use a live record as a fixture: it puts asset identifiers, and sometimes
    # a person's name, into the repo.

    # Monitor (DELL PRO 24 E2425HM) and dock (DELL PRO DOCK WD25).
    r = classify({"entitlements": [
        {"serviceLevelDescription": "Advanced Exchange Support",
         "endDate": future}]}, tiers)
    check("monitor and dock coverage is recognised", r["tier"], "Advanced Exchange")
    check("advanced exchange is not adp", r["adp"], "No")

    # Server (POWEREDGE R450). One long string carrying "Next Business Day".
    SERVER_LEVEL = ("Onsite Service After Remote Diagnosis (Consumer Customer)/"
                    " Next Business Day Onsite After Remote Diagnosis (for"
                    " business Customer)")
    r = classify({"entitlements": [
        {"serviceLevelDescription": SERVER_LEVEL, "endDate": future}]}, tiers)
    check("server NBD coverage is recognised", r["tier"], "Basic Onsite (NBD)")

    # Laptop (LATITUDE 5550) with accidental damage cover.
    r = classify({"entitlements": [
        {"serviceLevelDescription": "Complete Care / Accidental Damage",
         "endDate": future},
        {"serviceLevelDescription": "Return To Depot Support",
         "endDate": future}]}, tiers)
    check("real laptop adp is detected", r["adp"], "Yes")
    check("real laptop tier is depot", r["tier"], "Return to Depot")

    # Complete Care on its own must still count as accidental damage cover.
    r = classify({"entitlements": [
        {"serviceLevelDescription": "Dell Complete Care", "endDate": future}]},
        tiers)
    check("complete care alone is adp", r["adp"], "Yes")

    # ProSupport must still win over a less specific entitlement on the same
    # asset, so the new tier must not have changed the ordering.
    r = classify({"entitlements": [
        {"serviceLevelDescription": "Advanced Exchange Support", "endDate": future},
        {"serviceLevelDescription": "ProSupport Plus", "endDate": future}]}, tiers)
    check("prosupport plus still outranks advanced exchange",
          r["tier"], "ProSupport Plus")

    # The collision guard has to work for the new product types. Freshservice
    # and Dell name the same monitor differently, and only the model code is
    # common to both.
    check("monitor model code is a family",
          product_family("Dell Pro 24 Monitor - E2425HM_1"), "e2425")
    check("dell names the same monitor differently",
          product_family("DELL PRO 24 E2425HM"), "e2425")
    check("guard allows the same monitor",
          same_machine("Dell Pro 24 Monitor - E2425HM_1", "DELL PRO 24 E2425HM"),
          True)
    check("guard blocks a different monitor",
          same_machine("Dell 27 Monitor - P2725HE_1", "DELL PRO 24 E2425HM"),
          False)
    check("dock model code is a family",
          product_family("Dell Pro Dock - WD25 _1"), "wd25")
    check("guard allows the same dock",
          same_machine("Dell WD25 Dock_1", "DELL PRO DOCK WD25"), True)
    check("server family is recognised",
          product_family("POWEREDGE R450"), "poweredge")

    # A monitor named after its user carries no model, which must not block it.
    check("a monitor named after a person names no family",
          product_family("Jane Doe Monitor_2"), None)
    check("guard allows an unnamed monitor",
          same_machine("Jane Doe Monitor_2", "DELL PRO 24 E2425HM"), True)

    # A VM's asset tag is not a service tag, and must not be treated as one.
    tag_pattern = re.compile(DEFAULT_CONFIG["service_tag_pattern"])
    check("a VM asset tag is not a service tag",
          bool(tag_pattern.match("ASSET-1234")), False)
    check("a 10 character serial is not a service tag",
          bool(tag_pattern.match("AB12CD34EF")), False)
    check("a real server tag is a service tag",
          bool(tag_pattern.match("ABC1234")), True)
    check("a real dock tag is a service tag",
          bool(tag_pattern.match("XYZ7890")), True)

    check("the new asset types are in the defaults",
          [t for t in ("Server", "Monitor", "Dock")
           if t in DEFAULT_CONFIG["asset_types"]],
          ["Server", "Monitor", "Dock"])

    print("\n%s" % ("all checks passed" if not fails else "FAILURES:\n  " + "\n  ".join(fails)))
    return 1 if fails else 0


def cli(argv=None):
    """Turn the failures a scheduled run can actually hit into one readable
    line and a non-zero exit, rather than a traceback in a job log."""
    try:
        return main(argv)
    except ConfigError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    except HttpError as exc:
        sys.stderr.write("aborted: %s\n" % exc)
        return 1
    except urllib.error.URLError as exc:
        sys.stderr.write("aborted: network error: %s\n" % exc.reason)
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        return 130


if __name__ == "__main__":
    sys.exit(cli())
