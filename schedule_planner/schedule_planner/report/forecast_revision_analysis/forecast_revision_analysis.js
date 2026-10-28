// Copyright (c) 2026, Indiba Solutions and contributors
// For license information, please see license.txt

frappe.query_reports["Forecast Revision Analysis"] = {
	filters: [
		{
			fieldname: "customer",
			label: __("Customer"),
			fieldtype: "Link",
			options: "Customer",
			reqd: 1,
			on_change: function () {
				frappe.query_report.set_filter_value("item_code", "");
			},
		},
		{ fieldname: "item_code", label: __("Item Code"), fieldtype: "Link", options: "Item", reqd: 1 },
		{
			fieldname: "from_date",
			label: __("Delivery Date From"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -3),
		},
		{
			fieldname: "to_date",
			label: __("Delivery Date To"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), 6),
		},
	],
	formatter: function (value, row, column, data, default_formatter) {
		if (column.fieldname && column.fieldname.startsWith("d_")) {
			return value || "";
		}
		return default_formatter(value, row, column, data);
	},
	onload: function (report) {
		report.page.add_inner_button(__("Export with Colors"), function () {
			const filters = report.get_values();
			if (!filters.customer || !filters.item_code) {
				frappe.msgprint(__("Please select Customer and Item Code first."));
				return;
			}
			frappe.call({
				method: "frappe.desk.query_report.run",
				args: { report_name: "Forecast Revision Analysis", filters: filters, ignore_prepared_report: true },
				callback: function (r) {
					if (r.message && r.message.result) {
						_export_to_csv(r.message.columns, r.message.result);
					}
				},
			});
		});
	},
};

function _export_to_csv(columns, data) {
	function strip_html(str) {
		if (!str) return "";
		const match = String(str).match(/([\d,]+)/);
		return match ? match[1].replace(/,/g, "") : str;
	}
	const headers = columns.map((c) => c.label);
	const rows = data.map((row) => columns.map((c) => strip_html(row[c.fieldname] || "")));
	const csv = [headers, ...rows].map((r) => r.map((v) => `"${v}"`).join(",")).join("\n");
	const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
	const url = URL.createObjectURL(blob);
	const link = document.createElement("a");
	link.href = url;
	link.download = "Forecast_Revision_Analysis.csv";
	link.click();
	URL.revokeObjectURL(url);
}
