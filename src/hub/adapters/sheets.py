from __future__ import annotations

from typing import Any

from hub.adapters.base import ActionContext, Adapter, Registry


class SheetsAdapter(Adapter):
    """Spreadsheets the operations team reads: one collection per sheet, one record per row."""

    system = "sheets"
    rate_per_second = 1.0
    burst = 10

    def register(self, registry: Registry) -> None:
        self.action(
            registry,
            "append_row",
            self.append_row,
            side_effect=True,
            verify=self.verify_by_key("rows"),
        )
        self.action(registry, "find_rows", self.find_rows)

    def append_row(self, ctx: ActionContext, sheet: str, row: dict[str, Any]) -> dict[str, Any]:
        rec = {"sheet": sheet, "row": row}
        return self.store.put(
            ctx.session, self.system, "rows", self.store.new_id("row"), rec, ctx.idempotency_key
        )

    def find_rows(self, ctx: ActionContext, sheet: str) -> dict[str, Any]:
        rows = self.store.find(ctx.session, self.system, "rows", sheet=sheet)
        return {"rows": [r["row"] for r in rows], "count": len(rows)}
