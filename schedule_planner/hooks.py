app_name = "schedule_planner"
app_title = "Schedule Planner"
app_publisher = "Indiba Solutions"
app_description = (
	"Customer schedule (PDF/Excel/CSV) ingestion, auto-forecast, Sales Order & "
	"Sales Forecast automation with amendment tracking, and automatic "
	"Material Request generation."
)
app_email = "rohit@indibasolutions.com"
app_license = "MIT"

# Fixtures
# --------
# Custom fields added on core doctypes (Sales Order, Sales Forecast, Material
# Request/Material Request Item) so that installing this app is enough -
# nothing has to be created by hand in the UI.
fixtures = [
	{
		"dt": "Custom Field",
		"filters": [
			["dt", "in", ["Sales Order", "Sales Forecast", "Material Request", "Material Request Item"]],
			["fieldname", "like", "custom_sp_%"],
		],
	}
]

# Doc Events
# ----------
# Customer Schedule: wired to its own controller. See
# schedule_planner/schedule_planner/doctype/customer_schedule/customer_schedule.py
#
# Delivery Note: any shipment (whether or not it came from one of our Sales
# Orders) changes stock and can also represent demand for an item that has no
# active Customer Schedule at all - reconciling the Auto Material Request
# here is what makes "shipping without a schedule" still get picked up.
doc_events = {
	"Customer Schedule": {
		"before_submit": "schedule_planner.schedule_planner.doctype.customer_schedule.customer_schedule.before_submit",
		"on_submit": "schedule_planner.schedule_planner.doctype.customer_schedule.customer_schedule.on_submit",
		"on_cancel": "schedule_planner.schedule_planner.doctype.customer_schedule.customer_schedule.on_cancel",
	},
	"Delivery Note": {
		"on_submit": "schedule_planner.utils.material_request.on_delivery_note_change",
		"on_cancel": "schedule_planner.utils.material_request.on_delivery_note_change",
	},
}

# Scheduled Tasks
# ---------------
# A single daily tick that internally checks Schedule Planning Settings
# (material_request_trigger_mode / mr_schedule_frequency) and decides whether
# to actually run today. Kept as one hook so the frequency stays a runtime
# setting instead of a hardcoded cron string.
scheduler_events = {
	"daily": [
		"schedule_planner.tasks.scheduled_material_request_dispatch",
	],
}

# Website / desk assets (none required beyond what doctypes ship with)
# app_include_js = []
# app_include_css = []
