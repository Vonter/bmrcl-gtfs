"""GTFS table builders — one function per output file, returning a DataFrame."""
import calendar as cal
import logging
from datetime import date, datetime, timedelta

import networkx as nx
import pandas as pd

from helpers import *

# Terminal stations per route (used for direction fallback and Kannada names).
ROUTE_TERMINALS = {
    "GREEN": ("BIET", "APTS"),
    "PURPLE": ("CLGA", "WHTM"),
    "YELLOW": ("RVR", "BMSD"),
}

# Entrance -> platform pathway traversal time (seconds) per station; deeper/taller
# stations take longer. Stations not listed use the default.
DEFAULT_PATHWAY_TRAVERSAL_SECONDS = 90
PATHWAY_TRAVERSAL_SECONDS = {
    'KGWA': 120, 'JDHP': 120, 'SBJT': 120, 'BTML': 120,  # Majestic, Jayadeva, Silk Board, BTM Layout
    'BYPH': 60, 'SPGD': 60,                              # Baiyappanahalli, Mantri Square
}

# BMRCL discounts Smart Card fares versus single-journey tokens: a base 5% off at
# peak hours, deepened to 10% off for off-peak entries (the early/midday/late
# weekday windows) and all day on Sundays and national holidays. Entry time is
# matched in GTFS via timeframes. (Media Brief, 08.02.2025, effective 09.02.2025.)
SMARTCARD_PEAK_DISCOUNT = 0.05
SMARTCARD_OFFPEAK_DISCOUNT = 0.10


def add_agency():
    return pd.DataFrame([{
        'agency_id': 'BMRCL',
        'agency_name': 'Bengaluru Metro Rail Corporation Limited (BMRCL)',
        'agency_url': 'https://www.bmrc.co.in',
        'agency_timezone': 'Asia/Kolkata',
        'agency_lang': 'en',
        'agency_phone': '1800-425-12345',
        'agency_fare_url': 'https://www.bmrc.co.in/',
    }])


def add_feed_info():
    start_date, end_date = _feed_window()
    return pd.DataFrame([{
        'feed_publisher_name': 'Bengaluru Metro Rail Corporation Limited (BMRCL)',
        'feed_publisher_url': 'https://www.bmrc.co.in',
        'feed_lang': 'en',
        'default_lang': 'en',
        'feed_start_date': start_date,
        'feed_end_date': end_date,
        'feed_version': start_date,
        'feed_contact_url': 'https://www.bmrc.co.in/',
    }])


def add_calendar():
    start_date, end_date = _feed_window()
    days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
    services = {
        'weekday': [0, 1, 1, 1, 1, 1, 0],
        'monday':  [1, 0, 0, 0, 0, 0, 0],
        'holiday': [0, 0, 0, 0, 0, 0, 0],
        'sunday':  [0, 0, 0, 0, 0, 0, 1],
    }
    rows = [
        {**dict(zip(days, flags)), 'start_date': start_date, 'end_date': end_date, 'service_id': sid}
        for sid, flags in services.items()
    ]
    return pd.DataFrame(rows)


def add_routes():
    return pd.DataFrame([
        {
            'route_short_name': 'Purple',
            'route_long_name': 'Whitefield (Kadugodi) - Challaghatta',
            'agency_id': 'BMRCL', 'route_type': 1, 'route_id': 'PURPLE',
            'route_desc': 'Purple Line passes through Whitefield (Kadugodi), Majestic and Challaghatta',
            'route_color': '8c2877', 'route_text_color': 'ffffff',
        },
        {
            'route_short_name': 'Green',
            'route_long_name': 'Madavara - Silk Institute',
            'agency_id': 'BMRCL', 'route_type': 1, 'route_id': 'GREEN',
            'route_desc': 'Green Line passes through Madavara, Majestic and Silk Institute',
            'route_color': '009933', 'route_text_color': 'ffffff',
        },
        {
            'route_short_name': 'Yellow',
            'route_long_name': 'Rashtreeya Vidyalaya Road - Delta Electronics Bommasandra',
            'agency_id': 'BMRCL', 'route_type': 1, 'route_id': 'YELLOW',
            'route_desc': 'Yellow Line passes through Rashtreeya Vidyalaya Road and Delta Electronics Bommasandra',
            'route_color': 'ffdf00', 'route_text_color': '000000',
        },
    ])


