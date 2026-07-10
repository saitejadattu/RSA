"""
generate_import_commands.py

Production-ready generator for:
- run_imports.bat
- run_imports_dry_run.bat
- company-sheet-downloader/missing_files.txt

Adjust if your companies.txt format differs.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
DOWNLOADER = ROOT / "company-sheet-downloader"
DATA_DIR = DOWNLOADER / "data"
COMPANIES = DOWNLOADER / "companies.txt"
FAILED_GENERATION = DOWNLOADER / "failed_command_generation.txt"
RUN_BAT = ROOT / "run_imports.bat"
RUN_DRY = ROOT / "run_imports_dry_run.bat"
MISSING = DOWNLOADER / "missing_files.txt"

DATE_FORMATS = (
    "%d-%b-%Y",
    "%d %b %Y",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
)

def normalize_header(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\n", " ")).strip().lower()

def parse_date(value: str):
    value = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError(f"Unsupported date: {value}")

def batch_cmd(script, company, received, flag, file_path, dry=False):
    cmd = [
        f'python {script} ^',
        f'--company-name "{company}" ^',
        f'--received-on "{received}" ^',
        f'--{flag} "{file_path.as_posix()}"'
    ]
    if dry:
        cmd[-1] += " ^"
        cmd.append("--dry-run")
    return "\n".join(cmd)

def main():
    if not COMPANIES.exists():
        raise FileNotFoundError(COMPANIES)

    with COMPANIES.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        reader.fieldnames = [normalize_header(h) for h in reader.fieldnames]

        rows = list(reader)

    resp_count = short_count = miss_r = miss_s = skipped = 0
    skipped_companies = []
    failed_dates = []
    seen = set()

    run_lines = ["@echo off", ""]
    dry_lines = ["@echo off", ""]
    missing = []

    for row in rows:
        company = row.get("company name", "").strip()
        received = row.get("opportunity received on", "").strip()

        if not company or not received:
            skipped += 1
            skipped_companies.append(
                f"Missing Company Name or Date -> Company='{company}' Date='{received}'"
            )
            continue

        key = (company, received)
        if key in seen:
            continue
        seen.add(key)

        try:
            iso = parse_date(received).strftime("%Y-%m-%d")
        except Exception as e:
            skipped += 1
            failed_dates.append(
                f"{company} | {received} | Invalid Date | {e}"
            )
            continue

        base = f"{iso}_{company}"
        resp = DATA_DIR / f"{base}_responses.txt"
        short = DATA_DIR / f"{base}_shortlists.txt"

        if resp.exists():
            run_lines += [f'echo Importing: {company}',
                          batch_cmd("scripts/import_company_response.py", company, received, "response-sheet", resp),
                          ""]
            dry_lines += [f'echo Importing: {company}',
                          batch_cmd("scripts/import_company_response.py", company, received, "response-sheet", resp, True),
                          ""]
            resp_count += 1
        else:
            missing.append(f"{company} | {received} | Response Missing")
            miss_r += 1

        if short.exists():
            run_lines += [f'echo Importing: {company}',
                          batch_cmd("scripts/import_company_shortlist.py", company, received, "shortlist-sheet", short),
                          ""]
            dry_lines += [f'echo Importing: {company}',
                          batch_cmd("scripts/import_company_shortlist.py", company, received, "shortlist-sheet", short, True),
                          ""]
            short_count += 1
        else:
            missing.append(f"{company} | {received} | Shortlist Missing")
            miss_s += 1

    run_lines += ["echo.", "echo Import completed.", "pause"]
    dry_lines += ["echo.", "echo Dry-run completed.", "pause"]

    RUN_BAT.write_text("\n".join(run_lines), encoding="utf-8")
    RUN_DRY.write_text("\n".join(dry_lines), encoding="utf-8")
    MISSING.write_text("\n".join(missing), encoding="utf-8")
    report = []

    if skipped_companies:
        report.append("===== MISSING COMPANY / DATE =====")
        report.extend(skipped_companies)
        report.append("")

    if failed_dates:
        report.append("===== INVALID DATES =====")
        report.extend(failed_dates)
        report.append("")

    if missing:
        report.append("===== MISSING FILES =====")
        report.extend(missing)

    FAILED_GENERATION.write_text(
        "\n".join(report),
        encoding="utf-8",
    )
    print("=" * 60)
    print("Import Command Generation Summary")
    print("=" * 60)

    print(f"Companies Read             : {len(rows)}")
    print(f"Rows Skipped               : {skipped}")

    print(f"Response Commands Created  : {resp_count}")
    print(f"Shortlist Commands Created : {short_count}")

    print(f"Missing Response Files     : {miss_r}")
    print(f"Missing Shortlist Files    : {miss_s}")

    print()

    if skipped_companies:
        print("Skipped Companies")
        for x in skipped_companies:
            print("  -", x)

    if failed_dates:
        print("\nInvalid Dates")
        for x in failed_dates:
            print("  -", x)

    print()

    print("Reports Generated")
    print(f"  {RUN_BAT.name}")
    print(f"  {RUN_DRY.name}")
    print(f"  {MISSING.name}")
    print(f"  {FAILED_GENERATION.name}")

    print("=" * 60)

if __name__ == "__main__":
    main()
