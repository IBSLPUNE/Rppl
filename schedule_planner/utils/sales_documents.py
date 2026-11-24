# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

"""Turns a submitted Customer Schedule's Firm rows into per-delivery-date
Sales Orders, and its Forecast/Raw rows into one Sales Forecast per
customer. Sales Orders are only ever created or updated as DRAFTS. A submitted
Sales Order that nothing has been done with yet (no delivery, invoice, work
order, ...) is cancelled and replaced by a Draft when a new schedule changes it;
one that has been processed is never touched. (The Sales Forecast is amended -
cancel + copy + resubmit - when a new schedule replaces it.)

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


def _sales_uom_factor(item_code):
	"""(uom, conversion_factor) the Sales Order line must use.

	Customer schedules are in the item's Stock UOM (e.g. Nos). The Sales Order is
	made in the item's Sales UOM (e.g. M) when it has one, and ERPNext then needs
	the line's conversion_factor = stock units in ONE sales unit, taken from the
	Item's UOM Conversion Detail table. With no Sales UOM, or no conversion row
	for it, the line stays in the Stock UOM (factor 1) - never a guessed factor."""

	stock_uom, sales_uom = frappe.db.get_value("Item", item_code, ["stock_uom", "sales_uom"]) or (None, None)
	if not sales_uom or sales_uom == stock_uom:
		return stock_uom, 1.0
	factor = flt(
		frappe.db.get_value("UOM Conversion Detail", {"parent": item_code, "uom": sales_uom}, "conversion_factor")
	)
	if factor <= 0:
		return stock_uom, 1.0
	return sales_uom, factor


def _item_rate(item_code, ctx=None, uom=None, cf=1.0):
	"""Selling rate per ONE unit of the order line's UOM, in the Sales Order's own
	currency. Item.standard_rate is per Stock UOM and in company currency, so it
	is multiplied by the line's conversion factor; for a foreign-currency customer
	a rate from the customer's Selling Price List (in that currency) is preferred,
	and otherwise standard_rate is converted with the order's exchange rate."""

	cf = flt(cf) or 1.0
	standard = flt(frappe.db.get_value("Item", item_code, "standard_rate")) * cf
	if not ctx or ctx["currency"] == ctx["company_currency"]:
		return standard

	if ctx.get("price_list"):
		base = {"item_code": item_code, "price_list": ctx["price_list"], "selling": 1, "currency": ctx["currency"]}
		price = None
		if uom and cf != 1:
			price = frappe.db.get_value("Item Price", dict(base, uom=uom), "price_list_rate")  # already per this UOM
		if not price:
			price = frappe.db.get_value("Item Price", base, "price_list_rate")
			price = flt(price) * cf if price else None
		if price:
			return flt(price)

	rate = flt(ctx["conversion_rate"]) or 1.0
	return round(standard / rate, 6)


def _currency_context(customer, company, transaction_date):
	"""Currency the Sales Order must be made in. ERPNext refuses an order whose
	currency differs from the customer's party account currency ("Accounting
	Entry for Customer ... can only be made in currency: USD"), and when the
	document is created from code nothing fills the currency in as the form
	does - so work it out here."""

	company_currency = frappe.get_cached_value("Company", company, "default_currency")
	currency = None
	try:
		from erpnext.accounts.party import get_party_account_currency

		currency = get_party_account_currency("Customer", customer, company)
	except Exception:
		pass
	currency = currency or frappe.db.get_value("Customer", customer, "default_currency") or company_currency

	ctx = {
		"currency": currency,
		"company_currency": company_currency,
		"conversion_rate": 1.0,
		"price_list": None,
	}
	if currency == company_currency:
		return ctx

	from erpnext.setup.utils import get_exchange_rate

	rate = flt(get_exchange_rate(currency, company_currency, transaction_date))
	if not rate:
		frappe.throw(
			f"No exchange rate found for {currency} to {company_currency}. Add a Currency Exchange "
			f"record for {currency} -> {company_currency} (Accounts > Currency Exchange) and submit the "
			f"schedule again. Customer {customer} is billed in {currency}.",
			title="Exchange Rate Missing",
		)
	ctx["conversion_rate"] = rate

	# Customer's own price list, then its group's, then Selling Settings - used
	# only when it is priced in the same currency as the order.
	candidates = [frappe.db.get_value("Customer", customer, "default_price_list")]
	group = frappe.db.get_value("Customer", customer, "customer_group")
	if group:
		candidates.append(frappe.db.get_value("Customer Group", group, "default_price_list"))
	candidates.append(frappe.db.get_single_value("Selling Settings", "selling_price_list"))
	for name in candidates:
		if name and frappe.db.get_value("Price List", name, "currency") == currency:
			ctx["price_list"] = name
			break
	return ctx


