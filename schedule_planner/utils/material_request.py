# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

"""Keeps ONE live, continuously-reconciled Draft "Auto Material Request" in
sync with actual net requirement, instead of creating a fresh MR document
every time something changes. Every call to reconcile_material_requests()
recomputes net requirement from scratch and then:

  - adds a line for any item that just went short,
  - raises/lowers the quantity on an existing line as demand or supply moves,
  - removes a line (or deletes the whole MR, if nothing is left short) for
    any item that is no longer short - e.g. a schedule got cancelled, a
    Purchase Order came in, or a Delivery Note used up stock differently
    than expected.

Net requirement, per item, over the planning horizon:

    Demand  = Firm + Forecast/Raw from the latest submitted Customer Schedule
              per customer+item+date (a firm delivery date up to
              "Overdue Grace Period" days in the past still counts, in case
              it genuinely hasn't shipped yet), PLUS - only for an item with
              NO active schedule coverage at all - a forecast derived from
              real shipping history (Delivery Note), so a customer who never
              sends a formal schedule doesn't silently fall out of planning.

    Supply  = current stock (Bin) + open Purchase Order qty not yet received
              + any OTHER open Material Request for that item not yet
              converted to a PO - so this never asks for material that is
              already on order or already stocked.

    Shortfall = Demand - Supply.

Callers (all funnel into the same reconcile_material_requests engine, so the
live MR is always internally consistent no matter what triggered the run):
  - run_immediate_mr_check(customer)     - after one customer's schedule is
                                             submitted, when trigger mode is
                                             "Immediate"
  - run_scheduled_mr_generation()        - the daily/weekly scheduler tick,
                                             when trigger mode is "Scheduled"
  - adjust_for_schedule_cancel(customer) - after a Customer Schedule is
                                             cancelled - always runs,
                                             regardless of trigger mode, so
                                             the live MR shrinks/clears
                                             promptly rather than over-buying
  - on_delivery_note_change(doc, method) - after any Delivery Note is
                                             submitted/cancelled - always
                                             runs, so a shipment with no
                                             schedule behind it still gets
                                             picked up
  - generate_material_request_manual()   - the "Generate Material Request"
                                             button in api.py, works
                                             regardless of trigger mode
"""

from datetime import timedelta

import frappe
from frappe.utils import add_months, cint, flt, getdate, today


def _settings():
	return frappe.get_cached_doc("Schedule Planning Settings")


def _company_or_none(settings):
	return settings.default_company or frappe.db.get_single_value("Global Defaults", "default_company")


def _resolve_warehouse(settings, company, override=None):
	warehouse = override or settings.default_warehouse
	if not warehouse and company:
		wh = frappe.db.sql(
			"SELECT name FROM `tabWarehouse` WHERE is_group=0 AND company=%s ORDER BY name LIMIT 1",
			(company,),
			as_dict=True,
		)
		warehouse = wh[0]["name"] if wh else None
	if not warehouse:
		frappe.throw(
			"No warehouse configured. Set <b>Default Warehouse</b> in Schedule "
			"Planning Settings, or create at least one non-group Warehouse for "
			f"company {company}."
		)
	return warehouse


# ---------------------------------------------------------------------------
# Demand: schedule-based (the normal case)
# ---------------------------------------------------------------------------

def _schedule_demand_map(start_date, end_date):
	"""Global across ALL customers - deliberately not filtered to whichever
	customer's schedule triggered this run. A shared item can be needed by
	more than one customer, and the live Material Request is a single
	item-level pool, so a per-customer-scoped recompute would corrupt it by
	silently dropping other customers' demand for the same item."""

	rows = frappe.db.sql(
		"""
		SELECT item_code, SUM(quantity) AS total_required_qty
		FROM (
			SELECT
				csi.item_code,
				csi.quantity,
				ROW_NUMBER() OVER (
					PARTITION BY cs.customer, csi.item_code, csi.delivery_date
					ORDER BY cs.schedule_date DESC, cs.creation DESC
				) AS rn
			FROM `tabCustomer Schedule Item` csi
			INNER JOIN `tabCustomer Schedule` cs ON cs.name = csi.parent
			WHERE
				cs.docstatus = 1
				AND csi.demand_type IN ('Forecast', 'Raw', 'Firm')
				AND csi.quantity > 0
				AND csi.delivery_date >= %(start)s
				AND csi.delivery_date < %(end)s
		) ranked
		WHERE rn = 1
		GROUP BY item_code
		""",
		{"start": str(start_date), "end": str(end_date)},
		as_dict=True,
	)
	return {r.item_code: flt(r.total_required_qty) for r in rows}


