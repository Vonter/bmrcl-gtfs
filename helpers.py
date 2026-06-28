"""Geometry, geojson and lookup helpers shared by the GTFS table builders."""
import heapq
import logging
import math
from datetime import datetime, timedelta
from functools import lru_cache

import geojson
import pandas as pd
from shapely.geometry import LineString, Point
from shapely.ops import substring

# Set by parse.main() once the stops table exists; read by the cached
# coordinate/distance lookups below (None until then -> geojson fallback).
stops = None

__all__ = [
    "platform_number", "platform_stop_id", "_feed_window", "get_geojson_data",
    "parse_geojson", "get_entrances", "_kannada_names_by_code", "_station_names_by_code",
    "map_stop", "expand_stop", "haversine_distance", "calculate_distance_between_stops",
    "calculate_travel_time", "get_dwell_time_seconds", "_route_cumulative_seconds",
    "_distribute_in_gaps", "get_route_line_string", "get_route_stops", "trip_direction_id",
    "get_stop_sequence_for_trip", "get_platforms", "get_stop_curve_km", "_route_shape_points",
    "hms_to_seconds",
]

# GeoJSON LineString feature names per route (both directions).
ROUTE_FEATURE_NAMES = {
    "PURPLE": [
        "Purple Line (Challaghatta → Whitefield (Kadugodi))",
        "Purple Line (Whitefield (Kadugodi) → Challaghatta)",
    ],
    "GREEN": [
        "Green Line (Madavara -> Silk Institute)",
        "Green Line (Silk Institute -> Madavara)",
    ],
    "YELLOW": [
        "Yellow Line (Rashtreeya Vidyalaya Road → Delta Electronics Bommasandra)",
        "Yellow Line (Delta Electronics Bommasandra → Rashtreeya Vidyalaya Road)",
    ],
}

# Colours / refs used to associate geojson stations to routes via @relations.
ROUTE_COLORS = {"PURPLE": ["#e542de", "#8c2877"], "GREEN": ["#009933"], "YELLOW": ["#ffff00", "#FFD700"]}
ROUTE_REFS = {"PURPLE": ["Purple"], "GREEN": ["Green"], "YELLOW": ["Yellow"]}

# Real-world platform numbers (GTFS ``platform_code``) by route and direction.
# Empirical: GREEN/PURPLE dir0 head "down" the line, dir1 "up"; YELLOW dir1
# heads to Bommasandra, dir0 back to RV Road.
PLATFORM_NUMBERS = {
    'PURPLE': {0: '2', 1: '1'},
    'GREEN': {0: '2', 1: '1'},
    'YELLOW': {0: '1', 1: '2'},
}
# Interchanges number platforms across both lines, so the line that does not
# own platforms 1/2 is renumbered.
PLATFORM_NUMBER_OVERRIDES = {
    'KGWA': {('GREEN', 1): '3', ('GREEN', 0): '4'},  # Majestic: Green is 3/4
    'RVR': {('YELLOW', 1): '3', ('YELLOW', 0): '4'},  # RV Road: Yellow is 3/4
}

# Dwell times (seconds) collapse to three categories, each a 20% trimmed mean of the
# dwell that category holds.
# Dwells are summed when accumulating arrival times along a trip, so the estimator
# must be unbiased for the bulk of the distribution — which the median is not, its
# summed value ran arrivals late — yet robust to each line's skewing tail, which the
# plain mean is not (Green's short holds and Purple's long interchange holds drag it
# off the typical). Ordinary stops share one default; the two interchanges hold
# marginally longer when passed through mid-trip; the trip's turn-back point holds a
# long layover, applied only at the final stop (the train terminates there and is
# never propagated onward, so it is cosmetic) while origins depart at the scheduled
# time with no dwell. See get_dwell_time_seconds for the position rules.
DEFAULT_DWELL_SECONDS = 26
INTERCHANGE_DWELL_SECONDS = 34
TERMINAL_DWELL_SECONDS = 100
# Stations where two lines meet (Majestic, RV Road); they hold marginally longer than
# an ordinary stop when a train passes through them mid-trip.
INTERCHANGE_STATIONS = {"KGWA", "RVR"}