def _apply_currency(so, ctx):
	so.currency = ctx["currency"]
	so.conversion_rate = ctx["conversion_rate"]
	if ctx["price_list"]:
		so.selling_price_list = ctx["price_list"]
		so.price_list_currency = ctx["currency"]
		so.plc_conversion_rate = ctx["conversion_rate"]


def _item_name(item_code):
	return frappe.db.get_value("Item", item_code, "item_name") or item_code


def _item_uom(item_code):
	return frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"


# ---------------------------------------------------------------------------
# Firm rows -> one Sales Order per delivery date
# ---------------------------------------------------------------------------

def sync_firm_to_sales_orders(doc):
	"""One Sales Order per delivery date. Every order is left as a DRAFT: it is
	created / updated here but never submitted by the system - the user reviews
	it (and sets any missing rate) and submits it by hand.

	- A Draft for that customer + date is updated in place to match this schedule.
	- A SUBMITTED order whose items / qty already match the schedule is left alone.
	  If they differ: when nothing has been done with it yet (_is_processed) it is
	  cancelled and replaced by a Draft that follows the schedule; when it has been
	  processed (delivered, invoiced, work order, ...) or cannot be cancelled it is
	  left exactly as it is and listed in a message.
	- Only orders this app created (custom_sp_is_schedule_generated = 1) are ever
	  looked at; any other Sales Order is untouched."""
	firm_rows = [r for r in doc.schedule_items if r.demand_type == "Firm" and flt(r.quantity) > 0]
	if not firm_rows:
		return

	company = _default_company()
	warehouse = _default_warehouse(company)

	by_date = defaultdict(list)
	for row in firm_rows:
		by_date[str(row.delivery_date)].append(row)

	report = {"submitted": [], "draft": [], "locked": [], "replaced": []}
	for delivery_date, rows_for_date in by_date.items():
		_sync_one_date_so(doc, delivery_date, rows_for_date, company, warehouse, report)

	_report_draft_orders(report)


def _report_draft_orders(report):
	drafts = report["draft"]
	locked = report.get("locked", [])
	replaced = report.get("replaced", [])
	if replaced:
		frappe.msgprint(
			(
				"<b>{0}</b> submitted Sales Order(s) had not been processed yet, so they were <b>cancelled</b> and "
				"replaced by a <b>Draft</b> that follows this schedule:<br><br>{1}"
			).format(
				len(replaced),
				"<br>".join(
					f"<a href='/app/sales-order/{old}'>{old}</a> &rarr; <a href='/app/sales-order/{new}'>{new}</a> "
					f"({frappe.utils.escape_html(str(date))})"
					for old, new, date in replaced
				),
			),
			title="Sales Orders Replaced",
			indicator="orange",
		)
	if locked:
		frappe.msgprint(
			(
				"<b>{0}</b> date(s) already have a <b>submitted</b> Sales Order that does not match this schedule and "
				"has already been processed (delivery, invoice, work order, advance payment, closed or on hold), or could "
				"not be cancelled. They were <b>not changed</b>:<br><br>{1}<br><br>Amend them by hand if needed."
			).format(
				len(locked),
				"<br>".join(
					f"<a href='/app/sales-order/{name}'>{name}</a> ({frappe.utils.escape_html(str(date))})"
					for name, date in locked
				),
			),
			title="Submitted Sales Orders Left Unchanged",
			indicator="red",
		)
	if not drafts:
		return
	lines = "<br>".join(
		f"<a href='/app/sales-order/{name}'>{name}</a> ({frappe.utils.escape_html(str(date))})"
		+ (f": no rate for {frappe.utils.escape_html(', '.join(missing))}" if missing else "")
		for name, date, missing in drafts
	)
	no_rate = sum(1 for _n, _d, missing in drafts if missing)
	extra = (
		f"<br><br>{no_rate} of them have an item without a rate (Item &gt; Standard Selling Rate is empty or 0) - "
		"set the rate on the order before submitting."
		if no_rate
		else ""
	)
	frappe.msgprint(
		(
			"<b>{0}</b> Sales Order(s) were created / updated as <b>Draft</b>. Review them and submit when ready."
			"<br><br>{1}{2}"
		).format(len(drafts), lines, extra),
		title="Sales Orders Saved as Draft",
		indicator="orange",
	)