# ---------------------------------------------------------------------------
# Demand: shipping-history fallback (no active schedule for this item at all)
# ---------------------------------------------------------------------------

def _shipping_fallback_map(covered_items, company, months, planning_months):
	"""For an item with real Delivery Note history but no active Customer
	Schedule coverage in the current horizon, project the next
	`planning_months` of demand from its average weekly shipping rate over
	the last `months`. Never touches an item already covered by
	_schedule_demand_map - this only fills the gap, it never overrides an
	actual customer-supplied number."""

	from_date = add_months(getdate(today()), -months)
	params = {"from_date": str(from_date), "today": str(getdate(today()))}
	company_sql = ""
	if company:
		company_sql = "AND dn.company = %(company)s"
		params["company"] = company

	rows = frappe.db.sql(
		f"""
		SELECT dni.item_code, SUM(dni.qty) AS shipped_qty
		FROM `tabDelivery Note Item` dni
		INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
		WHERE dn.docstatus = 1
		  AND dn.posting_date >= %(from_date)s
		  AND dn.posting_date <= %(today)s
		  {company_sql}
		GROUP BY dni.item_code
		""",
		params,
		as_dict=True,
	)

	weeks_in_window = max(flt(months) * 4.33, 1.0)
	weeks_remaining = max(flt(planning_months) * 4.33, 1.0)

	fallback = {}
	for r in rows:
		if r.item_code in covered_items:
			continue
		shipped = flt(r.shipped_qty)
		if shipped <= 0:
			continue
		weekly_avg = shipped / weeks_in_window
		fallback[r.item_code] = round(weekly_avg * weeks_remaining, 2)
	return fallback


# ---------------------------------------------------------------------------
# Supply: stock, open PO, other open MR
# ---------------------------------------------------------------------------

def _stock_map(items):
	if not items:
		return {}
	rows = frappe.db.sql(
		"SELECT item_code, SUM(actual_qty) AS stock_qty FROM `tabBin` WHERE item_code IN %(items)s GROUP BY item_code",
		{"items": items},
		as_dict=True,
	)
	return {r.item_code: flt(r.stock_qty) for r in rows}


def _open_po_map(items, company):
	"""Quantity already on order but not yet received - counting this as
	supply is what stops the reconciler from raising a second Material
	Request for something that's already on its way."""

	if not items:
		return {}
	params = {"items": items}
	company_sql = ""
	if company:
		company_sql = "AND po.company = %(company)s"
		params["company"] = company

	rows = frappe.db.sql(
		f"""
		SELECT poi.item_code, SUM(poi.qty - IFNULL(poi.received_qty, 0)) AS open_qty
		FROM `tabPurchase Order Item` poi
		INNER JOIN `tabPurchase Order` po ON po.name = poi.parent
		WHERE po.docstatus = 1
		  AND po.status NOT IN ('Completed', 'Closed', 'Cancelled')
		  AND poi.item_code IN %(items)s
		  {company_sql}
		GROUP BY poi.item_code
		""",
		params,
		as_dict=True,
	)
	return {r.item_code: max(flt(r.open_qty), 0.0) for r in rows}


