"""
Jane + Ads — ad strategy corpus ingestion (Jane Ads Playbook v1).

Reads the hand-seeded workbook (URI_Ad_Strategy_Corpus_Seed_v1.xlsx, Records tab)
and turns each row into a `Strategy`. The workbook is the source of truth: its
column order is load-bearing and its Lists tab supplies every controlled value,
so this maps by header name rather than position and fails loudly on an unknown
dropdown value instead of coercing it.

The hard rule — a row is only a record if every mandatory field is filled — is
enforced by the `Strategy` model itself. This module's job is to report which
rows failed and why, so a seeder can fix the sheet, rather than silently
importing 55 of 59 rows and leaving nobody the wiser.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

from .entities import Strategy, StrategyStatus
from .store import StrategyStore

# Records tab layout. The header lives on row 3 — rows 1–2 are the title banner
# and the pink/grey legend.
HEADER_ROW = 3

# Sheet header -> Strategy field. Keys must match the workbook exactly.
COLUMN_MAP: dict[str, str] = {
    "ID": "strategy_id",
    "Category": "category",
    "Claim": "claim",
    "Business Type": "business_type",
    "Budget Floor (₦/day)": "budget_floor_ngn_per_day",
    "Platform": "platform",
    "Funnel Stage": "funnel_stage",
    "Product Price Band (₦)": "product_price_band_ngn",
    "Sales Cycle": "sales_cycle",
    "Mechanism — why it works": "mechanism",
    "Evidence Grade": "evidence_grade",
    "Market Origin": "market_origin",
    "Transfer Verdict": "transfer_verdict",
    "Modification Required": "modification_required",
    "Source Type": "source_type",
    "Source Link": "source_link",
    "Source Date": "source_date",
    "Seeded By": "seeded_by",
    "Date Added": "date_added",
    "Status": "status",
    "Local Test Status": "local_test_status",
    "Local Result Notes": "local_result_notes",
    "Last Reviewed": "last_reviewed",
}


@dataclass
class RowError:
    row: int
    strategy_id: str
    reason: str


@dataclass
class ImportReport:
    """Every row is accounted for: imported + skipped_examples + len(errors)
    always equals the number of data rows read."""
    imported: int = 0
    skipped_examples: int = 0
    errors: list[RowError] = field(default_factory=list)

    @property
    def total_seen(self) -> int:
        return self.imported + self.skipped_examples + len(self.errors)

    def summary(self) -> str:
        return (
            f"{self.total_seen} rows read · {self.imported} imported · "
            f"{self.skipped_examples} EXAMPLE rows skipped · {len(self.errors)} rejected"
        )


def _clean(value: Any) -> Any:
    """Blank-ish spreadsheet cells ("", "  ", "None") all mean absent."""
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        return None if v in ("", "None", "-", "N/A") else v
    return value


def _coerce_budget(value: Any) -> Optional[float]:
    """Budget floors are ₦/day. Accepts the numeric cell openpyxl returns, and the
    "₦3,000" / "3,000" text a human might type instead."""
    v = _clean(value)
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    digits = str(v).replace("₦", "").replace(",", "").strip()
    try:
        return float(digits)
    except ValueError:
        raise ValueError(f"budget floor {value!r} is not a ₦/day number")


def row_to_strategy(row: dict[str, Any]) -> Strategy:
    """One sheet row -> one Strategy. Raises ValueError/ValidationError if the row
    is a note rather than a record."""
    data: dict[str, Any] = {}
    for header, field_name in COLUMN_MAP.items():
        raw = row.get(header)
        if field_name == "budget_floor_ngn_per_day":
            data[field_name] = _coerce_budget(raw)
        else:
            data[field_name] = _clean(raw)
    return Strategy(**data)


def _reason(exc: Exception) -> str:
    """Pydantic puts the useful part on the second line ("Value error, ..."), so the
    first line alone ("1 validation error for Strategy") tells a seeder nothing."""
    lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith("Value error,"):
            return ln.split("[type=")[0].strip()
    for ln in lines[1:]:
        if not ln.startswith("For further information"):
            return ln.split("[type=")[0].strip()
    return lines[0] if lines else str(exc)


async def import_rows(
    rows: Iterable[dict[str, Any]], store: StrategyStore
) -> ImportReport:
    """Import already-parsed rows. Kept separate from the file reader so the same
    path is exercised by tests without an .xlsx on disk."""
    report = ImportReport()
    for offset, row in enumerate(rows):
        sheet_row = HEADER_ROW + 1 + offset
        raw_id = str(_clean(row.get("ID")) or "")
        if not raw_id:
            continue  # trailing blank rows in a 1000-row sheet
        try:
            strategy = row_to_strategy(row)
        except Exception as exc:                      # noqa: BLE001 — reported, not raised
            report.errors.append(RowError(sheet_row, raw_id, _reason(exc)))
            continue
        if not strategy.is_ingestible:
            report.skipped_examples += 1
            continue
        await store.upsert_strategy(strategy)
        report.imported += 1
    return report


def read_records_sheet(path: str, sheet_name: str = "Records") -> list[dict[str, Any]]:
    """Read the Records tab into header-keyed dicts. openpyxl is an import-time
    dependency of this function only, so the corpus module stays importable in
    environments that never run ingestion."""
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet_name]
    headers = [ws.cell(HEADER_ROW, i).value for i in range(1, ws.max_column + 1)]
    unknown = [h for h in headers if h and h not in COLUMN_MAP]
    if unknown:
        raise ValueError(
            f"unrecognised column(s) {unknown} — the sheet changed shape; update COLUMN_MAP"
        )
    rows: list[dict[str, Any]] = []
    for r in range(HEADER_ROW + 1, ws.max_row + 1):
        row = {h: ws.cell(r, i + 1).value for i, h in enumerate(headers) if h}
        if any(v not in (None, "") for v in row.values()):
            rows.append(row)
    return rows
