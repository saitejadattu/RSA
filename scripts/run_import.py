"""One-command importer: master sheet + all responses + all shortlists.

Sheets are private (can't be auto-fetched), so you export each as a local .txt (tab-separated)
into backend/data/, then list them in a manifest. This runs every import in the right order,
resolving each file to its master opportunity — never creating phantom opportunities.

Manifest (data/manifest.tsv), tab- or comma-separated, one row per response/shortlist file:

    type        file                          company              received_on   role
    response    responses/New Age AI.txt      New Age              30-Jun-2026   AI Intern
    response    responses/Revocept.txt        Revocept Solutions
    shortlist   shortlists/New Age AI.txt     New Age              30-Jun-2026   AI Intern

- received_on / role are OPTIONAL: leave blank when the company has a single opening.
- Lines starting with # and blank lines are ignored.
- Paths are relative to backend/data/ (or absolute).

    python scripts/run_import.py --master companies.txt --manifest data/manifest.tsv --dry-run
    python scripts/run_import.py --master companies.txt --manifest data/manifest.tsv
"""
import argparse
import asyncio
import csv
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parent
sys.path.append(str(SCRIPTS))
sys.path.append(str(SCRIPTS.parent))

import import_company_master as master_importer
import import_company_response as response_importer
import import_company_shortlist as shortlist_importer

DATA_DIR = SCRIPTS.parent / "data"


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (DATA_DIR / value)


def read_manifest(path: Path) -> list[dict[str, str]]:
    raw = path.read_text(encoding="utf-8-sig")
    lines = [line for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        return []
    delimiter = "\t" if "\t" in lines[0] else ","
    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    rows = []
    for row in reader:
        norm = {(k or "").strip().lower(): (v.strip() if v else "") for k, v in row.items() if k}
        if norm.get("file"):
            rows.append(norm)
    return rows


async def run(master: str | None, manifest_path: str, dry_run: bool) -> dict:
    results: dict = {"mode": "dry_run" if dry_run else "apply", "master": None, "responses": [], "shortlists": []}

    if master:
        results["master"] = await master_importer.import_company_master(str(resolve_path(master)), dry_run)

    rows = read_manifest(Path(manifest_path))
    # responses before shortlists (shortlist merges onto an existing application when present)
    rows.sort(key=lambda r: 0 if r.get("type", "response").lower() != "shortlist" else 1)

    for row in rows:
        kind = row.get("type", "response").lower()
        label = f"{row['company']} [{row.get('received_on') or 'single'}{'/' + row['role'] if row.get('role') else ''}]"
        args = SimpleNamespace(
            company_name=row["company"],
            received_on=row.get("received_on") or None,
            role=row.get("role") or None,
            dry_run=dry_run,
        )
        bucket = results["shortlists"] if kind == "shortlist" else results["responses"]
        try:
            if kind == "shortlist":
                args.shortlist_sheet = str(resolve_path(row["file"]))
                bucket.append({"label": label, **await shortlist_importer.import_shortlist(args)})
            else:
                args.response_sheet = str(resolve_path(row["file"]))
                bucket.append({"label": label, **await response_importer.import_response_sheet(args)})
        except Exception as exc:  # keep going; report per-file failures
            bucket.append({"label": label, "file": row["file"], "error": str(exc)})

    return results


def summarize(results: dict) -> None:
    print(json.dumps(results, default=str, indent=2))
    errors = [r for r in results["responses"] + results["shortlists"] if r.get("error")]
    print(f"\n{'DRY RUN' if results['mode'] == 'dry_run' else 'APPLIED'}: "
          f"{len(results['responses'])} response file(s), {len(results['shortlists'])} shortlist file(s), "
          f"{len(errors)} error(s).")
    for e in errors:
        print(f"  ERROR [{e['label']}]: {e['error']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch import master + responses + shortlists from a manifest.")
    parser.add_argument("--master", default=None, help="Master sheet file (relative to data/ or absolute). Optional if already imported.")
    parser.add_argument("--manifest", required=True, help="Manifest TSV/CSV listing response & shortlist files.")
    parser.add_argument("--dry-run", action="store_true", help="Validate mappings and parse without writing.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    summarize(asyncio.run(run(args.master, args.manifest, args.dry_run)))
