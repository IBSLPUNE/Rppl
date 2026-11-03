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

from schedule_planner.utils import file_parsers, forecast_engine, item_mapping, safety
from schedule_planner.utils.items import ensure_item_exists


# ---------------------------------------------------------------------------
# 1. Analyse an uploaded file (first call after upload)
# ---------------------------------------------------------------------------

def _parse_selected_items(selected_items):
	"""Item codes from the dialog as strings - an all-digit code such as
	902900022 can arrive as a JSON number, which would never match the
	(string) item_code on the rows and silently drop the item."""
	if not selected_items:
		return None
	return [str(code) for code in json.loads(selected_items)]


def _parse_item_methods(item_methods):
	"""{item_code: method code} from the dialog's per-item dropdowns."""
	if not item_methods:
		return {}
	chosen = json.loads(item_methods) if isinstance(item_methods, str) else item_methods
	return {str(code): forecast_engine.validate_method(method) for code, method in chosen.items() if method}


def _customer_has_method_fields():
	meta = frappe.get_meta("Customer")
	return meta.has_field("custom_sp_forecast_method") and meta.has_field("custom_sp_item_forecast_methods")


def get_saved_forecast_methods(customer):
	"""(default method code, {item_code: method code}) last used for this
	customer; (None, {}) when nothing has been saved yet."""
	if not customer or not _customer_has_method_fields():
		return None, {}
	default_label, raw = frappe.db.get_value(
		"Customer", customer, ["custom_sp_forecast_method", "custom_sp_item_forecast_methods"]
	)
	try:
		saved = json.loads(raw) if raw else {}
	except ValueError:
		saved = {}
	return forecast_engine.method_from_label(default_label), {
		str(k): v for k, v in saved.items() if v in forecast_engine.METHODS
	}


def save_forecast_methods(customer, forecast_method, item_methods, items):
	"""Remember what was used so the next upload for this customer starts from
	it: the dialog's default, and the method of each item that took part."""
	if not customer or not _customer_has_method_fields():
		return
	_default, saved = get_saved_forecast_methods(customer)
	for item in items:
		saved[str(item)] = forecast_engine.method_for(item, forecast_method, item_methods)
	frappe.db.set_value(
		"Customer",
		customer,
		{
			"custom_sp_forecast_method": forecast_engine.METHOD_LABELS[forecast_method],
			"custom_sp_item_forecast_methods": json.dumps(saved),
		},
		update_modified=False,
	)


def _attach_forecast_defaults(analysis, customer):
	"""Start the dialog from what this customer used last time."""
	default, saved = get_saved_forecast_methods(customer)
	default = default or "LAST_4_WEEKS_AVG"
	for item in analysis.get("item_details", []):
		item["forecast_method"] = saved.get(str(item["item_code"])) or default
	analysis["default_forecast_method"] = default
	return analysis


def _method_labels(items, default, item_methods):
	"""{item_code: method label} stored on the schedule rows of each item."""
	return {
		item: forecast_engine.METHOD_LABELS[forecast_engine.method_for(item, default, item_methods)]
		for item in items
	}


def build_file_check(original_rows, skipped_parts=None):
	"""What the customer's file said, before anything was dropped: total qty,
	line count, month span and the part numbers the user chose to skip (with
	their quantities). Shown in the Pre-submit Check so the system total can
	be reconciled against the file."""
	skipped_parts = set(skipped_parts or [])
	total = 0.0
	skipped = {}
	months, dates = set(), []
	for r in original_rows:
		qty = flt(r.get("quantity"))
		total += qty
		d = getdate(r["delivery_date"])
		dates.append(d)
		months.add(d.strftime("%Y-%m"))
		part = r.get("file_part_no") or r.get("item_code")
		if part in skipped_parts:
			e = skipped.setdefault(
				part,
				{"file_part_no": part, "description": r.get("file_description") or r.get("item_name") or "", "qty": 0.0, "lines": 0},
			)
			e["qty"] += qty
			e["lines"] += 1
	return {
		"total_qty": round(total, 2),
		"lines": len(original_rows),
		"months": len(months),
		"from_date": str(min(dates)) if dates else "",
		"to_date": str(max(dates)) if dates else "",
		"skipped": [dict(v, qty=round(v["qty"], 2)) for v in skipped.values()],
	}


