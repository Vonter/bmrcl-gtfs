"""BMRCL GTFS pipeline: schedule parsing, feed assembly and variant builds."""
import io
import json
import logging
import os
import subprocess
import zipfile
from datetime import datetime, timedelta

import pandas as pd

import helpers
from helpers import *
from gtfs import *

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("debug.log"), logging.StreamHandler()],
)

GTFSTIDY = "tools/gtfstidy"
GTFSTIDY_FLAGS = "-SCRmcsOeDWI"

# Reference epoch for clock-time arithmetic: ``datetime.strptime(t, '%H:%M')``
# yields a 1900-01-01 datetime, so seconds-since are measured from this anchor.
_TIME_EPOCH = datetime(1900, 1, 1)

# Schedule JSON filename prefix -> GTFS service_id. Saturday files feed the
# 'holiday' service; a dedicated 'holiday' prefix maps to it too.
PREFIX_TO_SERVICE_ID = {
    "weekday": "weekday", "monday": "monday", "sunday": "sunday",
    "saturday": "holiday", "holiday": "holiday",
}

# Minimum separation (seconds) between two same-direction trains sharing track on
# one line/service. Two trains closer than this are physically implausible, so the
# de-bunch pass drops the surplus one (favouring explicit operator trips over
# synthesised frequency trains). Kept below the tightest published headway
# (Purple KGWA→WHTM at 3.3 min = 198 s) so genuine high-frequency service stands.
MIN_HEADWAY_SECONDS = 60

# Tables gtfstidy doesn't model and would drop; kept out of the tidy input and
# re-injected into the final zip afterwards.
POST_TIDY_FILES = [
    "translations.txt", "calendar.txt", "calendar_dates.txt", "fare_media.txt",
    "fare_products.txt", "fare_leg_rules.txt", "timeframes.txt", "areas.txt", "stop_areas.txt",
]
FARES_V2_FILES = {
    "fare_media.txt", "fare_products.txt", "fare_leg_rules.txt", "timeframes.txt",
    "areas.txt", "stop_areas.txt",
}


# ---------------------------------------------------------------------------
# Schedule parsing
# ---------------------------------------------------------------------------

def _spans_overlap(a, b):
    """True if two trains share at least one station (their stop-index spans meet)."""
    return max(a['lo'], b['lo']) <= min(a['hi'], b['hi'])


def _train_phase(entry, cumulative):
    """Per-train scalar whose difference equals two trains' separation anywhere they meet.

    A train's arrival at stop index *X* is ``start ± (cumulative[X] - cumulative[origin])``,
    so for two same-direction trains the ``cumulative[X]`` term cancels: their gap is the
    same at every shared stop. Projecting that gap onto a single value lets the de-bunch
    compare trains without picking a station.
    """
    start_sec = hms_to_seconds(entry['start_time'])
    if entry['direction'] == 1:
        return start_sec - cumulative[entry['lo']]
    return start_sec + cumulative[entry['hi']]


def _debunch_schedule(schedule_data):
    """Drop trains running too close behind another on the same line/service/direction.

    Within each ``(route, service, direction)`` group, explicit operator trips are
    retained unconditionally; synthesised frequency trains are then taken in time
    order and a train is dropped when it falls within ``MIN_HEADWAY_SECONDS`` of an
    already-retained train it shares track with. So when an explicit trip and a
    frequency train collide the explicit one wins, and surplus frequency trains are
    removed rather than left bunched.
    """
    groups = {}
    for entry in schedule_data:
        groups.setdefault((entry['route'], entry['service_id'], entry['direction']), []).append(entry)

    kept = []
    dropped = 0
    for (route, _service, _direction), entries in groups.items():
        cumulative = _route_cumulative_seconds(route)
        for entry in entries:
            entry['_phase'] = _train_phase(entry, cumulative)

        retained = [e for e in entries if e['explicit']]
        for entry in sorted((e for e in entries if not e['explicit']), key=lambda e: e['_phase']):
            if any(_spans_overlap(entry, r) and abs(entry['_phase'] - r['_phase']) < MIN_HEADWAY_SECONDS
                   for r in retained):
                dropped += 1
                continue
            retained.append(entry)
        kept.extend(retained)

    for entry in kept:
        entry.pop('_phase', None)
    if dropped:
        logging.info(f"De-bunching dropped {dropped} surplus frequency trains (< {MIN_HEADWAY_SECONDS}s headway)")
    return kept


