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


def _item_rate(item_code, ctx=None):
	"""Selling rate in the Sales Order's own currency. Item.standard_rate is
	kept in company currency, so for a foreign-currency customer a rate from
	the customer's Selling Price List (in that currency) is preferred, and
	otherwise standard_rate is converted with the order's exchange rate."""

	standard = flt(frappe.db.get_value("Item", item_code, "standard_rate"))
	if not ctx or ctx["currency"] == ctx["company_currency"]:
		return standard

	if ctx.get("price_list"):
		price = frappe.db.get_value(
			"Item Price",
			{"item_code": item_code, "price_list": ctx["price_list"], "selling": 1, "currency": ctx["currency"]},
			"price_list_rate",
		)
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
	"""One Sales Order per delivery date. An order whose items ALL have a rate
	is submitted; if even one item has no rate (0) the order is saved as a
	Draft so nothing is committed at a zero price - set the rate and submit it
	by hand, or let the next schedule for that date do it once the rates exist."""
	firm_rows = [r for r in doc.schedule_items if r.demand_type == "Firm" and flt(r.quantity) > 0]
	if not firm_rows:
		return

	company = _default_company()
	warehouse = _default_warehouse(company)

	by_date = defaultdict(list)
	for row in firm_rows:
		by_date[str(row.delivery_date)].append(row)

	report = {"submitted": [], "draft": []}
	for delivery_date, rows_for_date in by_date.items():
		_sync_one_date_so(doc, delivery_date, rows_for_date, company, warehouse, report)

	_report_draft_orders(report)


def _report_draft_orders(report):
	drafts = report["draft"]
	if not drafts:
		return
	lines = "<br>".join(
		f"<a href='/app/sales-order/{name}'>{name}</a> ({frappe.utils.escape_html(str(date))}): "
		f"no rate for {frappe.utils.escape_html(', '.join(missing))}"
		for name, date, missing in drafts
	)
	frappe.msgprint(
		(
			"<b>{0}</b> Sales Order(s) were saved as <b>Draft</b> because one or more items have no rate "
			"(Item &gt; Standard Selling Rate is empty or 0). {1} Sales Order(s) with every rate present were submitted."
			"<br><br>{2}<br><br>Set the rate on the order (or on the Item) and submit it."
		).format(len(drafts), len(report["submitted"]), lines),
		title="Sales Orders Left as Draft",
		indicator="orange",
	)


def _missing_rate_items(so):
	return [r.item_code for r in so.items if flt(r.rate) <= 0]


def _finish_sales_order(so, delivery_date, report):
	"""The order is already inserted: submit it only when every line has a rate."""
	missing = _missing_rate_items(so)
	if missing:
		report["draft"].append((so.name, delivery_date, sorted(set(missing))))
		return so.name
	so.submit()
	report["submitted"].append(so.name)
	return so.name


def _sync_one_date_so(doc, delivery_date, rows_for_date, company, warehouse, report=None):
	report = report if report is not None else {"submitted": [], "draft": []}
	new_items = {}
	for row in rows_for_date:
		new_items[row.item_code] = flt(new_items.get(row.item_code, 0)) + flt(row.quantity)

	existing = frappe.db.get_value(
		"Sales Order",
		{
			"customer": doc.customer,
			"custom_sp_is_schedule_generated": 1,
			"custom_sp_delivery_date": delivery_date,
			"docstatus": ["in", [0, 1]],
		},
		["name", "docstatus"],
		order_by="creation desc",
	)
	existing_name, existing_status = (existing[0], existing[1]) if existing else (None, None)

	if existing_name and existing_status == 0:
		# still a Draft (an earlier schedule found an item without a rate): keep it,
		# bring its lines up to this schedule, keep any rate that was typed in, and
		# submit it now if every line has a rate
		so_name = _update_draft_sales_order(existing_name, doc, delivery_date, new_items, company, warehouse, report)
	elif existing_name:
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
		so_name = _amend_sales_order(existing_name, doc, delivery_date, new_items, company, warehouse, report)
	else:
		so_name = _create_sales_order(doc, delivery_date, new_items, company, warehouse, report)

	for row in rows_for_date:
		row.linked_sales_order = so_name


def _append_so_items(so, new_items, warehouse, delivery_date, ctx=None, fallback_rates=None):
	for item_code, qty in new_items.items():
		rate = _item_rate(item_code, ctx)
		if rate <= 0 and fallback_rates:
			# an order being replaced keeps the rate it already had
			rate = flt(fallback_rates.get(item_code))
		so.append(
			"items",
			{
				"item_code": item_code,
				"qty": qty,
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
	so.transaction_date = doc.schedule_date or getdate()
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
		line.qty = new_items[line.item_code]
		line.delivery_date = delivery_date
		if flt(line.rate) <= 0:  # a rate typed into the draft is kept
			line.rate = _item_rate(line.item_code, ctx)
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
			old_rates.setdefault(line.item_code, flt(line.rate))
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

	ctx = _currency_context(doc.customer, company, amended.transaction_date)
	_apply_currency(amended, ctx)
	_append_so_items(amended, new_items, warehouse, delivery_date, ctx, fallback_rates=old_rates)

	amended.insert(ignore_permissions=True)
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
