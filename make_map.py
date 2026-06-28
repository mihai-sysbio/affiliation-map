import csv
import hashlib
import json
import math
import os
import re
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

import matplotlib.pyplot as plt
import requests
import shapefile


OPENALEX = "https://api.openalex.org"

NATURAL_EARTH_URL = (
    "https://naturalearth.s3.amazonaws.com/"
    "110m_cultural/ne_110m_admin_0_countries.zip"
)

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
    "region",
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


def cache_file(cache_dir, namespace, key):
    subdir = cache_dir / namespace
    subdir.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return subdir / f"{digest}.json"


def openalex_params(select=None):
    params = {}

    api_key = os.getenv("OPENALEX_API_KEY")
    if api_key:
        params["api_key"] = api_key

    if select:
        params["select"] = select

    return params


def get_json_cached(url, cache_dir, namespace, key, select=None):
    path = cache_file(cache_dir, namespace, key)

    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    response = requests.get(
        url,
        params=openalex_params(select),
        timeout=30,
        headers={"User-Agent": "standard-GEM-affiliation-map/1.0"},
    )

    if response.status_code == 404:
        return None

    response.raise_for_status()
    data = response.json()

    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    time.sleep(0.2)

    return data


def get_openalex_work(doi, cache_dir):
    doi_url = f"https://doi.org/{doi}"
    url = f"{OPENALEX}/works/{quote(doi_url, safe='')}"

    return get_json_cached(
        url=url,
        cache_dir=cache_dir,
        namespace="works",
        key=doi_url,
        select="id,doi,display_name,publication_year,authorships",
    )


def short_openalex_id(value):
    if not value:
        return ""

    return str(value).rstrip("/").split("/")[-1]


def get_openalex_institution(institution, cache_dir):
    institution_id = institution.get("id") or ""
    ror = institution.get("ror") or ""

    if institution_id:
        key = institution_id
        identifier = short_openalex_id(institution_id)
    elif ror:
        key = ror
        identifier = ror
    else:
        return None

    url = f"{OPENALEX}/institutions/{quote(identifier, safe=':/')}"

    return get_json_cached(
        url=url,
        cache_dir=cache_dir,
        namespace="institutions",
        key=key,
        select="id,ror,display_name,country_code,type,geo",
    )


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

        dois = []
        seen = set()

        for row in reader:
            value = row.get("doi")
            if not value:
                continue

            doi = clean_doi(value)

            if doi and doi not in seen:
                seen.add(doi)
                dois.append(doi)

        return dois


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def empty_location():
    return {
        "city": "",
        "region": "",
        "country": "",
        "country_code": "",
        "latitude": "",
        "longitude": "",
    }


def location_from_institution(institution):
    geo = institution.get("geo") or {}

    return {
        "city": geo.get("city") or "",
        "region": geo.get("region") or "",
        "country": geo.get("country") or "",
        "country_code": geo.get("country_code") or institution.get("country_code") or "",
        "latitude": geo.get("latitude") if geo.get("latitude") is not None else "",
        "longitude": geo.get("longitude") if geo.get("longitude") is not None else "",
    }


def build_affiliation_rows(dois, cache_dir):
    rows = []

    for index, doi in enumerate(dois, start=1):
        print(f"[{index}/{len(dois)}] {doi}")

        work = get_openalex_work(doi, cache_dir)

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
                **empty_location(),
                "match_status": "doi_not_found",
            })
            continue

        title = work.get("display_name") or ""
        publication_year = work.get("publication_year") or ""

        authorships = work.get("authorships") or []

        for author_position, authorship in enumerate(authorships, start=1):
            author = authorship.get("author") or {}
            institutions = authorship.get("institutions") or []

            base = {
                "doi": doi,
                "title": title,
                "publication_year": publication_year,
                "author_position": author_position,
                "author_name": author.get("display_name") or "",
                "author_openalex_id": author.get("id") or "",
                "raw_affiliation": raw_affiliations(authorship),
            }

            if not institutions:
                rows.append({
                    **base,
                    "institution_id": "",
                    "ror": "",
                    "institution_name": "",
                    "institution_type": "",
                    **empty_location(),
                    "match_status": "no_institution_match",
                })
                continue

            for dehydrated_institution in institutions:
                full_institution = get_openalex_institution(
                    dehydrated_institution,
                    cache_dir,
                )

                institution = full_institution or dehydrated_institution
                location = location_from_institution(institution)

                has_coordinates = (
                    location["latitude"] != ""
                    and location["longitude"] != ""
                )

                if full_institution is None:
                    match_status = "institution_matched_not_hydrated"
                elif has_coordinates:
                    match_status = "matched_with_location"
                else:
                    match_status = "matched_without_location"

                rows.append({
                    **base,
                    "institution_id": institution.get("id") or dehydrated_institution.get("id") or "",
                    "ror": institution.get("ror") or dehydrated_institution.get("ror") or "",
                    "institution_name": institution.get("display_name") or dehydrated_institution.get("display_name") or "",
                    "institution_type": institution.get("type") or dehydrated_institution.get("type") or "",
                    **location,
                    "match_status": match_status,
                })

    return rows


