// Copyright (c) 2026, Indiba Solutions and contributors
// For license information, please see license.txt

frappe.query_reports["Material Planning Dashboard"] = {
	filters: [
		{
			fieldname: "planning_months",
			label: __("Planning Horizon (Months)"),
			fieldtype: "Int",
			description: __("Leave blank to use the default from Schedule Planning Settings."),
		},
		{
			fieldname: "lead_time_months",
			label: __("Lead Time (Months)"),
			fieldtype: "Int",
			description: __("Leave blank to use the default from Schedule Planning Settings."),
		},
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: "\nShortfall\nCovered\nSurplus",
		},
	],
	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (!data) return value;

		if (column.fieldname === "status") {
			const colors = { Shortfall: "red", Surplus: "orange", Covered: "green" };
			const color = colors[data.status] || "gray";
			return `<span style="color:var(--${color}-600,${color});font-weight:600">${frappe.utils.escape_html(data.status)}</span>`;
		}
		if (column.fieldname === "net_shortfall" && data.net_shortfall > 0) {
			return `<span style="color:var(--red-600,#d1372a);font-weight:600">${value}</span>`;
		}
		if (column.fieldname === "demand_source" && data.demand_source === "Shipping History (No Schedule)") {
			return `<span style="color:var(--orange-600,#c07a1e)">${frappe.utils.escape_html(value)}</span>`;
		}
		return value;
	},
};
