"""Extract BMRCL (Namma Metro) spatial data from OpenStreetMap via Overpass.

Produces ``geojson/export.geojson`` containing every piece of spatial data the
GTFS build (``parse.py``) needs:

* **Station nodes** (``public_transport=station``) with their station ``code``,
  Kannada name, wheelchair tag and website. Each station carries a synthesised
  ``@relations`` list (colour + ref of every metro line passing within
  ``STATION_ROUTE_MAX_M`` metres) so the route/stop matching in ``parse.py``
  keeps working exactly as it did with the hand-curated overpass-turbo export.
* **Route polylines** (one ``LineString`` per OSM route relation) named exactly
  like the relation, so ``ROUTE_FEATURE_NAMES`` lookups resolve.
* **Subway entrances** (``railway=subway_entrance``) as ``Point`` features, each
  annotated with the ``station_code`` of its nearest station. These feed the
  ``location_type=2`` entrances and ``pathways.txt`` in the GTFS output.

Run standalone (``python osm.py``) to refresh the export before building GTFS.
"""

import json
import logging
import math
import os

import requests
from shapely.geometry import LineString, MultiLineString, Point

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("debug.log"), logging.StreamHandler()],
)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NETWORK = "Namma Metro"
# Bounding box covering the Bengaluru metropolitan area: south, west, north, east.
BBOX = (12.70, 77.35, 13.25, 77.85)
OUTPUT_PATH = "geojson/export.geojson"

# A station is considered part of a line if it lies within this many metres of
# the line's polyline. Generous enough for platform/track offsets, tight enough
# to keep interchange detection correct.
STATION_ROUTE_MAX_M = 250
# Maximum distance an entrance may be from a station to be associated with it.
ENTRANCE_STATION_MAX_M = 800
# Maximum distance a stop_position node may be from a station to count as one of
# its platforms. Tight, since stop nodes sit on the station's own platforms.
PLATFORM_STATION_MAX_M = 200

# Equirectangular projection reference latitude (central Bengaluru) used to turn
# lon/lat into local metres for distance/projection maths.
_REF_LAT = 12.97
_M_PER_DEG_LAT = 111320.0
_M_PER_DEG_LON = 111320.0 * math.cos(math.radians(_REF_LAT))


def _to_metres(lon, lat):
    """Project lon/lat to local planar metres (equirectangular)."""
    return (lon * _M_PER_DEG_LON, lat * _M_PER_DEG_LAT)


def _overpass(query):
    response = requests.post(
        OVERPASS_URL,
        data=query.encode("utf-8"),
        headers={"User-Agent": "bmrcl-gtfs/1.0", "Content-Type": "text/plain"},
        timeout=180,
    )
    response.raise_for_status()
    return response.json()


def fetch_route_relations():
    """Return metro route relations with full member-way geometry."""
    query = f'[out:json][timeout:180];rel[network="{NETWORK}"][route=subway];out geom;'
    elements = _overpass(query)["elements"]
    return [e for e in elements if e["type"] == "relation"]


def _as_point_node(element):
    """Normalise an Overpass element to a node-like dict with top-level lon/lat.

    Stations mapped as ways/areas carry their position under ``center``; this
    flattens them so the rest of the pipeline can treat every station the same.
    """
    if "lon" in element and "lat" in element:
        return element
    center = element.get("center")
    if not center:
        return None
    element = dict(element)
    element["lon"], element["lat"] = center["lon"], center["lat"]
    return element


def fetch_stations():
    """Return subway stations within the Bengaluru bounding box.

    Stations may be mapped in OSM as a ``railway=station`` node *or* as an
    area/way (e.g. Goraguntepalya/YPI), whose centroid we use. As a last
    resort, a coded ``stop_position`` stands in for any station still missing,
    so the feed covers every line stop.
    """
    south, west, north, east = BBOX
    bbox = f"{south},{west},{north},{east}"

    station_query = (
        f"[out:json][timeout:120];"
        f"(node[railway=station][station=subway]({bbox});"
        f" way[railway=station][station=subway]({bbox}););"
        f"out tags center;"
    )
    stations = []
    seen = set()
    for element in _overpass(station_query)["elements"]:
        node = _as_point_node(element)
        if node is None:
            continue
        code = node["tags"].get("code") or node["tags"].get("ref")
        if not code or code in seen:
            continue
        seen.add(code)
        stations.append(node)

    fallback_query = (
        f"[out:json][timeout:120];"
        f'node["code"]["subway"="yes"][public_transport=stop_position]({bbox});'
        f"out tags center;"
    )
    for element in _overpass(fallback_query)["elements"]:
        node = _as_point_node(element)
        if node is None:
            continue
        code = node["tags"].get("code")
        if not code or code in seen:
            continue
        seen.add(code)
        tags = dict(node["tags"])
        tags["public_transport"] = "station"
        tags["railway"] = "station"
        tags["station"] = "subway"
        node["tags"] = tags
        stations.append(node)
        logging.info("Synthesised station %s (%s) from stop_position", code, tags.get("name"))

    return stations