@frappe.whitelist()
def analyse_schedule_file(file_url, customer):
	header, rows, schedule_format = file_parsers.extract_header_and_rows(file_url, customer)

	# The file's part numbers are not our item codes. Keep what the file said
	# in file_part_no, and pre-fill item_code for every part number that
	# resolves unambiguously; the client shows the Item Mapping step for the
	# rest (and lets the user review/change any of them).
	rows = [
		dict(
			r,
			file_part_no=r["item_code"],
			file_description=r.get("item_name") or r["item_code"],
			horizon_end=header.get("horizon_end"),
		)
		for r in rows
	]
	mapping = item_mapping.resolve(customer, item_mapping.distinct_parts(rows))
	rows = item_mapping.remap_rows(
		rows, {m["file_part_no"]: m["item_code"] for m in mapping if m["status"] == item_mapping.MATCHED}
	)

	analysis = _build_analysis(header, rows, schedule_format)
	analysis["file_check"] = build_file_check(rows)
	analysis["item_mapping"] = mapping
	analysis["mapping_complete"] = all(m["status"] == item_mapping.MATCHED for m in mapping)
	return _attach_forecast_defaults(analysis, customer)


def _build_analysis(header, rows, schedule_format):
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


@frappe.whitelist()
def confirm_item_mapping(customer, rows_json, mapping_json, header_json=None, schedule_format=None):
	"""Second step of an upload: the user has reviewed/changed the Item
	Mapping table. Validates it, remembers any override, and returns the same
	analysis shape as analyse_schedule_file() so the forecast dialog can
	continue with real Items."""

	rows = json.loads(rows_json)
	mapping = json.loads(mapping_json)
	header = json.loads(header_json) if header_json else {}

	remapped, final_mapping = item_mapping.confirm(customer, rows, mapping)

	analysis = _build_analysis(header, remapped, schedule_format)
	skipped_parts = [m.get("file_part_no") for m in mapping if int(m.get("skip") or 0)]
	analysis["file_check"] = build_file_check(rows, skipped_parts)
	analysis["item_mapping"] = final_mapping
	analysis["mapping_complete"] = True
	return _attach_forecast_defaults(analysis, customer)


@frappe.whitelist()
def get_schedule_item_mapping(schedule_name):
	"""Item Mapping for a saved Draft: one line per customer part number
	(what the file said) with the Item currently on the schedule."""

	doc = frappe.get_doc("Customer Schedule", schedule_name)
	rows = frappe.get_all(
		"Customer Schedule Item",
		filters={"parent": schedule_name},
		fields=["item_code", "item_name", "file_part_no"],
		order_by="idx",
	)
	current, order = {}, []
	for r in rows:
		part = (r.file_part_no or r.item_code or "").strip()
		if part and part not in current:
			current[part] = (r.item_code, r.item_name)
			order.append(part)

	resolved = {m["file_part_no"]: m for m in item_mapping.resolve(doc.customer, [{"file_part_no": p, "file_description": current[p][1]} for p in order])}
	mapping = []
	for part in order:
		m = dict(resolved[part])
		m["item_code"], m["item_name"] = current[part]
		m["status"], m["source"] = item_mapping.MATCHED, "On this schedule"
		m["suggestions"] = ""
		mapping.append(m)
	return {"customer": doc.customer, "docstatus": doc.docstatus, "mapping": mapping}


@frappe.whitelist()
def update_schedule_item_mapping(schedule_name, mapping_json):
	doc = frappe.get_doc("Customer Schedule", schedule_name)
	if doc.docstatus != 0:
		frappe.throw("Item Mapping can only be changed on a Draft schedule.")

	mapping = json.loads(mapping_json)
	rows = [
		{
			"item_code": r.item_code,
			"item_name": r.item_name,
			"file_part_no": (r.file_part_no or r.item_code),
			"delivery_date": str(r.delivery_date),
			"quantity": r.quantity,
			"demand_type": r.demand_type,
			"auto_forecast": r.auto_forecast,
			"forecast_method": r.forecast_method,
		}
		for r in doc.schedule_items
	]
	remapped, _final = item_mapping.confirm(doc.customer, rows, mapping)

	doc.set("schedule_items", [])
	for row in remapped:
		doc.append(
			"schedule_items",
			{
				"item_code": row["item_code"],
				"item_name": row.get("item_name") or row["item_code"],
				"file_part_no": row.get("file_part_no"),
				"delivery_date": row["delivery_date"],
				"quantity": row["quantity"],
				"demand_type": row["demand_type"],
				"auto_forecast": row.get("auto_forecast", 0),
				"forecast_method": row.get("forecast_method"),
			},
		)
	doc.save(ignore_permissions=True)
	return {"status": "ok", "message": f"Item mapping updated on {doc.name}."}


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
def preview_forecast_method(rows_json, forecast_method, selected_items=None, target_months=None, item_methods=None):
	rows = json.loads(rows_json)
	selected = _parse_selected_items(selected_items)

	if selected is not None:
		rows = [r for r in rows if r["item_code"] in selected]

	if not rows:
		return []

	preview = forecast_engine.build_preview(
		rows, forecast_method, target_months, item_methods=_parse_item_methods(item_methods)
	)
	return safety.annotate_preview(preview, rows)


