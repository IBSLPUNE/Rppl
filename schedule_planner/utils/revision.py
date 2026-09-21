# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

"""Revision detection: compares a Customer Schedule about to be submitted
against the last *submitted* schedule for the same customer + vendor, and
records what changed - separately for Firm and Forecast/Raw rows - so the
client can see, and the business can report on, how often a customer revises
their commitments.

Only customer-supplied rows (auto_forecast = 0) are compared. Rows this
system generated itself (auto_forecast = 1) are our own extrapolation, not a
customer revision, and must never count towards these stats.
"""

import frappe
from frappe.utils import flt, today

# Raw is treated as part of the Forecast bucket for comparison purposes,
# same as the original prototype did.
_BUCKET = {"Firm": "Firm", "Forecast": "Forecast", "Raw": "Forecast"}


def _bucket(demand_type):
	return _BUCKET.get(demand_type, "Forecast")


def detect_and_log_revisions(doc):
	"""Mutates doc.revision_log (appends rows) and doc.schedule_items
	(sets previous_quantity for reference). Returns a summary dict."""

	previous_name = frappe.db.get_value(
		"Customer Schedule",
		{
			"customer": doc.customer,
			"supplier_vendor": doc.supplier_vendor,
			"docstatus": 1,
			"name": ["!=", doc.name],
		},
		"name",
		order_by="schedule_date desc, creation desc",
	)

	if not previous_name:
		return {"firm_changes": 0, "forecast_changes": 0, "previous_schedule": None}

	prev_rows = frappe.db.get_all(
		"Customer Schedule Item",
		filters={"parent": previous_name, "auto_forecast": 0},
		fields=["item_code", "delivery_date", "quantity", "demand_type"],
	)
	old_map = {}
	for row in prev_rows:
		key = (row.item_code, str(row.delivery_date), _bucket(row.demand_type))
		old_map[key] = old_map.get(key, 0) + flt(row.quantity)

	new_map = {}
	for row in doc.schedule_items:
		if row.auto_forecast:
			continue
		if flt(row.quantity) <= 0:
			continue
		key = (row.item_code, str(row.delivery_date), _bucket(row.demand_type))
		new_map[key] = new_map.get(key, 0) + flt(row.quantity)
		row.previous_quantity = old_map.get(key, 0)

	firm_changes = 0
	forecast_changes = 0

	all_keys = set(old_map.keys()) | set(new_map.keys())
	for key in sorted(all_keys):
		item_code, delivery_date, bucket = key
		old_qty = flt(old_map.get(key, 0))
		new_qty = flt(new_map.get(key, 0))
		if old_qty == new_qty:
			continue

		change = new_qty - old_qty
		change_pct = round((change / old_qty) * 100, 2) if old_qty else None

		doc.append(
			"revision_log",
			{
				"item_code": item_code,
				"delivery_date": delivery_date,
				"demand_type": bucket,
				"old_qty": old_qty,
				"new_qty": new_qty,
				"change": change,
				"change_pct": change_pct,
				"previous_schedule": previous_name,
				"revision_date": today(),
			},
		)

		if bucket == "Firm":
			firm_changes += 1
		else:
			forecast_changes += 1

	return {
		"firm_changes": firm_changes,
		"forecast_changes": forecast_changes,
		"previous_schedule": previous_name,
	}


def apply_revision_stats(customer, result, schedule_name):
	"""Upserts the Customer Revision Stats counter row for this customer."""

	exists = frappe.db.exists("Customer Revision Stats", customer)

	if not result.get("previous_schedule"):
		# First schedule ever for this customer+vendor pair - nothing to
		# compare, but make sure a stats row exists so reports don't break.
		if not exists:
			frappe.get_doc(
				{
					"doctype": "Customer Revision Stats",
					"customer": customer,
					"total_firm_revisions": 0,
					"total_forecast_revisions": 0,
					"last_revision_date": today(),
					"last_schedule": schedule_name,
				}
			).insert(ignore_permissions=True)
		return

	if not (result.get("firm_changes") or result.get("forecast_changes")):
		return

	if exists:
		stats = frappe.get_doc("Customer Revision Stats", customer)
	else:
		stats = frappe.get_doc(
			{
				"doctype": "Customer Revision Stats",
				"customer": customer,
				"total_firm_revisions": 0,
				"total_forecast_revisions": 0,
			}
		)

	stats.total_firm_revisions = flt(stats.total_firm_revisions or 0) + result.get("firm_changes", 0)
	stats.total_forecast_revisions = flt(stats.total_forecast_revisions or 0) + result.get(
		"forecast_changes", 0
	)
	stats.last_revision_date = today()
	stats.last_schedule = schedule_name

	if stats.is_new():
		stats.insert(ignore_permissions=True)
	else:
		stats.save(ignore_permissions=True)
