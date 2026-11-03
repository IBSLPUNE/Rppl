# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

"""Safety nets around the three places a wrong number can hurt:

  1. Pre-submit check   - what the file said vs what the schedule now holds,
						  parts that were skipped, and what has to be
						  acknowledged before Submit.
  2. Forecast limit     - an auto-forecast weekly qty above N x the item's
						  history needs a named approver before Submit.
  3. Months of cover    - a Material Request line that would leave more than
						  N months of stock after it arrives needs a named
						  approver (with a written reason) before Submit.

All limits live in Schedule Planning Settings (0 switches a limit off) so
the business can tune them without a code change."""

import hashlib
import json
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import add_months, cint, flt, getdate, now_datetime, today

DEFAULT_FORECAST_MULTIPLIER = 2.0
DEFAULT_MAX_COVER_MONTHS = 12.0
DEFAULT_APPROVER_ROLE = "System Manager"
SHIPPING_REFERENCE_MONTHS = 6


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _settings():
	return frappe.get_cached_doc("Schedule Planning Settings")


def _number_setting(fieldname, default):
	value = _settings().get(fieldname)
	if value in (None, ""):
		return default
	return flt(value)


def forecast_limit_multiplier():
	return _number_setting("forecast_limit_multiplier", DEFAULT_FORECAST_MULTIPLIER)


def max_cover_months():
	return _number_setting("max_cover_months", DEFAULT_MAX_COVER_MONTHS)


def approver_role():
	return (_settings().get("approver_role") or DEFAULT_APPROVER_ROLE).strip()


def user_is_approver(user=None):
	user = user or frappe.session.user
	return user == "Administrator" or approver_role() in frappe.get_roles(user)


# ---------------------------------------------------------------------------
# Forecast limit
# ---------------------------------------------------------------------------

def _file_weekly(own_rows):
	"""Average weekly qty of the customer-supplied rows of ONE item, over the
	weeks from its first delivery to the end of the customer's file."""
	if not own_rows:
		return 0.0
	dates = [getdate(r["delivery_date"]) for r in own_rows]
	start, end = min(dates), max(dates)
	horizon = [getdate(r["horizon_end"]) for r in own_rows if r.get("horizon_end")]
	if horizon:
		end = max(end, max(horizon))
	weeks = max(1.0, (end - start).days / 7.0 + 1)
	return sum(flt(r["quantity"]) for r in own_rows) / weeks


def _shipped_weekly(item_code, months=SHIPPING_REFERENCE_MONTHS):
	"""Average weekly Delivery Note qty over the last `months` months."""
	from_date = add_months(getdate(today()), -months)
	res = frappe.db.sql(
		"""
		SELECT SUM(dni.qty)
		FROM `tabDelivery Note Item` dni
		INNER JOIN `tabDelivery Note` dn ON dn.name = dni.parent
		WHERE dni.item_code = %(item)s AND dn.docstatus = 1
		  AND dn.posting_date >= %(from_date)s AND dn.posting_date <= %(today)s
		""",
		{"item": item_code, "from_date": str(from_date), "today": str(getdate(today()))},
	)
	total = flt(res[0][0]) if res and res[0][0] else 0.0
	return total / (months * 13.0 / 3.0)


def check_item(item_code, own_rows, forecast_weekly, multiplier=None):
	"""Compare one item's forecast weekly qty with its history (the higher of
	the file's own weekly average and the last 6 months' shipments)."""
	multiplier = forecast_limit_multiplier() if multiplier is None else multiplier
	file_weekly = _file_weekly(own_rows)
	shipped_weekly = _shipped_weekly(item_code)
	history = max(file_weekly, shipped_weekly)
	forecast_weekly = flt(forecast_weekly)
	flagged = bool(
		multiplier > 0
		and forecast_weekly > 0.01
		and (history <= 0.01 or forecast_weekly > multiplier * history + 0.005)
	)
	return {
		"item_code": item_code,
		"forecast_weekly": round(forecast_weekly, 2),
		"file_weekly": round(file_weekly, 2),
		"shipped_weekly": round(shipped_weekly, 2),
		"history_weekly": round(history, 2),
		"ratio": round(forecast_weekly / history, 2) if history > 0.01 else None,
		"multiplier": multiplier,
		"flagged": flagged,
	}


