"""Excel export: sheets of rows (built by the frontend from the screen's lines) → .xlsx bytes."""

import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

AMOUNT = "#,##0"
DECIMAL = "#,##0.0"


def build_workbook(sheets: list[dict]) -> bytes:
    """One worksheet per sheet. Row 0 is the header (bold, frozen); `bold`/`indent` are row indices."""
    wb = Workbook()
    wb.remove(wb.active)
    for sheet in sheets:
        ws = wb.create_sheet(sheet["name"])
        bold, indent = set(sheet.get("bold", [])), set(sheet.get("indent", []))
        for i, row in enumerate(sheet["rows"]):
            ws.append(row)
            for cell in ws[i + 1]:
                if isinstance(cell.value, float) and not cell.value.is_integer():
                    cell.number_format = DECIMAL
                elif isinstance(cell.value, int | float):
                    cell.number_format = AMOUNT
                if i == 0 or i in bold:
                    cell.font = Font(bold=True)
            if i in indent:
                ws.cell(i + 1, 1).alignment = Alignment(indent=1)
        ws.freeze_panes = "B2"
        ws.column_dimensions["A"].width = 38
        for col in range(2, ws.max_column + 1):
            ws.column_dimensions[ws.cell(1, col).column_letter].width = 16
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
