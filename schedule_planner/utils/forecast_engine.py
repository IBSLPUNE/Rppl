# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

"""Single, unified auto-forecast engine (there used to be two divergent
copies of this logic in the original prototype - one in the upload flow,
one in the post-creation "extend forecast" flow - with different method
names and different math. This module is the only implementation now, used
by both the initial upload dialog and any later top-up.

Methods, per the client's requirement:
  - LAST_4_WEEKS_AVG            : the file's last 4 calendar weeks added up and
                                   divided by 4 - a week with no quantity counts
                                   as 0 (100, 0, 400, 0 -> 125). The 4 weeks end
                                   where the customer's file ends, not at the
                                   last non-zero week.
  - LAST_3/6/9_MONTHS_SHIPPING_AVG : average weekly qty actually *shipped*
                                   (Delivery Note) in the last 3 / 6 / 9 months
                                   (13 / 26 / 39 weeks), falling back to the
                                   last-4-weeks figure only if there is no
                                   shipping history yet (a brand new item)

Forecast is only generated for items the caller selects (selected_items).
An item close to being discontinued can simply be left unchecked so it does
not get 7 months of fabricated future demand.
"""

from collections import defaultdict
from datetime import timedelta

import frappe
from frappe.utils import add_months, flt, getdate, today
from dateutil.relativedelta import relativedelta

# months of Delivery Note history behind each shipping method
SHIPPING_MONTHS = {
	"LAST_3_MONTHS_SHIPPING_AVG": 3,
	"LAST_6_MONTHS_SHIPPING_AVG": 6,
	"LAST_9_MONTHS_SHIPPING_AVG": 9,
}

METHODS = ("LAST_4_WEEKS_AVG", *SHIPPING_MONTHS)

METHOD_LABELS = {
	"LAST_4_WEEKS_AVG": "Average of Last 4 Weeks",
	"LAST_3_MONTHS_SHIPPING_AVG": "Average Weekly Shipment - Last 3 Months",
	"LAST_6_MONTHS_SHIPPING_AVG": "Average Weekly Shipment - Last 6 Months",
	"LAST_9_MONTHS_SHIPPING_AVG": "Average Weekly Shipment - Last 9 Months",
}


LABEL_TO_METHOD = {label: code for code, label in METHOD_LABELS.items()}


def method_from_label(label):
	"""Method code from what is stored on a schedule row (its label)."""
	return LABEL_TO_METHOD.get((label or "").strip())