# Inter-station run time from one kinematic train model shared by every line: a train
# accelerates at TRAIN_ACCEL_MPS2, cruises at the line's peak speed, then brakes at
# TRAIN_DECEL_MPS2 — a symmetric trapezoidal speed profile. For a segment long enough
# to reach cruise the closed form is (accel/decel penalty) + distance / peak; the
# affine model this replaces was exactly that cruise solution, and the kinematic form
# additionally handles the few segments too short to reach cruise (a triangular
# profile). Only the combination 1/accel + 1/decel and the peak speed are identifiable
# from run-time-vs-distance, so accel and decel are fit jointly as equal (the model is
# insensitive to splitting them); they are kept as separate constants only so an
# asymmetric pair can be set by hand. Peak speed stays per line: one network-wide value
# cannot match both Purple's curvier, closely spaced alignment and Green's faster one —
# it forces a ~±150s end-to-end bias per line, the same line-correlated residual the
# per-line fit removes. Peak speed is effective (fit on straight-line stop distance,
# which understates the curving track), so it reads below the trains' design top speed.
# YELLOW is unmodelled and uses the network-wide peak (also the default for any
# unmodelled route). Each peak is fit to minimise end-to-end arrival error (the
# accumulated error at the trip's final stop), which keeps the whole-line journey time
# unbiased; a per-stop fit instead left Purple ~67s fast at its terminus.
TRAIN_ACCEL_MPS2 = 0.9
TRAIN_DECEL_MPS2 = 0.9
DEFAULT_PEAK_SPEED_MPS = 12.69  # ~46 km/h effective cruise
PEAK_SPEED_MPS = {
    "PURPLE": 11.92,  # ~43 km/h; closely spaced, curvier alignment
    "GREEN": 13.87,   # ~50 km/h; faster, more widely spaced stops
    "YELLOW": DEFAULT_PEAK_SPEED_MPS,  # unmodelled, network-wide peak
}

# Measured run times (seconds) for the few directed segments whose run time the
# distance-based kinematic model above cannot predict: terminal approaches and
# speed-restricted stretches where the model is off by >=35s. These segments use the
# measured value directly. Keyed by (route_id, from_stop, to_stop) in travel order;
# directional, because an approach restriction applies inbound but not out — UWVL->WHTM
# (the Whitefield terminal approach) runs 207s inbound while the reverse matches the
# model. The peak speeds above are re-fit with these in place, so the kinematic model
# covers only the segments it can predict.
SEGMENT_RUN_TIME_OVERRIDES = {
    ("GREEN", "RVR", "BSNK"): 128,
    ("GREEN", "BSNK", "RVR"): 128,
    ("PURPLE", "UWVL", "WHTM"): 208,
    ("PURPLE", "SVRD", "IDN"): 160,
    ("PURPLE", "IDN", "SVRD"): 160,
    ("PURPLE", "GDCP", "DKIA"): 135,
    ("PURPLE", "DKIA", "GDCP"): 135,
    ("PURPLE", "BYPH", "JTPM"): 130,
    ("PURPLE", "JTPM", "BYPH"): 130,
    ("PURPLE", "MDVP", "KRAM"): 120,
    ("PURPLE", "KRAM", "MDVP"): 120,
}


def platform_number(station_code, route_id, direction_id):
    """GTFS ``platform_code`` for a station's directional platform."""
    override = PLATFORM_NUMBER_OVERRIDES.get(station_code, {})
    if (route_id, direction_id) in override:
        return override[(route_id, direction_id)]
    return PLATFORM_NUMBERS.get(route_id, {}).get(direction_id, '')


def platform_stop_id(station_code, route_id, direction_id):
    """Stop id for a station's directional platform, e.g. ``KGWA_PF1``.

    Disambiguated by the real platform number (unique within a station: 1/2 at
    ordinary stations, 1–4 at interchanges), so the single ``PF`` tag works for
    every line without colliding.
    """
    return f"{station_code}_PF{platform_number(station_code, route_id, direction_id)}"


def _feed_window():
    """(start, end) feed dates as YYYYMMDD: today and one year out."""
    start = datetime.now().strftime('%Y%m%d')
    end = (datetime.now() + timedelta(days=365)).strftime('%Y%m%d')
    return start, end


