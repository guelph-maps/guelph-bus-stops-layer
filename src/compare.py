"""Compare the City bus stops against the bus stops already in OSM.

Pulls bus stops from OpenStreetMap (Overpass) within Guelph, then matches each
City stop to them -- by stop ID first, since OSM mappers already carry it as
``ref``, and by distance only where OSM has a stop with no ref. The result is
written to data/gaps.geojson, one row per City stop that needs work, plus one
per OSM stop the City no longer lists:

  * "missing"   -- no OSM stop carries this ref and none sits unreferenced
                   beside it (add the stop);
  * "noref"     -- an OSM stop with no ref is this one (add ref=<StopId>);
  * "far"       -- the OSM stop with this ref is more than FAR_M away (one of
                   the two is in the wrong place);
  * "duplicate" -- two OSM stops more than FAR_M apart carry this ref;
  * "unnamed"   -- matched, but the OSM stop has no name;
  * "mismatch"  -- matched, but its OSM name differs;
  * "ondemand"  -- missing, but the City serves it by on-demand transit only:
                   a pick-up point, which may or may not belong in OSM as a
                   stop, so it is not offered as a plain "missing";
  * "osm_only"  -- an OSM Guelph Transit stop whose ref the City does not list
                   (retired, renumbered, or a typo).

Distances are equirectangular metres -- exact enough at city scale, and no GIS
dependency. A flaky Overpass falls back to the cached response, but only after
every mirror has been tried several times -- see _load_osm.
"""

import json
import math
import re
import time

import requests
from layerkit import overpass
# Raised by overpass.load_osm; callers and tests name it as compare's.
from layerkit.overpass import OsmUnavailable  # noqa: F401

from src import config, history

STATUSES = ("missing", "noref", "far", "duplicate", "unnamed", "mismatch",
            "ondemand", "osm_only")


