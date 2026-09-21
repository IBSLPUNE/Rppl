# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

import frappe


def ensure_item_exists(item_code, item_name=None):
	if frappe.db.exists("Item", item_code):
		return

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
