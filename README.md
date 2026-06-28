# bmrcl-gtfs

Unofficial [GTFS](https://gtfs.org/) dataset for Namma Metro routes, stops and timetables operated by BMRCL in Bengaluru. Derived from timetable information published [on the official BMRCL website](https://www.bmrc.co.in/metro-timings/).

## Caveat

The source for the data is the timetable information published [on the official BMRCL website](https://www.bmrc.co.in/metro-timings/). The timetable does not specify stop timings at intermediate stops. The timings in the GTFS dataset are an approximation based on the stop spacing, average dwell time and average train speed. Discrepancies of the order of a couple of minutes between the GTFS stop timings and actual stop timings on the ground are expected. Additionally, the timetable information published by BMRCL on their website does not list all the short loop trains operated, nor does it list the exact extent of every short loop train. The same persists in the GTFS as potential inconsistencies compared with actual on ground operations.

## GTFS

The GTFS dataset can be found **[here](https://raw.githubusercontent.com/Vonter/bmrcl-gtfs/main/gtfs/bmrcl.zip)**

A variant of the GTFS where schedule information is recorded as frequency-based scheduling can be found **[here](https://raw.githubusercontent.com/Vonter/bmrcl-gtfs/main/gtfs/bmrcl-frequencies.zip)**

## Visualization

Visualize the routes, stops and timetables in the GTFS dataset, on a web browser, using [Transit Lens](https://app.transit-lens.com/).

## Validations

- [gtfs-validator](validation/gtfs-validator)

## Pipeline

The GTFS dataset is built from two sources: the timetables published by BMRCL (for schedules) and OpenStreetMap (for spatial data). The stages below run in sequence.

### 1. Fetch timetables ([fetch.py](fetch.py))

Downloads the timetable images from the [official BMRCL website](https://www.bmrc.co.in/metro-timings/) into [images/](images/), named `<schedule>-<line>-<timestamp>` (e.g. `monday-timetable-purple-*.jpg`). Images are only fetched when the BMRCL website contains images newer than the locally stored images in [images/](images/).

### 2. Transcribe schedules ([schedule/](schedule/))

The timetable images are transcribed into one JSON file per schedule and line, named `<prefix>-<LINE>.json` (e.g. [weekday-PURPLE.json](schedule/weekday-PURPLE.json)). Each file records service as `frequency` bands (headway windows over a section) and explicit `trips` (turn-back services with exact times or headway bands). This transcribe step can be done with the help of the browser-based tool available in [viz/index.html](viz/index.html).

### 3. Fetch spatial data ([osm.py](osm.py))

Queries OpenStreetMap via the Overpass API for Namma Metro stations, route polylines, platforms and entrances within Bengaluru, and writes them to [geojson/export.geojson](geojson/export.geojson). Each station is annotated with the lines passing near it, so the build can match stops to routes.

### 4. Build the GTFS ([parse.py](parse.py))

The core build, supported by the table builders in [gtfs.py](gtfs.py) and the geometry/lookup helpers in [helpers.py](helpers.py). It reads `schedule/*.json` and `geojson/export.geojson` (plus the holiday dates in [data/holidays.txt](data/holidays.txt)), expands frequencies into trips, de-bunches over-close trains, and synthesises stop times, shapes and fares. The result is written first as plain-text GTFS tables in [intermediate/](intermediate/), then tidied with [gtfstidy](tools/gtfstidy) into the published feeds:

- [gtfs/bmrcl.zip](https://raw.githubusercontent.com/Vonter/bmrcl-gtfs/main/gtfs/bmrcl.zip): the primary feed (explicit trips)
- [gtfs/bmrcl-frequencies.zip](https://raw.githubusercontent.com/Vonter/bmrcl-gtfs/main/gtfs/bmrcl-frequencies.zip): the companion frequency-based feed

### 5. Validate ([validate.py](validate.py))

Runs the GTFS through [gtfs-validator](tools/gtfs-validator) for validation, writing the validation report to [validation/gtfs-validator/](validation/gtfs-validator).

## Credits

- [BMRCL](https://www.bmrc.co.in/metro-timings/)

## AI Declaration

Components of this repository, including code and documentation, were written with assistance from Claude AI.
