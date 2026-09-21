# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

"""Turns a submitted Customer Schedule's Firm rows into per-delivery-date
Sales Orders, and its Forecast/Raw rows into one Sales Forecast per
customer - amending (cancel + copy + resubmit) the existing document instead
of creating a duplicate whenever a new schedule changes what a date/item
already committed to.

NOTE on the "Sales Forecast" doctype: this module assumes the same field
names the client's original prototype used against their live site
(company, posting_date, from_date, frequency, demand_number, parent_warehouse
on the parent; item_code, item_name, uom, demand_qty, delivery_date,
warehouse on both the "selected_items" and "items" child tables). If your
site's Sales Forecast doctype differs, adjust `_build_sales_forecast_doc`
below to match.
"""

from collections import defaultdict

import frappe
from frappe.utils import flt, getdate


# ---------------------------------------------------------------------------
# Shared defaults
# ---------------------------------------------------------------------------

def _settings():
	return frappe.get_cached_doc("Schedule Planning Settings")


def _default_company():
	company = _settings().default_company
	if company:
		return company
	company = frappe.db.get_single_value("Global Defaults", "default_company")
	if not company:
		frappe.throw(
			"No default company configured. Set Default Company in "
			"Schedule Planning Settings."
		)
	return company


def _default_warehouse(company):
	warehouse = _settings().default_warehouse
	if warehouse:
		return warehouse
	warehouse = frappe.db.get_value("Warehouse", {"is_group": 0, "company": company}, "name", order_by="name")
	if not warehouse:
		frappe.throw(
			f"No warehouse found for company {company}. Set Default Warehouse "
			"in Schedule Planning Settings."
		)
	return warehouse


def _item_rate(item_code):
	return frappe.db.get_value("Item", item_code, "standard_rate") or 0


def _item_name(item_code):
	return frappe.db.get_value("Item", item_code, "item_name") or item_code


def _item_uom(item_code):
	return frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"


# ---------------------------------------------------------------------------
# Firm rows -> one Sales Order per delivery date
# ---------------------------------------------------------------------------

def sync_firm_to_sales_orders(doc):
	firm_rows = [r for r in doc.schedule_items if r.demand_type == "Firm" and flt(r.quantity) > 0]
	if not firm_rows:
		return

	company = _default_company()
	warehouse = _default_warehouse(company)

	by_date = defaultdict(list)
	for row in firm_rows:
		by_date[str(row.delivery_date)].append(row)

	for delivery_date, rows_for_date in by_date.items():
		_sync_one_date_so(doc, delivery_date, rows_for_date, company, warehouse)


def _sync_one_date_so(doc, delivery_date, rows_for_date, company, warehouse):
	new_items = {}
	for row in rows_for_date:
		new_items[row.item_code] = flt(new_items.get(row.item_code, 0)) + flt(row.quantity)

	existing_name = frappe.db.get_value(
		"Sales Order",
		{
			"customer": doc.customer,
			"custom_sp_is_schedule_generated": 1,
			"custom_sp_delivery_date": delivery_date,
			"docstatus": 1,
		},
		"name",
	)

	if existing_name:
		existing_items = {
			r.item_code: flt(r.qty)
			for r in frappe.get_all(
				"Sales Order Item", filters={"parent": existing_name}, fields=["item_code", "qty"]
			)
		}
		if existing_items == new_items:
			for row in rows_for_date:
				row.linked_sales_order = existing_name
			return
		so_name = _amend_sales_order(existing_name, doc, delivery_date, new_items, company, warehouse)
	else:
		so_name = _create_sales_order(doc, delivery_date, new_items, company, warehouse)

	for row in rows_for_date:
		row.linked_sales_order = so_name


def _append_so_items(so, new_items, warehouse, delivery_date):
	for item_code, qty in new_items.items():
		so.append(
			"items",
			{
				"item_code": item_code,
				"qty": qty,
				"rate": _item_rate(item_code),
				"warehouse": warehouse,
				"delivery_date": delivery_date,
			},
		)


def _create_sales_order(doc, delivery_date, new_items, company, warehouse):
	so = frappe.new_doc("Sales Order")
	so.customer = doc.customer
	so.company = company
	so.transaction_date = doc.schedule_date or getdate()
	so.delivery_date = delivery_date
	so.custom_sp_is_schedule_generated = 1
	so.custom_sp_delivery_date = delivery_date
	so.custom_sp_customer_schedule = doc.name

	_append_so_items(so, new_items, warehouse, delivery_date)

	so.insert(ignore_permissions=True)
	so.submit()
	return so.name