def annotate_preview(preview, rows):
	"""Add the limit check to each preview row that would really get forecast
	quantity, so the dialog can warn before anything is created."""
	multiplier = forecast_limit_multiplier()
	own_by_item = defaultdict(list)
	for r in rows:
		if not r.get("auto_forecast"):
			own_by_item[r["item_code"]].append(r)
	for p in preview:
		p["forecast_flag"] = False
		p["limit_multiplier"] = multiplier
		if flt(p.get("auto_qty")) > 0:
			res = check_item(p["item_code"], own_by_item.get(p["item_code"], []), p.get("predicted_value"), multiplier)
			p["history_weekly"] = res["history_weekly"]
			p["forecast_ratio"] = res["ratio"]
			p["forecast_flag"] = res["flagged"]
	return preview


def _doc_rows(doc):
	horizon = str(doc.horizon_end) if doc.get("horizon_end") else None
	return [
		{
			"item_code": r.item_code,
			"delivery_date": str(r.delivery_date),
			"quantity": flt(r.quantity),
			"auto_forecast": cint(r.get("auto_forecast")),
			"horizon_end": horizon,
		}
		for r in doc.schedule_items
	]


def forecast_flags_for_rows(rows, multiplier=None):
	"""Items whose auto-forecast weekly qty is over the limit. Auto rows are
	weekly and equal per item, so the weekly figure is their average."""
	multiplier = forecast_limit_multiplier() if multiplier is None else multiplier
	if multiplier <= 0:
		return []
	own, auto = defaultdict(list), defaultdict(list)
	for r in rows:
		(auto if cint(r.get("auto_forecast")) else own)[r["item_code"]].append(r)
	flags = []
	for item_code, auto_rows in auto.items():
		total = sum(flt(r["quantity"]) for r in auto_rows)
		res = check_item(item_code, own.get(item_code, []), total / len(auto_rows), multiplier)
		if res["flagged"]:
			res["forecast_total"] = round(total, 2)
			res["weeks"] = len(auto_rows)
			flags.append(res)
	return sorted(flags, key=lambda x: x["item_code"])


def _signature(flags):
	text = "|".join(f"{f['item_code']}:{f['forecast_weekly']:.2f}" for f in sorted(flags, key=lambda x: x["item_code"]))
	return hashlib.md5(text.encode()).hexdigest() if text else ""


# ---------------------------------------------------------------------------
# Pre-submit report / acknowledgement / approval
# ---------------------------------------------------------------------------

def _skipped_parts(doc):
	try:
		return json.loads(doc.get("skipped_parts_json") or "[]")
	except ValueError:
		return []


