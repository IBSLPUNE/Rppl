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

# the customer's own weekly average for the item, taken from the uploaded file
DOCUMENT_METHOD = "DOCUMENT_AVG"

METHODS = ("LAST_4_WEEKS_AVG", *SHIPPING_MONTHS, DOCUMENT_METHOD)

METHOD_LABELS = {
	"LAST_4_WEEKS_AVG": "Average of Last 4 Weeks",
	"LAST_3_MONTHS_SHIPPING_AVG": "Average Weekly Shipment - Last 3 Months",
	"LAST_6_MONTHS_SHIPPING_AVG": "Average Weekly Shipment - Last 6 Months",
	"LAST_9_MONTHS_SHIPPING_AVG": "Average Weekly Shipment - Last 9 Months",
	DOCUMENT_METHOD: "Average of Customer Document",
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


def shipping_avg(item_code, item_rows, months, customer=None, info=None):
	"""Average weekly Delivery Note qty over the last `months` months
	(13 / 26 / 39 weeks for 3 / 6 / 9). When the schedule's customer is given,
	only THAT customer's Delivery Notes count - an item shared by two customers
	would otherwise get the combined shipments as the forecast of each."""
	today_date = getdate(today())
	from_date = add_months(today_date, -months)

	shipped = frappe.db.sql(
		"""
		SELECT SUM(IFNULL(NULLIF(dni.stock_qty, 0), dni.qty * IFNULL(NULLIF(dni.conversion_factor, 0), 1))) AS total_qty
		FROM `tabDelivery Note Item` dni
		INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
		WHERE dni.item_code = %(item_code)s
		  AND dn.docstatus = 1
		  AND dn.posting_date >= %(from_date)s
		  AND dn.posting_date <= %(today)s
		  AND (%(customer)s = '' OR dn.customer = %(customer)s)
		""",
		{"item_code": item_code, "from_date": str(from_date), "today": str(today_date), "customer": customer or ""},
		as_dict=True,
	)
	total_shipped = flt(shipped[0].total_qty) if shipped and shipped[0].total_qty else 0

	# No shipments in the period = 0 (no fallback to the file's last 4 weeks). The item then
	# gets the customer-document average instead, since that is the higher of the two.
	weekly = round(total_shipped / (months * 13.0 / 3.0), 2) if total_shipped > 0 else 0.0
	if info is not None:
		info.update(total=round(total_shipped, 2), label="shipped, last {0} months".format(months))
	return weekly, [weekly]


def last_3_month_shipping_avg(item_code, item_rows):
	return shipping_avg(item_code, item_rows, 3)


def _weekly_value(method, item_code, item_rows, customer=None, info=None):
	"""(weekly qty, sample). `info`, when given, is filled with the figure the
	weekly qty was made from (total shipped / the file's last 4 weeks) and a
	short label for it - shown in brackets in the forecast dialog."""
	if method == "LAST_4_WEEKS_AVG":
		value, weeks = last_4_week_avg(item_rows)
		if info is not None:
			info.update(total=round(sum(weeks), 2) if len(weeks) == 4 else None, label="file's last 4 weeks")
		return value, weeks
	return shipping_avg(item_code, item_rows, SHIPPING_MONTHS[method], customer, info)


# ---------------------------------------------------------------------------
# Document average (the customer's own weekly rate for the item)
#
# The auto-forecast weekly qty is, per item, the HIGHER of
#   - the chosen forecast method (last 4 weeks / shipping averages), and
#   - the document average: all quantity the customer's file carries for the
#     item divided by the weeks between the item's start and end date.
# Schedule Planning Settings decide whether this comparison is on and how the
# start / end date are taken.
# ---------------------------------------------------------------------------

DOC_PERIOD_ITEM_SHEET_END = "Item first date to end of file"
DOC_PERIOD_ITEM_LAST_QTY = "Item first date to item's last quantity"
DOC_PERIOD_SHEET = "First date of file to end of file"
DOC_PERIOD_ITEM_LAST_DATE = "Item first date to item's last date"
DOC_PERIOD_MODES = (
	DOC_PERIOD_ITEM_SHEET_END,
	DOC_PERIOD_ITEM_LAST_QTY,
	DOC_PERIOD_ITEM_LAST_DATE,
	DOC_PERIOD_SHEET,
)

SOURCE_METHOD = "Forecast Method"
SOURCE_DOCUMENT = "Document Average"


def document_average_settings():
	"""(enabled, period mode) from Schedule Planning Settings, with defaults
	so the engine also works before the settings were ever saved."""
	enabled, mode = 1, DOC_PERIOD_ITEM_SHEET_END
	try:
		settings = frappe.get_cached_doc("Schedule Planning Settings")
		if settings.get("use_document_average") not in (None, ""):
			enabled = int(settings.get("use_document_average"))
		mode = settings.get("document_avg_period") or mode
	except Exception:
		pass
	if mode not in DOC_PERIOD_MODES:
		mode = DOC_PERIOD_ITEM_SHEET_END
	return bool(enabled), mode


START_AFTER_LAST_QTY = "After Item's Last Non-Zero Quantity"
START_AFTER_ITEM_END = "After Item's End Date"
START_AFTER_FILE_END = "After End of File"
START_MODES = (START_AFTER_LAST_QTY, START_AFTER_ITEM_END, START_AFTER_FILE_END)


def auto_forecast_start_mode():
	"""Where the auto forecast begins (Schedule Planning Settings > Auto Forecast Starts)."""
	mode = START_AFTER_LAST_QTY
	try:
		mode = frappe.get_cached_doc("Schedule Planning Settings").get("auto_forecast_start") or mode
	except Exception:
		pass
	return mode if mode in START_MODES else START_AFTER_LAST_QTY


def _item_end_date(item_rows):
	"""The item's last printed date in the sheet (zero quantity included), when the
	reader recorded it; else the file's last date; else None."""
	ends = [getdate(r["item_end"]) for r in item_rows if r.get("item_end")]
	return max(ends) if ends else _horizon_end(item_rows)


def _file_end_date(rows):
	"""Last date of the whole file: every item's end date, the file's horizon and the
	last quantity date (auto-forecast rows already in the schedule are ignored)."""
	own = [r for r in rows if not r.get("auto_forecast")]
	dates = [getdate(r["delivery_date"]) for r in own]
	for r in own:
		for key in ("item_end", "horizon_end"):
			if r.get(key):
				dates.append(getdate(r[key]))
	return max(dates) if dates else None


def sheet_start_date(rows):
	"""First date of the customer's file: the earliest item start (zero or
	non-zero quantity), else the earliest quantity date."""
	dates = []
	for r in rows:
		if r.get("auto_forecast"):
			continue
		dates.append(getdate(r["item_start"]) if r.get("item_start") else getdate(r["delivery_date"]))
	return min(dates) if dates else None


def document_weekly(item_rows, mode=None, sheet_start=None):
	"""The customer's own weekly rate for ONE item, over its whole period.

	Returns None when the item has no customer quantity, else a dict with the
	weekly qty, the quantity it was based on and the period used. Weeks are
	counted from the start date to the end of the last bucket, so a week-start
	date counts as a full 7 days (a file of 4 weekly columns = 4 weeks)."""
	mode = mode or document_average_settings()[1]
	own = [r for r in item_rows if not r.get("auto_forecast") and flt(r.get("quantity")) > 0]
	if not own:
		return None

	total = sum(flt(r["quantity"]) for r in own)
	last_row = max(own, key=lambda r: getdate(r["delivery_date"]))
	first_qty = min(getdate(r["delivery_date"]) for r in own)
	last_qty = getdate(last_row["delivery_date"])
	item_start = min([getdate(r["item_start"]) for r in item_rows if r.get("item_start")] + [first_qty])
	file_end = max(d for d in (_horizon_end(item_rows), last_qty) if d)

	if mode == DOC_PERIOD_ITEM_LAST_QTY:
		start, end = item_start, last_qty
	elif mode == DOC_PERIOD_ITEM_LAST_DATE:
		# the item's own last date in the sheet, zero quantity included
		start, end = item_start, max(_item_end_date(item_rows) or last_qty, last_qty)
	elif mode == DOC_PERIOD_SHEET:
		start, end = min(sheet_start, item_start) if sheet_start else item_start, file_end
	else:
		start, end = item_start, file_end

	# a monthly bucket stands for its whole month, not for 7 days
	tail = flt(last_row.get("bucket_days")) if end == last_qty and flt(last_row.get("bucket_days")) > 7 else 7
	weeks = max(((end - start).days + tail) / 7.0, 1.0)
	return {
		"weekly": round(total / weeks, 2),
		"total": round(total, 2),
		"from": str(start),
		"to": str(end),
		"weeks": round(weeks, 2),
		"mode": mode,
	}


def round_qty(value):
	"""Forecast quantities are whole numbers (half rounds up: 631.5 -> 632)."""
	return float(int(flt(value) + 0.5))


def resolve_item(item_code, item_rows, default_method, item_methods=None, sheet_start=None, source="best", memo=None, customer=None):
	"""Everything the forecast needs to know about ONE item.

	The item's method is the one the user picked for it (item_methods), else the
	dialog's default. When the user did not pick one and the customer
	document's own weekly average is higher than that method's figure, the item
	is switched to the "Average of Customer Document" method - the dropdown shows
	it, and the user can change it back. A method the user picked is never
	overridden.

	source: "best"     the real behaviour above
	        "method"   force the item's own / default method (no switch)
	        "document" force the document average"""
	explicit = (item_methods or {}).get(item_code)
	base_method = validate_method(explicit or default_method)

	doc_enabled, mode = document_average_settings()
	doc = document_weekly(item_rows, mode, sheet_start)
	doc_qty = flt(doc["weekly"]) if doc else 0.0

	basis = {}
	if base_method == DOCUMENT_METHOD:
		base_qty, sample = doc_qty, ([doc_qty] if doc else [])
		basis = {"total": doc["total"] if doc else None, "label": "customer document"}
	else:
		key = (base_method, item_code)
		if memo is not None and key in memo:
			base_qty, sample, basis = memo[key]
		else:
			basis = {}
			base_qty, sample = _weekly_value(base_method, item_code, item_rows, customer, basis)
			if memo is not None:
				memo[key] = (base_qty, sample, basis)
	base_qty = flt(base_qty)

	switched = bool(not explicit and doc_enabled and base_method != DOCUMENT_METHOD and doc_qty > base_qty)
	method = DOCUMENT_METHOD if switched else base_method
	if source == "method":
		method, switched = base_method, False
	elif source == "document":
		method = DOCUMENT_METHOD

	weekly = doc_qty if method == DOCUMENT_METHOD else base_qty
	return {
		"method": method,
		"base_method": base_method,
		"switched": switched,
		"weekly": round_qty(weekly),
		"source": SOURCE_DOCUMENT if method == DOCUMENT_METHOD else SOURCE_METHOD,
		"method_weekly": round(base_qty, 2),
		"document_weekly": round(doc_qty, 2),
		"document": doc,
		"document_enabled": doc_enabled,
		"sample": sample,
		"method_total": basis.get("total"),
		"method_basis_label": basis.get("label"),
	}


# ---------------------------------------------------------------------------
# Preview (used live in the dialog whenever the method dropdown changes)
# ---------------------------------------------------------------------------

def generated_qty_by_item(
	rows, target_months, method, selected_items=None, item_methods=None, source="best", memo=None, customer=None,
	weeks_out=None,
):
	"""{item_code: total qty the auto-forecast would add} for these rows - the
	same numbers extend_rows() will really create (only the NEW rows count,
	not an auto-forecast already sitting in the schedule). When `weeks_out` (a
	dict) is given it receives {item_code: number of weekly rows created}."""

	original = {id(r) for r in rows}
	totals = defaultdict(float)
	for row in _extend(
		rows,
		target_months,
		method,
		selected_items=selected_items,
		item_methods=item_methods,
		source=source,
		memo=memo,
		customer=customer,
	):
		if id(row) not in original and row.get("auto_forecast"):
			totals[row["item_code"]] += flt(row["quantity"])
			if weeks_out is not None:
				weeks_out[row["item_code"]] = weeks_out.get(row["item_code"], 0) + 1
	return {k: round(v, 2) for k, v in totals.items()}


def _weeks_to_months(weeks):
	return round(weeks * 7.0 / 30.4375, 1)


def build_preview(rows, method, target_months=None, item_methods=None, customer=None):
	method = validate_method(method)
	memo = {}
	generated, by_method, by_document = {}, {}, {}
	weeks_method, weeks_document = {}, {}
	if target_months:
		months = int(target_months)
		kw = dict(item_methods=item_methods, memo=memo, customer=customer)
		generated = generated_qty_by_item(rows, months, method, **kw)
		by_method = generated_qty_by_item(rows, months, method, source="method", weeks_out=weeks_method, **kw)
		by_document = generated_qty_by_item(rows, months, method, source="document", weeks_out=weeks_document, **kw)

	by_item = defaultdict(list)
	for row in rows:
		by_item[row["item_code"]].append(row)

	sheet_start = sheet_start_date(rows)
	preview = []
	for item_code, item_rows in by_item.items():
		item_rows = sorted(item_rows, key=lambda x: x["delivery_date"])
		info = resolve_item(item_code, item_rows, method, item_methods, sheet_start, memo=memo, customer=customer)
		doc = info["document"] or {}
		preview.append(
			{
				"item_code": item_code,
				# the method this item will really use (may be the document average)
				"method": info["method"],
				"base_method": info["base_method"],
				"auto_switched": info["switched"],
				"description": METHOD_LABELS[info["method"]],
				"predicted_value": info["weekly"],
				"method_value": info["method_weekly"],
				"document_value": info["document_weekly"],
				"document_from": doc.get("from"),
				"document_to": doc.get("to"),
				"document_weeks": doc.get("weeks"),
				"document_total": doc.get("total"),
				"method_total": info["method_total"],
				"method_basis_label": info["method_basis_label"],
				"used": info["source"],
				"document_enabled": info["document_enabled"],
				"sample_values": info["sample"],
				"auto_qty": generated.get(item_code, 0),
				"auto_qty_method": by_method.get(item_code, 0),
				"auto_qty_document": by_document.get(item_code, 0),
				# how many months of auto forecast each figure covers (weekly rows x 7 / 30.4375)
				"auto_months_method": _weeks_to_months(weeks_method.get(item_code, 0)),
				"auto_months_document": _weeks_to_months(weeks_document.get(item_code, 0)),
			}
		)
	return preview


# ---------------------------------------------------------------------------
# Extension - generates the actual future weekly rows
# ---------------------------------------------------------------------------

def extend_rows(rows, target_months, method, selected_items=None, item_methods=None, customer=None):
	"""rows: list of row dicts (item_code, item_name, delivery_date, quantity,
	demand_type[, auto_forecast]). Returns a NEW list (original + generated).

	target_months is counted from each item's own first date in the sheet (zero
	or non-zero quantity); the auto-forecast runs to first date + target_months and
	starts the week after the item's last non-zero quantity, the item's last date
	in the sheet, or the end of the file - whichever the settings say. The weekly
	qty is the higher of the chosen method and the document average (when
	that comparison is switched on in the settings)."""
	return _extend(rows, target_months, method, selected_items, item_methods, customer=customer)


def _extend(rows, target_months, method, selected_items=None, item_methods=None, source="best", memo=None, customer=None):
	method = validate_method(method)
	target_months = int(target_months)

	by_item = defaultdict(list)
	for row in rows:
		by_item[row["item_code"]].append(row)

	extended = list(rows)
	sheet_start = sheet_start_date(rows)
	start_mode = auto_forecast_start_mode()
	file_end = _file_end_date(rows) if start_mode == START_AFTER_FILE_END else None

	for item_code, item_rows in by_item.items():
		if selected_items is not None and item_code not in selected_items:
			continue

		item_sorted = sorted(item_rows, key=lambda x: x["delivery_date"])
		# first date = the item's first date in the sheet, zero OR non-zero quantity
		# (item_start, recorded by the file reader); if a reader did not record it,
		# the item's first customer quantity is used.
		starts = [getdate(r["item_start"]) for r in item_sorted if r.get("item_start")]
		own_dates = [getdate(r["delivery_date"]) for r in item_sorted if not r.get("auto_forecast")]
		first_date = min(starts + own_dates) if (starts or own_dates) else getdate(item_sorted[0]["delivery_date"])
		qty_dates = [getdate(r["delivery_date"]) for r in item_sorted if (r.get("quantity") or 0) > 0]
		last_date = max(qty_dates) if qty_dates else getdate(item_sorted[-1]["delivery_date"])
		item_end = add_months(first_date, target_months)
		# where the auto forecast begins (setting): after the last non-zero quantity,
		# after the item's own last date in the sheet, or after the end of the whole file
		if start_mode == START_AFTER_ITEM_END:
			last_date = max(last_date, _item_end_date(item_sorted) or last_date)
		elif start_mode == START_AFTER_FILE_END and file_end:
			last_date = max(last_date, file_end)

		info = resolve_item(
			item_code, item_sorted, method, item_methods, sheet_start, source=source, memo=memo, customer=customer
		)
		weekly_qty, used = info["weekly"], info["source"]
		if not weekly_qty or weekly_qty <= 0:
			continue

		basis = {
			"source": used,
			"method": info["method"],
			"base_method": info["base_method"],
			"auto_switched": info["switched"],
			"method_weekly": info["method_weekly"],
			"document_weekly": info["document_weekly"],
			"document": info["document"],
			"weekly_used": weekly_qty,
		}

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
						"quantity": weekly_qty,
						"demand_type": "Forecast",
						"auto_forecast": 1,
						"forecast_source": used,
						"forecast_basis": basis,
					}
				)
				existing_dates.add(date_key)
			next_date += timedelta(days=7)

	extended.sort(key=lambda x: (x["item_code"], x["delivery_date"]))
	return extended
