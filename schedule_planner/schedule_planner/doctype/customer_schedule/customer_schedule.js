// Copyright (c) 2026, Indiba Solutions and contributors
// For license information, please see license.txt

frappe.ui.form.on("Customer Schedule", {
	refresh: function (frm) {
		if (frm.is_new()) {
			frm.add_custom_button(__("Upload Schedule File"), function () {
				_upload_and_analyse(frm);
			}).addClass("btn-primary");
		}

		if (!frm.is_new() && frm.doc.docstatus === 0) {
			frm.add_custom_button(__("Extend Forecast"), function () {
				_show_extend_dialog(frm);
			});
		}

		if (frm.doc.docstatus === 1) {
			frm.add_custom_button(
				__("Generate Material Request"),
				function () {
					_show_mr_dialog(frm);
				},
				__("Planning")
			);
			frm.add_custom_button(
				__("Compare Revisions"),
				function () {
					_show_revision_dialog(frm);
				},
				__("Planning")
			);
		}
	},
});

// ============================================================================
// UPLOAD + ANALYSE (PDF / Excel / CSV - format is auto-detected per customer)
// ============================================================================

function _upload_and_analyse(frm) {
	if (!frm.doc.customer) {
		frappe.msgprint(__("Select a Customer before uploading a schedule file."));
		return;
	}

	new frappe.ui.FileUploader({
		folder: "Home/Attachments",
		on_success: function (file_doc) {
			frappe.show_alert({ message: __("Analysing schedule file..."), indicator: "orange" });
			frappe.call({
				method: "schedule_planner.api.analyse_schedule_file",
				args: { file_url: file_doc.file_url, customer: frm.doc.customer },
				freeze: true,
				freeze_message: __("Reading schedule file..."),
				callback: function (r) {
					if (!r.message) return;
					_show_create_dialog(frm, file_doc.file_url, r.message);
				},
			});
		},
	});
}

function _show_create_dialog(frm, file_url, analysis) {
	const available_months = parseInt(analysis.available_months || 0);
	const default_target = available_months + 3;
	const rows_json = JSON.stringify(analysis.parsed_rows || []);
	const header_json = JSON.stringify(analysis.header || {});

	const dialog = new frappe.ui.Dialog({
		title: __("Customer Forecast Planning"),
		size: "extra-large",
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "summary",
				options: `
					<div style="padding:12px 15px;background:var(--bg-light-gray,#f8f9fa);border-radius:8px;margin-bottom:12px;font-size:13px">
						<b>${__("File Analysis")}</b><br>
						${__("Release")}: <b>${frappe.utils.escape_html(analysis.header.release_number || "-")}</b>
						&nbsp;|&nbsp; ${__("Data available")}: <b>${available_months}</b> ${__("month(s)")}
						&nbsp;|&nbsp; ${analysis.from_date} &rarr; ${analysis.to_date}
					</div>
				`,
			},
			{
				fieldtype: "HTML",
				fieldname: "item_table",
				options: _item_table_html(analysis.item_details || []),
			},
			{ fieldtype: "Section Break" },
			{
				label: __("How many total months do you want (file + auto-forecast)?"),
				fieldname: "target_months",
				fieldtype: "Int",
				default: default_target,
				reqd: 1,
				description: __("File contains {0} month(s). Consider your parts lead time when choosing this.", [
					available_months,
				]),
			},
			{ fieldtype: "Column Break" },
			{
				label: __("Auto Forecast Method"),
				fieldname: "forecast_method",
				fieldtype: "Select",
				options: [
					{ label: "Average of Last 4 Weeks", value: "LAST_4_WEEKS_AVG" },
					{ label: "Average Weekly Shipment - Last 3 Months", value: "LAST_3_MONTHS_SHIPPING_AVG" },
				],
				default: "LAST_4_WEEKS_AVG",
				reqd: 1,
			},
			{ fieldtype: "HTML", fieldname: "method_help", options: _method_help_html("LAST_4_WEEKS_AVG") },
			{ fieldtype: "HTML", fieldname: "forecast_preview", options: `<div class="sp-forecast-preview"></div>` },
		],
		primary_action_label: __("Create Customer Schedule (Draft)"),
		primary_action: function (values) {
			const target_months = parseInt(values.target_months);
			if (!target_months || target_months < available_months) {
				frappe.msgprint({
					title: __("Invalid Months"),
					message: __("File contains {0} month(s). Required months cannot be less than that.", [
						available_months,
					]),
					indicator: "red",
				});
				return;
			}

			const selected = _get_selected_items(dialog);
			if (selected.length === 0) {
				frappe.msgprint({ message: __("Select at least one item."), indicator: "orange" });
				return;
			}

			dialog.disable_primary_action();
			frappe.call({
				method: "schedule_planner.api.create_schedule_with_forecast",
				args: {
					file_url: file_url,
					customer: frm.doc.customer,
					target_months: target_months,
					forecast_method: values.forecast_method,
					selected_items: JSON.stringify(selected),
					header_json: header_json,
					rows_json: rows_json,
					schedule_format: analysis.schedule_format,
				},
				freeze: true,
				freeze_message: __("Creating Customer Schedule..."),
				callback: function (r) {
					dialog.enable_primary_action();
					if (!r.message || r.message.status !== "Success") return;
					dialog.hide();
					frappe.show_alert({ message: r.message.message, indicator: "green" });
					frappe.set_route("Form", "Customer Schedule", r.message.docname);
				},
			});
		},
	});

	dialog.fields_dict.forecast_method.df.onchange = function () {
		const method = dialog.get_value("forecast_method");
		dialog.fields_dict.method_help.$wrapper.html(_method_help_html(method));
		_refresh_preview_from_rows(dialog, rows_json, method, _get_selected_items(dialog));
	};

	dialog.show();
	_wire_item_table(dialog, function () {
		_refresh_preview_from_rows(dialog, rows_json, dialog.get_value("forecast_method"), _get_selected_items(dialog));
	});
	_refresh_preview_from_rows(dialog, rows_json, "LAST_4_WEEKS_AVG", _get_selected_items(dialog));
}