def _other_open_mr_map(items, company, exclude_name=None):
	"""Quantity already sitting on some OTHER open (Draft or Submitted, not
	yet fully converted to a PO) Material Request - ours or a human's -
	counted as supply for the same reason as open PO qty above.
	`exclude_name` is always our own live pool MR, so it never subtracts
	itself from its own shortfall calculation."""

	if not items:
		return {}
	conditions = ["mr.docstatus IN (0, 1)", "mr.material_request_type = 'Purchase'"]
	params = {"items": items}
	if company:
		conditions.append("mr.company = %(company)s")
		params["company"] = company
	if exclude_name:
		conditions.append("mr.name != %(exclude_name)s")
		params["exclude_name"] = exclude_name
	where_sql = " AND ".join(conditions)

	rows = frappe.db.sql(
		f"""
		SELECT mri.item_code, SUM(mri.qty - IFNULL(mri.ordered_qty, 0)) AS open_qty
		FROM `tabMaterial Request Item` mri
		INNER JOIN `tabMaterial Request` mr ON mr.name = mri.parent
		WHERE {where_sql}
		  AND mri.item_code IN %(items)s
		GROUP BY mri.item_code
		""",
		params,
		as_dict=True,
	)
	return {r.item_code: max(flt(r.open_qty), 0.0) for r in rows}


# ---------------------------------------------------------------------------
# The live pool Material Request - one per company, always Draft
# ---------------------------------------------------------------------------

def _find_existing_pool_mr_name(company):
	filters = {"custom_sp_auto_generated": 1, "docstatus": 0, "material_request_type": "Purchase"}
	if company:
		filters["company"] = company
	names = frappe.get_all("Material Request", filters=filters, order_by="creation desc", limit=1, pluck="name")
	return names[0] if names else None


def _get_or_new_pool_mr(company):
	name = _find_existing_pool_mr_name(company)
	if name:
		return frappe.get_doc("Material Request", name)
	return frappe.new_doc("Material Request")


# ---------------------------------------------------------------------------
# Snapshot - the single source of truth used by BOTH the reconciler and the
# Material Planning Dashboard report, so the dashboard can never drift from
# what the live MR actually reflects.
# ---------------------------------------------------------------------------

def compute_planning_snapshot(planning_months=None, lead_time_months=None):
	settings = _settings()
	planning_months = int(planning_months or settings.default_planning_months or 12)
	lead_time_months = int(lead_time_months or settings.default_lead_time_months or 7)
	grace_days = int(settings.overdue_grace_days if settings.overdue_grace_days is not None else 30)

	today_date = getdate(today())
	start_date = today_date - timedelta(days=grace_days)
	end_date = add_months(today_date, planning_months)
	req_date = add_months(today_date, lead_time_months)

	company = _company_or_none(settings)

	schedule_demand = _schedule_demand_map(start_date, end_date)

	fallback_demand = {}
	fallback_on = settings.enable_shipping_fallback_forecast
	fallback_on = 1 if fallback_on is None else cint(fallback_on)
	if fallback_on:
		fallback_months = int(settings.shipping_fallback_months or 3)
		fallback_demand = _shipping_fallback_map(
			set(schedule_demand.keys()), company, fallback_months, planning_months
		)

	demand_map, source_map = {}, {}
	for item_code, qty in schedule_demand.items():
		demand_map[item_code] = qty
		source_map[item_code] = "Customer Schedule"
	for item_code, qty in fallback_demand.items():
		demand_map[item_code] = qty
		source_map[item_code] = "Shipping History (No Schedule)"

	items_in_scope = list(demand_map.keys())
	stock_map = _stock_map(items_in_scope)
	open_po_map = _open_po_map(items_in_scope, company)
	pool_name = _find_existing_pool_mr_name(company)
	other_open_mr_map = _other_open_mr_map(items_in_scope, company, exclude_name=pool_name)

	pool_items_map = {}
	if pool_name:
		pool_rows = frappe.get_all(
			"Material Request Item", filters={"parent": pool_name}, fields=["item_code", "qty"]
		)
		for r in pool_rows:
			pool_items_map[r.item_code] = flt(r.qty)

	rows = []
	for item_code in sorted(items_in_scope):
		demand = demand_map.get(item_code, 0.0)
		stock = stock_map.get(item_code, 0.0)
		open_po = open_po_map.get(item_code, 0.0)
		other_mr = other_open_mr_map.get(item_code, 0.0)
		net = demand - (stock + open_po + other_mr)
		if net > 0.01:
			status = "Shortfall"
		elif net < -0.01:
			status = "Surplus"
		else:
			status = "Covered"
		rows.append(
			{
				"item_code": item_code,
				"demand_source": source_map.get(item_code),
				"total_demand": demand,
				"current_stock": stock,
				"open_po_qty": open_po,
				"other_open_mr_qty": other_mr,
				"net_shortfall": max(net, 0.0),
				"net_surplus": max(-net, 0.0),
				"status": status,
				"auto_mr_qty": pool_items_map.get(item_code, 0.0),
			}
		)

	return {
		"rows": rows,
		"planning_months": planning_months,
		"lead_time_months": lead_time_months,
		"grace_days": grace_days,
		"start_date": str(start_date),
		"end_date": str(end_date),
		"required_by": str(req_date),
		"company": company,
		"pool_mr": pool_name,
	}


