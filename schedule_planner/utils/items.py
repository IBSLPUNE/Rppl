# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import cint


def ensure_item_exists(item_code, item_name=None, allow_create=None):
	"""Guarantee the Item exists. An Item is only ever created here when the
	caller says so explicitly (allow_create=True, e.g. the user ticked "Create
	Item" in the Item Mapping step) or the "Auto-create Item" setting is on -
	otherwise an unknown part number is an error the user must resolve by
	mapping it, so a typo or a customer's own number can never silently turn
	into a junk Item that then drives purchasing."""

	if frappe.db.exists("Item", item_code):
		return

	if allow_create is None:
		allow_create = cint(frappe.db.get_single_value("Schedule Planning Settings", "auto_create_missing_items"))
	if not allow_create:
		frappe.throw(
			f"Item <b>{item_code}</b> does not exist. Map the customer's part number to an existing Item "
			"in the Item Mapping step (or tick Create Item there).",
			title="Item Not Found",
		)

	item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups"
	uom = frappe.db.get_value("UOM", "Nos", "name") or "Nos"

	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_name or item_code,
			"item_group": item_group,
			"stock_uom": uom,
			"is_stock_item": 1,
			"opening_stock": 0,
			"description": f"Auto-created from Schedule Planner. {item_name or item_code}",
		}
	).insert(ignore_permissions=True)