// ============================================================================
// EXTEND FORECAST (Draft schedule already created)
// ============================================================================

function _show_extend_dialog(frm) {
	frappe.call({
		method: "schedule_planner.api.get_schedule_month_info",
		args: { schedule_name: frm.doc.name },
		freeze: true,
		freeze_message: __("Analysing schedule..."),
		callback: function (r) {
			if (!r.message) return;
			const info = r.message;
			if (!info.available_months) {
				frappe.msgprint(__("No schedule rows found on this document yet."));
				return;
			}

			const dialog = new frappe.ui.Dialog({
				title: __("Extend Forecast"),
				size: "extra-large",
				fields: [
					{
						fieldtype: "HTML",
						fieldname: "summary",
						options: `
							<div style="padding:10px 12px;background:var(--bg-light-gray,#f8f9fa);border-radius:6px;margin-bottom:12px;font-size:12px">
								${__("Current coverage")}: <b>${info.available_months}</b> ${__("month(s)")}
								&nbsp;|&nbsp; ${info.from_date} &rarr; ${info.to_date}
							</div>
						`,
					},
					{ fieldtype: "HTML", fieldname: "item_table", options: _item_table_html(info.item_details || []) },
					{ fieldtype: "Section Break" },
					{
						fieldtype: "Int",
						fieldname: "target_months",
						label: __("Total months required (file + auto)"),
						default: info.available_months + 3,
						reqd: 1,
					},
					{ fieldtype: "Column Break" },
					{
						fieldtype: "Select",
						fieldname: "forecast_method",
						label: __("Auto Forecast Method"),
						options: [
							{ label: "Average of Last 4 Weeks", value: "LAST_4_WEEKS_AVG" },
							{ label: "Average Weekly Shipment - Last 3 Months", value: "LAST_3_MONTHS_SHIPPING_AVG" },
						],
						default: "LAST_4_WEEKS_AVG",
						reqd: 1,
					},
					{ fieldtype: "HTML", fieldname: "method_help", options: _method_help_html("LAST_4_WEEKS_AVG") },
					{ fieldtype: "HTML", fieldname: "forecast_preview", options: `<div class="sp-forecast-preview"></div>` },
				],
				primary_action_label: __("Generate Forecast"),
				primary_action: function (values) {
					const selected = _get_selected_items(dialog);
					if (selected.length === 0) {
						frappe.msgprint({ message: __("Select at least one item."), indicator: "orange" });
						return;
					}
					const target = parseInt(values.target_months);
					if (target <= info.available_months) {
						frappe.msgprint({
							message: __("Enter a value greater than {0}.", [info.available_months]),
							indicator: "orange",
						});
						return;
					}
					dialog.hide();
					frappe.call({
						method: "schedule_planner.api.extend_forecast",
						args: {
							schedule_name: frm.doc.name,
							target_months: target,
							forecast_method: values.forecast_method,
							selected_items: JSON.stringify(selected),
						},
						freeze: true,
						freeze_message: __("Generating forecast..."),
						callback: function (res) {
							if (!res.message) return;
							frappe.show_alert({ message: res.message.message, indicator: "green" });
							frm.reload_doc();
						},
					});
				},
			});

			dialog.fields_dict.forecast_method.df.onchange = function () {
				const method = dialog.get_value("forecast_method");
				dialog.fields_dict.method_help.$wrapper.html(_method_help_html(method));
				_refresh_preview_from_schedule(dialog, frm.doc.name, method, _get_selected_items(dialog));
			};

			dialog.show();
			_wire_item_table(dialog, function () {
				_refresh_preview_from_schedule(
					dialog,
					frm.doc.name,
					dialog.get_value("forecast_method"),
					_get_selected_items(dialog)
				);
			});
			_refresh_preview_from_schedule(dialog, frm.doc.name, "LAST_4_WEEKS_AVG", _get_selected_items(dialog));
		},
	});
}