def _route_index_lookup(route_id):
    """(ordered_stop_ids, index_map) where index_map maps stop_id -> route position."""
    route_order = get_route_stops(route_id)
    index_map = {}
    for i, stop_id in enumerate(route_order):
        index_map[stop_id] = i
        index_map[stop_id.upper()] = i
    return route_order, index_map


def _resolve_index(index_map, stop_id):
    """Resolve a stop_id to its route position, tolerating case differences."""
    if stop_id in index_map:
        return index_map[stop_id]
    return index_map.get(stop_id.upper())


def parse_schedule():
    """Parse the ``schedule/`` JSON files into per-train records.

    ``"frequency"`` rows give nested combined section headways: each section adds
    only the residual trains beyond what the larger sections through it supply
    (``from`` = measuring point, ``toward`` = direction). ``"trips"`` rows are
    explicit turn-back services with exact ``times`` or headway ``bands``.

    Per file: (1) full-line frequency rows, (2) explicit trips, (3) nested
    frequency rows sparsest-first, each residual-filling to its combined headway.
    """
    directory_path = "./schedule"
    if not os.path.exists(directory_path):
        logging.error(f"Schedule directory not found: {directory_path}")
        return []

    schedule_data = []
    for filename in sorted(os.listdir(directory_path)):
        if not filename.endswith('.json'):
            continue

        try:
            with open(os.path.join(directory_path, filename)) as f:
                loaded = json.load(f)

            prefix = filename.split("-")[0]
            route = filename.split("-")[1].replace(".json", "")
            service_id = PREFIX_TO_SERVICE_ID.get(prefix)
            if not service_id:
                logging.warning(f"Unknown service prefix '{prefix}' in '{filename}', skipping.")
                continue
            if not isinstance(loaded, dict):
                logging.warning(f"Schedule file '{filename}' is not an object, skipping.")
                continue

            route_order, index_map = _route_index_lookup(route)
            if not route_order:
                logging.warning(f"No route order for route '{route}' ({filename}), skipping.")
                continue
            full_span = len(route_order) - 1

            # Trains already materialised for this file, used to count how many
            # pass a section's measuring point before residual fill.
            placed = []
            # Guards against emitting the same physical train twice — chiefly the
            # boundary instant two adjacent (inclusive) headway bands both produce.
            seen_trains = set()

            def add_train(origin, destination, start_dt, explicit=False):
                o_idx = _resolve_index(index_map, origin)
                d_idx = _resolve_index(index_map, destination)
                if o_idx is None or d_idx is None:
                    logging.warning(f"{filename}: cannot place {origin}->{destination}, unknown stop.")
                    return
                key = (origin, destination, start_dt)
                if key in seen_trains:
                    return
                seen_trains.add(key)
                lo, hi = sorted((o_idx, d_idx))
                direction = 1 if o_idx < d_idx else 0
                placed.append({'lo': lo, 'hi': hi, 'direction': direction, 'start_dt': start_dt})
                schedule_data.append({
                    'file': filename, 'route': route, 'service_id': service_id,
                    'origin': origin, 'destination': destination,
                    'start_time': start_dt.strftime('%H:%M:%S'),
                    'direction': direction, 'lo': lo, 'hi': hi, 'explicit': explicit,
                })

            def expand_band(origin, destination, band, explicit=False):
                try:
                    headway = float(band['headway'])
                    start_dt = datetime.strptime(band['start'], '%H:%M')
                    end_dt = datetime.strptime(band['end'], '%H:%M')
                except (KeyError, TypeError, ValueError):
                    logging.warning(f"{filename}: invalid band {band} for {origin}->{destination}, skipping.")
                    return
                if headway <= 0:
                    return
                current = start_dt
                while current <= end_dt:
                    add_train(origin, destination, current, explicit=explicit)
                    current += timedelta(minutes=headway)

            # Collect frequency rows, splitting full-line from nested.
            full_line_bands = []   # (from, toward, band)
            nested_bands = []      # (from, toward, band, span, headway)
            for row in loaded.get("frequency", []):
                origin, toward = row.get('from'), row.get('toward')
                o_idx = _resolve_index(index_map, origin) if origin else None
                t_idx = _resolve_index(index_map, toward) if toward else None
                if o_idx is None or t_idx is None:
                    logging.warning(f"{filename}: unknown frequency section {origin}->{toward}, skipping.")
                    continue
                span = abs(o_idx - t_idx)
                for band in row.get('bands', []):
                    if span >= full_span:
                        full_line_bands.append((origin, toward, band))
                    else:
                        try:
                            headway = float(band['headway'])
                        except (KeyError, TypeError, ValueError):
                            logging.warning(f"{filename}: invalid headway in {band}, skipping.")
                            continue
                        nested_bands.append((origin, toward, band, span, headway))

            # 1. Full-line frequency rows.
            for origin, toward, band in full_line_bands:
                expand_band(origin, toward, band)

            # 2. Explicit physical turn-back trips.
            for row in loaded.get("trips", []):
                origin, destination = row.get('from'), row.get('to')
                if origin is None or destination is None:
                    logging.warning(f"{filename}: trip row missing from/to: {row}, skipping.")
                    continue
                for time_str in row.get('times', []):
                    try:
                        add_train(origin, destination, datetime.strptime(time_str, '%H:%M'), explicit=True)
                    except ValueError:
                        logging.warning(f"{filename}: invalid trip time '{time_str}' for {origin}->{destination}.")
                for band in row.get('bands', []):
                    expand_band(origin, destination, band, explicit=True)

            # 3. Nested frequency rows: residual fill, sparsest section first.
            # Processing largest headway first (span as tiebreak) guarantees an
            # enclosing section is placed before the sections nested inside it add
            # only their residual trains.
            nested_bands.sort(key=lambda x: (-x[4], -x[3]))
            for origin, toward, band, _span, headway in nested_bands:
                try:
                    start_dt = datetime.strptime(band['start'], '%H:%M')
                    end_dt = datetime.strptime(band['end'], '%H:%M')
                except (KeyError, ValueError):
                    logging.warning(f"{filename}: invalid band {band} for {origin}->{toward}, skipping.")
                    continue
                if headway <= 0 or end_dt < start_dt:
                    continue

                headway_sec = headway * 60.0
                measuring_idx = _resolve_index(index_map, origin)
                direction = 1 if _resolve_index(index_map, toward) > measuring_idx else 0

                # Times at which trains already placed pass this measuring point
                # (in this direction, within the window), derived from each train's
                # start and the cumulative run time from its origin to here. These
                # through trains are fixed anchors the short-turns interleave between.
                cumulative = _route_cumulative_seconds(route)
                window_start = (start_dt - _TIME_EPOCH).total_seconds()
                window_end = (end_dt - _TIME_EPOCH).total_seconds()
                existing = []
                start_gap = None  # closest any placed train passes here to the band start
                for p in placed:
                    if p['direction'] != direction or not (p['lo'] <= measuring_idx <= p['hi']):
                        continue
                    origin_idx = p['lo'] if direction == 1 else p['hi']
                    pass_sec = ((p['start_dt'] - _TIME_EPOCH).total_seconds()
                                + abs(cumulative[measuring_idx] - cumulative[origin_idx]))
                    gap = abs(pass_sec - window_start)
                    if start_gap is None or gap < start_gap:
                        start_gap = gap
                    if window_start <= pass_sec <= window_end:
                        existing.append(pass_sec)

                # The band start is the published first short-turn departure from the
                # measuring point (the section origin), so pin a real short-loop train
                # to that instant, marking it explicit so the de-bunch keeps it over the
                # long loop.
                #
                # But only when the band start isn't already served: if a through train
                # passes within half a headway, it already covers that slot, and pinning
                # would sit a short-turn right behind it (a sub-headway gap the de-bunch
                # would not even catch). There the through train is the band-start train,
                # so leave it be.
                if start_gap is None or start_gap >= headway_sec / 2:
                    add_train(origin, toward, start_dt, explicit=True)

                # Lay the section down as its own even cadence: subdivide each interval
                # between consecutive fixed trains (through passes plus the window edges)
                # into as-equal-as-possible pieces of about one headway, rounding
                # gap/headway to pick the per-gap count. This keeps the spacing uniform
                # however the through headway divides into the section headway, instead
                # of forcing a window-wide train count whose remainder bunches: an 11-min
                # through gap carrying a 5-min section becomes two 5.5-min pieces, not the
                # 3:40 cluster a global residual count produced. The de-bunch remains a
                # safety net for the rare pin/through near-collision.
                anchors = sorted([window_start, window_end, *existing])
                for a, b in zip(anchors, anchors[1:]):
                    pieces = int(round((b - a) / headway_sec))
                    for pass_sec in _distribute_in_gaps([a, b], pieces - 1):
                        add_train(origin, toward, _TIME_EPOCH + timedelta(seconds=pass_sec))

        except Exception as e:
            logging.error(f"Failed to parse {filename}: {e}")

    return _debunch_schedule(schedule_data)


