# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

"""Single, unified auto-forecast engine (there used to be two divergent
copies of this logic in the original prototype - one in the upload flow,
one in the post-creation "extend forecast" flow - with different method
names and different math. This module is the only implementation now, used
by both the initial upload dialog and any later top-up.

Exactly two methods, per the client's requirement:
  - LAST_4_WEEKS_AVG            : average of the item's last 4 known weeks
  - LAST_3_MONTHS_SHIPPING_AVG  : average weekly qty actually *shipped*
                                   (Delivery Note) in the last 3 months,
                                   falling back to the file's own last-3-month
                                   figures only if there is no shipping
                                   history yet (e.g. a brand new item/customer)

Forecast is only generated for items the caller selects (selected_items).
An item close to being discontinued can simply be left unchecked so it does
not get 7 months of fabricated future demand.
"""

from collections import defaultdict
from datetime import timedelta

import frappe
from frappe.utils import add_months, flt, getdate, today
from dateutil.relativedelta import relativedelta

METHODS = ("LAST_4_WEEKS_AVG", "LAST_3_MONTHS_SHIPPING_AVG")

METHOD_LABELS = {
	"LAST_4_WEEKS_AVG": "Average of Last 4 Weeks",
	"LAST_3_MONTHS_SHIPPING_AVG": "Average Weekly Shipment - Last 3 Months",
}


def validate_method(method):
	method = (method or "").strip().upper()
	if method not in METHODS:
		frappe.throw(f"Unsupported forecast method: {method}. Use one of {', '.join(METHODS)}.")
	return method


# ---------------------------------------------------------------------------
# Per-item weekly value
# ---------------------------------------------------------------------------

def _history_for_averaging(item_rows):
	"""Customer-supplied Forecast/Raw rows preferred; Firm rows only used as
	a last resort if the file had nothing else for this item."""
	history = [r for r in item_rows if r.get("demand_type") in ("Forecast", "Raw")]
	return history or list(item_rows)


def last_4_week_avg(item_rows):
	history = sorted(_history_for_averaging(item_rows), key=lambda x: x["delivery_date"])
	last_4 = history[-4:]
	if not last_4:
		return 0, []
	values = [flt(r["quantity"]) for r in last_4]
	return round(sum(values) / len(values), 2), values


def last_3_month_shipping_avg(item_code, item_rows):
	today_date = getdate(today())
	three_months_ago = add_months(today_date, -3)

	shipped = frappe.db.sql(
		"""
		SELECT SUM(dni.qty) AS total_qty
		FROM `tabDelivery Note Item` dni
		INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
		WHERE dni.item_code = %(item_code)s
		  AND dn.docstatus = 1
		  AND dn.posting_date >= %(from_date)s
		  AND dn.posting_date <= %(today)s
		""",
		{"item_code": item_code, "from_date": str(three_months_ago), "today": str(today_date)},
		as_dict=True,
	)
	total_shipped = flt(shipped[0].total_qty) if shipped and shipped[0].total_qty else 0

	if total_shipped > 0:
		# ~13 weeks in 3 months
		return round(total_shipped / 13.0, 2), [round(total_shipped / 13.0, 2)]

	# Fallback: no shipping history yet (new item/customer) - use the file's
	# own last-3-calendar-month figures instead.
	history = _history_for_averaging(item_rows)
	cutoff = add_months(today_date, -3)
	recent = [r for r in history if getdate(r["delivery_date"]) >= cutoff] or history
	if not recent:
		return 0, []
	values = [flt(r["quantity"]) for r in recent]
	return round(sum(values) / len(values), 2), values[-4:]


def _weekly_value(method, item_code, item_rows):
	if method == "LAST_4_WEEKS_AVG":
		return last_4_week_avg(item_rows)
	return last_3_month_shipping_avg(item_code, item_rows)


# ---------------------------------------------------------------------------
# Preview (used live in the dialog whenever the method dropdown changes)
# ---------------------------------------------------------------------------

def build_preview(rows, method):
	method = validate_method(method)

	by_item = defaultdict(list)
	for row in rows:
		by_item[row["item_code"]].append(row)

	preview = []
	for item_code, item_rows in by_item.items():
		item_rows = sorted(item_rows, key=lambda x: x["delivery_date"])
		value, sample = _weekly_value(method, item_code, item_rows)
		preview.append(
			{
				"item_code": item_code,
				"method": method,
				"description": METHOD_LABELS[method],
				"predicted_value": value,
				"sample_values": sample,
			}
		)
	return preview


# ---------------------------------------------------------------------------
# Extension - generates the actual future weekly rows
# ---------------------------------------------------------------------------

def extend_rows(rows, target_months, method, selected_items=None):
	"""rows: list of row dicts (item_code, item_name, delivery_date, quantity,
	demand_type[, auto_forecast]). Returns a NEW list (original + generated).

	target_months is measured per item from that item's own first delivery
	date to its last, then extended forward - so items with a shorter
	history than others are each topped up fairly, rather than everyone
	being extended to one shared calendar end-date."""

	method = validate_method(method)
	target_months = int(target_months)

	by_item = defaultdict(list)
	for row in rows:
		by_item[row["item_code"]].append(row)

	extended = list(rows)

	for item_code, item_rows in by_item.items():
		if selected_items is not None and item_code not in selected_items:
			continue

		item_sorted = sorted(item_rows, key=lambda x: x["delivery_date"])
		months_covered = len({getdate(r["delivery_date"]).strftime("%Y-%m") for r in item_sorted})
		months_to_add = target_months - months_covered
		if months_to_add <= 0:
			continue

		last_date = getdate(item_sorted[-1]["delivery_date"])
		item_end = last_date + relativedelta(months=months_to_add)

		weekly_qty, _ = _weekly_value(method, item_code, item_sorted)
		if not weekly_qty or weekly_qty <= 0:
			continue

		existing_dates = {str(getdate(r["delivery_date"])) for r in item_sorted}
		item_name = item_sorted[0].get("item_name", item_code)

		next_date = last_date + timedelta(days=7)
		while next_date <= item_end:
			date_key = str(next_date)
			if date_key not in existing_dates:
				extended.append(
					{
						"item_code": item_code,
						"item_name": item_name,
						"delivery_date": date_key,
						"quantity": round(weekly_qty, 2),
						"demand_type": "Forecast",
						"auto_forecast": 1,
					}
				)
				existing_dates.add(date_key)
			next_date += timedelta(days=7)

	extended.sort(key=lambda x: (x["item_code"], x["delivery_date"]))
	return extended