// ============================================================================
// MATERIAL REQUEST (manual trigger - available regardless of Settings mode)
// ============================================================================

function _show_mr_dialog(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Generate Material Request"),
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "mr_info",
				options: `
					<div style="background:var(--bg-orange,#fff7e6);border:1px solid var(--border-color,#f0d9a8);border-radius:8px;padding:12px 14px;margin-bottom:12px;font-size:13px">
						<b>${__("Formula")}:</b> ${__("(Firm + Forecast, incl. shipping-history fallback where a customer has no active schedule) − (Stock + Open PO + Other Open MR) = Shortfall")}<br>
						${__("This reruns the same live reconciliation used everywhere else - it updates ONE ongoing Auto Material Request (raising/lowering/removing lines as needed) rather than creating a new document each time.")}
					</div>
				`,
			},
			{ fieldtype: "Int", fieldname: "planning_months", label: __("Planning horizon (months)"), default: 12, reqd: 1 },
			{
				fieldtype: "Int",
				fieldname: "lead_time_months",
				label: __("Lead time (months)"),
				default: 7,
				reqd: 1,
				description: __("Required date = today + lead time."),
			},
			{ fieldtype: "Link", fieldname: "warehouse", label: __("Warehouse (optional)"), options: "Warehouse" },
		],
		primary_action_label: __("Reconcile Now"),
		primary_action: function (values) {
			dialog.hide();
			frappe.call({
				method: "schedule_planner.api.generate_material_request_manual",
				args: {
					customer: frm.doc.customer,
					planning_months: values.planning_months,
					lead_time_months: values.lead_time_months,
					warehouse: values.warehouse || null,
				},
				freeze: true,
				freeze_message: __("Calculating shortfalls..."),
				callback: function (r) {
					if (!r.message) return;
					const res = r.message;
					const counts = __("{0} added, {1} updated, {2} removed", [
						(res.created || []).length,
						(res.updated || []).length,
						(res.removed || []).length,
					]);
					frappe.msgprint({
						title: res.mr_name ? __("Auto Material Request Reconciled") : __("No Shortfall"),
						message: res.mr_name
							? `${res.message}<br>${counts}<br><br><a href="/app/material-request/${res.mr_name}" target="_blank">${__("Open")} ${res.mr_name}</a>`
							: res.message,
						indicator: res.mr_name ? "green" : "blue",
					});
				},
			});
		},
	});
	dialog.show();
}

// ============================================================================
// REVISION COMPARISON (submitted schedules only)
// ============================================================================

