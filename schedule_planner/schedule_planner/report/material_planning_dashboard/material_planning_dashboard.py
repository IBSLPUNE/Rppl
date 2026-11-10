# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

"""Live, per-item view of exactly what the Material Request reconciler
itself computed - demand (schedule-based or shipping-fallback), stock, open
PO, other open MR, net shortfall/surplus, and whatever is currently sitting
on the live Auto Material Request. This is deliberately the SAME function
(schedule_planner.utils.material_request.compute_planning_snapshot) that
reconcile_material_requests() uses to decide what to buy, so this report can
never show a different picture than what the live MR actually reflects -
it's the transparency layer on top of the same one source of truth."""

import frappe
from frappe import _

from frappe.utils import getdate

from schedule_planner.utils import material_request


def execute(filters=None):
	filters = frappe._dict(filters or {})

	snapshot = material_request.compute_planning_snapshot(
		planning_months=filters.get("planning_months") or None,
		lead_time_months=filters.get("lead_time_months") or None,
	)

	rows = snapshot["rows"]
	status_filter = filters.get("status")
	if status_filter:
		rows = [r for r in rows if r["status"] == status_filter]

	item_names, item_uoms = {}, {}
	if rows:
		for d in frappe.get_all(
			"Item",
			filters={"name": ["in", [r["item_code"] for r in rows]]},
			fields=["name", "item_name", "stock_uom"],
		):
			item_names[d.name] = d.item_name
			item_uoms[d.name] = d.stock_uom

	data = []
	for r in rows:
		data.append(
			{
				"item_code": r["item_code"],
				"item_name": item_names.get(r["item_code"], r["item_code"]),
				"uom": item_uoms.get(r["item_code"]) or "Nos",
				"demand_source": r["demand_source"],
				"total_demand": r["total_demand"],
				"current_stock": r["current_stock"],
				"open_po_qty": r["open_po_qty"],
				"other_open_mr_qty": r["other_open_mr_qty"],
				"available_qty": r["available_qty"],
				"reorder_level": r["reorder_level"] or None,
				"reorder_check": r["reorder_check"],
				"net_shortfall": r["net_shortfall"],
				"net_surplus": r["net_surplus"],
				"min_order_qty": r["min_order_qty"],
				"order_qty": r["order_qty"],
				"moq_extra": r["moq_extra"],
				"monthly_demand": r["monthly_demand"],
				"cover_after": r["cover_after"],
				"cover_flag": r["cover_flag"],
				"auto_mr_qty": r["auto_mr_qty"],
				"mr_decision": r["mr_decision"],
				"status": r["status"],
			}
		)

	columns = [
		{"label": _("Item Code"), "fieldname": "item_code", "fieldtype": "Link", "options": "Item", "width": 130},
		{"label": _("Item Name"), "fieldname": "item_name", "fieldtype": "Data", "width": 170},
		{"label": _("UOM (all qty)"), "fieldname": "uom", "fieldtype": "Data", "width": 90},
		{"label": _("Demand Source"), "fieldname": "demand_source", "fieldtype": "Data", "width": 190},
		{"label": _("Total Demand"), "fieldname": "total_demand", "fieldtype": "Float", "width": 110},
		{"label": _("Current Stock"), "fieldname": "current_stock", "fieldtype": "Float", "width": 110},
		{"label": _("Open PO Qty"), "fieldname": "open_po_qty", "fieldtype": "Float", "width": 100},
		{"label": _("Other Open MR Qty"), "fieldname": "other_open_mr_qty", "fieldtype": "Float", "width": 130},
		{"label": _("Available (Stock+PO+MR)"), "fieldname": "available_qty", "fieldtype": "Float", "width": 150},
		{"label": _("Re-order Level"), "fieldname": "reorder_level", "fieldtype": "Float", "width": 110},
		{"label": _("Reorder Check"), "fieldname": "reorder_check", "fieldtype": "Data", "width": 150},
		{"label": _("Net Shortfall"), "fieldname": "net_shortfall", "fieldtype": "Float", "width": 110},
		{"label": _("Net Surplus"), "fieldname": "net_surplus", "fieldtype": "Float", "width": 100},
		{"label": _("Min Order Qty"), "fieldname": "min_order_qty", "fieldtype": "Float", "width": 110},
		{"label": _("Order Qty (after MOQ)"), "fieldname": "order_qty", "fieldtype": "Float", "width": 140},
		{"label": _("Extra Due To MOQ"), "fieldname": "moq_extra", "fieldtype": "Float", "width": 120},
		{"label": _("Monthly Demand"), "fieldname": "monthly_demand", "fieldtype": "Float", "width": 110},
		{"label": _("Cover After Order (Months)"), "fieldname": "cover_after", "fieldtype": "Float", "precision": 1, "width": 150},
		{"label": _("On Live Auto MR"), "fieldname": "auto_mr_qty", "fieldtype": "Float", "width": 110},
		{"label": _("MR Decision"), "fieldname": "mr_decision", "fieldtype": "Data", "width": 300},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 90},
	]

	pool_note = (
		f"Live Auto Material Request: <a href=\"/app/material-request/{snapshot['pool_mr']}\">{snapshot['pool_mr']}</a>."
		if snapshot["pool_mr"]
		else "No live Auto Material Request right now - nothing is short."
	)
	message = (
		f"<b>All quantities are in the item's stock UOM (Nos).</b> Purchase Order, Material Request and "
		f"Delivery Note quantities raised in another UOM are converted to Nos first. "
		f"Demand window: {getdate(snapshot['start_date']):%d-%b-%Y} to {getdate(snapshot['end_date']):%d-%b-%Y} "
		f"({snapshot['planning_months']}M horizon, {snapshot['grace_days']}d overdue grace). "
		f"Required-by date used for new lines: {getdate(snapshot['required_by']):%d-%b-%Y} "
		f"({snapshot['lead_time_months']}M lead time). "
		f"A Material Request line is raised only when there is a shortfall and, for an item with a Re-order "
		f"Level, Available (Stock + Open PO + Other MR) is below that level; qty = greatest of net shortfall, "
		f"Min Order Qty and Re-order Qty. "
		f"Monthly Demand = the sum of each customer's own monthly rate (its quantity / the months its schedule "
		f"covers). Cover After Order = how many months (Available + Order Qty) lasts when used up week by week "
		f"against each week's demand; above {snapshot['cover_limit']:g} months "
		f"it is shown in red and the Material Request needs approval. {pool_note}"
	)

	return columns, data, message