@lru_cache(maxsize=None)
def get_geojson_data():
    with open("geojson/export.geojson") as f:
        return geojson.load(f)


def _wheelchair_boarding(value, default):
    """Map an OSM ``wheelchair`` tag to a GTFS ``wheelchair_boarding`` value."""
    return {'yes': 1, 'designated': 1, 'limited': 1, 'no': 2}.get((value or '').strip().lower(), default)


def parse_geojson():
    """Collect one base station record per physical station from the geojson."""
    stops_data = []
    for feature in get_geojson_data().features:
        props = feature.get('properties', {})
        if props.get('public_transport') != 'station' or not props.get('code'):
            continue
        stops_data.append({
            'stop_name': props['name'],
            'stop_id': props['code'],
            'stop_lat': feature.geometry.coordinates[1],
            'stop_lon': feature.geometry.coordinates[0],
            'zone_id': props['code'],
            'wheelchair_boarding': _wheelchair_boarding(props.get('wheelchair'), default=1),
        })
    return stops_data


def get_entrances():
    """Subway entrances (``railway=subway_entrance``) grouped by parent station code."""
    station_names = {
        f['properties'].get('code'): f['properties'].get('name')
        for f in get_geojson_data().features
        if f.get('properties', {}).get('public_transport') == 'station'
    }

    entrances_by_station = {}
    used_ids = set()
    for feature in get_geojson_data().features:
        props = feature.get('properties', {})
        if props.get('railway') != 'subway_entrance':
            continue
        station_code = props.get('station_code')
        if not station_code:
            continue

        # Use the OSM entrance ref verbatim after the underscore (e.g. KGWA_D),
        # falling back to a bare ``_E`` when the entrance has no ref.
        ref = (props.get('ref') or '').strip()
        base_id = f"{station_code}_{ref}" if ref else f"{station_code}_E"
        entrance_id, counter = base_id, 1
        while entrance_id in used_ids:
            counter += 1
            entrance_id = f"{base_id}_{counter}"
        used_ids.add(entrance_id)

        # OSM entrance names vary widely, so prefer "<Station> – Entrance <ref>".
        station_name = station_names.get(station_code, station_code)
        name = f"{station_name} – Entrance {ref}" if ref else f"{station_name} – Entrance"
        entrances_by_station.setdefault(station_code, []).append({
            'entrance_id': entrance_id,
            'stop_name': name,
            'stop_lat': feature.geometry.coordinates[1],
            'stop_lon': feature.geometry.coordinates[0],
            'wheelchair_boarding': _wheelchair_boarding(props.get('wheelchair'), default=0),
        })
    return entrances_by_station


def _kannada_names_by_code():
    """Map station code -> Kannada (``name:kn``) name from the geojson."""
    names = {}
    for feature in get_geojson_data().features:
        props = feature.get('properties', {})
        if props.get('code') and props.get('name:kn'):
            names.setdefault(props['code'], props['name:kn'])
    return names


def _station_names_by_code():
    """Map station code -> English (``name``) name from the geojson."""
    names = {}
    for feature in get_geojson_data().features:
        props = feature.get('properties', {})
        if props.get('public_transport') == 'station' and props.get('code') and props.get('name'):
            names.setdefault(props['code'], props['name'])
    return names


# ---------------------------------------------------------------------------
# Stop coordinate / name / distance helpers
# ---------------------------------------------------------------------------

@lru_cache(maxsize=None)
def _map_stop_from_geojson(stop_id):
    """Stop coordinates [lat, lon] from the geojson by station code."""
    for feature in get_geojson_data()['features']:
        geom = feature.get('geometry', {})
        if geom.get('type') == 'Point' and feature.get('properties', {}).get('code') == stop_id:
            lon, lat = geom['coordinates'][:2]
            return [lat, lon]
    logging.warning(f"Stop {stop_id} not found in GeoJSON")
    return [0.0, 0.0]