def _missing_rate_items(so):
	return [r.item_code for r in so.items if flt(r.rate) <= 0]


def _finish_sales_order(so, delivery_date, report):
	"""The order is already inserted. It stays a Draft: the system never submits a
	Sales Order (the client submits them by hand). Lines with no rate are only
	reported so the user can fix them before submitting."""
	report["draft"].append((so.name, delivery_date, sorted(set(_missing_rate_items(so)))))
	return so.name


# documents that mean a Sales Order has been acted on; (doctype, field holding the Sales Order)
_SO_FOLLOW_UPS = (
	("Delivery Note Item", "against_sales_order"),
	("Sales Invoice Item", "sales_order"),
	("Work Order", "sales_order"),
	("Pick List Item", "sales_order"),
	("Material Request Item", "sales_order"),
	("Purchase Order Item", "sales_order"),
	("Payment Entry Reference", "reference_name"),  # advance received against the order
)


def _is_processed(so_name):
	"""True when anything has been done with this Sales Order after it was
	submitted - delivered / billed in part or full, closed or put on hold, or any
	(not cancelled) Delivery Note, Invoice, Work Order, Pick List, Material
	Request, Purchase Order or advance payment points at it. Such an order must
	never be cancelled by the system."""
	row = frappe.db.get_value("Sales Order", so_name, ["status", "per_delivered", "per_billed"], as_dict=True)
	if not row:
		return True
	if row.status in ("Closed", "On Hold") or flt(row.per_delivered) > 0 or flt(row.per_billed) > 0:
		return True
	for doctype, field in _SO_FOLLOW_UPS:
		if not frappe.db.table_exists(doctype) or not frappe.get_meta(doctype).has_field(field):
			continue
		filters = {field: so_name, "docstatus": ["<", 2]}
		if doctype == "Payment Entry Reference":
			filters["reference_doctype"] = "Sales Order"
		if frappe.db.exists(doctype, filters):
			return True
	return False


def _sync_one_date_so(doc, delivery_date, rows_for_date, company, warehouse, report=None):
	report = report if report is not None else {"submitted": [], "draft": [], "locked": []}
	report.setdefault("locked", [])
	report.setdefault("replaced", [])
	new_items = {}
	for row in rows_for_date:
		new_items[row.item_code] = flt(new_items.get(row.item_code, 0)) + flt(row.quantity)

	# only Sales Orders this app made for this customer and date are ever looked at
	match = {
		"customer": doc.customer,
		"custom_sp_is_schedule_generated": 1,
		"custom_sp_delivery_date": delivery_date,
	}
	draft = frappe.db.get_value("Sales Order", dict(match, docstatus=0), "name", order_by="creation desc")
	if draft:
		# a Draft: bring its lines up to this schedule, keep any rate that was typed in
		so_name = _update_draft_sales_order(draft, doc, delivery_date, new_items, company, warehouse, report)
	else:
		submitted = frappe.db.get_value("Sales Order", dict(match, docstatus=1), "name", order_by="creation desc")
		if submitted:
			# compare in stock UOM: the order line may be in the Sales UOM
			existing_items = {}
			for r in frappe.get_all(
				"Sales Order Item", filters={"parent": submitted}, fields=["item_code", "qty", "stock_qty", "conversion_factor"]
			):
				stock_qty = flt(r.stock_qty) or flt(r.qty) * (flt(r.conversion_factor) or 1.0)
				existing_items[r.item_code] = existing_items.get(r.item_code, 0.0) + stock_qty
			if set(existing_items) == set(new_items) and all(
				abs(existing_items[c] - flt(q)) <= 0.5 for c, q in new_items.items()
			):
				for row in rows_for_date:
					row.linked_sales_order = submitted
				return
			# differs from the schedule: replace it only if nothing has been done with it yet
			if _is_processed(submitted):
				report["locked"].append((submitted, delivery_date))
				return
			so_name = _amend_sales_order(submitted, doc, delivery_date, new_items, company, warehouse, report)
			if not so_name:  # could not be cancelled (a link ERPNext knows about): left as it is
				report["locked"].append((submitted, delivery_date))
				return
		else:
			so_name = _create_sales_order(doc, delivery_date, new_items, company, warehouse, report)

	for row in rows_for_date:
		row.linked_sales_order = so_name


