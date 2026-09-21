# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

"""Whitelisted endpoints backing the Customer Schedule client script.

Design note: analyse_schedule_file() does the (potentially expensive - LLM
call for PDFs) parsing exactly once and hands the parsed header/rows back to
the browser. Every subsequent call in the same upload session
(preview_forecast_method, create_schedule_with_forecast) is given that same
cached header/rows JSON by the client instead of re-parsing the file, so
changing the forecast-method dropdown or the target-months field never
re-triggers a file parse."""

import json
from collections import defaultdict

import frappe
from frappe.utils import flt, getdate

from schedule_planner.utils import file_parsers, forecast_engine
from schedule_planner.utils.items import ensure_item_exists


# ---------------------------------------------------------------------------
# 1. Analyse an uploaded file (first call after upload)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def analyse_schedule_file(file_url, customer):
	header, rows, schedule_format = file_parsers.extract_header_and_rows(file_url, customer)

	month_keys = sorted({getdate(r["delivery_date"]).strftime("%Y-%m") for r in rows})

	month_summary = defaultdict(lambda: defaultdict(float))
	for row in rows:
		month_key = getdate(row["delivery_date"]).strftime("%Y-%m")
		month_summary[month_key][row["item_code"]] += flt(row["quantity"])
	formatted_summary = {month: dict(items) for month, items in month_summary.items()}

	item_details = _rows_item_details(rows)
	preview = forecast_engine.build_preview(rows, "LAST_4_WEEKS_AVG")

	return {
		"status": "Success",
		"header": header,
		"schedule_format": schedule_format,
		"parsed_rows": rows,
		"available_months": len(month_keys),
		"from_date": min(r["delivery_date"] for r in rows),
		"to_date": max(r["delivery_date"] for r in rows),
		"months": month_keys,
		"items": sorted({r["item_code"] for r in rows}),
		"item_details": item_details,
		"month_summary": formatted_summary,
		"preview": preview,
	}


def _rows_item_details(rows):
	by_item = defaultdict(list)
	for row in rows:
		by_item[row["item_code"]].append(row)

	details = []
	for item_code, item_rows in sorted(by_item.items()):
		dates = [getdate(r["delivery_date"]) for r in item_rows]
		months = {d.strftime("%Y-%m") for d in dates}
		details.append(
			{
				"item_code": item_code,
				"item_name": item_rows[0].get("item_name", item_code),
				"months": len(months),
				"from_date": str(min(dates)),
				"to_date": str(max(dates)),
				"total_qty": sum(flt(r["quantity"]) for r in item_rows),
			}
		)
	return details


# ---------------------------------------------------------------------------
# 2. Live preview when the method dropdown / item selection changes
# ---------------------------------------------------------------------------

@frappe.whitelist()
def preview_forecast_method(rows_json, forecast_method, selected_items=None):
	rows = json.loads(rows_json)
	selected = json.loads(selected_items) if selected_items else None

	if selected is not None:
		rows = [r for r in rows if r["item_code"] in selected]

	if not rows:
		return []

	return forecast_engine.build_preview(rows, forecast_method)


@frappe.whitelist()
def preview_forecast_for_schedule(schedule_name, forecast_method, selected_items=None):
	"""Same as preview_forecast_method, but for the 'Extend Forecast' dialog
	on an already-saved Draft schedule, where the browser has no cached
	parsed-file rows to send back - so the rows are read from the doc."""

	selected = json.loads(selected_items) if selected_items else None

	rows = frappe.get_all(
		"Customer Schedule Item",
		filters={"parent": schedule_name},
		fields=["item_code", "item_name", "delivery_date", "quantity", "demand_type"],
	)
	rows = [dict(r) for r in rows if flt(r.quantity) > 0]
	for r in rows:
		r["delivery_date"] = str(r["delivery_date"])

	if selected is not None:
		rows = [r for r in rows if r["item_code"] in selected]

	if not rows:
		return []

	return forecast_engine.build_preview(rows, forecast_method)


# ---------------------------------------------------------------------------
# 3. Create the Customer Schedule (Draft) with auto-forecast applied
# ---------------------------------------------------------------------------