def fetch_entrances():
    """Return subway entrance nodes within the Bengaluru bounding box."""
    south, west, north, east = BBOX
    query = (
        f"[out:json][timeout:120];"
        f"node[railway=subway_entrance]({south},{west},{north},{east});"
        f"out tags center;"
    )
    return [e for e in _overpass(query)["elements"] if e["type"] == "node"]


def _merge_ways(ways):
    """Chain a list of coordinate sequences into one ordered coordinate list.

    Greedily stitches segments whose endpoints coincide; tolerant of segments
    given in reversed order. Any leftover (disconnected) segments are appended
    so no geometry is lost.
    """
    segments = [list(w) for w in ways if len(w) >= 2]
    if not segments:
        return []

    line = segments.pop(0)
    changed = True
    while segments and changed:
        changed = False
        for i, seg in enumerate(segments):
            if line[-1] == seg[0]:
                line += seg[1:]
            elif line[-1] == seg[-1]:
                line += seg[-2::-1]
            elif line[0] == seg[-1]:
                line = seg[:-1] + line
            elif line[0] == seg[0]:
                line = seg[:0:-1] + line
            else:
                continue
            segments.pop(i)
            changed = True
            break

    for seg in segments:
        line += seg
    return line


def _relation_stop_nodes(relation):
    """Ordered (lon, lat) list of the relation's ``stop`` role member nodes.

    These are the per-direction ``public_transport=stop_position`` nodes — i.e.
    where a train actually halts on that direction's platform — listed in travel
    order. Each direction relation contributes one stop node per station, so the
    two directions give the two side platforms.
    """
    nodes = []
    for member in relation.get("members", []):
        if member.get("role") != "stop" or member.get("type") != "node":
            continue
        if "lon" in member and "lat" in member:
            nodes.append((member["lon"], member["lat"]))
    return nodes


def _relation_line_coords(relation):
    """Merged (lon, lat) coordinate list for a route relation's track ways."""
    ways = []
    for member in relation.get("members", []):
        if member.get("type") != "way" or "geometry" not in member:
            continue
        # Skip stop/platform helper ways; keep the running track.
        if member.get("role") in ("platform", "platform_entry_only", "platform_exit_only"):
            continue
        ways.append([(pt["lon"], pt["lat"]) for pt in member["geometry"]])
    return _merge_ways(ways)