def _line_description(info, planning_months, lead_time_months):
	return (
		f"Auto MR [{info['demand_source']}]: Demand {info['total_demand']:,.0f} "
		f"- Stock {info['current_stock']:,.0f} - Open PO {info['open_po_qty']:,.0f} "
		f"- Other Open MR {info['other_open_mr_qty']:,.0f} = Shortfall "
		f"{info['net_shortfall']:,.0f} ({planning_months}M horizon, {lead_time_months}M lead time)"
	)


# ---------------------------------------------------------------------------
# The reconciler - the only place that ever writes the live pool MR
# ---------------------------------------------------------------------------

def reconcile_material_requests(planning_months=None, lead_time_months=None, warehouse=None, triggered_by=None):
	settings = _settings()
	snapshot = compute_planning_snapshot(planning_months, lead_time_months)
	company = snapshot["company"]

	if not company:
		frappe.throw(
			"No default company configured. Set <b>Default Company</b> in "
			"Schedule Planning Settings (or Global Defaults) before Material "
			"Requests can be generated."
		)

	lead_time_months = int(lead_time_months or settings.default_lead_time_months or 7)
	warehouse = _resolve_warehouse(settings, company, override=warehouse)
	req_date = snapshot["required_by"]

	pool = _get_or_new_pool_mr(company)
	existing_rows_by_item = {r.item_code: r for r in (pool.items or [])}

	shortfall_by_item = {r["item_code"]: r for r in snapshot["rows"] if r["net_shortfall"] > 0.01}
	covered = [r["item_code"] for r in snapshot["rows"] if r["net_shortfall"] <= 0.01]

	created, updated, removed = [], [], []

	if pool.items:
		keep = []
		for row in pool.items:
			if row.item_code in shortfall_by_item:
				keep.append(row)
			else:
				removed.append(row.item_code)
		pool.set("items", keep)

	for item_code, info in shortfall_by_item.items():
		qty = round(info["net_shortfall"], 2)
		desc = _line_description(info, snapshot["planning_months"], lead_time_months)
		row = existing_rows_by_item.get(item_code)
		still_present = row is not None and row in (pool.items or [])

		if still_present:
			changed = False
			if abs(flt(row.qty) - qty) > 0.01:
				row.qty = qty
				changed = True
			if row.get("custom_sp_demand_source") != info["demand_source"]:
				row.custom_sp_demand_source = info["demand_source"]
				changed = True
			row.description = desc
			if changed:
				updated.append(item_code)
		else:
			item_name = frappe.db.get_value("Item", item_code, "item_name") or item_code
			uom = (
				frappe.db.get_value("Item", item_code, "purchase_uom")
				or frappe.db.get_value("Item", item_code, "stock_uom")
				or "Nos"
			)
			pool.append(
				"items",
				{
					"item_code": item_code,
					"item_name": item_name,
					"qty": qty,
					"uom": uom,
					"stock_uom": uom,
					"warehouse": warehouse,
					"schedule_date": req_date,
					"description": desc,
					"custom_sp_demand_source": info["demand_source"],
				},
			)
			created.append(item_code)

	if not pool.items:
		message = "No shortfall right now - nothing to request."
		mr_name = None
		if not pool.is_new():
			mr_name_being_removed = pool.name
			frappe.delete_doc("Material Request", pool.name, ignore_permissions=True, force=True)
			message = (
				f"All shortfalls resolved - Auto Material Request <b>{mr_name_being_removed}</b> "
				f"had nothing left to request and was removed."
			)
		frappe.db.set_single_value("Schedule Planning Settings", "last_mr_reconciled_on", frappe.utils.now_datetime())
		return {
			"status": "ok",
			"mr_name": mr_name,
			"created": created,
			"updated": updated,
			"removed": removed,
			"covered": covered,
			"message": message,
			"triggered_by": triggered_by,
		}

	pool.custom_sp_auto_generated = 1
	pool.custom_sp_last_reconciled = frappe.utils.now_datetime()
	pool.material_request_type = "Purchase"
	pool.transaction_date = str(getdate(today()))
	pool.schedule_date = req_date
	pool.company = company
	pool.title = "Auto Material Request (Live Shortfall)"

	if pool.is_new():
		pool.naming_series = "MAT-MR-.YYYY.-"
		pool.insert(ignore_permissions=True)
	else:
		pool.save(ignore_permissions=True)

	frappe.db.set_single_value("Schedule Planning Settings", "last_mr_reconciled_on", frappe.utils.now_datetime())

	return {
		"status": "Success",
		"mr_name": pool.name,
		"created": created,
		"updated": updated,
		"removed": removed,
		"covered": covered,
		"message": (
			f"Auto Material Request <b>{pool.name}</b> reconciled: {len(created)} item(s) added, "
			f"{len(updated)} qty updated, {len(removed)} removed."
		),
		"triggered_by": triggered_by,
	}