def _order_date(doc, delivery_date):
	"""Sales Order Date: the schedule's date, but never later than the delivery
	date - ERPNext refuses an order whose Expected Delivery Date is before its
	own date, and a customer file can carry firm quantity dated before the day
	it was issued/uploaded (overdue or same-week lines). The delivery date is
	the customer's and is never moved; the order is dated on or before it."""
	order_date = getdate(doc.schedule_date) if doc.schedule_date else getdate()
	return min(order_date, getdate(delivery_date))


def _append_so_items(so, new_items, warehouse, delivery_date, ctx=None, fallback_rates=None):
	"""new_items are quantities in the item's STOCK UOM (what the customer sends).
	Each line is written in the item's Sales UOM with its conversion factor."""
	for item_code, stock_qty in new_items.items():
		uom, cf = _sales_uom_factor(item_code)
		rate = _item_rate(item_code, ctx, uom, cf)
		if rate <= 0 and fallback_rates:
			# an order being replaced keeps the rate it already had (kept per stock unit)
			rate = flt(fallback_rates.get(item_code)) * cf
		so.append(
			"items",
			{
				"item_code": item_code,
				"uom": uom,
				"conversion_factor": cf,
				"qty": flt(flt(stock_qty) / cf, 3),
				"stock_qty": flt(stock_qty),
				"rate": rate,
				"warehouse": warehouse,
				"delivery_date": delivery_date,
			},
		)


def _create_sales_order(doc, delivery_date, new_items, company, warehouse, report=None):
	report = report if report is not None else {"submitted": [], "draft": []}
	so = frappe.new_doc("Sales Order")
	so.customer = doc.customer
	so.company = company
	so.transaction_date = _order_date(doc, delivery_date)
	so.delivery_date = delivery_date
	so.custom_sp_is_schedule_generated = 1
	so.custom_sp_delivery_date = delivery_date
	so.custom_sp_customer_schedule = doc.name

	ctx = _currency_context(doc.customer, company, so.transaction_date)
	_apply_currency(so, ctx)
	_append_so_items(so, new_items, warehouse, delivery_date, ctx)

	so.insert(ignore_permissions=True)
	return _finish_sales_order(so, delivery_date, report)


def _update_draft_sales_order(existing_name, doc, delivery_date, new_items, company, warehouse, report):
	so = frappe.get_doc("Sales Order", existing_name)
	ctx = _currency_context(doc.customer, company, so.transaction_date)

	kept, seen = [], set()
	for line in so.items:
		if line.item_code in new_items and line.item_code not in seen:
			seen.add(line.item_code)
			kept.append(line)
	so.set("items", kept)
	for line in so.items:
		# the schedule quantity is in Stock UOM; the line is in the Sales UOM (also repairs
		# a draft that an older version wrote with the stock quantity in the sales UOM)
		uom, cf = _sales_uom_factor(line.item_code)
		old_cf = flt(line.conversion_factor) or 1.0
		if flt(line.rate) > 0 and abs(old_cf - cf) > 1e-9:
			line.rate = flt(line.rate) / old_cf * cf  # a typed rate stays the same per stock unit
		line.uom = uom
		line.conversion_factor = cf
		line.qty = flt(flt(new_items[line.item_code]) / cf, 3)
		line.stock_qty = flt(new_items[line.item_code])
		line.delivery_date = delivery_date
		if flt(line.rate) <= 0:  # a rate typed into the draft is kept
			line.rate = _item_rate(line.item_code, ctx, uom, cf)
	_append_so_items(so, {c: q for c, q in new_items.items() if c not in seen}, warehouse, delivery_date, ctx)

	so.custom_sp_customer_schedule = doc.name
	so.save(ignore_permissions=True)
	return _finish_sales_order(so, delivery_date, report)