def build_features(relations, stations, entrances):
    features = []

    # --- Route polylines and per-ref geometry for station matching ----------
    ref_to_colour = {}
    ref_to_lines = {}
    for relation in relations:
        tags = relation.get("tags", {})
        coords = _relation_line_coords(relation)
        if len(coords) < 2:
            continue

        ref = tags.get("ref")
        if ref:
            ref_to_colour.setdefault(ref, tags.get("colour"))
            ref_to_lines.setdefault(ref, []).append(
                LineString([_to_metres(lon, lat) for lon, lat in coords])
            )

        properties = dict(tags)
        properties["@id"] = f"relation/{relation['id']}"
        features.append(
            {
                "type": "Feature",
                "properties": properties,
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[lon, lat] for lon, lat in coords],
                },
            }
        )

    ref_to_multiline = {
        ref: MultiLineString(lines) for ref, lines in ref_to_lines.items()
    }

    # --- Stations -----------------------------------------------------------
    station_points = []  # (code, shapely Point in metres, lon, lat)
    for node in stations:
        tags = node.get("tags", {})
        code = tags.get("code") or tags.get("ref")
        if not code:
            logging.warning("Station '%s' has no code; skipping", tags.get("name"))
            continue

        lon, lat = node["lon"], node["lat"]
        point_m = Point(_to_metres(lon, lat))
        station_points.append((code, point_m, lon, lat))

        relations_for_station = []
        for ref, multiline in ref_to_multiline.items():
            if point_m.distance(multiline) <= STATION_ROUTE_MAX_M:
                relations_for_station.append(
                    {
                        "rel": ref,
                        "reltags": {
                            "colour": ref_to_colour.get(ref),
                            "ref": ref,
                            "network": NETWORK,
                        },
                        "role": "",
                    }
                )

        if not relations_for_station:
            logging.warning(
                "Station %s (%s) matched no line within %dm",
                code,
                tags.get("name"),
                STATION_ROUTE_MAX_M,
            )

        properties = dict(tags)
        properties["@id"] = f"node/{node['id']}"
        properties["code"] = code
        properties["@relations"] = relations_for_station
        features.append(
            {
                "type": "Feature",
                "properties": properties,
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            }
        )

    # --- Platforms (per-direction stop positions) ---------------------------
    # Each route relation lists one stop_position node per station for its
    # direction of travel. Matching those to the nearest station yields the two
    # side platforms (four at interchanges, where two lines cross). Direction is
    # assigned downstream in ``parse.py`` from the relation's ``from``/``to``
    # terminals, so platforms stay consistent with the trip direction logic.
    def _nearest_station_code(lon, lat):
        point_m = Point(_to_metres(lon, lat))
        nearest_code, nearest_dist = None, None
        for code, station_pt, _, _ in station_points:
            dist = point_m.distance(station_pt)
            if nearest_dist is None or dist < nearest_dist:
                nearest_code, nearest_dist = code, dist
        return nearest_code, nearest_dist

    platform_count = 0
    seen_platforms = set()
    for relation in relations:
        ref = relation.get("tags", {}).get("ref")
        if not ref:
            continue
        stop_nodes = _relation_stop_nodes(relation)
        if len(stop_nodes) < 2:
            continue

        from_code, _ = _nearest_station_code(*stop_nodes[0])
        to_code, _ = _nearest_station_code(*stop_nodes[-1])

        for lon, lat in stop_nodes:
            code, dist = _nearest_station_code(lon, lat)
            if code is None or dist > PLATFORM_STATION_MAX_M:
                continue
            key = (code, ref, from_code, to_code)
            if key in seen_platforms:
                continue
            seen_platforms.add(key)
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "@id": f"platform/{ref}/{code}/{from_code}-{to_code}",
                        "platform": ref,
                        "code": code,
                        "ref": ref,
                        "from": from_code,
                        "to": to_code,
                        "network": NETWORK,
                    },
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                }
            )
            platform_count += 1

    # --- Entrances ----------------------------------------------------------
    entrance_count = 0
    for node in entrances:
        tags = node.get("tags", {})
        lon, lat = node["lon"], node["lat"]
        point_m = Point(_to_metres(lon, lat))

        nearest_code, nearest_dist = None, None
        for code, station_pt, _, _ in station_points:
            dist = point_m.distance(station_pt)
            if nearest_dist is None or dist < nearest_dist:
                nearest_code, nearest_dist = code, dist

        if nearest_dist is not None and nearest_dist > ENTRANCE_STATION_MAX_M:
            logging.warning(
                "Entrance '%s' nearest station %s is %.0fm away; skipping",
                tags.get("name"),
                nearest_code,
                nearest_dist,
            )
            continue

        properties = dict(tags)
        properties["@id"] = f"node/{node['id']}"
        properties["station_code"] = nearest_code
        features.append(
            {
                "type": "Feature",
                "properties": properties,
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            }
        )
        entrance_count += 1

    logging.info(
        "Built %d line, %d station, %d platform and %d entrance features",
        len(relations),
        len(station_points),
        platform_count,
        entrance_count,
    )
    return {"type": "FeatureCollection", "features": features}


def main():
    logging.info("Fetching Namma Metro data from Overpass...")
    relations = fetch_route_relations()
    stations = fetch_stations()
    entrances = fetch_entrances()
    logging.info(
        "Fetched %d route relations, %d stations, %d entrances",
        len(relations),
        len(stations),
        len(entrances),
    )

    collection = build_features(relations, stations, entrances)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as handle:
        json.dump(collection, handle, ensure_ascii=False, indent=1)
    logging.info("Wrote %s", OUTPUT_PATH)


if __name__ == "__main__":
    main()