def add_attributions():
    """Credit the data sources behind the feed (OSM geometry, BMRCL timetables)."""
    return pd.DataFrame([
        {
            'attribution_id': 'bmrcl',
            'organization_name': 'Bengaluru Metro Rail Corporation Limited (BMRCL)',
            'is_producer': 0, 'is_operator': 1, 'is_authority': 1,
            'attribution_url': 'https://www.bmrc.co.in',
        },
        {
            'attribution_id': 'vonter',
            'organization_name': 'Vonter',
            'is_producer': 1, 'is_operator': 0, 'is_authority': 0,
            'attribution_url': 'https://www.github.com/Vonter/bmrcl-gtfs',
        },
    ], columns=['attribution_id', 'organization_name', 'is_producer', 'is_operator',
                'is_authority', 'attribution_url'])


def add_stops(base_stops):
    """Build the hierarchical stops table: parent stations, platforms, entrances.

    Per station: one parent (type 1); one boarding platform (type 0) per
    line/direction (two ordinary, four at the Majestic/RV Road interchanges); one
    entrance (type 2) per OSM ``subway_entrance``. Stations with entrances also get
    a road/platform level pair for the entrance->platform pathways.
    """
    columns = [
        'stop_id', 'stop_code', 'stop_name', 'stop_lat', 'stop_lon', 'zone_id',
        'location_type', 'parent_station', 'wheelchair_boarding', 'level_id', 'platform_code',
    ]
    if not base_stops:
        return pd.DataFrame(columns=columns)

    entrances_by_station = get_entrances()
    platforms = get_platforms()
    routes_by_station = {}
    for code, route_id, _direction in platforms:
        routes_by_station.setdefault(code, set()).add(route_id)

    rows = []
    for station in base_stops:
        code = station['stop_id']
        wheelchair = station.get('wheelchair_boarding', 1)
        has_entrances = code in entrances_by_station
        platform_level = f"{code}_L_platform" if has_entrances else ''
        road_level = f"{code}_L_road" if has_entrances else ''

        rows.append({
            'stop_id': code, 'stop_code': code, 'stop_name': station['stop_name'],
            'stop_lat': station['stop_lat'], 'stop_lon': station['stop_lon'],
            'zone_id': '', 'location_type': 1, 'parent_station': '',
            'wheelchair_boarding': wheelchair, 'level_id': '',
        })

        station_routes = sorted(routes_by_station.get(code, []))
        if station_routes:
            for route_id in station_routes:
                for direction in (0, 1):
                    coord = platforms.get((code, route_id, direction))
                    plat_lat, plat_lon = coord if coord else (station['stop_lat'], station['stop_lon'])
                    rows.append({
                        'stop_id': platform_stop_id(code, route_id, direction),
                        'stop_code': code, 'stop_name': station['stop_name'],
                        'stop_lat': plat_lat, 'stop_lon': plat_lon,
                        'zone_id': station.get('zone_id', code), 'location_type': 0,
                        'parent_station': code, 'wheelchair_boarding': wheelchair,
                        'level_id': platform_level,
                        'platform_code': platform_number(code, route_id, direction),
                    })
        else:
            # No OSM platforms matched: keep a single boarding stop on the bare code.
            rows.append({
                'stop_id': code, 'stop_code': code, 'stop_name': station['stop_name'],
                'stop_lat': station['stop_lat'], 'stop_lon': station['stop_lon'],
                'zone_id': station.get('zone_id', code), 'location_type': 0,
                'parent_station': code, 'wheelchair_boarding': wheelchair,
                'level_id': platform_level,
            })

        for entrance in entrances_by_station.get(code, []):
            rows.append({
                'stop_id': entrance['entrance_id'], 'stop_code': '',
                'stop_name': entrance['stop_name'], 'stop_lat': entrance['stop_lat'],
                'stop_lon': entrance['stop_lon'], 'zone_id': '', 'location_type': 2,
                'parent_station': code, 'wheelchair_boarding': entrance.get('wheelchair_boarding', 0),
                'level_id': road_level,
            })

    return pd.DataFrame(rows, columns=columns)


