# scripts/make_map.py

import csv
import hashlib
import json
import math
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import folium
import requests


OPENALEX = "https://api.openalex.org"


FIELDS = [
    "doi",
    "title",
    "publication_year",
    "author_position",
    "author_name",
    "author_openalex_id",
    "raw_affiliation",
    "institution_id",
    "ror",
    "institution_name",
    "institution_type",
    "city",
    "country",
    "country_code",
    "latitude",
    "longitude",
    "match_status",
]


def clean_doi(value):
    value = value.strip()
    value = re.sub(r"^https?://(dx\.)?doi\.org/", "", value, flags=re.I)
    value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    return value.lower().rstrip(".")


def cache_path(cache_dir, key):
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.json"


def get_openalex_work(doi, cache_dir):
    cache_dir.mkdir(parents=True, exist_ok=True)

    key = f"https://doi.org/{doi}"
    path = cache_path(cache_dir, key)

    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    url = f"{OPENALEX}/works/{quote(key, safe='')}"
    response = requests.get(url, timeout=30)

    if response.status_code == 404:
        return None

    response.raise_for_status()
    data = response.json()

    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    time.sleep(0.2)

    return data


def raw_affiliations(authorship):
    strings = []

    for value in authorship.get("raw_affiliation_strings") or []:
        if value and value not in strings:
            strings.append(value)

    for affiliation in authorship.get("affiliations") or []:
        value = affiliation.get("raw_affiliation_string")
        if value and value not in strings:
            strings.append(value)

    return " | ".join(strings)


def read_dois(path):
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)

        if "doi" not in reader.fieldnames:
            raise ValueError("Input CSV must contain a column named 'doi'.")

        return sorted({clean_doi(row["doi"]) for row in reader if row.get("doi")})


def write_affiliations(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def build_affiliation_rows(dois, rawdir):
    rows = []

    for index, doi in enumerate(dois, start=1):
        print(f"[{index}/{len(dois)}] {doi}")

        work = get_openalex_work(doi, rawdir)

        if work is None:
            rows.append({
                "doi": doi,
                "title": "",
                "publication_year": "",
                "author_position": "",
                "author_name": "",
                "author_openalex_id": "",
                "raw_affiliation": "",
                "institution_id": "",
                "ror": "",
                "institution_name": "",
                "institution_type": "",
                "city": "",
                "country": "",
                "country_code": "",
                "latitude": "",
                "longitude": "",
                "match_status": "doi_not_found",
            })
            continue

        title = work.get("display_name", "")
        year = work.get("publication_year", "")

        for author_position, authorship in enumerate(work.get("authorships") or [], start=1):
            author = authorship.get("author") or {}
            institutions = authorship.get("institutions") or []

            base = {
                "doi": doi,
                "title": title,
                "publication_year": year,
                "author_position": author_position,
                "author_name": author.get("display_name", ""),
                "author_openalex_id": author.get("id", ""),
                "raw_affiliation": raw_affiliations(authorship),
            }

            if not institutions:
                row = {
                    **base,
                    "institution_id": "",
                    "ror": "",
                    "institution_name": "",
                    "institution_type": "",
                    "city": "",
                    "country": "",
                    "country_code": "",
                    "latitude": "",
                    "longitude": "",
                    "match_status": "no_institution_match",
                }
                rows.append(row)
                continue

            for institution in institutions:
                geo = institution.get("geo") or {}

                row = {
                    **base,
                    "institution_id": institution.get("id", ""),
                    "ror": institution.get("ror", ""),
                    "institution_name": institution.get("display_name", ""),
                    "institution_type": institution.get("type", ""),
                    "city": geo.get("city", ""),
                    "country": geo.get("country", ""),
                    "country_code": geo.get("country_code", ""),
                    "latitude": geo.get("latitude", ""),
                    "longitude": geo.get("longitude", ""),
                    "match_status": "matched",
                }
                rows.append(row)

    return rows


def aggregate_for_map(rows):
    institutions = {}

    for row in rows:
        if row["match_status"] != "matched":
            continue

        if not row["institution_id"]:
            continue

        if not row["latitude"] or not row["longitude"]:
            continue

        key = row["institution_id"]

        if key not in institutions:
            institutions[key] = {
                "institution_id": row["institution_id"],
                "ror": row["ror"],
                "institution_name": row["institution_name"],
                "institution_type": row["institution_type"],
                "city": row["city"],
                "country": row["country"],
                "country_code": row["country_code"],
                "latitude": row["latitude"],
                "longitude": row["longitude"],
                "dois": set(),
                "author_affiliation_rows": 0,
            }

        institutions[key]["dois"].add(row["doi"])
        institutions[key]["author_affiliation_rows"] += 1

    return institutions.values()


def make_map(rows, output_html):
    institutions = aggregate_for_map(rows)

    fmap = folium.Map(location=[20, 0], zoom_start=2, tiles="OpenStreetMap")

    for institution in institutions:
        count = institution["author_affiliation_rows"]
        radius = 3 + 4 * math.log10(count + 1)

        popup = (
            f"<b>{institution['institution_name']}</b><br>"
            f"{institution['city']}, {institution['country']}<br>"
            f"ROR: {institution['ror']}<br>"
            f"Unique DOIs: {len(institution['dois'])}<br>"
            f"Author-institution rows: {count}"
        )

        folium.CircleMarker(
            location=[
                float(institution["latitude"]),
                float(institution["longitude"]),
            ],
            radius=radius,
            popup=popup,
            tooltip=f"{institution['institution_name']} ({count})",
            fill=True,
            fill_opacity=0.65,
        ).add_to(fmap)

    fmap.save(output_html)


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python make_map.py dois.csv")

    doi_csv = Path(sys.argv[1])

    rawdir = Path("cache")

    dois = read_dois(doi_csv)
    rows = build_affiliation_rows(dois, rawdir)

    affiliation_csv = "affiliations.csv"
    map_html = "affiliation_map.html"

    write_affiliations(rows, affiliation_csv)
    make_map(rows, map_html)

    print(f"Wrote {affiliation_csv}")
    print(f"Wrote {map_html}")


if __name__ == "__main__":
    main()