@frappe.whitelist()
def create_schedule_with_forecast(
	file_url,
	customer,
	target_months,
	forecast_method,
	selected_items=None,
	header_json=None,
	rows_json=None,
	schedule_format=None,
):
	try:
		target_months = int(target_months)
	except (TypeError, ValueError):
		frappe.throw("Target months must be a valid number.")
	if target_months <= 0:
		frappe.throw("Forecast months must be greater than zero.")

	forecast_method = forecast_engine.validate_method(forecast_method)
	selected = json.loads(selected_items) if selected_items else None

	if header_json and rows_json:
		header = json.loads(header_json)
		rows = json.loads(rows_json)
	else:
		# Cache missed (e.g. dialog reopened) - fall back to a fresh parse.
		header, rows, schedule_format = file_parsers.extract_header_and_rows(file_url, customer)

	if not rows:
		frappe.throw("No valid schedule rows to create a schedule from.")

	available_months = len({getdate(r["delivery_date"]).strftime("%Y-%m") for r in rows})
	if target_months < available_months:
		frappe.throw(
			f"The file contains {available_months} month(s) of data. You cannot "
			f"request only {target_months}. Required months must be "
			f"{available_months} or greater."
		)

	existing = frappe.db.get_value(
		"Customer Schedule",
		{
			"customer": customer,
			"release_number": header["release_number"],
			"supplier_vendor": header["supplier_vendor"],
			"docstatus": ["!=", 2],
		},
		"name",
	)
	if existing:
		frappe.throw(
			f"Release <b>{header['release_number']}</b> for customer <b>{customer}</b> "
			f"already exists as <a href='/app/customer-schedule/{existing}'>{existing}</a>.",
			title="Duplicate Schedule",
		)

	all_rows = forecast_engine.extend_rows(rows, target_months, forecast_method, selected_items=selected)

	schedule_doc = frappe.get_doc(
		{
			"doctype": "Customer Schedule",
			"naming_series": "CUST-SCH-.YYYY.-",
			"customer": customer,
			"supplier_vendor": header["supplier_vendor"],
			"release_number": header["release_number"],
			"schedule_date": header["schedule_date"],
			"schedule_format": schedule_format,
			"source_file": file_url,
			"status": "Draft",
			"schedule_items": [],
		}
	)

	for row in all_rows:
		ensure_item_exists(row["item_code"], row.get("item_name", row["item_code"]))
		schedule_doc.append(
			"schedule_items",
			{
				"item_code": row["item_code"],
				"item_name": row.get("item_name", row["item_code"]),
				"delivery_date": row["delivery_date"],
				"quantity": row["quantity"],
				"demand_type": row["demand_type"],
				"auto_forecast": row.get("auto_forecast", 0),
			},
		)

	schedule_doc.insert(ignore_permissions=True)

	auto_rows = [r for r in all_rows if r.get("auto_forecast")]
	auto_months = len({getdate(r["delivery_date"]).strftime("%Y-%m") for r in auto_rows}) if auto_rows else 0

	return {
		"status": "Success",
		"docname": schedule_doc.name,
		"file_months": available_months,
		"target_months": target_months,
		"auto_months": auto_months,
		"auto_rows": len(auto_rows),
		"total_rows": len(all_rows),
		"forecast_method": forecast_method,
		"message": (
			f"Schedule {schedule_doc.name} created as Draft. {available_months} month(s) "
			f"from the file + {auto_months} month(s) auto-forecast using "
			f"{forecast_engine.METHOD_LABELS[forecast_method]}. Review and Submit "
			f"when ready - Submitting is what creates the Sales Order(s), Sales "
			f"Forecast and (if enabled) Material Request."
		),
	}


# ---------------------------------------------------------------------------
# 4. Top up forecast on an existing Draft schedule
# ---------------------------------------------------------------------------

@frappe.whitelist()
def extend_forecast(schedule_name, target_months, forecast_method, selected_items=None):
	doc = frappe.get_doc("Customer Schedule", schedule_name)
	if doc.docstatus != 0:
		frappe.throw(
			"Only a Draft schedule can be extended here. For a submitted "
			"schedule, upload the customer's next schedule instead - it will "
			"amend the relevant Sales Order/Forecast automatically."
		)

	forecast_method = forecast_engine.validate_method(forecast_method)
	selected = json.loads(selected_items) if selected_items else None
	target_months = int(target_months)

	existing_rows = [
		{
			"item_code": r.item_code,
			"item_name": r.item_name,
			"delivery_date": str(r.delivery_date),
			"quantity": r.quantity,
			"demand_type": r.demand_type,
			"auto_forecast": r.auto_forecast,
		}
		for r in doc.schedule_items
	]

	all_rows = forecast_engine.extend_rows(existing_rows, target_months, forecast_method, selected_items=selected)

	existing_keys = {(r.item_code, str(r.delivery_date)) for r in doc.schedule_items}
	added = 0
	for row in all_rows:
		key = (row["item_code"], row["delivery_date"])
		if key in existing_keys:
			continue
		ensure_item_exists(row["item_code"], row.get("item_name"))
		doc.append(
			"schedule_items",
			{
				"item_code": row["item_code"],
				"item_name": row.get("item_name", row["item_code"]),
				"delivery_date": row["delivery_date"],
				"quantity": row["quantity"],
				"demand_type": row["demand_type"],
				"auto_forecast": row.get("auto_forecast", 0),
			},
		)
		existing_keys.add(key)
		added += 1

	if added:
		doc.save(ignore_permissions=True)

	return {
		"status": "ok",
		"added": added,
		"message": f"{added} forecast row(s) added." if added else "Nothing to add - already covers that many months.",
	}