def _amend_sales_order(existing_name, doc, delivery_date, new_items, company, warehouse, report=None):
	report = report if report is not None else {"submitted": [], "draft": []}
	old_so = frappe.get_doc("Sales Order", existing_name)
	old_rates = {}
	for line in old_so.items:
		if flt(line.rate) > 0:
			# per stock unit, so it can be re-applied whatever UOM the new line uses
			old_rates.setdefault(line.item_code, flt(line.rate) / (flt(line.conversion_factor) or 1.0))
	if old_so.docstatus == 1:
		old_so.flags.ignore_permissions = True
		frappe.db.savepoint("sp_cancel_so")
		try:
			old_so.cancel()
		except frappe.ValidationError:
			# ERPNext found something still linked to it: leave the order exactly as it was
			frappe.db.rollback(save_point="sp_cancel_so")
			return None

	amended = frappe.copy_doc(old_so)
	amended.amended_from = existing_name
	amended.customer = doc.customer
	amended.company = company
	amended.transaction_date = _order_date(doc, delivery_date)
	amended.delivery_date = delivery_date
	amended.custom_sp_is_schedule_generated = 1
	amended.custom_sp_delivery_date = delivery_date
	amended.custom_sp_customer_schedule = doc.name
	amended.items = []

	ctx = _currency_context(doc.customer, company, amended.transaction_date)
	_apply_currency(amended, ctx)
	_append_so_items(amended, new_items, warehouse, delivery_date, ctx, fallback_rates=old_rates)

	amended.insert(ignore_permissions=True)
	report.setdefault("replaced", []).append((existing_name, amended.name, delivery_date))
	return _finish_sales_order(amended, delivery_date, report)


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


def _forecast_parent_warehouse(company, warehouse):
	"""ERPNext's Sales Forecast only accepts a GROUP warehouse of the company
	as its Parent Warehouse, while Schedule Planning Settings holds a normal
	(leaf) warehouse - e.g. "Work In Progress - RPPL". Use the warehouse itself
	if it is a group, else its parent group, else the company's root group.
	The item rows keep the real warehouse."""
	info = frappe.db.get_value("Warehouse", warehouse, ["is_group", "company", "parent_warehouse"], as_dict=True)
	if info and info.is_group and info.company == company:
		return warehouse
	parent = info.parent_warehouse if info else None
	while parent:
		pinfo = frappe.db.get_value("Warehouse", parent, ["is_group", "company", "parent_warehouse"], as_dict=True)
		if pinfo and pinfo.is_group and pinfo.company == company:
			return parent
		parent = pinfo.parent_warehouse if pinfo else None
	root = frappe.db.get_value(
		"Warehouse", {"is_group": 1, "company": company, "parent_warehouse": ["in", ["", None]]}, "name", order_by="name"
	) or frappe.db.get_value("Warehouse", {"is_group": 1, "company": company}, "name", order_by="name")
	if not root:
		frappe.throw(
			f"No group warehouse found for company {company}. Sales Forecast needs a Parent Warehouse that is a group."
		)
	return root


def _build_sales_forecast_doc(doc, company, warehouse):
	fc = frappe.new_doc("Sales Forecast")
	fc.naming_series = "SF.YY.-."
	fc.company = company
	fc.posting_date = doc.schedule_date or getdate()
	fc.frequency = "Weekly"
	fc.demand_number = 6
	fc.parent_warehouse = _forecast_parent_warehouse(company, warehouse)
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
	amended.parent_warehouse = _forecast_parent_warehouse(company, warehouse)
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