# ---------------------------------------------------------------------------
# Callers
# ---------------------------------------------------------------------------

def run_immediate_mr_check(customer):
	settings = _settings()
	return reconcile_material_requests(
		planning_months=settings.default_planning_months or 12,
		lead_time_months=settings.default_lead_time_months or 7,
		warehouse=settings.default_warehouse,
		triggered_by=f"Customer Schedule submitted ({customer})",
	)


def run_scheduled_mr_generation():
	settings = _settings()
	return reconcile_material_requests(
		planning_months=settings.default_planning_months or 12,
		lead_time_months=settings.default_lead_time_months or 7,
		warehouse=settings.default_warehouse,
		triggered_by="Scheduled batch run",
	)


def adjust_for_schedule_cancel(customer):
	"""Always runs, regardless of Material Request Trigger Mode - a
	cancellation reduces demand, and the live MR should shrink/clear
	promptly rather than wait for the next scheduled batch, so the business
	never over-buys because of a stale shortfall figure."""
	settings = _settings()
	return reconcile_material_requests(
		planning_months=settings.default_planning_months or 12,
		lead_time_months=settings.default_lead_time_months or 7,
		warehouse=settings.default_warehouse,
		triggered_by=f"Customer Schedule cancelled ({customer})",
	)


def on_delivery_note_change(doc, method=None):
	"""Fired on both submit and cancel of ANY Delivery Note. Always runs,
	regardless of Material Request Trigger Mode - this is what makes a
	shipment against an item with no active Customer Schedule still show up
	in Material Request planning (via the shipping-history fallback in
	compute_planning_snapshot), and keeps the live MR in step with stock
	changes without waiting for the next scheduled batch."""
	settings = _settings()
	try:
		reconcile_material_requests(
			planning_months=settings.default_planning_months or 12,
			lead_time_months=settings.default_lead_time_months or 7,
			warehouse=settings.default_warehouse,
			triggered_by=f"Delivery Note {doc.name} ({method or 'change'})",
		)
	except Exception:
		frappe.log_error(
			title="Schedule Planner: Material Request reconciliation after Delivery Note failed",
			message=frappe.get_traceback(),
		)