def compare():
    """Build data/gaps.geojson + summary from City vs OSM. Returns the summary."""
    data, osm_date, from_cache = _load_osm()
    osm = _osm_stops(data)
    print(f"OSM bus stops:       {len(osm):,}")
    city = list(_city_stops())
    print(f"City stops:          {len(city):,}")

    gaps, counts = _match(city, osm)
    _write_gaps(gaps)

    summary = {"osm_stops": len(osm), "city": len(city),
               "osm_date": osm_date, "osm_stale": from_cache, **counts}
    with open(config.GAPS_COUNT_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print("Gaps: " + ", ".join(f"{counts[s]:,} {s}" for s in STATUSES))
    if from_cache:
        print(f"NOTE: compared against cached OSM data from {osm_date}, "
              f"not a live Overpass fetch.")
    history.record(summary)
    return summary


# --- OSM source -------------------------------------------------------------

def _overpass_query(at=None):
    """The Overpass QL for bus stops; ``at`` (ISO instant) asks for OSM as it
    stood then -- an attic query, used by the history backfill.

    Both tagging schemes are asked for: the legacy highway=bus_stop node, and
    a PTv2 platform tagged bus=yes without it (sometimes a way, which is why
    "out center" -- it gives every element a single lat/lon). Stop positions
    on the carriageway are not stops a rider waits at and are left out.
    """
    s, w, n, e = config.GUELPH_BBOX
    bbox = f"{s},{w},{n},{e}"
    settings = "[out:json][timeout:180]"
    if at:
        settings += f'[date:"{at}"]'
    return (
        settings + ";\n(\n"
        f'  nwr["highway"="bus_stop"]({bbox});\n'
        f'  nwr["public_transport"="platform"]["bus"="yes"]({bbox});\n'
        ");\nout center tags;"
    )


def _load_osm():
    """Return (data, osm_date, from_cache) for OSM bus stops.

    The fetch -- every mirror, the list retried, then the cache -- is
    layerkit.overpass.load_osm; the reasons for each rule are written there.
    """
    return overpass.load_osm(
        _overpass_query(), what="bus stops",
        urls=config.OVERPASS_URLS, rounds=config.OVERPASS_ROUNDS,
        round_wait=config.OVERPASS_ROUND_WAIT,
        cache_path=config.OSM_CACHE_PATH, fetch_path=config.OSM_FETCH_PATH,
        try_one=_try_overpass, undated=None)


def _try_overpass(url, query):
    """One attempt at one mirror; None if it failed or answered implausibly."""
    return overpass.try_overpass(
        url, query, user_agent=config.USER_AGENT,
        timeout=config.OVERPASS_TIMEOUT,
        min_elements=config.OSM_MIN_ELEMENTS,
        max_age_days=config.OSM_MAX_AGE_DAYS,
        # This layer still accepts a reply that carries no timestamp_osm_base,
        # as it always has. The sidewalks, bike, gardens and water layers
        # refuse one; dropping this line makes this layer do the same.
        allow_undated=True)


def _load_cache():
    """(data, osm_date) for the cached Overpass reply, or None."""
    return overpass.load_cache(config.OSM_CACHE_PATH, config.OSM_FETCH_PATH)


def _osm_base(data):
    """How current the OSM half of the diff is: the reply's own
    osm3s.timestamp_osm_base, trimmed to the minute for display."""
    return overpass.osm_base(data, undated=None)


def _osm_base_time(data):
    """The same timestamp as an aware datetime, or None if absent or garbled."""
    return overpass.osm_base_time(data)


def _cache_date():
    """How current the cached reply is: the sidecar, else the file's mtime."""
    return overpass.cache_date(config.OSM_CACHE_PATH, config.OSM_FETCH_PATH)


def _save_fetch(fetched, url, osm_base, data):
    overpass.save_fetch(config.OSM_FETCH_PATH, fetched, url, osm_base, data)



def _osm_stops(data):
    """Flatten the reply into stops: position, ref(s), name, whose."""
    stops = []
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        if "lat" in el and "lon" in el:
            lat, lon = el["lat"], el["lon"]
        elif el.get("center"):
            lat, lon = el["center"]["lat"], el["center"]["lon"]
        else:
            continue
        stops.append({
            "lat": lat,
            "lon": lon,
            "refs": _refs(tags.get("ref")),
            "raw_ref": tags.get("ref"),
            "name": tags.get("name"),
            "otype": el.get("type"),
            "oid": el.get("id"),
            "agency": _agency(tags),
        })
    return stops


def _refs(value):
    """The integer stop IDs a ref tag carries ("109", "109;5810"), or ()."""
    out = []
    for part in (value or "").split(";"):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return tuple(out)


def _agency(tags):
    """"ours", "other" (another agency's stop), or "unknown" (untagged)."""
    named = " ".join(tags.get(k, "") for k in ("network", "operator")).lower()
    if not named.strip():
        return "unknown"
    return "ours" if config.NETWORK.lower() in named else "other"


# --- City source ------------------------------------------------------------

def _city_stops():
    with open(config.SLIM_PATH, encoding="utf-8") as f:
        for line in f:
            feat = json.loads(line)
            props = feat.get("properties") or {}
            lon, lat = feat["geometry"]["coordinates"][:2]
            yield {
                "ref": props["ref"],
                "name": props.get("name"),
                "fixed_route": props.get("fixed_route") != "no",
                "on_demand": props.get("on_demand"),
                "lat": lat,
                "lon": lon,
            }


# --- Matching ---------------------------------------------------------------

def _match(city, osm):
    """One gap row per City stop that needs work, plus OSM-only stops."""
    gaps = []
    counts = {s: 0 for s in STATUSES}
    # Another agency's stop is never a candidate for anything: GO Transit's
    # platforms at Guelph Central sit metres from Guelph Transit's.
    ours = [o for o in osm if o["agency"] != "other"]
    by_ref = {}
    for o in ours:
        for r in o["refs"]:
            by_ref.setdefault(r, []).append(o)
    claimed = _claim_unreferenced(city, [o for o in ours if not o["refs"]])

    for c in city:
        cands = by_ref.get(c["ref"], [])
        extra = {}
        if cands:
            match = min(cands, key=lambda o: _dist(c, o))
            far_twin = [o for o in cands if _dist(o, match) > config.FAR_M]
            if far_twin:
                status = "duplicate"
                extra["other_url"] = _osm_url(far_twin[0])
            elif _dist(c, match) > config.FAR_M:
                status = "far"
            elif not match["name"]:
                status = "unnamed"
            elif _norm(match["name"]) != _norm(c["name"]):
                status = "mismatch"
            else:
                continue  # an OSM stop with this ref and name is here
        elif c["ref"] in claimed:
            match = claimed[c["ref"]]
            status = "noref"
        else:
            match = None
            status = "missing" if c["fixed_route"] else "ondemand"
        gaps.append(_gap_feature(c, status, match, extra))
        counts[status] += 1

    # The other direction: a stop OSM says is Guelph Transit's, whose number
    # the City no longer lists. Only explicitly-tagged stops, so a GO or
    # untagged stop's own numbering is never read as one of these.
    city_refs = {c["ref"] for c in city}
    for o in ours:
        if o["agency"] == "ours" and o["refs"] and \
                not any(r in city_refs for r in o["refs"]):
            gaps.append(_osm_only_feature(o))
            counts["osm_only"] += 1
    return gaps, counts


def _claim_unreferenced(city, unreferenced):
    """{City ref: OSM stop} for OSM stops with no ref that are evidently it.

    An unreferenced OSM stop is given to the City stop nearest it, and only if
    that is within NEAR_M. Nearest-first matters: a quarter of the City's stops
    have another stop -- usually the opposite direction -- within 30 m, and a
    City-side "anything within NEAR_M" would give one OSM node to both. When
    two OSM stops pick the same City stop, the nearer one keeps it.
    """
    claimed = {}
    best = {}
    for o in unreferenced:
        c = min(city, key=lambda c: _dist(c, o), default=None)
        if c is None:
            continue
        d = _dist(c, o)
        if d <= config.NEAR_M and d < best.get(c["ref"], math.inf):
            claimed[c["ref"]] = o
            best[c["ref"]] = d
    return claimed


def _osm_url(o):
    return f"https://www.openstreetmap.org/{o['otype']}/{o['oid']}"


def _gap_feature(c, status, match, extra):
    props = {
        "ref": c["ref"],
        "name": c["name"],
        "status": status,
    }
    if c["on_demand"]:
        props["on_demand"] = c["on_demand"]
    if match:
        props["osm_name"] = match["name"]
        props["osm_ref"] = match["raw_ref"]
        props["osm_url"] = _osm_url(match)
        props["osm_lat"] = round(match["lat"], 6)
        props["osm_lon"] = round(match["lon"], 6)
        props["distance_m"] = round(_dist(c, match))
    props.update(extra)
    return {
        "type": "Feature",
        "properties": props,
        "geometry": {"type": "Point",
                     "coordinates": [round(c["lon"], 6), round(c["lat"], 6)]},
    }


def _osm_only_feature(o):
    return {
        "type": "Feature",
        "properties": {
            "ref": o["raw_ref"],
            "name": None,
            "status": "osm_only",
            "osm_name": o["name"],
            "osm_ref": o["raw_ref"],
            "osm_url": _osm_url(o),
        },
        "geometry": {"type": "Point",
                     "coordinates": [round(o["lon"], 6), round(o["lat"], 6)]},
    }


def _write_gaps(gaps):
    with open(config.GAPS_GEOJSON_PATH, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": gaps}, f)


# --- Geometry and names -----------------------------------------------------

def _dist(a, b):
    """Metres between two {lat, lon}; equirectangular, fine at city scale."""
    lat0 = math.radians((a["lat"] + b["lat"]) / 2)
    dx = math.radians(b["lon"] - a["lon"]) * math.cos(lat0)
    dy = math.radians(b["lat"] - a["lat"])
    return 6371008.8 * math.hypot(dx, dy)


def _norm(name):
    """Loose match key: case, punctuation, "&"/"and" and spacing only.

    Deliberately no more. Guelph Transit's names are formulaic ("Edinburgh at
    Ginger northbound") and OSM's, where present, were mostly copied from
    them; an OSM name that drops the direction or spells out "Street" is a
    real difference the mapper should look at, not noise to normalise away.
    Affects matching only; the displayed name keeps the raw data.
    """
    if not name:
        return ""
    s = name.lower().replace("&", " and ").replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()