# Platform level_index (GTFS elevation ordinal) per station, relative to road (0):
# most platforms are elevated (+2); underground stations are negative; a few sit
# higher or lower. Stations not listed use the default.
DEFAULT_PLATFORM_LEVEL_INDEX = 2
PLATFORM_LEVEL_INDEX = {
    'BYPH': 1, 'SPGD': 1,                                    # Baiyappanahalli, Mantri Square
    'KGWA': -2, 'BRCS': -2, 'VSWA': -2, 'VDSA': -2,          # Majestic, Krantivira Sangolli
    'CBPK': -2, 'CKPE': -2,                                  # Rayanna, Central College, Vidhana
                                                            # Soudha, Cubbon Park, Chickpete
    'JDHP': 3, 'BTML': 3, 'SBJT': 3,                         # Jayadeva, BTM Layout, Silk Board
}


def add_levels(stops_df):
    """Road/platform levels for stations that have mapped entrances."""
    levels = []
    for level_id in stops_df['level_id'].dropna().unique():
        if level_id.endswith('_L_road'):
            levels.append({'level_id': level_id, 'level_index': 0, 'level_name': 'Road'})
        elif level_id.endswith('_L_platform'):
            code = level_id[:-len('_L_platform')]
            index = PLATFORM_LEVEL_INDEX.get(code, DEFAULT_PLATFORM_LEVEL_INDEX)
            levels.append({'level_id': level_id, 'level_index': index, 'level_name': 'Platform'})
    return pd.DataFrame(levels, columns=['level_id', 'level_index', 'level_name'])


def add_pathways(stops_df):
    """Bidirectional walkways linking each entrance to every platform of its station."""
    platforms_by_parent = {}
    for _, platform in stops_df[stops_df['location_type'] == 0].iterrows():
        platforms_by_parent.setdefault(platform['parent_station'], []).append(platform)

    pathways = []
    for _, entrance in stops_df[stops_df['location_type'] == 2].iterrows():
        code = entrance['parent_station']
        traversal_time = PATHWAY_TRAVERSAL_SECONDS.get(code, DEFAULT_PATHWAY_TRAVERSAL_SECONDS)
        for platform in platforms_by_parent.get(code, []):
            pathways.append({
                'pathway_id': f"{entrance['stop_id']}_to_{platform['stop_id']}",
                'from_stop_id': entrance['stop_id'],
                'to_stop_id': platform['stop_id'],
                'pathway_mode': 6, 'is_bidirectional': 1,
                'traversal_time': traversal_time,
            })

    return pd.DataFrame(pathways, columns=[
        'pathway_id', 'from_stop_id', 'to_stop_id', 'pathway_mode',
        'is_bidirectional', 'traversal_time',
    ])


def add_trips(schedule_data):
    rows = []
    for trip in schedule_data:
        route_id = trip['route']
        service_id = trip.get('service_id')
        if not service_id:
            logging.warning(f"Missing service_id for trip from {trip.get('file')}, defaulting to 'weekday'")
            service_id = 'weekday'

        shape_id = f"s{len(rows)}"
        trip_id = f"t{len(rows)}"
        # Annotate the schedule record so add_stop_times/add_shapes can join back.
        trip['route_id'], trip['shape_id'], trip['trip_id'] = route_id, shape_id, trip_id

        rows.append({
            'route_id': route_id,
            'service_id': service_id,
            'trip_headsign': expand_stop(trip['destination']),
            'direction_id': trip_direction_id(route_id, trip['origin'], trip['destination']),
            'shape_id': shape_id,
            'trip_id': trip_id,
            'wheelchair_accessible': 1,
        })
    return pd.DataFrame(rows)


def normalize_time(time):
    """GTFS time string, rolling early-morning hours (00–02) past midnight (24–26)."""
    hour = time.hour + 24 if time.hour < 3 else time.hour
    return f"{hour:02d}:{time.minute:02d}:{time.second:02d}"


