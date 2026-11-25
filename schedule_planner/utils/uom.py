"""Units of measure on customer schedules.

A schedule row's `quantity` is ALWAYS kept in the item's Stock UOM, because the
forecast, safety checks and Material Request planning all work in stock units.
When a customer file states its own unit (e.g. "pcs", "M"), the quantity is
converted to Stock UOM when the schedule is created. The Sales Order UOM is ONE
field on the Customer Schedule (not per row), blank by default, set by the user.
When set, the Sales Order lines are made in that UOM, with the conversion taken from the
Item's UOM Conversion table. With no Sales Order UOM each line uses its item's
Sales UOM (or the Stock UOM when the item has none)."""

import frappe
from frappe.utils import flt

# a customer writing "pcs" / "each" means a counted piece; use the first of
# these that exists as a UOM on this site
_PIECE_WORDS = {"pc", "pcs", "piece", "pieces", "ea", "each", "no", "nos", "unit", "units"}
_PIECE_UOMS = ("Nos", "Pcs", "Piece", "Unit")


def resolve_uom(text):
	"""The UOM name for a unit printed in a customer file, or None. Never guesses
	beyond an exact (case-insensitive) UOM name and the piece words above."""
	text = (text or "").strip().rstrip(".")
	if not text:
		return None
	found = frappe.db.get_value("UOM", {"name": text}, "name")
	if found:
		return found
	if text.lower() in _PIECE_WORDS:
		for name in _PIECE_UOMS:
			if frappe.db.exists("UOM", name):
				return name
	return None


def stock_uom_factor(item_code, uom):
	"""Stock units in ONE `uom` of the item (1 for the Stock UOM itself), or None
	when the item has no conversion for that UOM."""
	stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
	if not uom or uom == stock_uom:
		return 1.0
	factor = flt(frappe.db.get_value("UOM Conversion Detail", {"parent": item_code, "uom": uom}, "conversion_factor"))
	return factor if factor > 0 else None


def normalise_file_units(rows):
	"""Rows as read from a customer file may carry `file_uom` (the unit text the
	file printed). Convert each such `quantity` to the item's Stock UOM. Stops with
	a clear message when the unit is unknown or the item has no conversion for it -
	a wrong quantity must never get through quietly. Run it AFTER item mapping, so
	`item_code` is the real Item.

	Returns the one unit every row of the file was in, or None (not used to fill the
	Sales Order UOM, which the user chooses)."""
	problems, units = [], set()
	for row in rows:
		text = row.pop("file_uom", None)
		if not text:
			units.add(None)
			continue
		uom = resolve_uom(text)
		if not uom:
			problems.append(f"{row['item_code']}: unit '{text}' is not a UOM on this site")
			continue
		factor = stock_uom_factor(row["item_code"], uom)
		if factor is None and text.strip().rstrip(".").lower() in _PIECE_WORDS:
			# "pcs" / "each" on a file, and the Item is counted in Nos / Pcs / Piece: the same unit
			stock_uom = frappe.db.get_value("Item", row["item_code"], "stock_uom")
			if stock_uom and stock_uom.lower() in {u.lower() for u in _PIECE_UOMS}:
				uom, factor = stock_uom, 1.0
		if factor is None:
			problems.append(
				f"{row['item_code']}: the file gives quantity in {uom}, but the Item has no UOM Conversion for {uom}"
			)
			continue
		row["quantity"] = flt(row["quantity"]) * factor
		units.add(uom)
	if problems:
		shown = sorted(set(problems))
		frappe.throw(
			"The unit of measure in the file cannot be applied:<br>"
			+ "<br>".join(shown[:15])
			+ ("<br>..." if len(shown) > 15 else "")
			+ "<br><br>Add the UOM / its conversion on the Item, then upload the file again.",
			title="Unit of Measure",
		)
	return next(iter(units)) if len(units) == 1 else None


def check_doc_uom(doc):
	"""Before submit: the schedule's Sales Order UOM must be convertible for every item."""
	uom = doc.get("sales_order_uom")
	if not uom:
		return
	bad = sorted(
		{r.item_code for r in doc.schedule_items if r.demand_type == "Firm" and stock_uom_factor(r.item_code, uom) is None}
	)
	if bad:
		frappe.throw(
			f"Sales Order UOM <b>{uom}</b> cannot be used: these items have no UOM Conversion for it:<br>"
			+ "<br>".join(bad[:15])
			+ ("<br>..." if len(bad) > 15 else "")
			+ "<br><br>Add the conversion on the Item, or clear Sales Order UOM to use each item's own Sales UOM.",
			title="Unit of Measure",
		)
