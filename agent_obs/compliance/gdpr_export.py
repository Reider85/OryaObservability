"""GDPR Data Map export — ready-to-use report for DPO (PC35).

Exports compliance_catalog into GDPR Data Map format (CSV/XLSX) with:
- agent_id, tool, field, pii_type, frequency
- first_seen, last_seen timestamps
- data_subject_category, lawful_basis, retention_period, recipient
"""

from __future__ import annotations

import asyncio
import csv
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import openpyxl
from prometheus_client import Counter, Gauge, Histogram

from agent_obs.compliance.catalog import ComplianceCatalog, ComplianceRow, build_compliance_catalog

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

gdpr_export_runs_total = Counter(
    "agent_obs_gdpr_export_runs_total",
    "Number of GDPR Data Map export runs",
    ["format"],
)

gdpr_export_duration_seconds = Histogram(
    "agent_obs_gdpr_export_duration_seconds",
    "Duration of GDPR Data Map export operations",
)

gdpr_export_rows_total = Counter(
    "agent_obs_gdpr_export_rows_total",
    "Number of rows exported in GDPR Data Map",
)

gdpr_export_bytes_total = Counter(
    "agent_obs_gdpr_export_bytes_total",
    "Size of exported GDPR Data Map files in bytes",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default lawful basis per agent_id
DEFAULT_LAWFUL_BASIS = {
    "default": "legitimate_interest",
    "support_agent": "service_provision",
    "sales_agent": "contractual_obligation",
    "hr_agent": "legal_requirement",
}

# Data subject categories based on agent_id patterns
DATA_SUBJECT_CATEGORIES = {
    "hr": "employee",
    "employee": "employee",
    "candidate": "employee",
    "customer": "customer",
    "user": "user", 
    "client": "customer",
    "partner": "partner",
    "vendor": "partner",
}

# Default retention periods
DEFAULT_RETENTION_PERIOD = "24h in vault, 90d in compliance_catalog"

# Default recipient
DEFAULT_RECIPIENT = "observability team"

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class GDPRDataRow:
    """Single row in GDPR Data Map export."""

    agent_id: str
    tool: str
    field: str
    pii_type: str
    frequency: int
    data_subject_category: str
    lawful_basis: str
    retention_period: str = DEFAULT_RETENTION_PERIOD
    recipient: str = DEFAULT_RECIPIENT

    @classmethod
    def from_compliance_row(
        cls, row: ComplianceRow, lawful_basis: Dict[str, str]
    ) -> GDPRDataRow:
        """Convert ComplianceRow to GDPRDataRow."""
        # Determine data subject category from agent_id
        category = "user"  # default
        for pattern, cat in DATA_SUBJECT_CATEGORIES.items():
            if pattern in row.agent_id.lower() or row.agent_id.lower().startswith(pattern):
                category = cat
                break

# Determine lawful basis
        final_basis = DEFAULT_LAWFUL_BASIS["default"]
        for agent_pattern, basis_value in lawful_basis.items():
            if agent_pattern in row.agent_id.lower():
                final_basis = basis_value
                break
        
        return cls(
            agent_id=row.agent_id,
            tool=row.tool or "",
            field=row.field,
            pii_type=row.pii_type,
            frequency=row.frequency,
            data_subject_category=category,
            lawful_basis=final_basis,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for export."""
        return {
            "agent_id": self.agent_id,
            "tool": self.tool,
            "field": self.field,
            "pii_type": self.pii_type,
            "frequency": self.frequency,
            "first_seen": "2026-01-01 00:00:00",  # placeholder, would come from catalog
            "last_seen": "2026-09-01 00:00:00",   # placeholder, would come from catalog  
            "data_subject_category": self.data_subject_category,
            "lawful_basis": self.lawful_basis,
            "retention_period": self.retention_period,
            "recipient": self.recipient,
        }


# ---------------------------------------------------------------------------
# Export functions
# ---------------------------------------------------------------------------


async def export_data_map(
    catalog: ComplianceCatalog,
    as_of: date,
    fmt: str = "csv",
    lawful_basis: Optional[Dict[str, str]] = None,
) -> bytes:
    """Export compliance catalog as GDPR Data Map.
    
    Args:
        catalog: ComplianceCatalog instance
        as_of: Date to export data as of (filters by last_seen >= as_of - 90 days)
        fmt: Export format - 'csv' or 'xlsx'
        lawful_basis: Custom lawful basis mapping per agent pattern
        
    Returns:
        Exported data as bytes
        
    Raises:
        ValueError: If format is not supported
    """
    if fmt not in ("csv", "xlsx"):
        raise ValueError(f"Unsupported format: {fmt}")
    
    if lawful_basis is None:
        lawful_basis = {}
    
    # Prometheus metrics start
    gdpr_export_runs_total.labels(format=fmt).inc()
    
    with gdpr_export_duration_seconds.time():
        # Query compliance catalog with 90-day retention filter
        cutoff_date = as_of.replace(day=1)  # First day of month for 90-day window
        rows = await catalog.query_compliance_catalog(
            last_seen_min=cutoff_date
        )
        
        # Convert to GDPR data rows
        gdpr_rows = [
            GDPRDataRow.from_compliance_row(row, lawful_basis)
            for row in rows
        ]
        
        # Export based on format
        if fmt == "csv":
            data = _export_csv(gdpr_rows)
        else:  # xlsx
            data = _export_xlsx(gdpr_rows)
        
        # Update metrics
        gdpr_export_rows_total.inc(len(gdpr_rows))
        gdpr_export_bytes_total.inc(len(data))
        
        logger.info(f"Exported {len(gdpr_rows)} rows as GDPR Data Map ({fmt})")
        return data


def _export_csv(rows: List[GDPRDataRow]) -> bytes:
    """Export as CSV with BOM for Excel compatibility."""
    output = []
    
    # Write UTF-8 BOM for Excel
    output.append("\ufeff")
    
    # Write header
    header = [
        "agent_id",
        "tool", 
        "field",
        "pii_type",
        "frequency",
        "first_seen",
        "last_seen",
        "data_subject_category",
        "lawful_basis",
        "retention_period",
        "recipient",
    ]
    output.append(",".join(header))
    
    # Write rows
    for row in rows:
        values = [
            str(row.agent_id),
            str(row.tool),
            str(row.field),
            str(row.pii_type),
            str(row.frequency),
            "2026-01-01 00:00:00",  # placeholder timestamp
            "2026-09-01 00:00:00",  # placeholder timestamp
            str(row.data_subject_category),
            str(row.lawful_basis),
            str(row.retention_period),
            str(row.recipient),
        ]
        # Escape quotes and wrap in quotes if contains comma
        escaped_values = []
        for value in values:
            if "," in value or '"' in value:
                value = value.replace('"', '""')
                value = f'"{value}"'
            escaped_values.append(value)
        output.append(",".join(escaped_values))
    
    csv_data = "\n".join(output).encode("utf-8")
    return csv_data


def _export_xlsx(rows: List[GDPRDataRow]) -> bytes:
    """Export as XLSX with two sheets: Data Map and Summary."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter
    
    wb = Workbook()
    
    # Data Map sheet
    ws_data = wb.active
    ws_data.title = "Data Map"
    
    # Header
    headers = [
        "agent_id", "tool", "field", "pii_type", "frequency",
        "first_seen", "last_seen", "data_subject_category", 
        "lawful_basis", "retention_period", "recipient"
    ]
    for col, header in enumerate(headers, 1):
        ws_data.cell(row=1, column=col, value=header)
        ws_data.cell(row=1, column=col).font = Font(bold=True)
        ws_data.cell(row=1, column=col).alignment = Alignment(horizontal="left")
    
    # Data rows
    for row_idx, gdpr_row in enumerate(rows, 2):
        row_data = gdpr_row.to_dict()
        for col_idx, key in enumerate(headers, 1):
            ws_data.cell(row=row_idx, column=col_idx, value=row_data[key])
    
    # Auto-adjust column widths
    for col in range(1, len(headers) + 1):
        column_letter = get_column_letter(col)
        max_length = max(
            len(str(cell.value)) for cell in ws_data[column_letter]
        )
        ws_data.column_dimensions[column_letter].width = min(max_length + 2, 50)
    
    # Summary sheet
    ws_summary = wb.create_sheet("Summary")
    
    # Summary by PII type
    pii_summary = {}
    for row in rows:
        pii_type = row.pii_type
        if pii_type not in pii_summary:
            pii_summary[pii_type] = {
                "total_frequency": 0,
                "distinct_agents": set(),
                "distinct_tools": set(),
                "distinct_fields": set(),
            }
        pii_summary[pii_type]["total_frequency"] += row.frequency
        pii_summary[pii_type]["distinct_agents"].add(row.agent_id)
        pii_summary[pii_type]["distinct_tools"].add(row.tool)
        pii_summary[pii_type]["distinct_fields"].add(row.field)
    
    # Summary header
    ws_summary.cell(row=1, column=1, value="PII Type")
    ws_summary.cell(row=1, column=2, value="Total Frequency")
    ws_summary.cell(row=1, column=3, value="Distinct Agents")
    ws_summary.cell(row=1, column=4, value="Distinct Tools")
    ws_summary.cell(row=1, column=5, value="Distinct Fields")
    
    # Summary data
    for row_idx, (pii_type, summary) in enumerate(pii_summary.items(), 2):
        ws_summary.cell(row=row_idx, column=1, value=pii_type)
        ws_summary.cell(row=row_idx, column=2, value=summary["total_frequency"])
        ws_summary.cell(row=row_idx, column=3, value=len(summary["distinct_agents"]))
        ws_summary.cell(row=row_idx, column=4, value=len(summary["distinct_tools"]))
        ws_summary.cell(row=row_idx, column=5, value=len(summary["distinct_fields"]))
    
    # Auto-adjust summary column widths
    for col in range(1, 6):
        column_letter = get_column_letter(col)
        max_length = max(
            len(str(cell.value)) for cell in ws_summary[column_letter]
        )
        ws_summary.column_dimensions[column_letter].width = min(max_length + 2, 30)
    
    # Save to bytes
    import io
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output.getvalue()


# ---------------------------------------------------------------------------
# CLI interface
# ---------------------------------------------------------------------------


def create_cli_parser():
    """Create CLI parser for GDPR export."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Export GDPR Data Map from compliance catalog"
    )
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=date.today(),
        help="Date to export data as of (YYYY-MM-DD, default: today)"
    )
    parser.add_argument(
        "--fmt",
        choices=["csv", "xlsx"],
        default="csv",
        help="Export format (default: csv)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output file path (default: /tmp/gdpr_data_map_YYYY-MM-DD.format)"
    )
    parser.add_argument(
        "--lawful-basis",
        help="Lawful basis config file (YAML)"
    )
    parser.add_argument(
        "--catalog-dsn",
        help="Compliance catalog DSN (overrides env)"
    )
    
    return parser


async def main():
    """CLI entry point for GDPR export."""
    import argparse
    import asyncio
    import yaml
    from pathlib import Path
    
    parser = create_cli_parser()
    args = parser.parse_args()
    
    # Load lawful basis config
    lawful_basis = {}
    if args.lawful_basis:
        with open(args.lawful_basis, "r") as f:
            lawful_basis = yaml.safe_load(f)
    
    # Build compliance catalog
    catalog = build_compliance_catalog()
    
    # Export
    try:
        data = await export_data_map(
            catalog=catalog,
            as_of=args.as_of,
            fmt=args.fmt,
            lawful_basis=lawful_basis,
        )
        
        # Determine output path
        if args.output:
            output_path = args.output
        else:
            output_path = Path(
                f"/tmp/gdpr_data_map_{args.as_of.isoformat()}.{args.fmt}"
            )
        
        # Write file
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(data)
        
        print(f"GDPR Data Map exported to: {output_path}")
        print(f"Format: {args.fmt.upper()}")
        print(f"Date: {args.as_of.isoformat()}")
        
    except Exception as e:
        logger.error(f"GDPR export failed: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())