def add_stop_times(schedule_data):
    stop_times_data = []
    for entry in schedule_data:
        trip_id = entry['trip_id']
        route_id = entry['route_id']
        origin = entry['origin']
        destination = entry['destination']
        start_time = datetime.strptime(entry['start_time'], '%H:%M:%S')

        route_stops = get_stop_sequence_for_trip(route_id, origin, destination)
        direction_id = trip_direction_id(route_id, origin, destination)
        # Measure shape_dist_traveled as curve distance from the origin, matching add_shapes.
        origin_curve_km = get_stop_curve_km(route_id, origin)

        current_time = start_time
        cumulative_km = 0.0
        for stop_sequence, stop_id in enumerate(route_stops, 1):
            arrival_time = current_time
            position = ("origin" if stop_sequence == 1
                        else "terminal" if stop_sequence == len(route_stops)
                        else "middle")
            departure_time = current_time + timedelta(
                seconds=get_dwell_time_seconds(stop_id, position))

            stop_curve_km = get_stop_curve_km(route_id, stop_id)
            if origin_curve_km is not None and stop_curve_km is not None:
                shape_dist = abs(stop_curve_km - origin_curve_km)
            else:
                shape_dist = cumulative_km

            stop_times_data.append({
                'trip_id': trip_id,
                'stop_sequence': stop_sequence,
                'stop_id': platform_stop_id(stop_id, route_id, direction_id),
                'arrival_time': normalize_time(arrival_time),
                'departure_time': normalize_time(departure_time),
                'shape_dist_traveled': round(shape_dist, 3),
                # Intermediate times are interpolated, not published, so approximate.
                'timepoint': 0,
            })

            if stop_sequence < len(route_stops):
                next_stop_id = route_stops[stop_sequence]
                distance_km = calculate_distance_between_stops(stop_id, next_stop_id)
                cumulative_km += distance_km
                current_time = departure_time + timedelta(
                    minutes=calculate_travel_time(distance_km, route_id, stop_id, next_stop_id))

    return pd.DataFrame(stop_times_data)


def add_shapes(schedule_data):
    """Generate shapes for trips from the geojson route lines."""
    rows = []
    processed = set()
    for shape in schedule_data:
        shape_id = shape.get('shape_id')
        if not shape_id or shape_id in processed:
            continue
        processed.add(shape_id)

        line = get_route_line_string(shape.get('route_id'))
        if line is None:
            continue
        origin_lat, origin_lon = map_stop(shape['origin'])
        destination_lat, destination_lon = map_stop(shape['destination'])
        for sequence, (lat, lon, cumulative_km) in enumerate(
            _route_shape_points(line, origin_lat, origin_lon, destination_lat, destination_lon)
        ):
            rows.append({
                'shape_id': shape_id, 'shape_pt_lat': lat, 'shape_pt_lon': lon,
                'shape_pt_sequence': sequence, 'shape_dist_traveled': cumulative_km,
            })
    return pd.DataFrame(rows)


def get_holidays(years):
    """2nd and 4th Saturday of each month, plus the dates in data/holidays.txt."""
    holidays = []
    for year in years:
        for month in range(1, 13):
            saturdays = 0
            for week in cal.monthcalendar(year, month):
                if week[5] != 0:
                    saturdays += 1
                    if saturdays in (2, 4):
                        holidays.append(date(year, month, week[5]))

    with open('data/holidays.txt') as file:
        for line in file:
            try:
                holiday = datetime.strptime(line.strip(), '%d.%m.%Y').date()
                if holiday not in holidays:
                    holidays.append(holiday)
            except ValueError:
                logging.error(f"Could not parse date from holidays.txt on line: {line}")
    return holidays