# ---------------------------------------------------------------------------
# Output: intermediate files & zips
# ---------------------------------------------------------------------------

def write_data(tables):
    """Write GTFS intermediate text files. Empty levels/pathways are skipped."""
    for name, df in tables.items():
        if name in ('levels', 'pathways') and df.empty:
            continue
        df.to_csv(f"intermediate/{name}.txt", index=False)


def _run_gtfstidy(flags, src, out):
    subprocess.run([GTFSTIDY, flags, src, "-o", out], check=False)


def _reinject_post_tidy(zip_path, frequencies_csv=None):
    """Drop gtfstidy's copies of the tables we control and re-add our own.

    Optionally replaces frequencies.txt with corrected ``frequencies_csv`` text.
    """
    for name in POST_TIDY_FILES:
        subprocess.run(["zip", "-d", zip_path, name], check=False)
    if frequencies_csv is not None:
        subprocess.run(["zip", "-d", zip_path, "frequencies.txt"], check=False)

    with zipfile.ZipFile(zip_path, "a") as zip_ref:
        for name in POST_TIDY_FILES:
            source = os.path.join("intermediate", name)
            if os.path.exists(source):
                zip_ref.write(source, arcname=name)
        if frequencies_csv is not None:
            zip_ref.writestr("frequencies.txt", frequencies_csv)


