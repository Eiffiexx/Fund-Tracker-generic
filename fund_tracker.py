#!/usr/bin/env python3
"""
Install:
    python3 -m pip install pdfplumber pypdfium2

The script scans ~/Downloads recursively and writes results
to ~/Downloads/fund_tracker_results. Output is: id,date,ror,aum,updated.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Sequence

import pdfplumber


MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
MONTH_PATTERN = (
    r"January|February|March|April|May|June|July|August|September|October|November|December"
)

# Stable IDs are intentionally based on manager/fund wording, never report dates.
# Add a new entry here when onboarding another manager. More specific patterns
# should appear before broad ones.
DEFAULT_PROFILE_DATA = [
    (r"quintik managed futures|qmf_report", "QMF", "Quintik Managed Futures Fund"),
    (r"pythagoras.*quant.*long[- _]?short", "PYTH_QMLS", "Pythagoras Quant Long-Short Fund"),
    (r"pythagoras.*arbitrage", "PYTH_ARB", "Pythagoras Arbitrage Fund"),
    (r"pythagoras.*alpha.*long.*biased|\bpalb\b", "PYTH_PALB", "Pythagoras Alpha Long Biased Fund"),
    (r"pythagoras.*absolute.*return", "PYTH_ABS", "Pythagoras Absolute Return Fund"),
    (r"holland park.*digital assets", "HOLLAND_PARK_DAF", "Holland Park Digital Assets Fund"),
    (r"vadantia.*quant fx", "VADANTIA_QFX", "Vadantia Quant FX"),
    (r"bowmoor.*global alpha", "BOWMOOR_GAF_D", "Bowmoor Capital Global Alpha Fund - Share Class D"),
    (r"quantica managed futures", "QUANTICA_QMF", "Quantica Managed Futures Program"),
    (
        r"takahe.*global markets|takah[eé].*global markets|takah[eé].*\bGMF\b|\bGMF\b.*takah[eé]",
        "TAKAHE_GMF",
        "Takahe Global Markets Fund",
    ),
]


@dataclass
class Profile:
    pattern: str
    id: str
    fund_name: str
    table_index: int = 0


@dataclass
class Candidate:
    rows: list[dict]
    reported: dict[int, float]
    score: int
    origin: str


def month_end(year: int, month: int) -> str:
    return date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def parse_number(value: object) -> float | None:
    if value is None:
        return None
    token = str(value).strip().replace(",", "").replace("−", "-")
    token = re.sub(r"[*†‡]+", "", token).replace("%", "").strip()
    if token.lower() in {"", "-", "--", "n/a", "na", "none", "null"}:
        return None
    token = token.strip("()")
    try:
        return float(token)
    except ValueError:
        return None


def undouble(value: object) -> str:
    """Fix PDFs whose text layer duplicates every glyph (JJaann, 22002266)."""
    text = "" if value is None else str(value).strip()
    if len(text) >= 2 and len(text) % 2 == 0 and text[::2] == text[1::2]:
        return text[::2]
    return text


def clean_filename(path: Path) -> str:
    name = path.stem.replace("_", " ")
    name = re.sub(r"(?i)\b(fact ?sheet|one pager|monthly report|report|update)\b", " ", name)
    name = re.sub(rf"(?i)\b(?:{MONTH_PATTERN})\b(?:\s+\d{{1,2}}(?:st|nd|rd|th)?)?\s+20\d{{2}}", " ", name)
    return " ".join(name.split()).strip(" -")


def slug_id(name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name.upper())
    stop = {"FUND", "THE", "CLASS", "SHARE", "REPORT", "FACTSHEET", "CAPITAL", "PROGRAM"}
    words = [word for word in words if word not in stop]
    return "_".join(words[:5]) or "UNKNOWN"


def load_profiles(path: Path | None) -> list[Profile]:
    if path and path.exists():
        with path.open(encoding="utf-8") as handle:
            return [Profile(**item) for item in json.load(handle)]
    return [Profile(pattern, fund_id, fund_name) for pattern, fund_id, fund_name in DEFAULT_PROFILE_DATA]


def match_profile(path: Path, text: str, profiles: Sequence[Profile]) -> Profile:
    # The filename is the least ambiguous signal. A factsheet may mention other
    # strategies in its narrative, so only fall back to document text afterward.
    for profile in profiles:
        if re.search(profile.pattern, path.name, flags=re.IGNORECASE | re.DOTALL):
            return profile
    for profile in profiles:
        if re.search(profile.pattern, text[:12000], flags=re.IGNORECASE | re.DOTALL):
            return profile
    name = detect_generic_name(path, text)
    return Profile(pattern="", id=slug_id(name), fund_name=name)


def detect_generic_name(path: Path, text: str) -> str:
    for line in text.splitlines()[:40]:
        candidate = " ".join(line.split()).strip()
        if 4 <= len(candidate) <= 100 and re.search(r"(?i)\b(fund|program|strategy)\b", candidate):
            if not re.search(r"(?i)performance|return|facts|report|terms", candidate):
                return candidate
    return clean_filename(path)


def extract_pdf(
    path: Path, enable_ocr: bool, vision_script: Path, allow_vision: bool
) -> tuple[str, list[list[list[str | None]]], str]:
    page_texts: list[str] = []
    tables: list[list[list[str | None]]] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            page_texts.append(page.extract_text(x_tolerance=2, y_tolerance=3, layout=True) or "")
            tables.extend(page.extract_tables() or [])
    text = "\n".join(page_texts)
    method = "pdf_text"
    if enable_ocr and len(re.sub(r"\s+", "", text)) < 80:
        ocr_text = ocr_pdf(path, vision_script, allow_vision)
        if ocr_text.strip():
            text = ocr_text
            method = "ocr"
    return text, tables, method


def ocr_pdf(path: Path, vision_script: Path, allow_vision: bool) -> str:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return ""
    tesseract = shutil.which("tesseract")
    swift = (
        shutil.which("swift")
        if allow_vision and sys.platform == "darwin" and vision_script.exists()
        else None
    )
    if not tesseract and not swift:
        return ""
    output: list[str] = []
    with tempfile.TemporaryDirectory(prefix="fund_tracker_ocr_") as temp_dir:
        pdf = pdfium.PdfDocument(str(path))
        for page_number, page in enumerate(pdf):
            image_path = Path(temp_dir) / f"page_{page_number + 1}.png"
            page.render(scale=2.5).to_pil().save(image_path)
            if tesseract:
                command = [tesseract, str(image_path), "stdout", "--psm", "6"]
            else:
                command = [swift, str(vision_script), str(image_path)]
            environment = None
            if swift:
                import os
                environment = os.environ.copy()
                environment["CLANG_MODULE_CACHE_PATH"] = str(Path(temp_dir) / "clang-cache")
                environment["SWIFT_MODULECACHE_PATH"] = str(Path(temp_dir) / "swift-cache")
            result = subprocess.run(command, capture_output=True, text=True, timeout=180, env=environment)
            if result.returncode == 0:
                output.append(result.stdout)
    return "\n".join(output)


def report_date(path: Path, text: str) -> str | None:
    filename = path.stem.replace("_", " ")
    header = "\n".join(text.splitlines()[:80])
    for source in (filename, header):
        matches: list[date] = []
        for match in re.finditer(
            rf"(?i)\b({MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(20\d{{2}})\b", source
        ):
            month = datetime.strptime(match.group(1).title(), "%B").month
            matches.append(date(int(match.group(3)), month, int(match.group(2))))
        for match in re.finditer(
            rf"(?i)\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_PATTERN})[,]?\s+(20\d{{2}})\b", source
        ):
            month = datetime.strptime(match.group(2).title(), "%B").month
            matches.append(date(int(match.group(3)), month, int(match.group(1))))
        if matches:
            return max(matches).isoformat()
        month_matches = list(re.finditer(rf"(?i)\b({MONTH_PATTERN})\s+(20\d{{2}})\b", source))
        if month_matches:
            dates = [
                date(
                    int(match.group(2)),
                    datetime.strptime(match.group(1).title(), "%B").month,
                    1,
                )
                for match in month_matches
            ]
            latest = max(dates)
            return month_end(latest.year, latest.month)
    return None


def extract_aum(text: str) -> tuple[int | None, str | None]:
    patterns = [
        r"(?i)\b(?:fund|strategy|program)\s*AUM\s*(?:\(\s*(million|billion)\s*\))?\s*[:\-]?\s*([$€£]|USD|EUR|GBP)?\s*([\d,.'’]+)\s*(million|billion|mn|mm|m|bn|b)?\b",
        r"(?i)\bAUM\s*(?:\(\s*(million|billion)\s*\))?\s*[:\-]?\s*([$€£]|USD|EUR|GBP)?\s*([\d,.'’]+)\s*(million|billion|mn|mm|m|bn|b)?\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        unit = (match.group(1) or match.group(4) or "").lower()
        value = float(re.sub(r"[,'’]", "", match.group(3)))
        if unit in {"million", "mn", "mm", "m"}:
            value *= 1_000_000
        elif unit in {"billion", "bn", "b"}:
            value *= 1_000_000_000
        currency_token = (match.group(2) or "").upper()
        currency = {"$": "USD", "€": "EUR", "£": "GBP", "USD": "USD", "EUR": "EUR", "GBP": "GBP"}.get(currency_token)
        return int(round(value)), currency
    return None, None


def table_candidate(table: list[list[object]], origin: str) -> Candidate | None:
    cleaned = [[undouble(cell) for cell in row] for row in table]
    header_index = None
    for index, row in enumerate(cleaned[:8]):
        words = [re.sub(r"[^a-z]", "", cell.lower())[:3] for cell in row]
        if sum(month in words for month in MONTHS) >= 8:
            header_index = index
            break
    if header_index is None:
        return None
    header = cleaned[header_index]
    month_columns: dict[int, int] = {}
    annual_column = None
    for column, cell in enumerate(header):
        word = re.sub(r"[^a-z]", "", cell.lower())
        if word[:3] in MONTHS:
            month_columns[column] = MONTHS.index(word[:3]) + 1
        elif word in {"year", "ytd"} and column > 0:
            annual_column = column
    rows: list[dict] = []
    reported: dict[int, float] = {}
    years = 0
    for raw_row in cleaned[header_index + 1 :]:
        year_column = next((i for i, cell in enumerate(raw_row) if re.fullmatch(r"20\d{2}", cell.strip())), None)
        if year_column is None:
            continue
        year = int(raw_row[year_column])
        years += 1
        for column, month in month_columns.items():
            if column >= len(raw_row):
                continue
            value = parse_number(raw_row[column])
            if value is not None:
                rows.append({"year": year, "month": month, "ror": value / 100})
        if annual_column is not None and annual_column < len(raw_row):
            value = parse_number(raw_row[annual_column])
            if value is not None:
                reported[year] = value / 100
    if not rows:
        return None
    return Candidate(rows, reported, years * 10 + len(month_columns), origin)


def text_candidates(text: str, updated: str | None) -> list[Candidate]:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    candidates: list[Candidate] = []
    for index, line in enumerate(lines):
        header_words = {word[:3].lower() for word in re.findall(r"[A-Za-z]+", line)}
        if sum(month in header_words for month in MONTHS) < 8:
            continue
        parsed_rows: list[dict] = []
        reported: dict[int, float] = {}
        years = 0
        misses = 0
        for row_line in lines[index + 1 :]:
            match = re.match(r"^(20\d{2})\s+(.+)$", row_line)
            if not match:
                if years:
                    misses += 1
                    if misses >= 4:
                        break
                continue
            misses = 0
            year = int(match.group(1))
            values = [parse_number(token) for token in re.findall(r"[-−]?\d[\d,.]*(?:%|\*+)?", match.group(2))]
            values = [value for value in values if value is not None]
            if len(values) < 2:
                continue
            years += 1
            annual = values[-1] / 100
            monthly = values[:-1]
            reported[year] = annual
            if len(monthly) >= 12:
                month_numbers = range(1, 13)
                monthly = monthly[:12]
            elif updated and year == int(updated[:4]):
                month_numbers = range(1, len(monthly) + 1)
            else:
                month_numbers = range(13 - len(monthly), 13)
            for month, value in zip(month_numbers, monthly):
                parsed_rows.append({"year": year, "month": month, "ror": value / 100})
        if parsed_rows:
            candidates.append(Candidate(parsed_rows, reported, years * 10 + 12, "text_table"))
    return candidates


def compound(values: Iterable[float]) -> float:
    result = 1.0
    for value in values:
        result *= 1 + value
    return result - 1


def load_manual_overrides(path: Path | None, source_name: str) -> tuple[list[dict], dict[int, float], dict] | None:
    if not path or not path.exists():
        return None
    matched: list[dict] = []
    metadata: dict = {}
    reported: dict[int, float] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if not re.search(row["source_pattern"], source_name, flags=re.IGNORECASE):
                continue
            metadata = row
            year = int(row["year"])
            for month, key in enumerate(MONTHS, 1):
                value = parse_number(row[key])
                if value is not None:
                    matched.append({"year": year, "month": month, "ror": value / 100})
            annual = parse_number(row["year_return"])
            if annual is not None:
                reported[year] = annual / 100
    return (matched, reported, metadata) if matched else None


def process_source(path: Path, profiles: Sequence[Profile], args: argparse.Namespace) -> tuple[list[dict], list[dict], dict]:
    override = load_manual_overrides(args.manual_overrides, path.name)
    if path.suffix.lower() == ".pdf":
        text, tables, extraction_method = extract_pdf(
            path, args.ocr and not override, args.vision_script, args.vision_ocr
        )
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
        tables = []
        extraction_method = "plain_text"
    profile = match_profile(path, text, profiles)
    updated = report_date(path, text)
    aum, aum_currency = extract_aum(text)
    candidates: list[Candidate] = []
    if override:
        rows, reported, metadata = override
        selected = Candidate(rows, reported, 10_000, "manual_override")
        profile = Profile("", metadata["id"], metadata["fund_name"])
        updated = metadata["updated"] or updated
        aum = int(metadata["aum"]) if metadata["aum"] else aum
        aum_currency = metadata["aum_currency"] or aum_currency
    else:
        for index, table in enumerate(tables):
            candidate = table_candidate(table, f"pdf_table_{index}")
            if candidate:
                candidates.append(candidate)
        # Grid extraction preserves blank month cells and separates multiple
        # share classes. Plain-text parsing is a fallback for borderless tables.
        if not candidates:
            candidates.extend(text_candidates(text, updated))
        selected = sorted(candidates, key=lambda item: (-item.score, candidates.index(item)))[profile.table_index] if candidates else None
    if selected and not updated:
        latest = max(selected.rows, key=lambda row: (row["year"], row["month"]))
        updated = month_end(latest["year"], latest["month"])
    records: list[dict] = []
    qc: list[dict] = []
    if selected:
        for row in selected.rows:
            records.append(
                {
                    "id": profile.id,
                    "date": month_end(row["year"], row["month"]),
                    "ror": row["ror"],
                    "aum": aum,
                    "updated": updated,
                    "source_file": path.name,
                    "fund_name": profile.fund_name,
                    "aum_currency": aum_currency,
                    "extraction_method": selected.origin,
                }
            )
        for year, reported_value in selected.reported.items():
            values = [row["ror"] for row in selected.rows if row["year"] == year]
            compounded = compound(values)
            additive = sum(values)
            if re.search(r"(?i)non[- ]compounded", text) or (
                selected.origin == "manual_override"
                and abs(additive - reported_value) < abs(compounded - reported_value)
            ):
                calculated = additive
                calculation_method = "additive"
            else:
                calculated = compounded
                calculation_method = "compounded"
            difference = abs(calculated - reported_value)
            qc.append(
                {
                    "source_file": path.name,
                    "id": profile.id,
                    "year": year,
                    "months_found": len(values),
                    "calculated_year_ror": round(calculated, 6),
                    "reported_year_ror": round(reported_value, 6),
                    "difference": round(difference, 6),
                    "calculation_method": calculation_method,
                    "pass": difference < args.qc_tolerance,
                }
            )
    audit = {
        "source_file": path.name,
        "fund_name": profile.fund_name,
        "id": profile.id,
        "updated": updated,
        "aum": aum,
        "aum_currency": aum_currency,
        "pdf_extraction": extraction_method,
        "selected_table": selected.origin if selected else "none",
        "rows_extracted": len(records),
        "status": "ok" if records else "needs_review",
        "message": "" if records else "No monthly return table was extracted",
    }
    return records, qc, audit


def discover(inputs: Sequence[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in inputs:
        path = Path(raw).expanduser()
        if path.is_dir():
            paths.extend(item for item in path.rglob("*") if item.suffix.lower() in {".pdf", ".txt"})
        elif path.suffix.lower() in {".pdf", ".txt"}:
            paths.append(path)
    return sorted(dict.fromkeys(path.resolve() for path in paths))


def write_csv(path: Path, rows: Sequence[dict], fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    package = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "inputs",
        nargs="*",
        help="PDF/text files or folders (default: your Downloads folder)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output folder (default: ~/Downloads/fund_tracker_results)",
    )
    parser.add_argument("--profiles", type=Path, default=package / "fund_profiles.json")
    parser.add_argument("--manual-overrides", type=Path, default=package / "manual_overrides.csv")
    parser.add_argument("--vision-script", type=Path, default=package / "vision_ocr.swift")
    parser.add_argument("--vision-ocr", action="store_true", help="Use macOS Vision if Tesseract is unavailable")
    parser.add_argument("--no-ocr", action="store_false", dest="ocr")
    parser.add_argument("--qc-tolerance", type=float, default=0.01)
    args = parser.parse_args()

    downloads = Path.home() / "Downloads"
    if not args.inputs:
        args.inputs = [str(downloads)]
    if args.output_dir is None:
        args.output_dir = downloads / "fund_tracker_results"

    sources = discover(args.inputs)
    if not sources:
        parser.error("No PDF or text files were found")
    profiles = load_profiles(args.profiles)
    records: list[dict] = []
    quality: list[dict] = []
    audit: list[dict] = []
    for source in sources:
        try:
            source_records, source_quality, source_audit = process_source(source, profiles, args)
            records.extend(source_records)
            quality.extend(source_quality)
            audit.append(source_audit)
            print(f"{source.name}: {len(source_records)} rows ({source_audit['selected_table']})")
        except Exception as error:
            audit.append({"source_file": source.name, "status": "error", "message": str(error)})
            print(f"{source.name}: ERROR: {error}", file=sys.stderr)

    records.sort(key=lambda row: (row["id"], row["date"], row["updated"] or "", row["source_file"]))
    deduplicated: dict[tuple[str, str], dict] = {}
    for row in records:
        key = (row["id"], row["date"])
        if key not in deduplicated or (row["updated"] or "") >= (deduplicated[key]["updated"] or ""):
            deduplicated[key] = row
    final = sorted(deduplicated.values(), key=lambda row: (row["id"], row["date"]))
    for row in final:
        row["ror"] = f"{row['ror']:.10f}".rstrip("0").rstrip(".")
        row["aum"] = "" if row["aum"] is None else row["aum"]

    write_csv(args.output_dir / "tracker_output.csv", final, ["id", "date", "ror", "aum", "updated"])
    write_csv(
        args.output_dir / "tracker_quality_check.csv",
        quality,
        ["source_file", "id", "year", "months_found", "calculated_year_ror", "reported_year_ror", "difference", "calculation_method", "pass"],
    )
    write_csv(
        args.output_dir / "tracker_source_audit.csv",
        audit,
        ["source_file", "fund_name", "id", "updated", "aum", "aum_currency", "pdf_extraction", "selected_table", "rows_extracted", "status", "message"],
    )
    write_csv(
        args.output_dir / "tracker_debug_output.csv",
        final,
        ["id", "date", "ror", "aum", "updated", "source_file", "fund_name", "aum_currency", "extraction_method"],
    )
    failures = sum(row.get("status") != "ok" for row in audit)
    print(f"Wrote {len(final)} unique monthly rows from {len(sources)} sources; {failures} source(s) need review.")
    print(f"Results folder: {args.output_dir.resolve()}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
