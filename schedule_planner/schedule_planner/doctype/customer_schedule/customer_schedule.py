# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from schedule_planner.utils import revision as revision_utils
from schedule_planner.utils import safety, sales_documents


class CustomerSchedule(Document):
	pass


def before_submit(doc, method=None):
	"""Compare against the last submitted schedule for this customer + vendor
	*before* this document becomes the new "latest" one, and write the diff
	into the Revision Log grid. The counts are stashed on the in-memory doc
	(not persisted) so on_submit can roll them into Customer Revision Stats
	without recomputing."""
	safety.enforce_before_submit(doc)
	result = revision_utils.detect_and_log_revisions(doc)
	doc._sp_revision_result = result


def on_submit(doc, method=None):
	result = getattr(doc, "_sp_revision_result", None) or {
		"firm_changes": 0,
		"forecast_changes": 0,
		"previous_schedule": None,
	}
	revision_utils.apply_revision_stats(doc.customer, result, doc.name)

	# Firm rows -> one Sales Order per delivery date, cancel+amend if a date's
	# item/qty mix changed since the last schedule.
	sales_documents.sync_firm_to_sales_orders(doc)

	# Forecast rows -> this customer's Sales Forecast is wholesale replaced
	# (previous submitted forecast for the customer is cancelled and amended)
	# because a new schedule makes the old forecast horizon obsolete.
	sales_documents.sync_forecast_to_sales_forecast(doc)

	_maybe_trigger_immediate_material_request(doc)

	doc.db_set("status", "Processed")


def on_cancel(doc, method=None):
	# Downstream Sales Orders / Sales Forecasts are deliberately left alone -
	# by the time someone cancels a schedule, those documents may already be
	# invoiced/delivered/acted upon, and silently cancelling real transactions
	# from here would be dangerous. Surface a reminder instead.
	linked_so = sorted({row.linked_sales_order for row in doc.schedule_items if row.linked_sales_order})
	linked_sf = sorted({row.linked_sales_forecast for row in doc.schedule_items if row.linked_sales_forecast})

	if linked_so or linked_sf:
		frappe.msgprint(
			(
				"This schedule generated <b>{0}</b> Sales Order(s) and <b>{1}</b> "
				"Sales Forecast(s). They have <b>not</b> been cancelled automatically - "
				"review and cancel/amend them by hand if that is what you intend."
			).format(len(linked_so), len(linked_sf)),
			title="Downstream Documents Untouched",
			indicator="orange",
		)

	# The Material Request pool, however, IS always adjusted - cancelling a
	# schedule removes its demand from the shared shortfall calculation, and
	# the live Auto Material Request should shrink/clear right away rather
	# than keep asking for material that's no longer needed.
	_adjust_material_request_for_cancel(doc)

	doc.db_set("status", "Cancelled")


def _maybe_trigger_immediate_material_request(doc):
	from schedule_planner.utils import material_request

	settings = frappe.get_cached_doc("Schedule Planning Settings")
	if settings.material_request_trigger_mode != "Immediate":
		return

	try:
		material_request.run_immediate_mr_check(doc.customer)
	except Exception as e:
		# Never let an MR-generation problem block the schedule submission
		# that already happened - log it AND show the actual reason (not just
		# "see Error Log") since a silent/easy-to-miss toast is exactly what
		# makes "why did no Material Request get created" hard to debug.
		frappe.log_error(
			title="Schedule Planner: Immediate Material Request check failed",
			message=frappe.get_traceback(),
		)
		frappe.msgprint(
			f"Schedule submitted, but the automatic Material Request check "
			f"failed: <b>{frappe.utils.escape_html(str(e))}</b><br><br>"
			f"Fix the setting/issue above, then use the <b>Generate Material "
			f"Request</b> button to retry manually.",
			title="Material Request Check Failed",
			indicator="red",
		)


def _adjust_material_request_for_cancel(doc):
	from schedule_planner.utils import material_request

	try:
		material_request.adjust_for_schedule_cancel(doc.customer)
	except Exception as e:
		frappe.log_error(
			title="Schedule Planner: Material Request adjustment after cancel failed",
			message=frappe.get_traceback(),
		)
		frappe.msgprint(
			f"Schedule cancelled, but adjusting the automatic Material Request "
			f"failed: <b>{frappe.utils.escape_html(str(e))}</b><br><br>"
			f"Review the live Auto Material Request by hand, or use the "
			f"<b>Generate Material Request</b> button to retry.",
			title="Material Request Adjustment Failed",
			indicator="red",
		)
