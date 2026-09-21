# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

"""Registered under scheduler_events["daily"] in hooks.py. Kept as a single
daily tick (rather than separate daily/weekly cron entries) so that
Schedule Planning Settings.mr_schedule_frequency stays a runtime setting a
user can flip in the UI, instead of something that needs a code deploy."""

import frappe
from frappe.utils import getdate, today

from schedule_planner.utils import material_request


def scheduled_material_request_dispatch():
	settings = frappe.get_cached_doc("Schedule Planning Settings")

	if settings.material_request_trigger_mode != "Scheduled":
		return

	if settings.mr_schedule_frequency == "Weekly" and getdate(today()).weekday() != 0:
		# Monday only, when running weekly. Still ticks daily so a missed
		# Monday (site down, etc.) doesn't wait a whole extra week -
		# swap this for a last-run-date check if that matters to you.
		return

	try:
		material_request.run_scheduled_mr_generation()
	except Exception:
		frappe.log_error(
			title="Schedule Planner: scheduled Material Request run failed",
			message=frappe.get_traceback(),
		)
		return

	frappe.db.set_single_value("Schedule Planning Settings", "last_scheduled_mr_run", today())