def get_report(schedule_name):
	doc = frappe.get_doc("Customer Schedule", schedule_name)
	rows = _doc_rows(doc)
	own = [r for r in rows if not r["auto_forecast"]]
	auto = [r for r in rows if r["auto_forecast"]]

	system_qty = sum(r["quantity"] for r in own)
	skipped = _skipped_parts(doc)
	skipped_qty = flt(doc.get("skipped_qty"))
	file_total = flt(doc.get("file_total_qty"))
	included_qty = flt(doc.get("file_included_qty"))

	months = lambda rs: len({getdate(r["delivery_date"]).strftime("%Y-%m") for r in rs})

	# does the schedule still add up to the file? (system + skipped == file)
	reconciles = abs(file_total - (system_qty + skipped_qty)) <= max(1.0, file_total * 0.001) if file_total else None
	edited_since_upload = abs(system_qty - included_qty) > max(1.0, included_qty * 0.001) if included_qty else False

	flags = forecast_flags_for_rows(rows)
	signature = _signature(flags)
	approved = bool(
		flags and cint(doc.get("forecast_approved")) and doc.get("forecast_approval_signature") == signature
	)

	return {
		"schedule": doc.name,
		"customer": doc.customer,
		"release_number": doc.release_number,
		"docstatus": doc.docstatus,
		"file": {
			"total_qty": file_total,
			"included_qty": included_qty,
			"system_qty": round(system_qty, 2),
			"skipped_qty": skipped_qty,
			"reconciles": reconciles,
			"edited_since_upload": edited_since_upload,
			"from_date": str(doc.get("file_from_date") or ""),
			"to_date": str(doc.get("file_to_date") or ""),
			"horizon_end": str(doc.get("horizon_end") or ""),
			"months_parsed": cint(doc.get("file_months")),
			"months_in_lines": months(own),
			"lines_parsed": cint(doc.get("file_line_count")),
			"lines_in_schedule": len(own),
			"items": len({r["item_code"] for r in own}),
		},
		"skipped": skipped,
		"ack": {
			"required": bool(skipped),
			"done": bool(cint(doc.get("skipped_acknowledged"))),
			"by": doc.get("acknowledged_by"),
			"on": str(doc.get("acknowledged_on") or ""),
		},
		"forecast": {
			"lines": len(auto),
			"qty": round(sum(r["quantity"] for r in auto), 2),
			"months": months(auto),
			"multiplier": forecast_limit_multiplier(),
			"flags": flags,
			"needs_approval": bool(flags),
			"approved": approved,
			"approved_by": doc.get("forecast_approved_by") if approved else None,
			"approved_note": doc.get("forecast_approval_note") if approved else None,
			"approver_role": approver_role(),
			"can_approve": user_is_approver(),
		},
	}


def acknowledge_skipped(schedule_name):
	doc = frappe.get_doc("Customer Schedule", schedule_name)
	doc.check_permission("write")
	if doc.docstatus != 0:
		frappe.throw(_("Only a Draft schedule can be acknowledged."))
	doc.db_set(
		{
			"skipped_acknowledged": 1,
			"acknowledged_by": frappe.session.user,
			"acknowledged_on": now_datetime(),
		},
		update_modified=False,
	)
	doc.add_comment(
		"Info",
		_("Skipped parts acknowledged by {0}: {1}").format(
			frappe.session.user, ", ".join(str(s.get("file_part_no")) for s in _skipped_parts(doc)) or "-"
		),
	)
	return {"status": "ok"}


def approve_forecast_limit(schedule_name, note):
	if not user_is_approver():
		frappe.throw(
			_("Only users with the role {0} can approve a forecast above the limit.").format(
				f"<b>{approver_role()}</b>"
			),
			frappe.PermissionError,
		)
	note = (note or "").strip()
	if not note:
		frappe.throw(_("Write a short reason for approving this forecast."))

	doc = frappe.get_doc("Customer Schedule", schedule_name)
	doc.check_permission("write")
	if doc.docstatus != 0:
		frappe.throw(_("Only a Draft schedule can be approved."))

	flags = forecast_flags_for_rows(_doc_rows(doc))
	if not flags:
		return {"status": "ok", "message": _("Nothing above the limit - no approval needed.")}

	doc.db_set(
		{
			"forecast_approved": 1,
			"forecast_approved_by": frappe.session.user,
			"forecast_approved_on": now_datetime(),
			"forecast_approval_note": note,
			"forecast_approval_signature": _signature(flags),
		},
		update_modified=False,
	)
	doc.add_comment(
		"Info",
		_("Forecast above {0}x history approved by {1} for: {2}. Reason: {3}").format(
			forecast_limit_multiplier(),
			frappe.session.user,
			", ".join(f["item_code"] for f in flags),
			note,
		),
	)
	return {"status": "ok"}