def build_zip():
    """Create the final GTFS zip from intermediate files via gtfstidy."""
    # Exclude Fares v2 tables gtfstidy can't understand; re-injected afterwards.
    intermediate_zip = "gtfs/intermediate_bmrcl.zip"
    with zipfile.ZipFile(intermediate_zip, "w") as zipf:
        for folder, _, files in os.walk("intermediate"):
            for file in files:
                if file in FARES_V2_FILES:
                    continue
                file_path = os.path.join(folder, file)
                zipf.write(file_path, arcname=os.path.relpath(file_path, "intermediate"))

    _run_gtfstidy(GTFSTIDY_FLAGS, intermediate_zip, "gtfs/bmrcl.zip")
    _reinject_post_tidy("gtfs/bmrcl.zip")


def build_frequency_feed():
    """Build the companion frequency-based feed (``gtfs/bmrcl-frequencies.zip``).

    Same service as the primary feed, expressed compactly: gtfstidy's ``-T`` flag
    folds the regular headways in the explicit trips into ``frequencies.txt``.
    Requires ``gtfs/intermediate_bmrcl.zip`` (written by :func:`build_zip`).
    """
    intermediate_zip = "gtfs/intermediate_bmrcl.zip"
    output_zip = "gtfs/bmrcl-frequencies.zip"
    if not os.path.exists(intermediate_zip):
        logging.warning("Intermediate feed missing; skipping frequency feed build.")
        return

    _run_gtfstidy(GTFSTIDY_FLAGS + "T", intermediate_zip, output_zip)
    # gtfstidy can emit windows that overlap at headway transitions; trim them.
    fixed_frequencies = _fix_overlapping_frequencies(output_zip)
    _reinject_post_tidy(output_zip, frequencies_csv=fixed_frequencies)
    logging.info(f"Wrote companion frequency-based feed to {output_zip}")