@lru_cache(maxsize=None)
def map_stop(stop_id):
    """Stop coordinates [lat, lon], from the stops table if built, else the geojson."""
    if isinstance(stops, pd.DataFrame) and not stops.empty and 'stop_id' in stops.columns:
        match = stops[stops['stop_id'] == stop_id]
        if not match.empty:
            return [match['stop_lat'].iloc[0], match['stop_lon'].iloc[0]]
    return _map_stop_from_geojson(stop_id)


def expand_stop(stop_id):
    """Stop name for a stop_id, falling back to the id itself."""
    if isinstance(stops, pd.DataFrame) and {'stop_id', 'stop_name'}.issubset(stops.columns):
        match = stops[stops['stop_id'] == stop_id]
        if not match.empty:
            return match['stop_name'].iloc[0]
    return str(stop_id)


def haversine_distance(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points in kilometres."""
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


@lru_cache(maxsize=None)
def calculate_distance_between_stops(stop1_id, stop2_id):
    """Straight-line (haversine) distance in km between two stops."""
    if not isinstance(stops, pd.DataFrame) or 'stop_id' not in stops.columns:
        return 0
    s1 = stops[stops['stop_id'] == stop1_id]
    s2 = stops[stops['stop_id'] == stop2_id]
    if s1.empty or s2.empty:
        return 0
    return haversine_distance(
        s1['stop_lat'].iloc[0], s1['stop_lon'].iloc[0],
        s2['stop_lat'].iloc[0], s2['stop_lon'].iloc[0],
    )


def calculate_travel_time(distance_km, route_id=None, from_stop=None, to_stop=None):
    """Inter-station travel time in minutes.

    A directed segment with a measured run-time override (terminal approaches and
    speed-restricted stretches the geometry cannot capture) uses that value. Every
    other segment uses the kinematic train model: a symmetric trapezoidal speed
    profile — accelerate at TRAIN_ACCEL_MPS2 to the route's peak speed, cruise, then
    brake at TRAIN_DECEL_MPS2 — with a triangular profile (peaking below cruise) for
    segments too short to reach peak speed.
    """
    override = SEGMENT_RUN_TIME_OVERRIDES.get((route_id, from_stop, to_stop))
    if override is not None:
        return override / 60.0
    if distance_km <= 0:
        return 0
    distance_m = distance_km * 1000.0
    accel, decel = TRAIN_ACCEL_MPS2, TRAIN_DECEL_MPS2
    peak = PEAK_SPEED_MPS.get(route_id, DEFAULT_PEAK_SPEED_MPS)
    inverse_rate_sum = 1.0 / accel + 1.0 / decel
    accel_decel_distance = peak * peak / 2.0 * inverse_rate_sum
    if distance_m >= accel_decel_distance:
        seconds = peak / 2.0 * inverse_rate_sum + distance_m / peak
    else:
        peak_reached = math.sqrt(2.0 * distance_m * accel * decel / (accel + decel))
        seconds = peak_reached * inverse_rate_sum
    return seconds / 60.0


def get_dwell_time_seconds(stop_id, position="middle"):
    """Dwell time in seconds for a stop given its position in the trip.

    ``origin`` stops depart at the scheduled time and so hold no dwell; the
    ``terminal`` final stop holds a long turn-back layover (cosmetic, never
    propagated onward); interchanges hold marginally longer than the default
    ordinary stop when passed through mid-trip.
    """
    if position == "origin":
        return 0
    if position == "terminal":
        return TERMINAL_DWELL_SECONDS
    if stop_id in INTERCHANGE_STATIONS:
        return INTERCHANGE_DWELL_SECONDS
    return DEFAULT_DWELL_SECONDS


@lru_cache(maxsize=None)
def _route_cumulative_seconds(route_id):
    """Seconds from the route's first stop to each stop, along its ordered stops.

    Mirrors the dwell + run-time propagation in :func:`add_stop_times`, so the
    value at index *j* is when a train that started at index 0 reaches index *j*.
    Used to phase residual short-turn trains against the trains already running.
    """
    route_stops = get_route_stops(route_id)
    cumulative = [0.0]
    total = 0.0
    for i in range(len(route_stops) - 1):
        distance_km = calculate_distance_between_stops(route_stops[i], route_stops[i + 1])
        position = "origin" if i == 0 else "middle"
        total += (get_dwell_time_seconds(route_stops[i], position)
                  + calculate_travel_time(distance_km, route_id,
                                          route_stops[i], route_stops[i + 1]) * 60.0)
        cumulative.append(total)
    return cumulative


def _distribute_in_gaps(anchors, count):
    """Place ``count`` new points among sorted ``anchors`` to maximise spacing.

    Each new point splits the currently widest gap, so the combined set is as
    evenly spaced as possible. Returned points never coincide with an anchor.
    """
    segments = [[a, b, 0] for a, b in zip(anchors, anchors[1:]) if b > a]
    if not segments:
        return []
    heap = [(-(b - a), idx) for idx, (a, b, _k) in enumerate(segments)]
    heapq.heapify(heap)
    for _ in range(count):
        _spacing, idx = heapq.heappop(heap)
        segments[idx][2] += 1
        a, b, k = segments[idx]
        heapq.heappush(heap, (-(b - a) / (k + 1), idx))
    points = []
    for a, b, k in segments:
        for j in range(1, k + 1):
            points.append(a + (b - a) * j / (k + 1))
    return sorted(points)


# ---------------------------------------------------------------------------
# Route geometry & ordering
# ---------------------------------------------------------------------------

@lru_cache(maxsize=None)
def get_route_line_string(route_id):
    """LineString for a route from the geojson (lat, lon ordered)."""
    feature_names = ROUTE_FEATURE_NAMES.get(route_id)
    if not feature_names:
        return None
    features = get_geojson_data()['features']
    for name in feature_names:
        for feature in features:
            if feature['properties'].get('name') == name:
                geometry = geojson.utils.map_tuples(lambda c: (c[1], c[0]), feature['geometry'])
                return LineString(geometry['coordinates'])
    return None


@lru_cache(maxsize=None)
def get_route_stops(route_id):
    """Station codes for a route, ordered along its LineString.

    Stations are matched via the geojson ``@relations`` colour/ref tags, falling
    back to proximity against the route line if none match.
    """
    line = get_route_line_string(route_id)
    if line is None:
        logging.warning(f"Route LineString not found for route {route_id}")
        return []

    target_colors = ROUTE_COLORS.get(route_id, [])
    target_refs = ROUTE_REFS.get(route_id, [])
    found = []  # (stop_id, distance_along_line)

    for feature in get_geojson_data()['features']:
        props = feature.get('properties', {})
        if feature['geometry']['type'] != 'Point' or props.get('public_transport') != 'station':
            continue
        matched = any(
            rel.get('reltags', {}).get('colour') in target_colors
            or rel.get('reltags', {}).get('ref') in target_refs
            for rel in props.get('@relations', [])
        )
        code = props.get('code')
        if not matched or not code:
            continue
        lon, lat = feature['geometry']['coordinates'][:2]
        point = Point(lat, lon)
        found.append((code, line.project(point)))

    if not found and isinstance(stops, pd.DataFrame) and not stops.empty:
        logging.info(f"No stops via relations for {route_id}; trying proximity matching")
        for _, row in stops.iterrows():
            point = Point(row['stop_lat'], row['stop_lon'])
            if line.distance(point) <= 0.005:  # ~500m
                found.append((row['stop_id'], line.project(point)))

    found.sort(key=lambda x: x[1])
    stop_ids = [code for code, _ in found]
    if stop_ids:
        logging.info(f"Found {len(stop_ids)} stops for route {route_id}: {stop_ids[:5]}...")
    else:
        logging.warning(f"No stops found for route {route_id}")
    return stop_ids


def _index_ci(items, value):
    """Index of ``value`` in ``items``, tolerating case differences (None if absent)."""
    try:
        return items.index(value)
    except ValueError:
        upper = value.upper()
        for i, item in enumerate(items):
            if item.upper() == upper:
                return i
    return None


def trip_direction_id(route_id, origin, destination):
    """GTFS ``direction_id``: 1 if the trip runs in route-stop order, else 0.

    Centralises the convention so trips, stop_times and platforms agree.
    """
    all_stops = get_route_stops(route_id)
    origin_idx = _index_ci(all_stops, origin)
    dest_idx = _index_ci(all_stops, destination)
    if origin_idx is not None and dest_idx is not None:
        return 1 if origin_idx < dest_idx else 0

    fallback = {
        'GREEN': (['BIET'], ['APTS']),
        'PURPLE': (['WHTM'], ['CLGA']),
        'YELLOW': (['RVR'], ['BMSD']),
    }
    forward_origins, forward_destinations = fallback.get(route_id, ([], []))
    return 1 if origin in forward_origins or destination in forward_destinations else 0


def get_stop_sequence_for_trip(route_id, origin, destination):
    """Ordered stop ids for a trip, including all intermediate stops."""
    all_stops = get_route_stops(route_id)
    if not all_stops:
        return [origin, destination]

    origin_idx = _index_ci(all_stops, origin)
    dest_idx = _index_ci(all_stops, destination)
    if origin_idx is None or dest_idx is None:
        logging.warning(f"Cannot build stop sequence for {route_id}: {origin}->{destination}")
        return [origin, destination]

    if origin_idx <= dest_idx:
        return all_stops[origin_idx:dest_idx + 1]
    return all_stops[dest_idx:origin_idx + 1][::-1]


@lru_cache(maxsize=None)
def get_platforms():
    """Map ``(station_code, route_id, direction_id)`` to a platform ``(lat, lon)``.

    Consumes the per-direction platform points from ``osm.py`` and assigns each a
    ``direction_id`` by projecting the source relation's terminals onto the route
    line — the same ordering :func:`trip_direction_id` uses.
    """
    result = {}
    for feature in get_geojson_data()['features']:
        props = feature.get('properties', {})
        ref = props.get('platform')
        geometry = feature.get('geometry') or {}
        if not ref or geometry.get('type') != 'Point':
            continue

        route_id = ref.upper()
        code = props.get('code')
        line = get_route_line_string(route_id)
        if code is None or line is None:
            continue

        from_pos = line.project(Point(map_stop(props.get('from'))))
        to_pos = line.project(Point(map_stop(props.get('to'))))
        direction_id = 1 if from_pos < to_pos else 0
        lon, lat = geometry['coordinates']
        result.setdefault((code, route_id, direction_id), (lat, lon))
    return result


def _line_length_km(line):
    """Length of a (lat, lon) LineString in kilometres via haversine."""
    coords = list(line.coords)
    return sum(
        haversine_distance(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
        for i in range(len(coords) - 1)
    )


@lru_cache(maxsize=None)
def get_stop_curve_km(route_id, stop_id):
    """Curve distance (km) from the route line's start to a stop's projection.

    Both shapes and stop_times derive ``shape_dist_traveled`` from these
    cumulative curve distances, so values always line up (no chord-vs-curve drift).
    """
    line = get_route_line_string(route_id)
    if line is None:
        return None
    position = line.project(Point(map_stop(stop_id)))
    return _line_length_km(substring(line, 0, position)) if position > 0 else 0.0


def _route_shape_points(line, start_lat, start_lon, end_lat, end_lon):
    """Dense ``(lat, lon, cumulative_km)`` points along ``line`` between two endpoints.

    Shared by the synthetic-feed and official-feed shape builders so both publish
    the same high-resolution OSM geometry. Ordered along the direction of travel.
    """
    start_distance = line.project(Point(start_lat, start_lon))
    end_distance = line.project(Point(end_lat, end_lon))
    lo, hi = sorted((start_distance, end_distance))
    coords = list(substring(line, lo, hi).coords)
    if start_distance > end_distance:
        coords = coords[::-1]

    points = []
    cumulative_km = 0.0
    previous = None
    for lat, lon in coords:
        if previous is not None:
            cumulative_km += haversine_distance(previous[0], previous[1], lat, lon)
        previous = (lat, lon)
        points.append((lat, lon, round(cumulative_km, 3)))
    return points


def hms_to_seconds(value):
    """Seconds for an ``HH:MM:SS`` clock string (hours may exceed 24)."""
    hours, minutes, seconds = (int(part) for part in str(value).split(":"))
    return hours * 3600 + minutes * 60 + seconds