def _amend_sales_order(existing_name, doc, delivery_date, new_items, company, warehouse):
	old_so = frappe.get_doc("Sales Order", existing_name)
	if old_so.docstatus == 1:
		old_so.flags.ignore_permissions = True
		old_so.cancel()

	amended = frappe.copy_doc(old_so)
	amended.amended_from = existing_name
	amended.customer = doc.customer
	amended.company = company
	amended.transaction_date = doc.schedule_date or getdate()
	amended.delivery_date = delivery_date
	amended.custom_sp_is_schedule_generated = 1
	amended.custom_sp_delivery_date = delivery_date
	amended.custom_sp_customer_schedule = doc.name
	amended.items = []

	_append_so_items(amended, new_items, warehouse, delivery_date)

	amended.insert(ignore_permissions=True)
	amended.submit()
	return amended.name


# ---------------------------------------------------------------------------
# Forecast/Raw rows -> one Sales Forecast per customer, wholesale replaced
# ---------------------------------------------------------------------------

def sync_forecast_to_sales_forecast(doc):
	forecast_rows = [
		r for r in doc.schedule_items if r.demand_type in ("Forecast", "Raw") and flt(r.quantity) > 0
	]
	if not forecast_rows:
		return

	company = _default_company()
	warehouse = _default_warehouse(company)

	new_items = []
	new_signature = {}
	for row in forecast_rows:
		key = (row.item_code, str(row.delivery_date))
		new_signature[key] = flt(new_signature.get(key, 0)) + flt(row.quantity)
	for (item_code, delivery_date), qty in new_signature.items():
		new_items.append({"item_code": item_code, "delivery_date": delivery_date, "demand_qty": qty})

	existing_name = frappe.db.get_value(
		"Sales Forecast",
		{"custom_sp_customer": doc.customer, "custom_sp_is_schedule_generated": 1, "docstatus": 1},
		"name",
		order_by="creation desc",
	)

	if existing_name:
		existing_doc = frappe.get_doc("Sales Forecast", existing_name)
		existing_signature = {}
		for row in existing_doc.get("items", []):
			key = (row.item_code, str(row.delivery_date))
			existing_signature[key] = flt(existing_signature.get(key, 0)) + flt(row.demand_qty)

		if existing_signature == new_signature:
			sf_name = existing_name
		else:
			sf_name = _amend_sales_forecast(existing_doc, doc, new_items, company, warehouse)
	else:
		sf_name = _create_sales_forecast(doc, new_items, company, warehouse)

	for row in forecast_rows:
		row.linked_sales_forecast = sf_name


def _append_forecast_items(fc, new_items, warehouse):
	for item in new_items:
		row = {
			"item_code": item["item_code"],
			"item_name": _item_name(item["item_code"]),
			"uom": _item_uom(item["item_code"]),
			"delivery_date": item["delivery_date"],
			"demand_qty": item["demand_qty"],
			"warehouse": warehouse,
		}
		fc.append("selected_items", dict(row))
		fc.append("items", dict(row))


def _build_sales_forecast_doc(doc, company, warehouse):
	fc = frappe.new_doc("Sales Forecast")
	fc.naming_series = "SF.YY.-."
	fc.company = company
	fc.posting_date = doc.schedule_date or getdate()
	fc.frequency = "Weekly"
	fc.demand_number = 6
	fc.parent_warehouse = warehouse
	fc.custom_sp_is_schedule_generated = 1
	fc.custom_sp_customer = doc.customer
	fc.custom_sp_customer_schedule = doc.name
	return fc


def _create_sales_forecast(doc, new_items, company, warehouse):
	fc = _build_sales_forecast_doc(doc, company, warehouse)
	if new_items:
		fc.from_date = min(item["delivery_date"] for item in new_items)
	_append_forecast_items(fc, new_items, warehouse)
	fc.insert(ignore_permissions=True)
	fc.submit()
	return fc.name


def _amend_sales_forecast(existing_doc, doc, new_items, company, warehouse):
	if existing_doc.docstatus == 1:
		existing_doc.flags.ignore_permissions = True
		existing_doc.cancel()

	amended = frappe.copy_doc(existing_doc)
	amended.amended_from = existing_doc.name
	amended.company = company
	amended.posting_date = doc.schedule_date or getdate()
	amended.parent_warehouse = warehouse
	amended.custom_sp_is_schedule_generated = 1
	amended.custom_sp_customer = doc.customer
	amended.custom_sp_customer_schedule = doc.name
	amended.selected_items = []
	amended.items = []
	if new_items:
		amended.from_date = min(item["delivery_date"] for item in new_items)

	_append_forecast_items(amended, new_items, warehouse)

	amended.insert(ignore_permissions=True)
	amended.submit()
	return amended.name