def read_affiliations(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def download_natural_earth(cache_dir):
    ne_dir = cache_dir / "natural_earth"
    ne_dir.mkdir(parents=True, exist_ok=True)

    shp_path = ne_dir / "ne_110m_admin_0_countries.shp"

    if shp_path.exists():
        return shp_path

    zip_path = ne_dir / "ne_110m_admin_0_countries.zip"

    print("Downloading Natural Earth base map")

    response = requests.get(
        NATURAL_EARTH_URL,
        timeout=60,
        headers={"User-Agent": "standard-GEM-affiliation-map/1.0"},
    )
    response.raise_for_status()

    zip_path.write_bytes(response.content)

    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(ne_dir)

    return shp_path


def draw_world_map(ax, shp_path):
    reader = shapefile.Reader(str(shp_path))

    for shape in reader.shapes():
        points = shape.points
        parts = list(shape.parts) + [len(points)]

        for start, end in zip(parts[:-1], parts[1:]):
            segment = points[start:end]

            if not segment:
                continue

            x = [point[0] for point in segment]
            y = [point[1] for point in segment]

            ax.fill(
                x,
                y,
                facecolor="#f2f2f2",
                edgecolor="#bdbdbd",
                linewidth=0.35,
                zorder=1,
            )


def aggregate_locations(rows):
    institutions = {}

    for row in rows:
        if not row.get("institution_id"):
            continue

        if not row.get("latitude") or not row.get("longitude"):
            continue

        key = row["institution_id"]

        if key not in institutions:
            institutions[key] = {
                "institution_id": row["institution_id"],
                "institution_name": row["institution_name"],
                "city": row["city"],
                "region": row["region"],
                "country": row["country"],
                "country_code": row["country_code"],
                "latitude": float(row["latitude"]),
                "longitude": float(row["longitude"]),
                "dois": set(),
                "author_institution_rows": 0,
            }

        institutions[key]["dois"].add(row["doi"])
        institutions[key]["author_institution_rows"] += 1

    return list(institutions.values())


def make_static_map(rows, cache_dir, svg_path, png_path):
    shp_path = download_natural_earth(cache_dir)
    institutions = aggregate_locations(rows)

    if not institutions:
        raise RuntimeError(
            "No mappable institution locations found. "
            "Check affiliations.csv and the cached institution JSON files."
        )

    fig, ax = plt.subplots(figsize=(7.2, 3.9))

    draw_world_map(ax, shp_path)

    counts = [item["author_institution_rows"] for item in institutions]
    max_count = max(counts)

    for item in institutions:
        count = item["author_institution_rows"]

        # Area-scaled marker. Keeps large institutions visible without
        # letting them dominate the whole map.
        size = 12 + 85 * math.sqrt(count / max_count)

        ax.scatter(
            item["longitude"],
            item["latitude"],
            s=size,
            facecolor="#1f78b4",
            edgecolor="white",
            linewidth=0.45,
            alpha=0.82,
            zorder=3,
        )

    ax.set_xlim(-180, 180)
    ax.set_ylim(-60, 85)

    ax.set_xticks([])
    ax.set_yticks([])

    for spine in ax.spines.values():
        spine.set_visible(False)

    ax.set_title(
        "Geographic distribution of standard-GEM publication affiliations",
        fontsize=10,
        pad=8,
    )

    ax.text(
        -180,
        -66,
        "Marker area: author–institution rows. Base map: Natural Earth 110m.",
        fontsize=7,
        ha="left",
        va="top",
    )

    fig.tight_layout(pad=0.2)

    fig.savefig(svg_path, bbox_inches="tight")
    fig.savefig(png_path, dpi=600, bbox_inches="tight")
    plt.close(fig)


def print_qc(rows):
    n_rows = len(rows)
    n_dois = len({row["doi"] for row in rows if row.get("doi")})
    n_institutions = len({
        row["institution_id"]
        for row in rows
        if row.get("institution_id")
    })
    n_with_location = sum(
        1
        for row in rows
        if row.get("latitude") and row.get("longitude")
    )
    n_without_location = sum(
        1
        for row in rows
        if row.get("institution_id")
        and not (row.get("latitude") and row.get("longitude"))
    )

    statuses = {}
    for row in rows:
        status = row.get("match_status") or "unknown"
        statuses[status] = statuses.get(status, 0) + 1

    print("")
    print("QC")
    print(f"  DOI count: {n_dois}")
    print(f"  CSV rows: {n_rows}")
    print(f"  unique institutions: {n_institutions}")
    print(f"  rows with coordinates: {n_with_location}")
    print(f"  matched institution rows without coordinates: {n_without_location}")

    print("  match_status:")
    for status, count in sorted(statuses.items()):
        print(f"    {status}: {count}")


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python make_map.py dois.csv")

    doi_csv = Path(sys.argv[1])
    cache_dir = Path("cache")

    affiliations_csv = Path("affiliations.csv")
    svg_path = Path("affiliation_map.svg")
    png_path = Path("affiliation_map.png")

    dois = read_dois(doi_csv)
    rows = build_affiliation_rows(dois, cache_dir)

    write_csv(affiliations_csv, rows)
    make_static_map(rows, cache_dir, svg_path, png_path)
    print_qc(rows)

    print("")
    print(f"Wrote {affiliations_csv}")
    print(f"Wrote {svg_path}")
    print(f"Wrote {png_path}")


if __name__ == "__main__":
    main()
