"""Fetch BMRCL metro timetable images.

Downloads the timetable images shown on https://www.bmrc.co.in/metro-timings/
and stores them under ``images/`` named by schedule + line, with the fetch
timestamp. An image is only (over)written when it is new or its content
(MD5 hash) differs from the copy already on disk, so re-running is cheap and
leaves unchanged timetables untouched.
"""

import glob
import hashlib
import logging
import os
import re
from datetime import datetime, timezone

import requests
import urllib3


# The BMRCL host on :8282 serves an incomplete certificate chain, so standard
# verification fails even though the endpoint is legitimate.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
VERIFY_SSL = False


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("debug.log"), logging.StreamHandler()],
)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(BASE_DIR, "images")

# The metro-timings page is a Next.js app that loads the timetables from this
# JSON API; the images themselves are served from a separate path.
TIMINGS_API_URL = "https://www.bmrc.co.in:8282/api/users/getmeetingtimes"
IMAGE_BASE_URL = "https://www.bmrc.co.in:8282/MetroTiming"
API_KEY = "7a3ac55ef3482d34682eb75d52b44f44"

REQUEST_TIMEOUT = 60


def slugify(value: str) -> str:
    """Lowercase, hyphen-separated slug safe for use in a filename."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def fetch_timetable_entries() -> list:
    """Return the list of timetable records from the BMRCL API."""
    response = requests.get(
        TIMINGS_API_URL,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        timeout=REQUEST_TIMEOUT,
        verify=VERIFY_SSL,
    )
    response.raise_for_status()
    return response.json().get("results", [])


def download_image(image_path: str) -> bytes:
    """Download a single timetable image and return its raw bytes."""
    url = f"{IMAGE_BASE_URL}{image_path}"
    response = requests.get(url, timeout=REQUEST_TIMEOUT, verify=VERIFY_SSL)
    response.raise_for_status()
    return response.content


def save_if_changed(entry: dict) -> None:
    """Download one timetable image and write it only if it is new or changed."""
    image_path = entry.get("engImage")
    schedule = (entry.get("engHeading") or "").strip()
    line = (entry.get("engName") or "").strip()

    if not image_path or not schedule or not line:
        logging.warning("Skipping entry with missing fields: %s", entry)
        return

    name = f"{slugify(schedule)}-{slugify(line)}"
    extension = os.path.splitext(image_path)[1].lower() or ".jpg"

    data = download_image(image_path)
    new_hash = hashlib.md5(data).hexdigest()

    # The "corresponding image" is any existing file for this schedule + line,
    # regardless of the timestamp baked into its name.
    existing = sorted(glob.glob(os.path.join(IMAGES_DIR, f"{name}-*")))
    for path in existing:
        with open(path, "rb") as handle:
            if hashlib.md5(handle.read()).hexdigest() == new_hash:
                logging.info("Unchanged: %s", os.path.basename(path))
                return

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = os.path.join(IMAGES_DIR, f"{name}-{timestamp}{extension}")

    # Overwrite: drop the previous (now stale) copies for this schedule + line.
    for path in existing:
        os.remove(path)
        logging.info("Removed stale image: %s", os.path.basename(path))

    with open(target, "wb") as handle:
        handle.write(data)
    logging.info("Saved: %s", os.path.basename(target))


def main() -> None:
    os.makedirs(IMAGES_DIR, exist_ok=True)

    entries = fetch_timetable_entries()
    logging.info("Fetched %d timetable entries", len(entries))

    for entry in entries:
        try:
            save_if_changed(entry)
        except requests.RequestException as error:
            logging.error(
                "Failed to fetch image for %s / %s: %s",
                entry.get("engName"),
                entry.get("engHeading"),
                error,
            )


if __name__ == "__main__":
    main()