def enforce_before_submit(doc):
	"""Server-side gate - the dialog is a convenience, this is the rule."""
	if _skipped_parts(doc) and not cint(doc.get("skipped_acknowledged")):
		frappe.throw(
			_(
				"{0} part number(s) were skipped when this schedule was created. Click the Pre-submit Check "
				"button on this form, tick the acknowledgement, then Submit from there."
			).format(len(_skipped_parts(doc))),
			title=_("Pre-submit Check"),
		)

	flags = forecast_flags_for_rows(_doc_rows(doc))
	if flags:
		approved = cint(doc.get("forecast_approved")) and doc.get("forecast_approval_signature") == _signature(flags)
		if not approved:
			lines = "<br>".join(
				f"{f['item_code']}: forecast {f['forecast_weekly']:,.2f}/week vs history {f['history_weekly']:,.2f}/week"
				for f in flags
			)
			frappe.throw(
				_(
					"This forecast is above {0}x the item's history and needs approval from a user with the "
					"role {1}:<br><br>{2}"
				).format(forecast_limit_multiplier(), f"<b>{approver_role()}</b>", lines),
				title=_("Forecast Above Limit"),
			)


# ---------------------------------------------------------------------------
# Months of cover (Material Request)
# ---------------------------------------------------------------------------

def cover_months(available_qty, order_qty, monthly_demand):
	"""Months of demand covered by what is on hand/ordered plus this order."""
	if flt(monthly_demand) <= 0:
		return None
	return round((flt(available_qty) + flt(order_qty)) / flt(monthly_demand), 1)


def mr_excess_lines(doc):
	"""Lines of an auto Material Request whose cover (recomputed from the
	qty now on the line) is above the limit."""
	limit = max_cover_months()
	if limit <= 0 or not cint(doc.get("custom_sp_auto_generated")):
		return limit, []
	lines = []
	for r in doc.items:
		monthly = flt(r.get("custom_sp_monthly_demand"))
		if monthly <= 0:
			continue
		cover = cover_months(r.get("custom_sp_available_qty"), r.qty, monthly)
		if cover is not None and cover > limit + 0.05:
			lines.append(
				{
					"item_code": r.item_code,
					"qty": flt(r.qty),
					"monthly_demand": monthly,
					"cover": cover,
				}
			)
	return limit, lines


def mr_before_submit(doc, method=None):
	limit, lines = mr_excess_lines(doc)
	if not lines:
		return
	table = "<br>".join(
		f"{l['item_code']}: order {l['qty']:,.0f} leaves {l['cover']:,.1f} months of cover "
		f"(monthly demand {l['monthly_demand']:,.0f})"
		for l in lines
	)
	if not user_is_approver():
		frappe.throw(
			_(
				"This Material Request would leave more than {0} months of stock for:<br><br>{1}<br><br>"
				"It must be submitted by a user with the role {2}, or the quantities reduced."
			).format(limit, table, f"<b>{approver_role()}</b>"),
			title=_("Excess Cover - Approval Needed"),
		)
	note = (doc.get("custom_sp_cover_approval_note") or "").strip()
	if not note:
		frappe.throw(
			_(
				"Write the reason for ordering more than {0} months of cover in <b>Cover Approval Note</b> "
				"before submitting:<br><br>{1}"
			).format(limit, table),
			title=_("Excess Cover - Reason Needed"),
		)
	doc.add_comment(
		"Info",
		_("Excess cover (> {0} months) approved by {1}. Reason: {2}").format(limit, frappe.session.user, note),
	)


@frappe.whitelist()
def get_mr_cover_check(mr_name):
	"""For the Material Request form: which lines are over the cover limit."""
	doc = frappe.get_doc("Material Request", mr_name)
	limit, lines = mr_excess_lines(doc)
	return {
		"limit": limit,
		"lines": lines,
		"can_approve": user_is_approver(),
		"approver_role": approver_role(),
	}