function _show_revision_dialog(frm) {
	frappe.call({
		method: "schedule_planner.api.get_revision_comparison",
		args: { schedule_name: frm.doc.name },
		freeze: true,
		freeze_message: __("Loading revision comparison..."),
		callback: function (r) {
			if (!r.message) return;
			const data = r.message;
			if (!data.previous) {
				frappe.msgprint(__("No previous submitted schedule found to compare against."));
				return;
			}

			const { summary, rows } = data;
			const dir_icon = { increase: "&#9650;", decrease: "&#9660;", new: "+", removed: "&mdash;" };
			const rows_html = rows
				.map(function (row) {
					const color = row.change > 0 ? "var(--text-success,#1a6b3c)" : "var(--text-danger,#a93226)";
					return `<tr>
						<td>${frappe.utils.escape_html(row.item_code)}</td>
						<td>${frappe.datetime.str_to_user(row.delivery_date)}</td>
						<td>${row.demand_type}</td>
						<td style="text-align:right">${row.old_qty || "-"}</td>
						<td style="text-align:right">${row.new_qty || "-"}</td>
						<td style="text-align:right;color:${color}">${row.change > 0 ? "+" : ""}${row.change}</td>
						<td>${dir_icon[row.direction] || ""} ${row.direction}</td>
					</tr>`;
				})
				.join("");

			new frappe.ui.Dialog({
				title: `${__("Revision")}: ${data.current} ${__("vs")} ${data.previous}`,
				size: "extra-large",
				fields: [
					{
						fieldtype: "HTML",
						fieldname: "tbl",
						options: `
							<div style="font-size:12px;margin-bottom:8px;color:var(--text-muted)">
								<b style="color:var(--text-success,#1a6b3c)">&#9650; ${summary.total_changed}</b> ${__("changed")} &nbsp;
								<b style="color:var(--text-accent,#2490ef)">+ ${summary.total_added}</b> ${__("new")} &nbsp;
								<b style="color:var(--text-danger,#a93226)">&mdash; ${summary.total_removed}</b> ${__("removed")}
							</div>
							<div style="max-height:440px;overflow-y:auto">
							<table class="table table-bordered table-condensed" style="font-size:12px">
								<thead style="position:sticky;top:0;background:var(--fg-color,#fff)">
									<tr><th>${__("Item")}</th><th>${__("Date")}</th><th>${__("Type")}</th><th>${__("Old")}</th><th>${__("New")}</th><th>${__("Change")}</th><th>${__("Direction")}</th></tr>
								</thead>
								<tbody>${rows_html}</tbody>
							</table></div>
						`,
					},
				],
			}).show();
		},
	});
}

// ============================================================================
// SHARED HELPERS
// ============================================================================

function _item_table_html(item_details) {
	const rows_html = (item_details || [])
		.map(function (item) {
			return `<tr>
				<td style="width:36px;text-align:center">
					<input type="checkbox" class="sp-item-cb" data-item="${frappe.utils.escape_html(item.item_code)}" checked style="width:15px;height:15px;cursor:pointer">
				</td>
				<td style="font-weight:500;font-size:12px">${frappe.utils.escape_html(item.item_code)}</td>
				<td style="font-size:12px;color:var(--text-muted)">${frappe.utils.escape_html(item.item_name || "")}</td>
				<td style="text-align:center;font-size:12px">
					<span style="background:var(--bg-blue,#eaf4ff);color:var(--text-accent,#2490ef);padding:2px 8px;border-radius:4px;font-weight:500">${item.months} mo</span>
				</td>
				<td style="font-size:12px;color:var(--text-muted)">${item.from_date} &rarr; ${item.to_date}</td>
				<td style="text-align:right;font-size:12px">${Number(item.total_qty || 0).toLocaleString()}</td>
			</tr>`;
		})
		.join("");

	return `
		<div style="margin-bottom:8px;font-size:12px">
			<a href="#" class="sp-select-all">${__("Select all")}</a> &nbsp;|&nbsp;
			<a href="#" class="sp-deselect-all">${__("Deselect all")}</a>
			<span style="color:var(--text-muted)"> &mdash; ${__("uncheck an item to exclude it from auto-forecast (e.g. a closing part)")}</span>
		</div>
		<div style="max-height:260px;overflow-y:auto;border:1px solid var(--border-color,#d1d8dd);border-radius:6px">
			<table class="table table-condensed" style="margin:0;font-size:12px">
				<thead style="position:sticky;top:0;background:var(--fg-color,#fff);z-index:1">
					<tr>
						<th style="width:36px;text-align:center"><input type="checkbox" class="sp-cb-header" checked style="width:15px;height:15px;cursor:pointer"></th>
						<th>${__("Item")}</th><th>${__("Name")}</th><th style="text-align:center">${__("Months")}</th><th>${__("Range")}</th><th style="text-align:right">${__("Total Qty")}</th>
					</tr>
				</thead>
				<tbody>${rows_html}</tbody>
			</table>
		</div>
	`;
}