# ---------------------------------------------------------------------------
# 5. Per-item month coverage for a saved schedule (extend-forecast dialog)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_schedule_month_info(schedule_name):
	rows = frappe.get_all(
		"Customer Schedule Item",
		filters={"parent": schedule_name},
		fields=["item_code", "item_name", "delivery_date", "quantity", "demand_type", "auto_forecast"],
	)
	positive_rows = [r for r in rows if flt(r.quantity) > 0]
	if not positive_rows:
		return {"available_months": 0, "items": [], "item_details": [], "from_date": None, "to_date": None}

	all_dates = [getdate(r.delivery_date) for r in positive_rows]
	global_months = {d.strftime("%Y-%m") for d in all_dates}

	by_item = defaultdict(list)
	for r in positive_rows:
		by_item[r.item_code].append(r)

	item_details = []
	for item_code, item_rows in sorted(by_item.items()):
		item_dates = [getdate(r.delivery_date) for r in item_rows]
		item_months = {d.strftime("%Y-%m") for d in item_dates}
		item_details.append(
			{
				"item_code": item_code,
				"item_name": item_rows[0].item_name or item_code,
				"months": len(item_months),
				"from_date": str(min(item_dates)),
				"to_date": str(max(item_dates)),
				"total_qty": sum(flt(r.quantity) for r in item_rows),
			}
		)

	return {
		"available_months": len(global_months),
		"from_date": str(min(all_dates)),
		"to_date": str(max(all_dates)),
		"items": sorted(by_item.keys()),
		"item_count": len(by_item),
		"item_details": item_details,
	}


# ---------------------------------------------------------------------------
# 6. Revision comparison (uses the Revision Log written at submit time)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_revision_comparison(schedule_name):
	doc = frappe.get_doc("Customer Schedule", schedule_name)

	if not doc.revision_log:
		return {"current": doc.name, "previous": None}

	previous_name = doc.revision_log[0].previous_schedule

	rows = []
	for log in doc.revision_log:
		old_qty, new_qty = flt(log.old_qty), flt(log.new_qty)
		if old_qty == 0:
			direction = "new"
		elif new_qty == 0:
			direction = "removed"
		elif new_qty > old_qty:
			direction = "increase"
		else:
			direction = "decrease"
		rows.append(
			{
				"item_code": log.item_code,
				"delivery_date": str(log.delivery_date),
				"demand_type": log.demand_type,
				"old_qty": old_qty,
				"new_qty": new_qty,
				"change": flt(log.change),
				"direction": direction,
			}
		)

	summary = {
		"total_changed": len([r for r in rows if r["direction"] in ("increase", "decrease")]),
		"total_added": len([r for r in rows if r["direction"] == "new"]),
		"total_removed": len([r for r in rows if r["direction"] == "removed"]),
	}

	return {"current": doc.name, "previous": previous_name, "summary": summary, "rows": rows}


# ---------------------------------------------------------------------------
# 7. Manual Material Request trigger (works regardless of trigger mode).
#    `customer` is accepted only for the "triggered by" label in the result -
#    the underlying reconciliation is always global across all customers,
#    since the live Material Request is a single shared pool per item.
# ---------------------------------------------------------------------------

@frappe.whitelist()
def generate_material_request_manual(customer=None, planning_months=None, lead_time_months=None, warehouse=None):
	from schedule_planner.utils import material_request as mr_utils

	settings = frappe.get_cached_doc("Schedule Planning Settings")
	return mr_utils.reconcile_material_requests(
		planning_months=int(planning_months) if planning_months else (settings.default_planning_months or 12),
		lead_time_months=int(lead_time_months) if lead_time_months else (settings.default_lead_time_months or 7),
		warehouse=warehouse or settings.default_warehouse,
		triggered_by=f"Manual trigger ({customer})" if customer else "Manual trigger",
	)


# ---------------------------------------------------------------------------
# 8. Material Planning Dashboard data (used by the report, and available for
#    any other UI that wants the same live snapshot)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_material_planning_snapshot(planning_months=None, lead_time_months=None):
	from schedule_planner.utils import material_request as mr_utils

	return mr_utils.compute_planning_snapshot(
		planning_months=int(planning_months) if planning_months else None,
		lead_time_months=int(lead_time_months) if lead_time_months else None,
	)