def _fix_overlapping_frequencies(zip_path):
    """Return a corrected frequencies.txt (CSV text) with disjoint windows, or None."""
    with zipfile.ZipFile(zip_path, "r") as zin:
        if "frequencies.txt" not in zin.namelist():
            return None
        frequencies = pd.read_csv(io.BytesIO(zin.read("frequencies.txt")))

    if frequencies.empty:
        return frequencies.to_csv(index=False)

    frequencies["_start"] = frequencies["start_time"].map(hms_to_seconds)
    frequencies["_end"] = frequencies["end_time"].map(hms_to_seconds)
    frequencies = frequencies.sort_values(["trip_id", "_start"]).reset_index(drop=True)

    kept_rows = []
    for _, group in frequencies.groupby("trip_id", sort=False):
        rows = group.to_dict("records")
        for i in range(len(rows) - 1):
            if rows[i]["_end"] > rows[i + 1]["_start"]:
                rows[i]["_end"] = rows[i + 1]["_start"]
                rows[i]["end_time"] = rows[i + 1]["start_time"]
        kept_rows.extend(row for row in rows if row["_end"] > row["_start"])

    fixed = pd.DataFrame(kept_rows).drop(columns=["_start", "_end"])
    return fixed.to_csv(index=False)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def main():
    """End-to-end GTFS generation pipeline."""
    agency = add_agency()
    feed_info = add_feed_info()
    calendar = add_calendar()
    # Publish the stops table to helpers so its cached coordinate/distance lookups
    # resolve against built platforms rather than the geojson fallback.
    stops = helpers.stops = add_stops(parse_geojson())
    schedule_data = parse_schedule()
    routes = add_routes()
    trips = add_trips(schedule_data)
    stop_times = add_stop_times(schedule_data)
    shapes = add_shapes(schedule_data)
    calendar_dates = add_calendar_dates(feed_info)
    translations = add_translations(stops, routes, trips)

    levels = add_levels(stops)
    pathways = add_pathways(stops)
    attributions = add_attributions()

    fares_data = calculate_fares_from_network()
    fare_attributes = add_fare_attributes(fares_data)
    fare_rules = add_fare_rules(fares_data)
    fare_media = add_fare_media()
    fare_products = add_fare_products(fares_data)
    fare_leg_rules = add_fare_leg_rules(fares_data)
    timeframes = add_timeframes()
    areas = add_areas(fares_data)
    stop_areas = add_stop_areas(fares_data, stops)
    transfers = add_transfers()

    logging.info("Writing intermediate GTFS files to disk...")
    write_data({
        'agency': agency, 'feed_info': feed_info, 'calendar': calendar, 'stops': stops,
        'routes': routes, 'trips': trips, 'stop_times': stop_times, 'shapes': shapes,
        'calendar_dates': calendar_dates, 'translations': translations,
        'fare_attributes': fare_attributes, 'fare_rules': fare_rules, 'transfers': transfers,
        'attributions': attributions, 'levels': levels, 'pathways': pathways,
        'fare_media': fare_media, 'fare_products': fare_products, 'fare_leg_rules': fare_leg_rules,
        'timeframes': timeframes, 'areas': areas, 'stop_areas': stop_areas,
    })

    build_zip()

    try:
        logging.info("Building companion frequency-based feed...")
        build_frequency_feed()
    except Exception as e:
        logging.error(f"Failed to build companion frequency feed: {e}")


if __name__ == "__main__":
    main()