def add_calendar_dates(feed_info):
    start_date = datetime.strptime(feed_info['feed_start_date'].iloc[0], '%Y%m%d').date()
    end_date = datetime.strptime(feed_info['feed_end_date'].iloc[0], '%Y%m%d').date()

    rows = []
    for holiday in get_holidays([2026, 2027]):
        if not (start_date <= holiday <= end_date):
            continue
        date_str = holiday.strftime('%Y%m%d')
        # Suppress the regular service the holiday replaces, then run 'holiday'.
        replaced = 'monday' if holiday.weekday() == 0 else 'weekday'
        rows.append({'service_id': replaced, 'date': date_str, 'exception_type': 2})
        rows.append({'service_id': 'holiday', 'date': date_str, 'exception_type': 1})
    return pd.DataFrame(rows)


def add_translations(stops_df, routes_df, trips_df):
    """Kannada (``kn``) translations for stop names, route names and headsigns."""
    kn_by_code = _kannada_names_by_code()
    translations_data = []

    # Stop names — one row per parent station and platform, by station code.
    if 'location_type' in stops_df.columns:
        location_type = pd.to_numeric(stops_df['location_type'], errors='coerce').fillna(0)
        boarding = stops_df[location_type.isin([0, 1])]
    else:
        boarding = stops_df
    for _, stop in boarding.iterrows():
        translation = kn_by_code.get(stop.get('stop_code'))
        if translation:
            translations_data.append({
                'table_name': 'stops', 'field_name': 'stop_name',
                'record_id': stop['stop_id'], 'language': 'kn', 'translation': translation,
            })

    # Route long names — from the Kannada names of the two terminals.
    for _, route in routes_df.iterrows():
        terminals = ROUTE_TERMINALS.get(route['route_id'])
        if not terminals:
            continue
        kn_terminals = [kn_by_code.get(t) for t in terminals]
        if all(kn_terminals):
            translations_data.append({
                'table_name': 'routes', 'field_name': 'route_long_name',
                'record_id': route['route_id'], 'language': 'kn',
                'translation': ' - '.join(kn_terminals),
            })

    # Trip headsigns — keyed by field_value so one row covers every matching trip.
    if 'trip_headsign' in trips_df.columns:
        for headsign in trips_df['trip_headsign'].dropna().unique():
            match = boarding[boarding['stop_name'] == headsign]
            if match.empty:
                continue
            translation = kn_by_code.get(match.iloc[0]['stop_code'])
            if translation:
                translations_data.append({
                    'table_name': 'trips', 'field_name': 'trip_headsign',
                    'field_value': headsign, 'language': 'kn', 'translation': translation,
                })

    return pd.DataFrame(translations_data, columns=[
        'table_name', 'field_name', 'language', 'translation', 'record_id', 'field_value',
    ])


def build_metro_network():
    """NetworkX graph keyed by station code, edges between consecutive stations.

    Interchange stations (shared codes) connect the lines, so shortest paths
    cross between them.
    """
    metro_graph = nx.Graph()
    for route_id in ('PURPLE', 'GREEN', 'YELLOW'):
        route_stops = get_route_stops(route_id)
        if not route_stops:
            logging.warning(f"No stops found for {route_id} route")
            continue
        for i in range(len(route_stops) - 1):
            metro_graph.add_edge(route_stops[i], route_stops[i + 1])
    logging.info(f"Built metro network: {metro_graph.number_of_nodes()} nodes, {metro_graph.number_of_edges()} edges")
    return metro_graph


def calculate_travelled_stations(metro_graph, origin_name, dest_name):
    """Number of stations travelled (excluding origin)."""
    try:
        return len(nx.shortest_path(metro_graph, origin_name, dest_name)) - 1
    except (nx.NetworkXNoPath, KeyError):
        logging.warning(f"No path found between {origin_name} and {dest_name}")
        return 0


def calculate_fare_slab(travelled_stations):
    """Fare slab (1–10) from number of stations travelled.

    Reference: https://english.bmrc.co.in:8282/English/uploads/news/english//fileuploads/Revised_Fare_Chart_for_webiste.pdf
    """
    thresholds = [2, 4, 6, 8, 10, 15, 20, 25, 30]
    return sum(travelled_stations > t for t in thresholds) + 1


def calculate_fare(fare_slab):
    """Token fare (₹) for a fare slab."""
    fares = [10, 20, 30, 40, 50, 60, 70, 80, 90, 90]
    return fares[fare_slab - 1]


