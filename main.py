import re
import calendar
from pathlib import Path
from datetime import datetime, date

import pdfplumber
import pandas as pd
import numpy as np


PYTHAGORAS_FOLDER = Path("/Users/brain/Downloads/FW_ Crypto Arbitrage Strategy_ Update Return Request")

PDF_FILES = [
    PYTHAGORAS_FOLDER / "Pythagoras Quant Long Short Fund 28 February 2026 One Pager.pdf",
    PYTHAGORAS_FOLDER / "Pythagoras Alpha Long Biased Fund 28 February 2026 One Pager.pdf",
    PYTHAGORAS_FOLDER / "Pythagoras Arbitrage Fund 28 February 2026 One Pager.pdf",
    PYTHAGORAS_FOLDER / "Pythagoras Absolute Return Fund 28 February 2026 One Pager.pdf",
    Path("/Users/brain/Downloads/QMF_Report_May_2026.pdf"),
]

OUTPUT_FILE = "tracker_output.csv"
QC_FILE = "tracker_quality_check.csv"
DEBUG_FILE = "tracker_debug_output.csv"

FUND_ID_MAP = {
    "Pythagoras Quant Long Short Fund": "PYTH_QMLS",
    "Pythagoras Arbitrage Fund": "PYTH_ARB",
    "Pythagoras Alpha Long Biased Fund": "PYTH_PALB",
    "Pythagoras Absolute Return Fund": "PYTH_ABS",
    "Quintik Managed Futures Fund": "QMF",
}


def extract_text(pdf_path):
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text(x_tolerance=2, y_tolerance=3)
            if page_text:
                text += page_text + "\n"
    return text


def detect_fund_name(text, pdf_name):
    combined = f"{text} {pdf_name}".lower()

    if "quant long short" in combined or "quant long-short" in combined:
        return "Pythagoras Quant Long Short Fund"
    if "alpha long biased" in combined:
        return "Pythagoras Alpha Long Biased Fund"
    if "absolute return fund" in combined:
        return "Pythagoras Absolute Return Fund"
    if "arbitrage fund" in combined:
        return "Pythagoras Arbitrage Fund"
    if "quintik managed futures fund" in combined or "qmf" in combined:
        return "Quintik Managed Futures Fund"

    return "UNKNOWN"


def month_end(year, month):
    return date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def parse_percent(token):
    token = str(token)
    token = token.replace("%", "")
    token = token.replace("*", "")
    token = token.replace(",", "")
    token = token.strip()

    if token in ["", "-", "nan", "None"]:
        return None

    try:
        return float(token) / 100
    except ValueError:
        return None


def extract_fund_aum(text, fund_name):
    if fund_name == "Pythagoras Alpha Long Biased Fund":
        return 5_000_000

    match = re.search(
        r"\bFund AUM\s+\$?([\d,.]+)\s*(Million|M|Billion|B)?",
        text,
        re.IGNORECASE,
    )

    if not match:
        return None

    value = float(match.group(1).replace(",", ""))
    unit = match.group(2)

    if unit:
        unit = unit.lower()
        if unit in ["million", "m"]:
            value *= 1_000_000
        elif unit in ["billion", "b"]:
            value *= 1_000_000_000

    return int(value)


def extract_report_date(text, pdf_name):
    combined = text + " " + pdf_name

    match = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2})(?:st|nd|rd|th)?\s+(20\d{2})",
        combined,
        re.IGNORECASE,
    )

    if match:
        month = datetime.strptime(match.group(1).title(), "%B").month
        day = int(match.group(2))
        year = int(match.group(3))
        return date(year, month, day).isoformat()

    match = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})",
        combined,
        re.IGNORECASE,
    )

    if match:
        month = datetime.strptime(match.group(1).title(), "%B").month
        year = int(match.group(2))
        return month_end(year, month)

    return None


def is_header(line):
    upper = " ".join(line.upper().split())
    return "JAN FEB MAR APR" in upper and "DEC" in upper and "YEAR" in upper


def find_performance_tables(text):
    lines = [" ".join(line.strip().split()) for line in text.splitlines()]
    tables = []

    stop_words = [
        "All returns",
        "Performance Attribution",
        "Current Exposure",
        "Disclaimer",
        "Contact",
        "Manager Profiles",
        "Fund Terms",
        "Fund Term",
    ]

    for i, line in enumerate(lines):
        if not is_header(line):
            continue

        rows = []

        for next_line in lines[i + 1:]:
            if is_header(next_line):
                break

            if any(word.lower() in next_line.lower() for word in stop_words):
                break

            year_matches = list(re.finditer(r"\b20\d{2}\b", next_line))

            if not year_matches:
                continue

            for idx, match in enumerate(year_matches):
                start = match.start()
                end = year_matches[idx + 1].start() if idx + 1 < len(year_matches) else len(next_line)

                row_text = next_line[start:end]
                tokens = row_text.split()

                if not tokens:
                    continue

                year = tokens[0]

                if not re.match(r"^20\d{2}$", year):
                    continue

                nums = []

                for token in tokens[1:]:
                    value = parse_percent(token)
                    if value is not None:
                        nums.append(token)

                if len(nums) >= 2:
                    rows.append(" ".join([year] + nums))

        if rows:
            unique_rows = []
            seen = set()

            for row in rows:
                tokens = row.split()
                if not tokens:
                    continue

                year = tokens[0]
                nums = tokens[1:]

                if len(nums) > 13:
                    nums = nums[:13]

                cleaned_row = " ".join([year] + nums)

                if cleaned_row not in seen:
                    unique_rows.append(cleaned_row)
                    seen.add(cleaned_row)

            tables.append(unique_rows)

    return tables