def method_for(item_code, default, item_methods=None):
	"""The method used for one item: its own choice, else the default."""
	chosen = (item_methods or {}).get(item_code) or default
	return validate_method(chosen)


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
	a last resort if the file had nothing else for this item. Rows this
	system generated itself (auto_forecast) never feed an average."""
	own = [r for r in item_rows if not r.get("auto_forecast")] or list(item_rows)
	history = [r for r in own if r.get("demand_type") in ("Forecast", "Raw")]
	return history or own


def _horizon_end(item_rows):
	"""Last date the customer's file covers (zero weeks included), when the
	reader recorded it; None otherwise."""
	ends = [getdate(r["horizon_end"]) for r in item_rows if r.get("horizon_end")]
	return max(ends) if ends else None


# a bucket this many days wide or more is a "month" in a mixed daily/weekly/
# monthly report (Mytox) - its quantity is not a weekly figure
COARSE_BUCKET_DAYS = 21
COARSE_BUCKETS_USED = 3


def _coarse_tail_avg(history):
	"""When an item's file ends in monthly buckets, "the last 4 weeks" is
	meaningless (the window falls between two buckets, or on an empty one).
	Use the weekly rate of its last 3 monthly buckets that carry a quantity:
	each bucket's qty / (days it covers / 7), averaged. None when the tail
	is not monthly."""
	last = max(history, key=lambda r: getdate(r["delivery_date"]))
	if flt(last.get("bucket_days")) < COARSE_BUCKET_DAYS:
		return None
	buckets = sorted(
		(r for r in history if flt(r.get("bucket_days")) >= COARSE_BUCKET_DAYS and flt(r["quantity"]) > 0),
		key=lambda r: getdate(r["delivery_date"]),
	)[-COARSE_BUCKETS_USED:]
	rates = [flt(r["quantity"]) / (flt(r["bucket_days"]) / 7.0) for r in buckets]
	if not rates:
		return None
	return round(sum(rates) / len(rates), 2), [round(x, 2) for x in rates]


def last_4_week_avg(item_rows):
	"""Quantity in the file's last 4 calendar weeks / 4. Zero weeks are not
	rows (readers drop them), so they are counted here: 100, 0, 400, 0 gives
	(100 + 0 + 400 + 0) / 4 = 125, not (100 + 400) / 2. Items whose file ends
	in monthly buckets use _coarse_tail_avg instead."""
	history = _history_for_averaging(item_rows)
	if not history:
		return 0, []

	coarse = _coarse_tail_avg(history)
	if coarse:
		return coarse

	last_row = max(getdate(r["delivery_date"]) for r in history)
	end = max(d for d in (_horizon_end(item_rows), last_row) if d)
	start = end - timedelta(days=27)

	weeks = [0.0, 0.0, 0.0, 0.0]
	for r in history:
		d = getdate(r["delivery_date"])
		if start <= d <= end:
			weeks[(d - start).days // 7] += flt(r["quantity"])
	return round(sum(weeks) / 4.0, 2), [round(w, 2) for w in weeks]


def shipping_avg(item_code, item_rows, months):
	"""Average weekly Delivery Note qty over the last `months` months
	(13 / 26 / 39 weeks for 3 / 6 / 9)."""
	today_date = getdate(today())
	from_date = add_months(today_date, -months)

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
		{"item_code": item_code, "from_date": str(from_date), "today": str(today_date)},
		as_dict=True,
	)
	total_shipped = flt(shipped[0].total_qty) if shipped and shipped[0].total_qty else 0

	if total_shipped > 0:
		weekly = round(total_shipped / (months * 13.0 / 3.0), 2)
		return weekly, [weekly]

	# No shipping history yet (new item/customer): use the file's own last 4 weeks.
	return last_4_week_avg(item_rows)


def last_3_month_shipping_avg(item_code, item_rows):
	return shipping_avg(item_code, item_rows, 3)


def _weekly_value(method, item_code, item_rows):
	if method == "LAST_4_WEEKS_AVG":
		return last_4_week_avg(item_rows)
	return shipping_avg(item_code, item_rows, SHIPPING_MONTHS[method])


# ---------------------------------------------------------------------------
# Preview (used live in the dialog whenever the method dropdown changes)
# ---------------------------------------------------------------------------

def generated_qty_by_item(rows, target_months, method, selected_items=None, item_methods=None):
	"""{item_code: total qty the auto-forecast would add} for these rows - the
	same numbers extend_rows() will really create (only the NEW rows count,
	not an auto-forecast already sitting in the schedule)."""

	original = {id(r) for r in rows}
	totals = defaultdict(float)
	for row in extend_rows(rows, target_months, method, selected_items=selected_items, item_methods=item_methods):
		if id(row) not in original and row.get("auto_forecast"):
			totals[row["item_code"]] += flt(row["quantity"])
	return {k: round(v, 2) for k, v in totals.items()}


def build_preview(rows, method, target_months=None, item_methods=None):
	method = validate_method(method)
	generated = {}
	if target_months:
		generated = generated_qty_by_item(rows, int(target_months), method, item_methods=item_methods)

	by_item = defaultdict(list)
	for row in rows:
		by_item[row["item_code"]].append(row)

	preview = []
	for item_code, item_rows in by_item.items():
		item_rows = sorted(item_rows, key=lambda x: x["delivery_date"])
		item_method = method_for(item_code, method, item_methods)
		value, sample = _weekly_value(item_method, item_code, item_rows)
		preview.append(
			{
				"item_code": item_code,
				"method": item_method,
				"description": METHOD_LABELS[item_method],
				"predicted_value": value,
				"sample_values": sample,
				"auto_qty": generated.get(item_code, 0),
			}
		)
	return preview


# ---------------------------------------------------------------------------
# Extension - generates the actual future weekly rows
# ---------------------------------------------------------------------------

def extend_rows(rows, target_months, method, selected_items=None, item_methods=None):
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

		weekly_qty, _ = _weekly_value(method_for(item_code, method, item_methods), item_code, item_sorted)
		if not weekly_qty or weekly_qty <= 0:
			continue

		existing_dates = {str(getdate(r["delivery_date"])) for r in item_sorted}
		item_name = item_sorted[0].get("item_name", item_code)

		# Weeks the customer's file covers but left empty are the customer's
		# own zeros - forecast starts after the file's horizon, not inside it.
		file_end = _horizon_end(item_sorted)
		start_from = max(last_date, file_end) if file_end else last_date
		next_date = start_from + timedelta(days=7)
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