def calculate_fares_from_network():
    """Fares for all station pairs: {origin_code: {destination_code: fare}}.

    Fares are charged per station; the network's nodes are bare station codes,
    which platforms carry as their zone_id and the flat variant feed keys on
    directly, so the resulting rules match either way.
    """
    logging.info("Building metro network graph for fare calculation...")
    metro_graph = build_metro_network()
    if metro_graph.number_of_nodes() == 0:
        logging.warning("Metro network graph is empty. Cannot calculate fares.")
        return {}

    all_codes = sorted(metro_graph.nodes())
    logging.info(f"Calculating fares for {len(all_codes)} stations...")

    fares_data = {}
    for origin_code in all_codes:
        fares_data[origin_code] = {}
        for dest_code in all_codes:
            if origin_code == dest_code:
                continue
            travelled = calculate_travelled_stations(metro_graph, origin_code, dest_code)
            if travelled > 0:
                fares_data[origin_code][dest_code] = calculate_fare(calculate_fare_slab(travelled))

    logging.info(f"Calculated fares for {sum(len(d) for d in fares_data.values())} station pairs")
    return fares_data


def _fare_codes(fares_data):
    """All station codes participating in the fare network."""
    codes = set(fares_data.keys())
    for destinations in fares_data.values():
        codes.update(destinations.keys())
    return codes


def _fare_pairs(fares_data):
    """Iterate (origin, destination, price) over the fare network."""
    for origin, destinations in fares_data.items():
        for destination, price in destinations.items():
            yield origin, destination, price


def add_fare_attributes(fares_data):
    """One fare attribute per distinct token price."""
    by_price = {}
    for _origin, _destination, price in _fare_pairs(fares_data):
        by_price.setdefault(price, {
            'fare_id': f'token{price}', 'price': price, 'currency_type': 'INR',
            'payment_method': 1, 'transfers': '', 'agency_id': 'BMRCL',
        })
    return pd.DataFrame(list(by_price.values()))


def add_fare_rules(fares_data):
    """One fare rule per origin/destination pair."""
    rules = []
    seen = set()
    for origin, destination, price in _fare_pairs(fares_data):
        rule = {'fare_id': f'token{price}', 'route_id': '', 'origin_id': origin, 'destination_id': destination}
        key = tuple(rule.items())
        if key not in seen:
            seen.add(key)
            rules.append(rule)
    return pd.DataFrame(rules)


def area_id_for_stop(stop_code):
    """Fares v2 area id wrapping a single station."""
    return f"{stop_code}"


def add_areas(fares_data):
    """One Fares v2 area per station in the fare network, named by the station."""
    names = _station_names_by_code()
    areas = [{'area_id': area_id_for_stop(code), 'area_name': names.get(code, code)}
             for code in sorted(_fare_codes(fares_data))]
    return pd.DataFrame(areas, columns=['area_id', 'area_name'])


def add_stop_areas(fares_data, stops_df):
    """Map every platform of a station to that station's singleton fare area."""
    codes = _fare_codes(fares_data)
    stop_areas = []
    if not stops_df.empty and {'stop_code', 'location_type'}.issubset(stops_df.columns):
        location_type = pd.to_numeric(stops_df['location_type'], errors='coerce').fillna(0)
        for _, platform in stops_df[location_type == 0].iterrows():
            code = platform['stop_code']
            if code in codes:
                stop_areas.append({'area_id': area_id_for_stop(code), 'stop_id': platform['stop_id']})
    return pd.DataFrame(stop_areas, columns=['area_id', 'stop_id'])


# Smart Card pricing turns on entry time, matched via these timeframe groups.
# Weekday peak hours (08:00–12:00 and 16:00–21:00) keep the base 5% discount; the
# surrounding off-peak windows get 10%. Sundays and the 'holiday' service (the
# national holidays and special-timetable Saturdays it covers) are off-peak all
# day. (Media Brief, 08.02.2025.)
PEAK_TIMEFRAME_GROUP = 'peak'
OFFPEAK_TIMEFRAME_GROUP = 'offpeak'