@frappe.whitelist()
def preview_forecast_for_schedule(
	schedule_name, forecast_method, selected_items=None, target_months=None, item_methods=None
):
	"""Same as preview_forecast_method, but for the 'Extend Forecast' dialog
	on an already-saved Draft schedule, where the browser has no cached
	parsed-file rows to send back - so the rows are read from the doc."""

	selected = _parse_selected_items(selected_items)

	rows = frappe.get_all(
		"Customer Schedule Item",
		filters={"parent": schedule_name},
		fields=["item_code", "item_name", "delivery_date", "quantity", "demand_type", "auto_forecast"],
	)
	horizon_end = frappe.db.get_value("Customer Schedule", schedule_name, "horizon_end")
	rows = [dict(r) for r in rows if flt(r.quantity) > 0]
	for r in rows:
		r["delivery_date"] = str(r["delivery_date"])
		r["horizon_end"] = str(horizon_end) if horizon_end else None

	if selected is not None:
		rows = [r for r in rows if r["item_code"] in selected]

	if not rows:
		return []

	preview = forecast_engine.build_preview(
		rows, forecast_method, target_months, item_methods=_parse_item_methods(item_methods)
	)
	return safety.annotate_preview(preview, rows)


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
	item_methods=None,
	file_check_json=None,
):
	try:
		target_months = int(target_months)
	except (TypeError, ValueError):
		frappe.throw("Target months must be a valid number.")
	if target_months <= 0:
		frappe.throw("Forecast months must be greater than zero.")

	forecast_method = forecast_engine.validate_method(forecast_method)
	selected = _parse_selected_items(selected_items)
	methods = _parse_item_methods(item_methods)

	if header_json and rows_json:
		header = json.loads(header_json)
		rows = json.loads(rows_json)
	else:
		# Cache missed (e.g. dialog reopened) - fall back to a fresh parse.
		header, rows, schedule_format = file_parsers.extract_header_and_rows(file_url, customer)
		rows = [
			dict(
				r,
				file_part_no=r["item_code"],
				file_description=r.get("item_name") or r["item_code"],
				horizon_end=header.get("horizon_end"),
			)
			for r in rows
		]
		rows = item_mapping.apply_saved_automatically(customer, rows)

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

	all_rows = forecast_engine.extend_rows(
		rows, target_months, forecast_method, selected_items=selected, item_methods=methods
	)
	part_by_item = {r["item_code"]: r.get("file_part_no") for r in rows if r.get("file_part_no")}
	# only items that take part in auto-forecast get a method recorded
	label_by_item = _method_labels(
		{r["item_code"] for r in rows if selected is None or r["item_code"] in selected}, forecast_method, methods
	)

	included_qty = round(sum(flt(r["quantity"]) for r in rows), 2)
	file_check = json.loads(file_check_json) if file_check_json else build_file_check(rows)
	skipped_parts = file_check.get("skipped") or []
	skipped_qty = round(sum(flt(s_.get("qty")) for s_ in skipped_parts), 2)

	schedule_doc = frappe.get_doc(
		{
			"doctype": "Customer Schedule",
			"naming_series": "CUST-SCH-.YYYY.-",
			"file_total_qty": flt(file_check.get("total_qty")) or round(included_qty + skipped_qty, 2),
			"file_included_qty": included_qty,
			"file_line_count": len(rows),
			"file_months": available_months,
			"file_from_date": file_check.get("from_date") or None,
			"file_to_date": file_check.get("to_date") or None,
			"skipped_part_count": len(skipped_parts),
			"skipped_qty": skipped_qty,
			"skipped_parts_json": json.dumps(skipped_parts) if skipped_parts else None,
			"customer": customer,
			"supplier_vendor": header["supplier_vendor"],
			"release_number": header["release_number"],
			"schedule_date": header["schedule_date"],
			"horizon_end": header.get("horizon_end"),
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
				"file_part_no": row.get("file_part_no") or part_by_item.get(row["item_code"]),
				"delivery_date": row["delivery_date"],
				"quantity": row["quantity"],
				"demand_type": row["demand_type"],
				"auto_forecast": row.get("auto_forecast", 0),
				"forecast_method": label_by_item.get(row["item_code"]),
			},
		)

	schedule_doc.insert(ignore_permissions=True)
	save_forecast_methods(customer, forecast_method, methods, label_by_item.keys())

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
def extend_forecast(schedule_name, target_months, forecast_method, selected_items=None, item_methods=None):
	doc = frappe.get_doc("Customer Schedule", schedule_name)
	if doc.docstatus != 0:
		frappe.throw(
			"Only a Draft schedule can be extended here. For a submitted "
			"schedule, upload the customer's next schedule instead - it will "
			"amend the relevant Sales Order/Forecast automatically."
		)

	forecast_method = forecast_engine.validate_method(forecast_method)
	selected = _parse_selected_items(selected_items)
	methods = _parse_item_methods(item_methods)
	target_months = int(target_months)

	existing_rows = [
		{
			"item_code": r.item_code,
			"item_name": r.item_name,
			"delivery_date": str(r.delivery_date),
			"quantity": r.quantity,
			"demand_type": r.demand_type,
			"auto_forecast": r.auto_forecast,
			"horizon_end": str(doc.horizon_end) if doc.horizon_end else None,
		}
		for r in doc.schedule_items
	]

	all_rows = forecast_engine.extend_rows(
		existing_rows, target_months, forecast_method, selected_items=selected, item_methods=methods
	)
	label_by_item = _method_labels(
		{r.item_code for r in doc.schedule_items if selected is None or r.item_code in selected},
		forecast_method,
		methods,
	)
	# rows already on the schedule carry the method now chosen for their item
	for r in doc.schedule_items:
		if r.item_code in label_by_item:
			r.forecast_method = label_by_item[r.item_code]

	existing_keys = {(r.item_code, str(r.delivery_date)) for r in doc.schedule_items}
	part_by_item = {r.item_code: r.file_part_no for r in doc.schedule_items if r.file_part_no}
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
				"file_part_no": part_by_item.get(row["item_code"]),
				"delivery_date": row["delivery_date"],
				"quantity": row["quantity"],
				"demand_type": row["demand_type"],
				"auto_forecast": row.get("auto_forecast", 0),
				"forecast_method": label_by_item.get(row["item_code"]),
			},
		)
		existing_keys.add(key)
		added += 1

	if added or label_by_item:
		doc.save(ignore_permissions=True)
		save_forecast_methods(doc.customer, forecast_method, methods, label_by_item.keys())

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
		fields=["item_code", "item_name", "delivery_date", "quantity", "demand_type", "auto_forecast", "forecast_method"],
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
				# method chosen for this item last time (shown as the dropdown's start value)
				"forecast_method": next(
					(forecast_engine.method_from_label(r.forecast_method) for r in item_rows if r.forecast_method), None
				),
			}
		)

	customer = frappe.db.get_value("Customer Schedule", schedule_name, "customer")
	return {
		"default_forecast_method": get_saved_forecast_methods(customer)[0] or "LAST_4_WEEKS_AVG",
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


@frappe.whitelist()
def get_demand_breakup(item_code, planning_months=None):
	"""Row-by-row source of an item's Total Demand (dashboard double-click)."""
	from schedule_planner.utils import material_request as mr_utils

	return mr_utils.get_demand_breakup(item_code, int(planning_months) if planning_months else None)


# ---------------------------------------------------------------------------
# 6. Safety checks (Pre-submit Check, forecast limit approval)
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_presubmit_report(schedule_name):
	return safety.get_report(schedule_name)


@frappe.whitelist()
def acknowledge_presubmit(schedule_name):
	return safety.acknowledge_skipped(schedule_name)


@frappe.whitelist()
def approve_forecast_limit(schedule_name, note):
	return safety.approve_forecast_limit(schedule_name, note)
