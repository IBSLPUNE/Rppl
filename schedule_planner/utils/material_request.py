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

import json
import frappe
from frappe.utils import add_months, cint, flt, getdate, today

from schedule_planner.utils import safety

# Customer schedules, demand, stock and every limit are in the item's STOCK UOM
# (Nos). Purchase Orders, Material Requests and Delivery Notes can be raised in
# another UOM, so their quantities are converted to stock UOM before they are
# compared with demand/stock. "conversion_factor" = stock units per 1 unit of
# the line's own UOM.
_DNI_STOCK_QTY = "IFNULL(NULLIF(dni.stock_qty, 0), dni.qty * IFNULL(NULLIF(dni.conversion_factor, 0), 1))"
_POI_OPEN_STOCK_QTY = "(poi.qty - IFNULL(poi.received_qty, 0)) * IFNULL(NULLIF(poi.conversion_factor, 0), 1)"
_MRI_OPEN_STOCK_QTY = (
	"IFNULL(NULLIF(mri.stock_qty, 0), mri.qty * IFNULL(NULLIF(mri.conversion_factor, 0), 1))"
	" - IFNULL(mri.ordered_qty, 0)"
)


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


DAYS_PER_MONTH = safety.DAYS_PER_MONTH


def _customer_rates(rows):
	"""Monthly rate of each customer's demand for ONE item: what the customer's
	latest schedule holds for the item in the window, divided by the months
	between its first and last demand date (the last date counts as a full
	week). rows: anything with customer, delivery_date, quantity. Two customers
	with 6 and 9 months of data each keep their own rate; the item's monthly
	demand is the sum of the rates."""
	by_customer = {}
	for r in rows:
		qty = flt(r["quantity"] if isinstance(r, dict) else r.quantity)
		if qty <= 0:
			continue
		customer = r["customer"] if isinstance(r, dict) else r.customer
		when = getdate(r["delivery_date"] if isinstance(r, dict) else r.delivery_date)
		c = by_customer.setdefault(customer, {"customer": customer, "total": 0.0, "first": when, "last": when})
		c["total"] += qty
		c["first"] = min(c["first"], when)
		c["last"] = max(c["last"], when)
	out = []
	for c in sorted(by_customer.values(), key=lambda x: x["customer"] or ""):
		months = max(((c["last"] - c["first"]).days + 7) / DAYS_PER_MONTH, 1.0)
		out.append(
			{
				"customer": c["customer"],
				"total": round(c["total"], 2),
				"from": str(c["first"]),
				"to": str(c["last"]),
				"months": round(months, 2),
				"rate": round(c["total"] / months, 2),
			}
		)
	return out


def _schedule_demand_rows(start_date, end_date):
	"""The rows _schedule_demand_map adds up (latest submitted schedule per
	customer + item + delivery date, inside the window), one by one."""
	return frappe.db.sql(
		"""
		SELECT item_code, customer, delivery_date, quantity FROM (
			SELECT
				csi.item_code,
				cs.customer,
				csi.delivery_date,
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
		""",
		{"start": str(start_date), "end": str(end_date)},
		as_dict=True,
	)