_WEEKDAY_PEAK_WINDOWS = [('08:00:00', '12:00:00'), ('16:00:00', '21:00:00')]
_WEEKDAY_OFFPEAK_WINDOWS = [
    ('00:00:00', '08:00:00'), ('12:00:00', '16:00:00'), ('21:00:00', '24:00:00'),
]
_PEAK_SERVICES = ('weekday', 'monday')
_OFFPEAK_ALLDAY_SERVICES = ('sunday', 'holiday')

def add_timeframes():
    """Peak / off-peak entry windows that drive time-of-day Smart Card pricing.

    The windows fully partition each service day so every leg's entry time falls
    in exactly one group, which the leg rules need to resolve a Smart Card fare.
    """
    rows = []
    for service_id in _PEAK_SERVICES:
        for start, end in _WEEKDAY_PEAK_WINDOWS:
            rows.append({'timeframe_group_id': PEAK_TIMEFRAME_GROUP,
                         'start_time': start, 'end_time': end, 'service_id': service_id})
        for start, end in _WEEKDAY_OFFPEAK_WINDOWS:
            rows.append({'timeframe_group_id': OFFPEAK_TIMEFRAME_GROUP,
                         'start_time': start, 'end_time': end, 'service_id': service_id})
    for service_id in _OFFPEAK_ALLDAY_SERVICES:
        rows.append({'timeframe_group_id': OFFPEAK_TIMEFRAME_GROUP,
                     'start_time': '00:00:00', 'end_time': '24:00:00', 'service_id': service_id})
    return pd.DataFrame(rows, columns=[
        'timeframe_group_id', 'start_time', 'end_time', 'service_id'])


# Fare media offered by BMRCL. The QR Ticket carries the same full fare as the
# paper token but is bought through the mobile apps, so it rides its own media
# (fare_media_type 4 = mobile app) rather than being folded into the token.
FARE_MEDIA = [
    {'fare_media_id': 'token', 'fare_media_name': 'Token', 'fare_media_type': 1},
    {'fare_media_id': 'qr', 'fare_media_name': 'QR Ticket', 'fare_media_type': 4},
    {'fare_media_id': 'smartcard', 'fare_media_name': 'Smart Card', 'fare_media_type': 2},
    {'fare_media_id': 'touristcard', 'fare_media_name': 'Tourist Card', 'fare_media_type': 2},
]

# Single-journey offerings, expanded per entry timeframe. Both the token (full
# fare, time-independent) and the Smart Card fare are emitted for each timeframe
# so they match a leg with equal specificity and surface as alternatives — were
# only the Smart Card rule timeframe-scoped, its higher specificity would
# override and hide the token fare at that entry time.
LEG_FARE_OFFERINGS = [
    {'product_suffix': 'token', 'media_id': 'token',
     'timeframe_group': PEAK_TIMEFRAME_GROUP, 'discount': 0.0},
    {'product_suffix': 'qr', 'media_id': 'qr',
     'timeframe_group': PEAK_TIMEFRAME_GROUP, 'discount': 0.0},
    {'product_suffix': 'smartcard_peak', 'media_id': 'smartcard',
     'timeframe_group': PEAK_TIMEFRAME_GROUP, 'discount': SMARTCARD_PEAK_DISCOUNT},
    {'product_suffix': 'token', 'media_id': 'token',
     'timeframe_group': OFFPEAK_TIMEFRAME_GROUP, 'discount': 0.0},
    {'product_suffix': 'qr', 'media_id': 'qr',
     'timeframe_group': OFFPEAK_TIMEFRAME_GROUP, 'discount': 0.0},
    {'product_suffix': 'smartcard_offpeak', 'media_id': 'smartcard',
     'timeframe_group': OFFPEAK_TIMEFRAME_GROUP, 'discount': SMARTCARD_OFFPEAK_DISCOUNT},
]

# Tourist Cards: flat-rate day passes for unlimited travel over a validity window.
# Passes aren't leg-priced, so they ride as standalone products on their own media.
# The validity lives in the product id; the name stays the plain medium name.
TOURIST_PASSES = [
    {'fare_product_id': 'pass_1day', 'amount': 300},
    {'fare_product_id': 'pass_3day', 'amount': 600},
    {'fare_product_id': 'pass_5day', 'amount': 800},
]