def choose_primary_table(tables, fund_name):
    if not tables:
        return []

    if fund_name in [
        "Pythagoras Quant Long Short Fund",
        "Pythagoras Arbitrage Fund",
        "Pythagoras Alpha Long Biased Fund",
        "Pythagoras Absolute Return Fund",
    ]:
        return tables[0]

    return max(tables, key=len)


def infer_months(year, values, updated):
    if len(values) >= 13:
        monthly_values = values[:12]
        reported = values[12]
        return monthly_values, list(range(1, 13)), reported

    reported = values[-1]
    monthly_values = values[:-1]

    report_year = None
    report_month = None

    if updated:
        d = datetime.strptime(updated, "%Y-%m-%d").date()
        report_year = d.year
        report_month = d.month

    if report_year == year and report_month:
        month_numbers = list(range(1, 1 + len(monthly_values)))
    else:
        start_month = 13 - len(monthly_values)
        month_numbers = list(range(start_month, 13))

    return monthly_values, month_numbers, reported


def parse_table_rows(rows, static_id, aum, updated, source_file):
    records = []

    for row in rows:
        tokens = row.split()

        if not tokens:
            continue

        if not re.match(r"^20\d{2}$", tokens[0]):
            continue

        year = int(tokens[0])

        values = []
        for token in tokens[1:]:
            value = parse_percent(token)
            if value is not None:
                values.append(value)

        if not values:
            continue

        monthly_values, month_numbers, reported = infer_months(year, values, updated)

        for ror, month in zip(monthly_values, month_numbers):
            records.append({
                "id": static_id,
                "date": month_end(year, month),
                "ror": round(ror, 10),
                "aum": aum,
                "updated": updated,
                "source_file": source_file,
                "reported_year_ror": reported,
            })

    return records


def process_pdf(pdf_path):
    if not pdf_path.exists():
        print(f"SKIPPED missing file: {pdf_path}")
        return []

    print(f"Processing {pdf_path.name}")

    text = extract_text(pdf_path)
    fund_name = detect_fund_name(text, pdf_path.name)
    static_id = FUND_ID_MAP.get(fund_name, "UNKNOWN")
    aum = extract_fund_aum(text, fund_name)
    updated = extract_report_date(text, pdf_path.name)
    tables = find_performance_tables(text)
    primary_table = choose_primary_table(tables, fund_name)

    records = parse_table_rows(
        rows=primary_table,
        static_id=static_id,
        aum=aum,
        updated=updated,
        source_file=pdf_path.name,
    )

    print(f"  Fund: {fund_name}")
    print(f"  ID: {static_id}")
    print(f"  AUM: {aum}")
    print(f"  Updated: {updated}")
    print(f"  Tables found: {len(tables)}")
    print(f"  Rows extracted: {len(records)}")

    return records


def quality_check(df):
    results = []
    work = df.copy()
    work["year"] = pd.to_datetime(work["date"]).dt.year

    for (static_id, year), group in work.groupby(["id", "year"]):
        reported = group["reported_year_ror"].dropna()

        if reported.empty:
            continue

        monthly = group.sort_values("date")["ror"].tolist()
        calculated = np.prod([1 + r for r in monthly]) - 1
        reported_value = reported.iloc[0]
        diff = abs(calculated - reported_value)

        results.append({
            "id": static_id,
            "year": year,
            "months_found": len(monthly),
            "calculated_year_ror": round(calculated, 6),
            "reported_year_ror": round(reported_value, 6),
            "difference": round(diff, 6),
            "pass": diff < 0.01,
        })

    return pd.DataFrame(results)


def main():
    all_records = []

    print(f"Trying {len(PDF_FILES)} specific PDF files")

    for pdf_file in PDF_FILES:
        records = process_pdf(pdf_file)
        all_records.extend(records)

    df = pd.DataFrame(all_records)

    if df.empty:
        print("No data extracted.")
        return

    df = df.sort_values(["id", "date", "source_file"])
    df = df.drop_duplicates(subset=["id", "date"], keep="first")
    df = df.sort_values(["id", "date", "source_file"])
    boss_output = df[["id", "date", "ror", "aum", "updated"]].copy()
    boss_output.to_csv(OUTPUT_FILE, index=False)

    qc = quality_check(df)
    qc.to_csv(QC_FILE, index=False)

    df.to_csv(DEBUG_FILE, index=False)

    print(f"Saved {len(boss_output)} rows to {OUTPUT_FILE}")
    print(f"Saved quality check to {QC_FILE}")
    print(f"Saved debug file to {DEBUG_FILE}")
    print(qc)


if __name__ == "__main__":
    main()