def _week_count(today_date, planning_months):
	"""Weeks between today and the end of the planning window."""
	return max(-(-(add_months(today_date, int(planning_months)) - today_date).days // 7), 1)


def _week_buckets(rows, today_date, planning_months):
	"""[qty of week 1, week 2, ...]: week k runs from today + 7(k-1) days to
	today + 7k days; overdue demand (inside the grace window) is in week 1."""
	n = _week_count(today_date, planning_months)
	buckets = [0.0] * n
	for r in rows:
		when = getdate(r["delivery_date"] if isinstance(r, dict) else r.delivery_date)
		index = min(max((when - today_date).days, 0) // 7, n - 1)
		buckets[index] += flt(r["quantity"] if isinstance(r, dict) else r.quantity)
	return buckets


def _demand_profiles(start_date, end_date, today_date, planning_months):
	"""{item_code: {"total", "monthly", "profile", "rates"}} from the
	customers' schedules - monthly is the sum of each customer's own rate,
	profile is what the week-by-week cover is worked out from."""
	by_item = {}
	for r in _schedule_demand_rows(start_date, end_date):
		by_item.setdefault(r.item_code, []).append(r)
	out = {}
	for item_code, rows in by_item.items():
		rates = _customer_rates(rows)
		monthly = round(sum(c["rate"] for c in rates), 2)
		buckets = _week_buckets(rows, today_date, planning_months)
		out[item_code] = {
			"total": sum(flt(r.quantity) for r in rows),
			"monthly": monthly,
			"profile": safety.make_profile(buckets, monthly),
			"rates": rates,
		}
	return out


def _monthly_demand(rows):
	"""Month by month: what the customer sent (Firm; Forecast/Raw) and what
	the system added as auto forecast - the same rows the total is made of."""
	months = {}
	for r in rows:
		key = getdate(r.delivery_date).strftime("%Y-%m")
		m = months.setdefault(key, {"month": key, "firm": 0.0, "forecast": 0.0, "auto": 0.0})
		qty = flt(r.quantity)
		if r.auto_forecast:
			m["auto"] += qty
		elif r.demand_type == "Firm":
			m["firm"] += qty
		else:
			m["forecast"] += qty
	out = []
	for key in sorted(months):
		m = months[key]
		m.update({k: round(m[k], 2) for k in ("firm", "forecast", "auto")})
		m["total"] = round(m["firm"] + m["forecast"] + m["auto"], 2)
		out.append(m)
	return out


def _forecast_basis(item_code, rows):
	"""How each auto-forecast in the breakup was worked out, so the client can
	follow the number: which method, which period, how much was shipped (or
	supplied by the customer) in it and how that became the weekly qty.
	Shipment figures are re-read for the period ending on the day the
	schedule was created, i.e. the window the forecast really used."""

	from schedule_planner.utils import forecast_engine as fe

	by_schedule = {}
	for r in rows:
		if r.auto_forecast:
			by_schedule.setdefault(r.schedule, []).append(r)

	out = []
	for schedule, arows in by_schedule.items():
		arows = sorted(arows, key=lambda r: r.delivery_date)
		first = arows[0]
		method = fe.method_from_label(first.forecast_method)
		basis = {
			"schedule": schedule,
			"customer": first.customer,
			"method": method,
			"method_label": fe.METHOD_LABELS.get(method) or first.forecast_method or "Not recorded",
			"weekly_used": round(sum(flt(r.quantity) for r in arows) / len(arows), 2),
			"auto_from": str(arows[0].delivery_date),
			"auto_to": str(arows[-1].delivery_date),
			"auto_weeks": len(arows),
			"auto_total": round(sum(flt(r.quantity) for r in arows), 2),
			"created": str(getdate(first.creation)),
			"source": first.forecast_source or "",
		}
		try:
			stored = json.loads(first.forecast_basis_json) if first.forecast_basis_json else None
		except (TypeError, ValueError):
			stored = None
		if stored:
			# the comparison with the customer document's own average, as it was
			# worked out when the forecast was created
			basis["method_weekly"] = stored.get("method_weekly")
			basis["document_weekly"] = stored.get("document_weekly")
			basis["document"] = stored.get("document")
			basis["auto_switched"] = bool(stored.get("auto_switched"))
			base = stored.get("base_method")
			basis["base_method_label"] = fe.METHOD_LABELS.get(base) or ""

		own = frappe.get_all(
			"Customer Schedule Item",
			filters={"parent": schedule, "item_code": item_code, "auto_forecast": 0},
			fields=["delivery_date", "quantity", "demand_type"],
		)
		own = [
			{
				"delivery_date": str(r.delivery_date),
				"quantity": flt(r.quantity),
				"demand_type": r.demand_type,
				"auto_forecast": 0,
				"horizon_end": str(first.horizon_end) if first.horizon_end else None,
			}
			for r in own
		]

		def last_four_weeks():
			value, weeks = fe.last_4_week_avg(own)
			history = fe._history_for_averaging(own)
			if not history:
				return None
			end = max(d for d in (fe._horizon_end(own), max(getdate(r["delivery_date"]) for r in history)) if d)
			start = end - timedelta(days=27)
			return {
				"weekly_avg": value,
				"from": str(start),
				"to": str(end),
				"weeks": [
					{
						"from": str(start + timedelta(days=7 * i)),
						"to": str(start + timedelta(days=7 * i + 6)),
						"qty": w,
					}
					for i, w in enumerate(weeks)
				]
				if len(weeks) == 4
				else [],
				"total": round(sum(weeks), 2) if len(weeks) == 4 else None,
			}

		if method in fe.SHIPPING_MONTHS:
			months = fe.SHIPPING_MONTHS[method]
			to_date = getdate(first.creation)
			from_date = add_months(to_date, -months)
			params = {"item": item_code, "from_date": str(from_date), "to_date": str(to_date), "customer": first.customer or ""}
			by_month = frappe.db.sql(
				f"""
				SELECT DATE_FORMAT(dn.posting_date, '%%Y-%%m') AS ym, SUM({_DNI_STOCK_QTY}) AS qty
				FROM `tabDelivery Note Item` dni
				INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
				WHERE dni.item_code = %(item)s AND dn.docstatus = 1
				  AND dn.posting_date >= %(from_date)s AND dn.posting_date <= %(to_date)s
				  AND (%(customer)s = '' OR dn.customer = %(customer)s)
				GROUP BY ym ORDER BY ym
				""",
				params,
				as_dict=True,
			)
			total = round(sum(flt(r.qty) for r in by_month), 2)
			weeks = months * 13.0 / 3.0
			basis.update(
				{
					"kind": "shipping",
					"months": months,
					"from": str(from_date),
					"to": str(to_date),
					"total": total,
					"weeks": round(weeks, 1),
					"weekly_avg": round(total / weeks, 2) if total > 0 else 0,
					"by_month": [{"month": r.ym, "qty": round(flt(r.qty), 2)} for r in by_month],
				}
			)
		elif method == fe.DOCUMENT_METHOD:
			basis["kind"] = "document"
		elif method == "LAST_4_WEEKS_AVG":
			basis["kind"] = "last4"
			basis["last4"] = last_four_weeks()
		else:
			basis["kind"] = "unknown"
		out.append(basis)
	return out


def get_demand_breakup(item_code, planning_months=None):
	"""Where an item's Total Demand on the Material Planning Dashboard comes
	from: the same rows _schedule_demand_map adds up (latest submitted schedule
	per customer + item + delivery date, inside the demand window), listed one
	by one - or, for an item with no schedule, the shipping-history figure."""

	settings = _settings()
	planning_months = int(planning_months or settings.default_planning_months or 12)
	grace_days = int(settings.overdue_grace_days if settings.overdue_grace_days is not None else 30)
	today_date = getdate(today())
	start_date = today_date - timedelta(days=grace_days)
	end_date = add_months(today_date, planning_months)

	rows = frappe.db.sql(
		"""
		SELECT * FROM (
			SELECT
				cs.customer, cs.name AS schedule, cs.release_number, cs.schedule_date,
				csi.delivery_date, csi.demand_type, csi.quantity, csi.auto_forecast,
				csi.forecast_method, csi.forecast_source, csi.forecast_basis_json, cs.horizon_end, cs.creation,
				ROW_NUMBER() OVER (
					PARTITION BY cs.customer, csi.item_code, csi.delivery_date
					ORDER BY cs.schedule_date DESC, cs.creation DESC
				) AS rn
			FROM `tabCustomer Schedule Item` csi
			INNER JOIN `tabCustomer Schedule` cs ON cs.name = csi.parent
			WHERE
				cs.docstatus = 1
				AND csi.item_code = %(item)s
				AND csi.demand_type IN ('Forecast', 'Raw', 'Firm')
				AND csi.quantity > 0
				AND csi.delivery_date >= %(start)s
				AND csi.delivery_date < %(end)s
		) ranked
		WHERE rn = 1
		ORDER BY delivery_date, customer
		""",
		{"item": item_code, "start": str(start_date), "end": str(end_date)},
		as_dict=True,
	)

	out = {
		"item_code": item_code,
		"start_date": str(start_date),
		"end_date": str(end_date),
		"planning_months": planning_months,
		"grace_days": grace_days,
		"today": str(today_date),
		"rows": [
			{
				"customer": r.customer,
				"schedule": r.schedule,
				"release_number": r.release_number,
				"delivery_date": str(r.delivery_date),
				"demand_type": r.demand_type,
				"quantity": flt(r.quantity),
				"auto_forecast": int(r.auto_forecast or 0),
			}
			for r in rows
		],
	}
	out["total"] = sum(r["quantity"] for r in out["rows"])
	if rows:
		out["source"] = "Customer Schedule"
		out["forecast_basis"] = _forecast_basis(item_code, rows)
		out["monthly"] = _monthly_demand(rows)
		out["customer_rates"] = _customer_rates(rows)
		out["monthly_demand"] = round(sum(c["rate"] for c in out["customer_rates"]), 2)
		return out

	# no schedule rows: the shipping-history fallback (if it applies)
	fallback_on = settings.enable_shipping_fallback_forecast
	fallback_on = 1 if fallback_on is None else cint(fallback_on)
	months = int(settings.shipping_fallback_months or 3)
	company = _company_or_none(settings)
	demand = _shipping_fallback_map(set(), company, months, planning_months).get(item_code, 0.0) if fallback_on else 0.0
	shipped = frappe.db.sql(
		f"""
		SELECT SUM({_DNI_STOCK_QTY}) AS q FROM `tabDelivery Note Item` dni
		INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
		WHERE dni.item_code = %(item)s AND dn.docstatus = 1
		  AND dn.posting_date >= %(from)s AND dn.posting_date <= %(today)s
		""",
		{"item": item_code, "from": str(add_months(today_date, -months)), "today": str(today_date)},
		as_dict=True,
	)
	out["source"] = "Shipping History (No Schedule)" if demand else "None"
	out["fallback"] = {
		"enabled": bool(fallback_on),
		"months": months,
		"shipped_qty": flt(shipped[0].q) if shipped else 0.0,
		"demand": demand,
	}
	out["total"] = demand
	return out


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
		SELECT dni.item_code, SUM({_DNI_STOCK_QTY}) AS shipped_qty
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
		SELECT poi.item_code, SUM({_POI_OPEN_STOCK_QTY}) AS open_qty
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
		SELECT mri.item_code, SUM({_MRI_OPEN_STOCK_QTY}) AS open_qty
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
# Minimum order quantity (supplier MOQ)
# ---------------------------------------------------------------------------

def _min_order_map(items, warehouse=None):
	"""Smallest quantity a supplier will sell for each item, read from the
	standard ERPNext Item master (nothing custom to maintain):

	  - Item > Minimum Order Qty                      (min_order_qty)
	  - Item > Reorder table > Re-order Qty           (warehouse_reorder_qty)
	    for the planning warehouse, Purchase-type rows only

	The larger of the two is the effective minimum, so an item that has only
	one of them filled in works too."""

	if not items:
		return {}

	result = {}
	for r in frappe.get_all("Item", filters={"name": ["in", list(items)]}, fields=["name", "min_order_qty"]):
		result[r.name] = flt(r.min_order_qty)

	reorder_rows = frappe.get_all(
		"Item Reorder",
		filters={"parent": ["in", list(items)], "parenttype": "Item"},
		fields=["parent", "warehouse", "warehouse_reorder_qty", "material_request_type"],
	)
	# The Item's Reorder table says which warehouse a row is for ("Request
	# for"), but stock here is counted across every warehouse, so a row for
	# another warehouse (e.g. Finished Goods while the planning warehouse is
	# Stores) still applies. Rows for the planning warehouse win when present.
	exact, other = {}, {}
	for r in reorder_rows:
		if r.material_request_type and r.material_request_type != "Purchase":
			continue
		target = exact if (warehouse and r.warehouse == warehouse) else other
		target[r.parent] = max(target.get(r.parent, 0.0), flt(r.warehouse_reorder_qty))
	for item, qty in {**other, **exact}.items():
		result[item] = max(result.get(item, 0.0), qty)
	return result


def _reorder_level_map(items, warehouse=None):
	"""Item > Reorder table > Re-order Level (warehouse_reorder_level) per
	item, Purchase-type rows. Stock is compared across all warehouses, so the
	level is the row for the planning warehouse if there is one; otherwise the
	rows of the item's other warehouses added together (one level per
	warehouse). Only items with a level above zero are in the result - the
	rest have "no level set"."""

	if not items:
		return {}

	exact, generic = {}, {}
	rows = frappe.get_all(
		"Item Reorder",
		filters={"parent": ["in", list(items)], "parenttype": "Item"},
		fields=["parent", "warehouse", "warehouse_reorder_level", "material_request_type"],
	)
	for r in rows:
		if r.material_request_type and r.material_request_type != "Purchase":
			continue
		level = flt(r.warehouse_reorder_level)
		if level <= 0:
			continue
		if warehouse and r.warehouse == warehouse:
			exact[r.parent] = max(exact.get(r.parent, 0.0), level)
		else:
			generic[r.parent] = generic.get(r.parent, 0.0) + level
	return {**generic, **exact}


def apply_order_rules(net_shortfall, min_order_qty, enabled=True):
	"""Quantity that actually goes on the Material Request for one item.

	  - no shortfall                      -> 0 (never order just to meet a MOQ)
	  - shortfall below the item's MOQ     -> the MOQ (supplier won't sell less)
	  - shortfall at or above the MOQ      -> exactly the shortfall"""

	net = flt(net_shortfall)
	if net <= 0.01:
		return 0.0
	if enabled and flt(min_order_qty) > net:
		return flt(min_order_qty)
	return net


# ---------------------------------------------------------------------------
# Snapshot - the single source of truth used by BOTH the reconciler and the
# Material Planning Dashboard report, so the dashboard can never drift from
# what the live MR actually reflects.
# ---------------------------------------------------------------------------

def compute_planning_snapshot(planning_months=None, lead_time_months=None, warehouse=None):
	settings = _settings()
	planning_months = int(planning_months or settings.default_planning_months or 12)
	lead_time_months = int(lead_time_months or settings.default_lead_time_months or 7)
	grace_days = int(settings.overdue_grace_days if settings.overdue_grace_days is not None else 30)

	today_date = getdate(today())
	start_date = today_date - timedelta(days=grace_days)
	end_date = add_months(today_date, planning_months)
	req_date = add_months(today_date, lead_time_months)

	company = _company_or_none(settings)
	cover_limit = safety.max_cover_months()

	profiles = _demand_profiles(start_date, end_date, today_date, planning_months)
	schedule_demand = {item: p["total"] for item, p in profiles.items()}

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

	moq_on = settings.get("apply_min_order_qty")
	moq_on = 1 if moq_on is None else cint(moq_on)
	min_order_map = _min_order_map(items_in_scope, warehouse or settings.default_warehouse) if moq_on else {}

	reorder_level_map = _reorder_level_map(items_in_scope, warehouse or settings.default_warehouse)

	stock_map = _stock_map(items_in_scope)
	open_po_map = _open_po_map(items_in_scope, company)
	pool_name = _find_existing_pool_mr_name(company)
	other_open_mr_map = _other_open_mr_map(items_in_scope, company, exclude_name=pool_name)

	pool_items_map = {}
	if pool_name:
		pool_rows = frappe.get_all(
			"Material Request Item", filters={"parent": pool_name}, fields=["item_code", "qty", "stock_qty"]
		)
		for r in pool_rows:
			pool_items_map[r.item_code] = flt(r.stock_qty) or flt(r.qty)

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
		shortfall = max(net, 0.0)
		min_qty = min_order_map.get(item_code, 0.0)

		# Reorder-level gate: with a Re-order Level on the item, a Material
		# Request is only raised once what is available (stock + open PO +
		# other open MR) has fallen BELOW that level. No level set = old
		# behaviour (raise whenever there is a shortfall).
		available = stock + open_po + other_mr
		level = reorder_level_map.get(item_code, 0.0)
		if level > 0:
			below_level = available + 0.01 < level
			reorder_check = "Below Reorder Level" if below_level else "Above Reorder Level"
		else:
			below_level = True
			reorder_check = "Not Set"

		if shortfall <= 0.01:
			mr_decision = "No shortfall"
		elif not below_level:
			mr_decision = f"Wait - available {available:,.0f} is not below reorder level {level:,.0f}"
		else:
			mr_decision = "Raise MR"
		order_qty = apply_order_rules(shortfall, min_qty, moq_on) if mr_decision == "Raise MR" else 0.0

		# Months of cover: how many months of (average) demand the stock would
		# last once this order has arrived. Over the limit => red + approval.
		# Week by week: the stock (plus this order) is used up against each
		# month's demand in turn. Monthly demand shown is the sum of every
		# customer's own monthly rate; an item with no schedule is flat.
		known = profiles.get(item_code) if source_map.get(item_code) == "Customer Schedule" else None
		if known:
			monthly_demand, profile = known["monthly"], known["profile"]
		else:
			monthly_demand = demand / planning_months if planning_months else 0.0
			weeks = _week_count(today_date, planning_months)
			profile = safety.make_profile([monthly_demand * 7.0 / DAYS_PER_MONTH] * weeks, monthly_demand)
		cover_now = safety.cover_from_profile(available, 0, profile)
		cover_after = safety.cover_from_profile(available, order_qty, profile)
		cover_flag = bool(
			cover_limit > 0 and order_qty > 0.01 and cover_after is not None and cover_after > cover_limit + 0.05
		)
		rows.append(
			{
				"item_code": item_code,
				"demand_source": source_map.get(item_code),
				"total_demand": demand,
				"current_stock": stock,
				"open_po_qty": open_po,
				"other_open_mr_qty": other_mr,
				"net_shortfall": shortfall,
				"net_surplus": max(-net, 0.0),
				"available_qty": available,
				"reorder_level": level,
				"reorder_check": reorder_check,
				"mr_decision": mr_decision,
				"min_order_qty": min_qty,
				"order_qty": order_qty,
				"moq_extra": max(order_qty - shortfall, 0.0),
				"monthly_demand": monthly_demand,
				"demand_profile": profile,
				"cover_now": cover_now,
				"cover_after": cover_after,
				"cover_flag": cover_flag,
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
		"apply_min_order_qty": moq_on,
		"cover_limit": cover_limit,
	}


def _set_stock_uom(row, item_code, qty):
	"""An existing live-MR line is moved to the item's stock UOM, so its qty
	(always planned in stock UOM) means exactly what the line says."""
	uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"
	row.uom = uom
	row.stock_uom = uom
	row.conversion_factor = 1
	row.stock_qty = qty


def _set_cover_fields(row, info):
	row.custom_sp_available_qty = info["available_qty"]
	row.custom_sp_monthly_demand = round(info["monthly_demand"], 2)
	row.custom_sp_cover_months = info["cover_after"]
	row.custom_sp_demand_buckets = json.dumps(info["demand_profile"])


def _line_description(info, planning_months, lead_time_months):
	text = (
		f"Auto MR [{info['demand_source']}]: Demand {info['total_demand']:,.0f} "
		f"- Stock {info['current_stock']:,.0f} - Open PO {info['open_po_qty']:,.0f} "
		f"- Other Open MR {info['other_open_mr_qty']:,.0f} = Shortfall "
		f"{info['net_shortfall']:,.0f} ({planning_months}M horizon, {lead_time_months}M lead time)"
	)
	if flt(info.get("reorder_level")) > 0:
		text += (
			f". Available {info['available_qty']:,.0f} is below Re-order Level "
			f"{info['reorder_level']:,.0f}"
		)
	if info.get("moq_extra", 0) > 0.01:
		text += (
			f". Raised to supplier Minimum Order Qty {info['min_order_qty']:,.0f} "
			f"(+{info['moq_extra']:,.0f} above shortfall)"
		)
	if info.get("cover_after") is not None:
		text += (
			f". Months of cover after order: {info['cover_after']:,.1f}, counted week by week "
			f"(monthly demand {info['monthly_demand']:,.0f})"
		)
		if info.get("cover_flag"):
			text += f" - ABOVE LIMIT, approval needed"
	return text


# ---------------------------------------------------------------------------
# The reconciler - the only place that ever writes the live pool MR
# ---------------------------------------------------------------------------

def reconcile_material_requests(planning_months=None, lead_time_months=None, warehouse=None, triggered_by=None):
	settings = _settings()
	company = _company_or_none(settings)

	if not company:
		frappe.throw(
			"No default company configured. Set <b>Default Company</b> in "
			"Schedule Planning Settings (or Global Defaults) before Material "
			"Requests can be generated."
		)

	lead_time_months = int(lead_time_months or settings.default_lead_time_months or 7)
	warehouse = _resolve_warehouse(settings, company, override=warehouse)
	snapshot = compute_planning_snapshot(planning_months, lead_time_months, warehouse=warehouse)
	# a real date, not the snapshot's string: an existing line loaded from the
	# database carries a date object, and ERPNext's validate_schedule_date takes
	# min() over all lines - a str next to a date raises TypeError
	req_date = getdate(snapshot["required_by"])

	pool = _get_or_new_pool_mr(company)
	existing_rows_by_item = {r.item_code: r for r in (pool.items or [])}

	# a line is raised only when there is a shortfall AND (if the item has a
	# Re-order Level) available stock is below it
	shortfall_by_item = {r["item_code"]: r for r in snapshot["rows"] if r["order_qty"] > 0.01}
	covered = [r["item_code"] for r in snapshot["rows"] if r["order_qty"] <= 0.01]

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
		qty = round(info["order_qty"], 2)   # greatest of shortfall, MOQ and Re-order Qty
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
			_set_stock_uom(row, item_code, qty)
			_set_cover_fields(row, info)
			if changed:
				updated.append(item_code)
		else:
			item_name = frappe.db.get_value("Item", item_code, "item_name") or item_code
			# the planned qty is in stock UOM (Nos): the line is raised in stock UOM too
			uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"
			pool.append(
				"items",
				{
					"item_code": item_code,
					"item_name": item_name,
					"qty": qty,
					"uom": uom,
					"stock_uom": uom,
					"conversion_factor": 1,
					"stock_qty": qty,
					"warehouse": warehouse,
					"schedule_date": req_date,
					"description": desc,
					"custom_sp_demand_source": info["demand_source"],
					"custom_sp_available_qty": info["available_qty"],
					"custom_sp_monthly_demand": round(info["monthly_demand"], 2),
					"custom_sp_cover_months": info["cover_after"],
					"custom_sp_demand_buckets": json.dumps(info["demand_profile"]),
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
	pool.custom_sp_cover_excess = 1 if any(shortfall_by_item[i]["cover_flag"] for i in shortfall_by_item) else 0
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
	except Exception as e:
		frappe.log_error(
			title="Schedule Planner: Material Request reconciliation after Delivery Note failed",
			message=frappe.get_traceback(),
		)
		# the Delivery Note itself must still go through, but say why the
		# Material Request was not updated instead of failing silently
		frappe.msgprint(
			f"Delivery Note saved, but the Auto Material Request could not be updated: "
			f"<b>{frappe.utils.escape_html(str(e))}</b><br><br>Details are in Error Log "
			f"(Schedule Planner: Material Request reconciliation after Delivery Note failed).",
			title="Material Request Not Updated",
			indicator="orange",
		)