def discounted_amount(price, discount):
    """Charged amount (₹) after a Smart Card discount; exact, not rounded to rupees."""
    return price * (1 - discount)


def add_fare_media():
    """Fare media offered by BMRCL: token/QR, Smart Card and Tourist Card."""
    return pd.DataFrame(FARE_MEDIA, columns=['fare_media_id', 'fare_media_name', 'fare_media_type'])


def fare_product_id(price, product_suffix):
    """Fare product id, disambiguated by offering so variants never collide."""
    return f"fare_{price}_{product_suffix}"


def add_fare_products(fares_data):
    """Per-leg products (token + peak/off-peak Smart Card) plus Tourist Card passes."""
    media_names = {m['fare_media_id']: m['fare_media_name'] for m in FARE_MEDIA}
    prices = sorted({price for _o, _d, price in _fare_pairs(fares_data)})
    products = []
    seen = set()
    for price in prices:
        for offering in LEG_FARE_OFFERINGS:
            product_id = fare_product_id(price, offering['product_suffix'])
            if product_id in seen:  # token is shared across both timeframes
                continue
            seen.add(product_id)
            products.append({
                'fare_product_id': product_id,
                'fare_product_name': media_names[offering['media_id']],
                'fare_media_id': offering['media_id'],
                # INR has two minor units; amount must carry matching decimals.
                'amount': f"{discounted_amount(price, offering['discount']):.2f}",
                'currency': 'INR',
            })
    for pass_ in TOURIST_PASSES:
        products.append({
            'fare_product_id': pass_['fare_product_id'],
            'fare_product_name': media_names['touristcard'],
            'fare_media_id': 'touristcard',
            'amount': f"{pass_['amount']:.2f}",
            'currency': 'INR',
        })
    return pd.DataFrame(products, columns=[
        'fare_product_id', 'fare_product_name', 'fare_media_id', 'amount', 'currency',
    ])


def add_fare_leg_rules(fares_data):
    """Origin/destination leg rules, one per offering and entry timeframe."""
    rules = [
        {'leg_group_id': 'metro', 'network_id': '',
         'from_area_id': area_id_for_stop(origin), 'to_area_id': area_id_for_stop(destination),
         'from_timeframe_group_id': offering['timeframe_group'],
         'fare_product_id': fare_product_id(price, offering['product_suffix'])}
        for origin, destination, price in _fare_pairs(fares_data)
        for offering in LEG_FARE_OFFERINGS
    ]
    return pd.DataFrame(rules, columns=[
        'leg_group_id', 'network_id', 'from_area_id', 'to_area_id',
        'from_timeframe_group_id', 'fare_product_id',
    ])


def add_transfers():
    """Route-to-route interchange transfers, modelled platform-to-platform.

    A passenger may arrive on either platform of the first line and continue from
    either platform of the second, so we emit a transfer for each platform pair.
    Platform endpoints are used since validators reject station-level transfers.
    """
    interchanges = [
        ('KGWA', 'GREEN', 'PURPLE'),  # Majestic
        ('RVR', 'GREEN', 'YELLOW'),   # Rashtreeya Vidyalaya Road
    ]
    transfers_data = []
    for code, line_a, line_b in interchanges:
        platforms_a = [platform_stop_id(code, line_a, d) for d in (0, 1)]
        platforms_b = [platform_stop_id(code, line_b, d) for d in (0, 1)]
        for from_route, to_route, from_platforms, to_platforms in (
            (line_a, line_b, platforms_a, platforms_b),
            (line_b, line_a, platforms_b, platforms_a),
        ):
            for from_stop in from_platforms:
                for to_stop in to_platforms:
                    transfers_data.append({
                        'from_stop_id': from_stop, 'to_stop_id': to_stop,
                        'from_route_id': from_route, 'to_route_id': to_route,
                        'transfer_type': 2, 'min_transfer_time': 60,
                    })
    return pd.DataFrame(transfers_data)