function _wire_item_table(dialog, on_change) {
	setTimeout(function () {
		const $wrapper = dialog.$wrapper;

		$wrapper.find(".sp-cb-header").on("change", function () {
			$wrapper.find(".sp-item-cb").prop("checked", $(this).prop("checked"));
			on_change();
		});
		$wrapper.on("change", ".sp-item-cb", function () {
			const total = $wrapper.find(".sp-item-cb").length;
			const checked = $wrapper.find(".sp-item-cb:checked").length;
			$wrapper.find(".sp-cb-header").prop({ checked: checked === total, indeterminate: checked > 0 && checked < total });
			on_change();
		});
		$wrapper.find(".sp-select-all").on("click", function (e) {
			e.preventDefault();
			$wrapper.find(".sp-item-cb, .sp-cb-header").prop("checked", true);
			on_change();
		});
		$wrapper.find(".sp-deselect-all").on("click", function (e) {
			e.preventDefault();
			$wrapper.find(".sp-item-cb, .sp-cb-header").prop("checked", false);
			on_change();
		});
	}, 300);
}

function _get_selected_items(dialog) {
	const selected = [];
	dialog.$wrapper.find(".sp-item-cb:checked").each(function () {
		selected.push($(this).data("item"));
	});
	return selected;
}

function _method_help_html(method) {
	if (method === "LAST_3_MONTHS_SHIPPING_AVG") {
		return `
			<div style="padding:12px;background:var(--bg-orange,#fff7e6);border-left:4px solid #f39c12;margin-bottom:12px">
				<b>${__("Average Weekly Shipment - Last 3 Months")}</b><br><br>
				${__("Uses this item's actual Delivery Note shipments over the last 3 months, divided across ~13 weeks. Falls back to the file's own recent figures only if there is no shipment history yet.")}
			</div>
		`;
	}
	return `
		<div style="padding:12px;background:var(--bg-blue,#eaf4ff);border-left:4px solid #2490ef;margin-bottom:12px">
			<b>${__("Average of Last 4 Weeks")}</b><br><br>
			${__("Averages this item's last 4 known weekly quantities and repeats that value for every future week until the target month count is reached.")}
		</div>
	`;
}

function _render_preview(wrapper, message) {
	if (!message || !message.length) {
		wrapper.html(`<div style="padding:15px;color:var(--text-muted)">${__("No preview available.")}</div>`);
		return;
	}

	let html = `
		<div style="margin-top:8px">
			<h5>${__("Predicted Forecast Preview")}</h5>
			<div style="overflow:auto;max-height:260px">
				<table class="table table-bordered table-condensed" style="font-size:12px">
					<thead><tr><th>${__("Item")}</th><th>${__("Method")}</th><th>${__("Predicted Weekly Qty")}</th><th>${__("Sample Basis")}</th></tr></thead>
					<tbody>
	`;
	message.forEach(function (row) {
		const sample = (row.sample_values || []).map((v) => parseFloat(v).toFixed(2)).join(" , ");
		html += `<tr>
			<td><b>${frappe.utils.escape_html(row.item_code)}</b></td>
			<td>${frappe.utils.escape_html(row.description)}</td>
			<td><b>${parseFloat(row.predicted_value || 0).toFixed(2)}</b></td>
			<td>${sample}</td>
		</tr>`;
	});
	html += `</tbody></table></div></div>`;
	wrapper.html(html);
}

function _refresh_preview_from_rows(dialog, rows_json, method, selected_items) {
	const wrapper = dialog.fields_dict.forecast_preview.$wrapper;
	wrapper.html(`<div style="padding:15px;text-align:center;color:var(--text-muted)">${__("Calculating forecast...")}</div>`);
	frappe.call({
		method: "schedule_planner.api.preview_forecast_method",
		args: { rows_json: rows_json, forecast_method: method, selected_items: JSON.stringify(selected_items) },
		callback: function (r) {
			_render_preview(wrapper, r.message);
		},
	});
}

function _refresh_preview_from_schedule(dialog, schedule_name, method, selected_items) {
	const wrapper = dialog.fields_dict.forecast_preview.$wrapper;
	wrapper.html(`<div style="padding:15px;text-align:center;color:var(--text-muted)">${__("Calculating forecast...")}</div>`);
	frappe.call({
		method: "schedule_planner.api.preview_forecast_for_schedule",
		args: { schedule_name: schedule_name, forecast_method: method, selected_items: JSON.stringify(selected_items) },
		callback: function (r) {
			_render_preview(wrapper, r.message);
		},
	});
}
