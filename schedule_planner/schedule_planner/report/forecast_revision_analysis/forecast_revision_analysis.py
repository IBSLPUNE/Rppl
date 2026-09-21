# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

import datetime

import frappe


def execute(filters=None):
	filters = filters or {}
	columns, data = [], []

	if not filters.get("customer") or not filters.get("item_code"):
		return columns, data

	date_condition = ""
	if filters.get("from_date"):
		date_condition += " AND csi.delivery_date >= %(from_date)s"
	if filters.get("to_date"):
		date_condition += " AND csi.delivery_date <= %(to_date)s"

	all_schedules = frappe.db.sql(
		f"""
		SELECT
			cs.name AS schedule_name,
			cs.release_number,
			cs.schedule_date,
			csi.delivery_date,
			csi.quantity AS new_qty
		FROM `tabCustomer Schedule` cs
		INNER JOIN `tabCustomer Schedule Item` csi ON csi.parent = cs.name
		WHERE
			cs.customer = %(customer)s
			AND cs.docstatus = 1
			AND csi.item_code = %(item_code)s
			AND csi.demand_type IN ('Forecast', 'Raw')
			AND csi.quantity > 0
			{date_condition}
		ORDER BY cs.schedule_date ASC, csi.delivery_date ASC
		""",
		filters,
		as_dict=True,
	)

	if not all_schedules:
		return [], []

	rev_conditions = "srl.item_code = %(item_code)s AND cs.customer = %(customer)s"
	if filters.get("from_date"):
		rev_conditions += " AND srl.delivery_date >= %(from_date)s"
	if filters.get("to_date"):
		rev_conditions += " AND srl.delivery_date <= %(to_date)s"

	revision_rows = frappe.db.sql(
		f"""
		SELECT srl.parent AS schedule_name, srl.delivery_date, srl.`change`
		FROM `tabSchedule Revision Log` srl
		INNER JOIN `tabCustomer Schedule` cs ON cs.name = srl.parent
		WHERE {rev_conditions}
		""",
		filters,
		as_dict=True,
	)

	change_map = {}
	for r in revision_rows:
		change_map[(r.schedule_name, str(r.delivery_date))] = float(r.change or 0)

	delivery_dates = sorted({str(r.delivery_date) for r in all_schedules})

	seen_schedules = {}
	for r in all_schedules:
		if r.schedule_name not in seen_schedules:
			seen_schedules[r.schedule_name] = {
				"schedule_name": r.schedule_name,
				"schedule_date": str(r.schedule_date),
				"release_number": r.release_number,
			}
	schedule_list = list(seen_schedules.values())

	pivot = {}
	for r in all_schedules:
		pivot[(r.schedule_name, str(r.delivery_date))] = float(r.new_qty or 0)

	columns = [
		{"label": "Release", "fieldname": "release_number", "fieldtype": "Data", "width": 110},
		{"label": "Schedule Date", "fieldname": "schedule_date", "fieldtype": "Date", "width": 110},
		{
			"label": "Schedule",
			"fieldname": "schedule_name",
			"fieldtype": "Link",
			"options": "Customer Schedule",
			"width": 160,
		},
	]
	for d in delivery_dates:
		dt = datetime.date.fromisoformat(d)
		columns.append(
			{
				"label": dt.strftime("%d-%b-%y"),
				"fieldname": "d_" + d.replace("-", "_"),
				"fieldtype": "Data",
				"width": 110,
			}
		)

	data = []
	for sch in schedule_list:
		row = {
			"release_number": sch["release_number"],
			"schedule_date": sch["schedule_date"],
			"schedule_name": sch["schedule_name"],
		}
		for d in delivery_dates:
			col_key = "d_" + d.replace("-", "_")
			qty = pivot.get((sch["schedule_name"], d))
			change = change_map.get((sch["schedule_name"], d))
			if qty is not None:
				if change is not None and change > 0:
					color, bg = "#1a6b3c", "#eafaf1"
				elif change is not None and change < 0:
					color, bg = "#a93226", "#fdedec"
				else:
					color, bg = "#333", "transparent"
				row[col_key] = (
					f'<span style="color:{color};background:{bg};padding:1px 5px;'
					f'border-radius:3px;font-weight:500">{qty:,.0f}</span>'
				)
			else:
				row[col_key] = ""
		data.append(row)

	return columns, data
