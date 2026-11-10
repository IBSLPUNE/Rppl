// Copyright (c) 2026, Indiba Solutions and contributors
// For license information, please see license.txt

frappe.ui.form.on("Customer Schedule", {
	before_submit: function (frm) {
		// Every submit goes through the Pre-submit Check first. The dialog's
		// Submit button sets the flag and calls frm.save("Submit") again.
		if (frm._sp_precheck_ok) {
			frm._sp_precheck_ok = false;
			return;
		}
		frappe.validated = false;
		_presubmit(frm);
	},

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
			frm.add_custom_button(__("Item Mapping"), function () {
				_show_draft_mapping(frm);
			});
			frm.add_custom_button(__("Pre-submit Check"), function () {
				_presubmit(frm);
			}).addClass("btn-warning");
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

// Dates in our dialogs are always shown as 02-Nov-2026 so day and month can never be confused.
function sp_cs_date(x) {
	return x ? moment(x).format("DD-MMM-YYYY") : "";
}

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
					_after_analyse(frm, file_doc.file_url, r.message);
				},
			});
		},
	});
}

function _show_create_dialog(frm, file_url, analysis) {
	const available_months = parseInt(analysis.available_months || 0);
	const default_method = analysis.default_forecast_method || "LAST_4_WEEKS_AVG";
	const default_target = available_months + 3;
	const rows_json = JSON.stringify(analysis.parsed_rows || []);
	const header_json = JSON.stringify(analysis.header || {});
	const map_list = analysis.item_mapping || [];
	const mapping_banner = map_list.length
		? `<br>${__("Item Mapping")}: <b>${map_list.length}</b> ${__("part number(s) mapped")}
			&nbsp;<a href="#" class="sp-review-mapping">${__("Review / change mapping")}</a>`
		: "";

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
						&nbsp;|&nbsp; ${sp_cs_date(analysis.from_date)} &rarr; ${sp_cs_date(analysis.to_date)}
						${mapping_banner}
					</div>
				`,
			},
			{
				fieldtype: "HTML",
				fieldname: "item_table",
				options: _item_table_html(analysis.item_details || [], default_method),
			},
			{ fieldtype: "Section Break" },
			{
				label: __("How many total months do you want (file + auto-forecast)?"),
				fieldname: "target_months",
				fieldtype: "Int",
				default: default_target,
				reqd: 1,
				description: __(
					"Counted from each item's first date in the sheet (zero or non-zero qty): sheet starts 04-Jan, 6 months = up to 04-Jul. Auto-forecast starts the week after the item's last non-zero quantity. File contains {0} month(s).",
					[available_months]
				),
			},
			{ fieldtype: "Column Break" },
			{
				label: __("Auto Forecast Method"),
				fieldname: "forecast_method",
				fieldtype: "Select",
				options: [
					{ label: "Average of Last 4 Weeks", value: "LAST_4_WEEKS_AVG" },
					{ label: "Average Weekly Shipment - Last 3 Months", value: "LAST_3_MONTHS_SHIPPING_AVG" },
					{ label: "Average Weekly Shipment - Last 6 Months", value: "LAST_6_MONTHS_SHIPPING_AVG" },
					{ label: "Average Weekly Shipment - Last 9 Months", value: "LAST_9_MONTHS_SHIPPING_AVG" },
					{ label: "Average of Customer Document", value: "DOCUMENT_AVG" },
				],
				default: default_method,
				reqd: 1,
			},
			{ fieldtype: "HTML", fieldname: "method_help", options: _method_help_html(default_method) },
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
					item_methods: JSON.stringify(_get_item_methods(dialog)),
					header_json: header_json,
					rows_json: rows_json,
					schedule_format: analysis.schedule_format,
					file_check_json: JSON.stringify(analysis.file_check || {}),
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
		_apply_default_method(dialog, method);
		_refresh_preview_from_rows(dialog, rows_json, method, _get_selected_items(dialog));
	};

	dialog.fields_dict.target_months.df.onchange = frappe.utils.debounce(function () {
		_refresh_preview_from_rows(dialog, rows_json, dialog.get_value("forecast_method"), _get_selected_items(dialog));
	}, 500);

	dialog.show();
	dialog.$wrapper.on("click", ".sp-review-mapping", function (e) {
		e.preventDefault();
		_show_mapping_dialog({
			customer: frm.doc.customer,
			mapping: map_list,
			title: __("Item Mapping - review"),
			primary_label: __("Apply Changes"),
			on_confirm: function (mapping, map_dialog) {
				frappe.call({
					method: "schedule_planner.api.confirm_item_mapping",
					args: {
						customer: frm.doc.customer,
						rows_json: JSON.stringify(analysis.parsed_rows || []),
						mapping_json: JSON.stringify(mapping),
						header_json: header_json,
						schedule_format: analysis.schedule_format,
					},
					freeze: true,
					freeze_message: __("Applying Item Mapping..."),
					callback: function (r) {
						if (!r.message) return;
						map_dialog.hide();
						dialog.hide();
						_show_create_dialog(frm, file_url, r.message);
					},
				});
			},
		});
	});
	_wire_item_table(dialog, function () {
		_refresh_preview_from_rows(dialog, rows_json, dialog.get_value("forecast_method"), _get_selected_items(dialog));
	});
	_refresh_preview_from_rows(dialog, rows_json, default_method, _get_selected_items(dialog));
}

// ============================================================================
// ITEM MAPPING (customer part number -> our Item)
// ============================================================================

function _after_analyse(frm, file_url, analysis) {
	if (analysis.mapping_complete) {
		_show_create_dialog(frm, file_url, analysis);
		return;
	}
	_show_mapping_dialog({
		customer: frm.doc.customer,
		mapping: analysis.item_mapping || [],
		title: __("Item Mapping - match customer part numbers to your Items"),
		primary_label: __("Confirm Mapping & Continue"),
		on_confirm: function (mapping, dialog) {
			frappe.call({
				method: "schedule_planner.api.confirm_item_mapping",
				args: {
					customer: frm.doc.customer,
					rows_json: JSON.stringify(analysis.parsed_rows || []),
					mapping_json: JSON.stringify(mapping),
					header_json: JSON.stringify(analysis.header || {}),
					schedule_format: analysis.schedule_format,
				},
				freeze: true,
				freeze_message: __("Applying Item Mapping..."),
				callback: function (r) {
					if (!r.message) return;
					dialog.hide();
					_show_create_dialog(frm, file_url, r.message);
				},
			});
		},
	});
}

function _mapping_badge(status) {
	const colour = { Matched: "green", Review: "orange", "Not mapped": "red" }[status] || "gray";
	return `<span class="indicator-pill ${colour}"><span>${__(status)}</span></span>`;
}

function _suggestion_chips(text) {
	if (!text) return "";
	const parts = text.indexOf("(") > -1 ? text.split(/\),\s*/).map(function (s, i, a) {
		return i < a.length - 1 && s.slice(-1) !== ")" ? s + ")" : s;
	}) : text.split(/,\s*/);
	return parts
		.map(function (p) {
			const code = p.split(" (")[0].trim();
			return `<a href="#" class="sp-chip" data-code="${frappe.utils.escape_html(code)}" style="display:inline-block;margin:2px 4px 0 0;padding:1px 8px;border:1px solid var(--border-color,#d1d8dd);border-radius:10px;font-size:11px">${frappe.utils.escape_html(p)}</a>`;
		})
		.join("");
}

// opts: {customer, mapping[], title, intro, primary_label, on_confirm(mapping, dialog)}
function _show_mapping_dialog(opts) {
	const mapping = (opts.mapping || []).slice().sort(function (a, b) {
		return (a.status === "Matched") - (b.status === "Matched");
	});
	const controls = [];
	const pending = mapping.filter(function (m) {
		return m.status !== "Matched";
	}).length;

	const body = mapping
		.map(function (m, i) {
			return `<tr data-idx="${i}" class="${m.status === "Matched" ? "sp-ok" : "sp-todo"}">
				<td class="sp-st">${_mapping_badge(m.status)}</td>
				<td style="font-weight:500">${frappe.utils.escape_html(m.file_part_no)}</td>
				<td style="color:var(--text-muted);max-width:200px">${frappe.utils.escape_html(m.file_description || "")}</td>
				<td style="min-width:260px"><div class="sp-ctl"></div>
					<div class="sp-iname" style="font-size:11px;color:var(--text-muted)">${frappe.utils.escape_html(m.item_name || "")}</div>
					<div class="sp-sugg">${m.item_code ? "" : _suggestion_chips(m.suggestions)}</div></td>
				<td style="font-size:11px;color:var(--text-muted)">${frappe.utils.escape_html(m.source || "")}</td>
				<td style="text-align:center"><input type="checkbox" class="sp-create" title="${__("Create a new Item with this part number")}"></td>
				<td style="text-align:center"><input type="checkbox" class="sp-skip" title="${__("Leave these rows out")}"></td>
			</tr>`;
		})
		.join("");

	const dialog = new frappe.ui.Dialog({
		title: opts.title || __("Item Mapping"),
		size: "extra-large",
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "map_html",
				options: `
					<div class="sp-map-intro" style="padding:10px 12px;background:var(--bg-light-gray,#f8f9fa);border-radius:6px;margin-bottom:10px;font-size:12px">
						${opts.intro || __("The file uses the customer's own part numbers. Check which of your Items each one maps to. Anything you change is remembered for this customer next time.")}
						<br><b class="sp-count"></b>
						&nbsp;|&nbsp; <a href="#" class="sp-only-todo">${__("Show only unresolved")}</a>
						&nbsp;|&nbsp; <a href="#" class="sp-show-all">${__("Show all")}</a>
					</div>
					<div style="max-height:55vh;overflow-y:auto;border:1px solid var(--border-color,#d1d8dd);border-radius:6px">
						<table class="table table-condensed" style="margin:0;font-size:12px">
							<thead style="position:sticky;top:0;background:var(--fg-color,#fff);z-index:1"><tr>
								<th>${__("Status")}</th><th>${__("Customer Part No")}</th><th>${__("Description in file")}</th>
								<th>${__("Our Item")}</th><th>${__("Matched by")}</th>
								<th style="text-align:center">${__("Create Item")}</th><th style="text-align:center">${__("Skip")}</th>
							</tr></thead>
							<tbody>${body}</tbody>
						</table>
					</div>`,
			},
		],
		primary_action_label: opts.primary_label || __("Confirm Mapping"),
		primary_action: function () {
			const out = [],
				problems = [];
			mapping.forEach(function (m, i) {
				const $tr = dialog.$wrapper.find(`tr[data-idx="${i}"]`);
				const skip = $tr.find(".sp-skip").is(":checked") ? 1 : 0;
				const create = $tr.find(".sp-create").is(":checked") ? 1 : 0;
				const item = skip || create ? "" : (controls[i].get_value() || "").trim();
				if (!skip && !create && !item) problems.push(m.file_part_no);
				out.push({
					file_part_no: m.file_part_no,
					file_description: m.file_description,
					item_code: item,
					create_item: create,
					skip: skip,
					remember: 1,
				});
			});
			if (problems.length) {
				frappe.msgprint({
					title: __("Item Mapping Incomplete"),
					indicator: "orange",
					message:
						__("Pick an Item, tick Create Item, or tick Skip for:") +
						"<br>" +
						problems.map(frappe.utils.escape_html).join("<br>"),
				});
				return;
			}
			opts.on_confirm(out, dialog);
		},
	});

	dialog.show();

	function refresh_count() {
		let todo = 0;
		mapping.forEach(function (m, i) {
			const $tr = dialog.$wrapper.find(`tr[data-idx="${i}"]`);
			const done = $tr.find(".sp-skip").is(":checked") || $tr.find(".sp-create").is(":checked") || controls[i].get_value();
			if (!done) todo++;
		});
		dialog.$wrapper.find(".sp-count").text(
			__("{0} part number(s), {1} still need an Item", [mapping.length, todo])
		);
	}

	setTimeout(function () {
		mapping.forEach(function (m, i) {
			const $tr = dialog.$wrapper.find(`tr[data-idx="${i}"]`);
			const ctl = frappe.ui.form.make_control({
				parent: $tr.find(".sp-ctl"),
				df: {
					fieldtype: "Link",
					options: "Item",
					fieldname: "sp_item_" + i,
					placeholder: __("Select Item"),
					get_query: function () {
						return { filters: { disabled: 0 } };
					},
					change: function () {
						const code = ctl.get_value();
						const $name = $tr.find(".sp-iname");
						if (!code) {
							$name.text("");
							$tr.find(".sp-st").html(_mapping_badge("Not mapped"));
						} else {
							frappe.db.get_value("Item", code, "item_name").then(function (r) {
								$name.text((r.message && r.message.item_name) || "");
							});
							$tr.find(".sp-st").html(_mapping_badge("Matched"));
							$tr.find(".sp-create, .sp-skip").prop("checked", false);
						}
						refresh_count();
					},
				},
				render_input: true,
				only_input: true,
			});
			ctl.set_value(m.item_code || "");
			controls[i] = ctl;
		});

		dialog.$wrapper.on("click", ".sp-chip", function (e) {
			e.preventDefault();
			const idx = $(this).closest("tr").data("idx");
			controls[idx].set_value($(this).data("code"));
		});
		dialog.$wrapper.on("change", ".sp-create, .sp-skip", function () {
			const $tr = $(this).closest("tr");
			const idx = $tr.data("idx");
			if ($(this).is(":checked")) {
				$tr.find(this.className.indexOf("sp-create") > -1 ? ".sp-skip" : ".sp-create").prop("checked", false);
				controls[idx].set_value("");
				$tr.find(".sp-st").html(_mapping_badge(this.className.indexOf("sp-skip") > -1 ? "Not mapped" : "Matched"));
			}
			refresh_count();
		});
		dialog.$wrapper.find(".sp-only-todo").on("click", function (e) {
			e.preventDefault();
			dialog.$wrapper.find("tr.sp-ok").hide();
		});
		dialog.$wrapper.find(".sp-show-all").on("click", function (e) {
			e.preventDefault();
			dialog.$wrapper.find("tr.sp-ok").show();
		});
		refresh_count();
		if (pending && mapping.length > 15) dialog.$wrapper.find("tr.sp-ok").hide();
	}, 300);
}

// Item Mapping on a saved Draft: review/change which Item each customer part
// number points to, without re-uploading the file.
function _show_draft_mapping(frm) {
	frappe.call({
		method: "schedule_planner.api.get_schedule_item_mapping",
		args: { schedule_name: frm.doc.name },
		freeze: true,
		freeze_message: __("Loading Item Mapping..."),
		callback: function (r) {
			if (!r.message) return;
			_show_mapping_dialog({
				customer: r.message.customer,
				mapping: r.message.mapping,
				title: __("Item Mapping - {0}", [frm.doc.name]),
				intro: __("These are the Items currently on this Draft. Change one to re-point every row for that part number. Tick Skip to remove those rows. Changes are remembered for this customer."),
				primary_label: __("Update Item Mapping"),
				on_confirm: function (mapping, dialog) {
					frappe.call({
						method: "schedule_planner.api.update_schedule_item_mapping",
						args: { schedule_name: frm.doc.name, mapping_json: JSON.stringify(mapping) },
						freeze: true,
						freeze_message: __("Updating schedule..."),
						callback: function (res) {
							if (!res.message) return;
							dialog.hide();
							frappe.show_alert({ message: res.message.message, indicator: "green" });
							frm.reload_doc();
						},
					});
				},
			});
		},
	});
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
			const default_method = info.default_forecast_method || "LAST_4_WEEKS_AVG";
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
								&nbsp;|&nbsp; ${sp_cs_date(info.from_date)} &rarr; ${sp_cs_date(info.to_date)}
							</div>
						`,
					},
					{ fieldtype: "HTML", fieldname: "item_table", options: _item_table_html(info.item_details || [], default_method) },
					{ fieldtype: "Section Break" },
					{
						fieldtype: "Int",
						fieldname: "target_months",
						label: __("Total months required (file + auto)"),
						default: info.available_months + 3,
						reqd: 1,
						description: __("Counted from each item's first date in the sheet (zero or non-zero qty); auto-forecast starts the week after the item's last non-zero quantity."),
					},
					{ fieldtype: "Column Break" },
					{
						fieldtype: "Select",
						fieldname: "forecast_method",
						label: __("Auto Forecast Method"),
						options: [
							{ label: "Average of Last 4 Weeks", value: "LAST_4_WEEKS_AVG" },
							{ label: "Average Weekly Shipment - Last 3 Months", value: "LAST_3_MONTHS_SHIPPING_AVG" },
							{ label: "Average Weekly Shipment - Last 6 Months", value: "LAST_6_MONTHS_SHIPPING_AVG" },
							{ label: "Average Weekly Shipment - Last 9 Months", value: "LAST_9_MONTHS_SHIPPING_AVG" },
							{ label: "Average of Customer Document", value: "DOCUMENT_AVG" },
						],
						default: default_method,
						reqd: 1,
					},
					{ fieldtype: "HTML", fieldname: "method_help", options: _method_help_html(default_method) },
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
							item_methods: JSON.stringify(_get_item_methods(dialog)),
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
				_apply_default_method(dialog, method);
				_refresh_preview_from_schedule(dialog, frm.doc.name, method, _get_selected_items(dialog));
			};

			dialog.fields_dict.target_months.df.onchange = frappe.utils.debounce(function () {
				_refresh_preview_from_schedule(
					dialog,
					frm.doc.name,
					dialog.get_value("forecast_method"),
					_get_selected_items(dialog)
				);
			}, 500);

			dialog.show();
			_wire_item_table(dialog, function () {
				_refresh_preview_from_schedule(
					dialog,
					frm.doc.name,
					dialog.get_value("forecast_method"),
					_get_selected_items(dialog)
				);
			});
			_refresh_preview_from_schedule(dialog, frm.doc.name, default_method, _get_selected_items(dialog));
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
						<td>${sp_cs_date(row.delivery_date)}</td>
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

const SP_METHOD_CHOICES = [
	{ value: "LAST_4_WEEKS_AVG", label: "Last 4 Weeks" },
	{ value: "LAST_3_MONTHS_SHIPPING_AVG", label: "Shipping 3 Mo" },
	{ value: "LAST_6_MONTHS_SHIPPING_AVG", label: "Shipping 6 Mo" },
	{ value: "LAST_9_MONTHS_SHIPPING_AVG", label: "Shipping 9 Mo" },
	{ value: "DOCUMENT_AVG", label: "Document Avg" },
];

function _method_select_html(item_code, selected, explicit) {
	const options = SP_METHOD_CHOICES.map(
		(m) => `<option value="${m.value}"${m.value === selected ? " selected" : ""}>${__(m.label)}</option>`
	).join("");
	return `<select class="sp-item-method form-control input-xs" data-item="${frappe.utils.escape_html(item_code)}"${explicit ? ' data-user="1"' : ""} style="height:24px;padding:0 4px;font-size:12px;min-width:110px">${options}</select>`;
}

function _item_table_html(item_details, default_method) {
	const rows_html = (item_details || [])
		.map(function (item) {
			return `<tr>
				<td style="width:36px;text-align:center">
					<input type="checkbox" class="sp-item-cb" data-item="${frappe.utils.escape_html(item.item_code)}" checked style="width:15px;height:15px;cursor:pointer">
				</td>
				<td style="font-weight:500;font-size:12px"><div style="max-width:78px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${frappe.utils.escape_html(item.item_code)}">${frappe.utils.escape_html(item.item_code)}</div></td>
				<td style="font-size:12px;color:var(--text-muted)"><div style="max-width:100px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${frappe.utils.escape_html(item.item_name || "")}">${frappe.utils.escape_html(item.item_name || "")}</div></td>
				<td style="text-align:center;font-size:12px">
					<span style="background:var(--bg-blue,#eaf4ff);color:var(--text-accent,#2490ef);padding:2px 8px;border-radius:4px;font-weight:500">${item.months} mo</span>
				</td>
				<td style="font-size:12px;color:var(--text-muted)">${sp_cs_date(item.from_date)} &rarr; ${sp_cs_date(item.to_date)}</td>
				<td style="text-align:right;font-size:12px">${Number(item.total_qty || 0).toLocaleString()}</td>
				<td style="font-size:12px">${_method_select_html(
					item.item_code,
					item.forecast_method || default_method,
					// a method the customer already had picked for this item is kept; the
					// default and a document-average switch are worked out afresh
					!!item.forecast_method && item.forecast_method !== default_method && item.forecast_method !== "DOCUMENT_AVG"
				)}</td>
				<td class="sp-auto-qty" data-item="${frappe.utils.escape_html(item.item_code)}" style="text-align:right;font-size:12px;font-weight:500;color:var(--text-muted)">&hellip;</td>
				<td class="sp-auto-doc" data-item="${frappe.utils.escape_html(item.item_code)}" style="text-align:right;font-size:12px;font-weight:500;color:var(--text-muted)">&hellip;</td>
			</tr>`;
		})
		.join("");

	return `
		<div style="margin-bottom:8px;font-size:12px">
			<a href="#" class="sp-select-all">${__("Select all")}</a> &nbsp;|&nbsp;
			<a href="#" class="sp-deselect-all">${__("Deselect all")}</a>
			<span style="color:var(--text-muted)"> &mdash; ${__("uncheck an item to exclude it from auto-forecast (e.g. a closing part); pick a different Forecast Method for any item")}</span>
		</div>
		<div class="sp-limit-banner" style="display:none"></div>
		<div style="max-height:260px;overflow-y:auto;border:1px solid var(--border-color,#d1d8dd);border-radius:6px">
			<table class="table table-condensed" style="margin:0;font-size:12px">
				<thead style="position:sticky;top:0;background:var(--fg-color,#fff);z-index:1">
					<tr>
						<th style="width:36px;text-align:center"><input type="checkbox" class="sp-cb-header" checked style="width:15px;height:15px;cursor:pointer"></th>
						<th>${__("Item")}</th><th>${__("Name")}</th><th style="text-align:center">${__("Months")}</th><th>${__("Range")}</th><th style="text-align:right">${__("Total Qty")}</th><th>${__("Forecast Method")}</th><th style="text-align:right">${__("Auto Forecast Qty")}<div style="font-weight:400;font-size:10.5px;color:var(--text-muted)">${__("by method")}</div></th><th style="text-align:right">${__("Auto Forecast by Document")}<div style="font-weight:400;font-size:10.5px;color:var(--text-muted)">${__("customer's own average")}</div></th>
					</tr>
				</thead>
				<tbody>${rows_html}</tbody>
				<tfoot style="position:sticky;bottom:0;background:var(--fg-color,#fff)">
					<tr style="font-weight:600">
						<td></td><td colspan="4">${__("Total")}</td>
						<td style="text-align:right" class="sp-total-qty">${Number((item_details || []).reduce((a, i) => a + Number(i.total_qty || 0), 0)).toLocaleString()}</td>
						<td></td>
						<td style="text-align:right" class="sp-total-auto-method">&hellip;</td>
						<td style="text-align:right" class="sp-total-auto-doc">&hellip;</td>
					</tr>
					<tr style="font-weight:600">
						<td></td><td colspan="6" style="text-align:right;color:var(--text-muted);font-weight:400">${__("Auto forecast that will be created (the higher of the two for each item)")}</td>
						<td colspan="2" style="text-align:right" class="sp-total-auto-qty">&hellip;</td>
					</tr>
				</tfoot>
			</table>
		</div>
	`;
}

// Fill the "Auto Forecast Qty" column from a preview result. Items that are
// unticked (or get no forecast) show a dash.
function _fill_auto_qty(dialog, preview) {
	_sync_item_methods(dialog, preview);
	const by_item = {};
	const flagged = [];
	let total = 0;
	let total_method = 0;
	let total_doc = 0;
	let multiplier = 2;
	const fmt = (n) => Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
	(preview || []).forEach(function (p) {
		by_item[p.item_code] = p;
		total += Number(p.auto_qty || 0);
		total_method += Number(p.auto_qty_method || 0);
		total_doc += Number(p.auto_qty_document || 0);
		if (p.limit_multiplier) multiplier = p.limit_multiplier;
		if (p.forecast_flag) flagged.push(p);
	});

	// the figure that will really be created is shown bold (green tick); the other one is muted
	const paint = function ($cell, qty, is_used, p) {
		let html = qty ? fmt(qty) : "-";
		let color = "var(--text-muted)";
		let weight = 400;
		if (is_used && qty) {
			color = "var(--text-color)";
			weight = 600;
			html = "&#10003; " + html;
			if (p && p.forecast_flag) {
				const ratio = p.forecast_ratio === null || p.forecast_ratio === undefined ? __("no history") : Number(p.forecast_ratio).toFixed(1) + "x";
				html = "&#9888; " + html + ` <span style="font-weight:400">(${ratio})</span>`;
				color = "var(--red-600,#c53030)";
			} else {
				color = "var(--green-600,#2f855a)";
			}
		}
		$cell.css({ color: color, "font-weight": weight }).html(html);
	};

	dialog.$wrapper.find(".sp-auto-qty").each(function () {
		const p = by_item[String($(this).attr("data-item"))];
		const used_method = !!p && Number(p.auto_qty || 0) > 0 && p.used !== "Document Average";
		paint($(this), p ? Number(p.auto_qty_method || 0) : 0, used_method, p);
		if (p) $(this).attr("title", __("Forecast method: {0} per week", [fmt(p.method_value)]));
	});
	dialog.$wrapper.find(".sp-auto-doc").each(function () {
		const p = by_item[String($(this).attr("data-item"))];
		const used_doc = !!p && Number(p.auto_qty || 0) > 0 && p.used === "Document Average";
		paint($(this), p ? Number(p.auto_qty_document || 0) : 0, used_doc, p);
		if (p && p.document_enabled === false) {
			$(this).attr("title", __("Document comparison is switched off in Schedule Planning Settings"));
		} else if (p && p.document_from) {
			$(this).attr(
				"title",
				__("Customer document: {0} per week = {1} qty over {2} weeks ({3} to {4})", [
					fmt(p.document_value), fmt(p.document_total), fmt(p.document_weeks), sp_cs_date(p.document_from), sp_cs_date(p.document_to),
				])
			);
		}
	});
	dialog.$wrapper.find(".sp-total-auto-method").text(fmt(total_method));
	dialog.$wrapper.find(".sp-total-auto-doc").text(fmt(total_doc));
	dialog.$wrapper.find(".sp-total-auto-qty").text(fmt(total));

	const $banner = dialog.$wrapper.find(".sp-limit-banner");
	if (flagged.length) {
		$banner
			.html(
				`<div class="alert alert-danger" style="font-size:12px;padding:8px 12px;margin-bottom:8px">
					<b>&#9888; ${__("{0} item(s) have a forecast above {1}x their history:", [flagged.length, multiplier])}</b>
					${flagged.map((p) => frappe.utils.escape_html(p.item_code)).join(", ")}.<br>
					${__("You can still create the Draft, but Submit will need approval from a user with the approver role (Schedule Planning Settings). Consider a lower forecast method for these items.")}
				</div>`
			)
			.show();
	} else {
		$banner.hide().empty();
	}
}

function _mark_auto_qty_loading(dialog) {
	dialog.$wrapper.find(".sp-auto-qty, .sp-auto-doc, .sp-total-auto-qty, .sp-total-auto-method, .sp-total-auto-doc").html("&hellip;");
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
		$wrapper.on("change", ".sp-item-method", function () {
			// the user chose this item's method - the system will not switch it
			$(this).attr("data-user", "1");
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

// {item_code: method} for the items whose method the USER picked. Items left
// alone follow the dialog's default - or the document average where that is
// higher (the server works that out, see _sync_item_methods).
function _get_item_methods(dialog) {
	const methods = {};
	dialog.$wrapper.find(".sp-item-method[data-user='1']").each(function () {
		methods[String($(this).attr("data-item"))] = $(this).val();
	});
	return methods;
}

// the dialog-level "Auto Forecast Method" sets every item's dropdown (and
// hands every item back to the system until the user picks one again)
function _apply_default_method(dialog, method) {
	dialog.$wrapper.find(".sp-item-method").val(method).removeAttr("data-user");
}

// show, in each untouched item's dropdown, the method it will really use:
// "Document Avg" where the customer document's average is higher
function _sync_item_methods(dialog, preview) {
	(preview || []).forEach(function (p) {
		const $sel = dialog.$wrapper.find(".sp-item-method").filter(function () {
			return String($(this).attr("data-item")) === String(p.item_code);
		});
		if ($sel.length && !$sel.attr("data-user") && p.method) {
			$sel.val(p.method);
			$sel.attr("title", p.auto_switched ? __("Switched to the customer document's average because it is higher than {0}. Change it if you want another method.", [fmt_weekly(p.method_value)]) : "");
		}
	});
}

function fmt_weekly(n) {
	return Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function _get_selected_items(dialog) {
	const selected = [];
	dialog.$wrapper.find(".sp-item-cb:checked").each(function () {
		// attr(), not data(): jQuery turns "902900022" into a number
		selected.push(String($(this).attr("data-item")));
	});
	return selected;
}

function _method_help_html(method) {
	if (method === "DOCUMENT_AVG") {
		return `
			<div style="padding:12px;background:var(--bg-green,#e9f7ef);border-left:4px solid #2f855a;margin-bottom:12px">
				<b>${__("Average of Customer Document")}</b> <span style="color:var(--text-muted)">(${__("this choice applies to every item; change it per item in the table")})</span><br><br>
				${__("Uses the customer's own weekly average for the item: all quantity in the uploaded file for the item divided by the weeks of its period (the period is set in Schedule Planning Settings).")}
			</div>
		`;
	}
	const months = { LAST_3_MONTHS_SHIPPING_AVG: 3, LAST_6_MONTHS_SHIPPING_AVG: 6, LAST_9_MONTHS_SHIPPING_AVG: 9 }[method];
	if (months) {
		const weeks = Math.round((months * 13) / 3);
		return `
			<div style="padding:12px;background:var(--bg-orange,#fff7e6);border-left:4px solid #f39c12;margin-bottom:12px">
				<b>${__("Average Weekly Shipment - Last {0} Months", [months])}</b> <span style="color:var(--text-muted)">(${__("applies to every item; an item whose customer-document average is higher switches to Document Avg by itself - change any item in the table")})</span><br><br>
				${__("Uses this item's actual Delivery Note shipments over the last {0} months, divided by {1} weeks (weeks with no shipment count as zero). Falls back to the Last 4 Weeks average only if there is no shipment history yet.", [months, weeks])}
			</div>
		`;
	}
	return `
		<div style="padding:12px;background:var(--bg-blue,#eaf4ff);border-left:4px solid #2490ef;margin-bottom:12px">
			<b>${__("Average of Last 4 Weeks")}</b> <span style="color:var(--text-muted)">(${__("applies to every item; an item whose customer-document average is higher switches to Document Avg by itself - change any item in the table")})</span><br><br>
			${__("Takes the last 4 weeks of the file and divides the total by 4. Weeks with no quantity count as zero (e.g. 100, 0, 400, 0 gives 500 / 4 = 125). That value is repeated for every future week until the target month count is reached.")}
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
					<thead><tr><th>${__("Item")}</th><th>${__("Method")}</th><th>${__("Method Weekly Qty")}</th><th>${__("Document Weekly Avg")}</th><th>${__("Weekly Qty Used")}</th><th>${__("Sample Basis")}</th></tr></thead>
					<tbody>
	`;
	message.forEach(function (row) {
		const sample = (row.sample_values || []).map((v) => parseFloat(v).toFixed(2)).join(" , ");
		html += `<tr>
			<td><b>${frappe.utils.escape_html(row.item_code)}</b></td>
			<td>${frappe.utils.escape_html(row.description)}</td>
			<td>${parseFloat(row.method_value !== undefined ? row.method_value : row.predicted_value || 0).toFixed(2)}</td>
			<td title="${row.document_from ? sp_cs_date(row.document_from) + " - " + sp_cs_date(row.document_to) + ", " + parseFloat(row.document_weeks || 0).toFixed(1) + " " + __("weeks") : ""}">${row.document_enabled === false ? "-" : parseFloat(row.document_value || 0).toFixed(2)}</td>
			<td><b>${Number(row.predicted_value || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}</b> <span style="color:var(--text-muted)">(${row.used === "Document Average" ? __("document") : __("method")})</span></td>
			<td>${sample}</td>
		</tr>`;
	});
	html += `</tbody></table></div></div>`;
	wrapper.html(html);
}

function _refresh_preview_from_rows(dialog, rows_json, method, selected_items) {
	const wrapper = dialog.fields_dict.forecast_preview.$wrapper;
	wrapper.html(`<div style="padding:15px;text-align:center;color:var(--text-muted)">${__("Calculating forecast...")}</div>`);
	_mark_auto_qty_loading(dialog);
	frappe.call({
		method: "schedule_planner.api.preview_forecast_method",
		args: {
			rows_json: rows_json,
			forecast_method: method,
			selected_items: JSON.stringify(selected_items),
			target_months: parseInt(dialog.get_value("target_months")) || null,
			item_methods: JSON.stringify(_get_item_methods(dialog)),
			customer: (window.cur_frm && cur_frm.doc && cur_frm.doc.customer) || null,
		},
		callback: function (r) {
			_render_preview(wrapper, r.message);
			_fill_auto_qty(dialog, r.message);
		},
	});
}

function _refresh_preview_from_schedule(dialog, schedule_name, method, selected_items) {
	const wrapper = dialog.fields_dict.forecast_preview.$wrapper;
	wrapper.html(`<div style="padding:15px;text-align:center;color:var(--text-muted)">${__("Calculating forecast...")}</div>`);
	_mark_auto_qty_loading(dialog);
	frappe.call({
		method: "schedule_planner.api.preview_forecast_for_schedule",
		args: {
			schedule_name: schedule_name,
			forecast_method: method,
			selected_items: JSON.stringify(selected_items),
			target_months: parseInt(dialog.get_value("target_months")) || null,
			item_methods: JSON.stringify(_get_item_methods(dialog)),
		},
		callback: function (r) {
			_render_preview(wrapper, r.message);
			_fill_auto_qty(dialog, r.message);
		},
	});
}

// ============================================================================
// PRE-SUBMIT CHECK - shown instead of a blind Submit. The server enforces the
// same rules (utils/safety.py), this dialog is how the user clears them.
// ============================================================================

const SP_NUM = (v, d) => Number(v || 0).toLocaleString(undefined, { maximumFractionDigits: d === undefined ? 2 : d });
const SP_ESC = (v) => frappe.utils.escape_html(v === null || v === undefined ? "" : String(v));

function _presubmit(frm) {
	frappe.call({
		method: "schedule_planner.api.get_presubmit_report",
		args: { schedule_name: frm.doc.name },
		freeze: true,
		freeze_message: __("Running pre-submit check..."),
		callback: function (r) {
			if (!r.message) return;
			_show_presubmit_dialog(frm, r.message);
		},
	});
}

function _show_presubmit_dialog(frm, report) {
	const state = { report: report, reconcile_ok: false };

	const dialog = new frappe.ui.Dialog({
		title: __("Pre-submit Check - {0}", [frm.doc.name]),
		size: "extra-large",
		fields: [{ fieldtype: "HTML", fieldname: "body" }],
		primary_action_label: __("Submit"),
		primary_action: function () {
			dialog.hide();
			frm._sp_precheck_ok = true;
			frm.save("Submit");
		},
	});

	function can_submit() {
		const rep = state.report;
		if (rep.ack.required && !rep.ack.done) return false;
		if (rep.forecast.needs_approval && !rep.forecast.approved) return false;
		if (rep.file.reconciles === false && !state.reconcile_ok) return false;
		return true;
	}

	function badge(ok, text_ok, text_bad) {
		return ok
			? `<span class="indicator-pill green">${text_ok}</span>`
			: `<span class="indicator-pill red">${text_bad}</span>`;
	}

	function render() {
		const rep = state.report;
		const f = rep.file;
		const diff = f.total_qty - (f.system_qty + f.skipped_qty);
		let html = "";

		// 1. File vs system
		html += `<h6 style="margin-top:0">${__("1. File total vs schedule lines")}</h6>
		<table class="table table-bordered table-condensed" style="font-size:12px;margin-bottom:6px">
			<tr><td>${__("Total qty printed in the customer's file")}</td><td style="text-align:right"><b>${SP_NUM(f.total_qty)}</b></td></tr>
			<tr><td>${__("Qty in this schedule (customer lines only, forecast excluded)")}</td><td style="text-align:right"><b>${SP_NUM(f.system_qty)}</b></td></tr>
			<tr><td>${__("Qty of skipped part numbers")}</td><td style="text-align:right"><b>${SP_NUM(f.skipped_qty)}</b></td></tr>
			<tr><td>${__("Difference (file - schedule - skipped)")}</td><td style="text-align:right"><b style="color:${Math.abs(diff) > 0.5 ? "var(--red-500,#e24c4c)" : "var(--green-600,#38a169)"}">${SP_NUM(diff)}</b></td></tr>
		</table>`;
		if (f.reconciles === false) {
			html += `<div class="alert alert-danger" style="font-size:12px;padding:8px 12px">
				${__("The schedule does not add up to the file.")}
				${f.edited_since_upload ? __("Customer lines were edited after upload (loaded {0}, now {1}).", [SP_NUM(f.included_qty), SP_NUM(f.system_qty)]) : ""}
				<br><label style="margin:6px 0 0"><input type="checkbox" class="sp-reconcile-cb" ${state.reconcile_ok ? "checked" : ""}> ${__("I know why the totals differ and it is correct")}</label></div>`;
		} else if (f.reconciles === true) {
			html += `<div style="margin-bottom:8px">${badge(true, __("Schedule matches the file"), "")}</div>`;
		}

		// 2. Parsed
		html += `<h6>${__("2. What was read from the file")}</h6>
		<div style="font-size:12px;margin-bottom:8px">
			${__("Months in file")}: <b>${f.months_parsed || f.months_in_lines}</b>
			${f.from_date ? ` (${SP_ESC(sp_cs_date(f.from_date))} &rarr; ${SP_ESC(sp_cs_date(f.to_date))})` : ""}
			&nbsp;|&nbsp; ${__("Lines loaded")}: <b>${f.lines_in_schedule}</b>${f.lines_parsed ? ` ${__("of")} ${f.lines_parsed}` : ""}
			&nbsp;|&nbsp; ${__("Items")}: <b>${f.items}</b>
			&nbsp;|&nbsp; ${__("Skipped part numbers")}: <b>${rep.skipped.length}</b>
		</div>`;

		// 3. Skipped
		html += `<h6>${__("3. Skipped / unmapped part numbers")}</h6>`;
		if (!rep.skipped.length) {
			html += `<div style="margin-bottom:8px">${badge(true, __("Nothing was skipped"), "")}</div>`;
		} else {
			html += `<table class="table table-bordered table-condensed" style="font-size:12px;margin-bottom:6px">
				<thead><tr><th>${__("Customer Part No")}</th><th>${__("Description")}</th><th style="text-align:right">${__("Lines")}</th><th style="text-align:right">${__("Qty not planned")}</th></tr></thead><tbody>
				${rep.skipped.map((s) => `<tr><td>${SP_ESC(s.file_part_no)}</td><td>${SP_ESC(s.description)}</td><td style="text-align:right">${SP_NUM(s.lines, 0)}</td><td style="text-align:right">${SP_NUM(s.qty)}</td></tr>`).join("")}
				</tbody></table>`;
			if (rep.ack.done) {
				html += `<div style="margin-bottom:8px">${badge(true, __("Acknowledged by {0}", [SP_ESC(rep.ack.by)]), "")}</div>`;
			} else {
				html += `<div class="alert alert-warning" style="font-size:12px;padding:8px 12px"><label style="margin:0"><input type="checkbox" class="sp-ack-cb"> <b>${__("I have checked these skipped parts. They will NOT be planned or ordered.")}</b></label></div>`;
			}
		}

		// 4. Forecast
		const fc = rep.forecast;
		html += `<h6>${__("4. Auto forecast vs history (limit {0}x)", [fc.multiplier])}</h6>
		<div style="font-size:12px;margin-bottom:6px">${__("Forecast lines")}: <b>${fc.lines}</b> &nbsp;|&nbsp; ${__("Forecast qty")}: <b>${SP_NUM(fc.qty)}</b> &nbsp;|&nbsp; ${__("Months")}: <b>${fc.months}</b></div>`;
		if (!fc.needs_approval) {
			html += `<div style="margin-bottom:8px">${badge(true, __("Forecast is within the limit"), "")}</div>`;
		} else {
			html += `<table class="table table-bordered table-condensed" style="font-size:12px;margin-bottom:6px">
				<thead><tr><th>${__("Item")}</th><th style="text-align:right">${__("Forecast / week")}</th><th style="text-align:right">${__("History / week")}</th><th style="text-align:right">${__("x History")}</th><th style="text-align:right">${__("Forecast total")}</th></tr></thead><tbody>
				${fc.flags.map((x) => `<tr style="color:var(--red-600,#c53030)"><td>${SP_ESC(x.item_code)}</td><td style="text-align:right">${SP_NUM(x.forecast_weekly)}</td><td style="text-align:right">${SP_NUM(x.history_weekly)}</td><td style="text-align:right"><b>${x.ratio === null ? __("no history") : SP_NUM(x.ratio, 1) + "x"}</b></td><td style="text-align:right">${SP_NUM(x.forecast_total)}</td></tr>`).join("")}
				</tbody></table>`;
			if (fc.approved) {
				html += `<div style="margin-bottom:8px">${badge(true, __("Approved by {0}: {1}", [SP_ESC(fc.approved_by), SP_ESC(fc.approved_note)]), "")}</div>`;
			} else if (fc.can_approve) {
				html += `<div class="alert alert-danger" style="font-size:12px;padding:8px 12px">
					<b>${__("This forecast is above {0}x history and needs your approval.", [fc.multiplier])}</b>
					<textarea class="form-control sp-approve-note" rows="2" placeholder="${__("Reason (required) - e.g. customer confirmed new programme")}" style="margin:6px 0"></textarea>
					<button class="btn btn-xs btn-danger sp-approve-btn">${__("Approve Forecast Above Limit")}</button></div>`;
			} else {
				html += `<div class="alert alert-danger" style="font-size:12px;padding:8px 12px">${__("This forecast is above {0}x history. A user with the role {1} must open this schedule and approve it before it can be submitted.", [fc.multiplier, "<b>" + SP_ESC(fc.approver_role) + "</b>"])}</div>`;
			}
		}

		dialog.fields_dict.body.$wrapper.html(html);
		dialog.get_primary_btn().prop("disabled", !can_submit());
	}

	function reload() {
		frappe.call({
			method: "schedule_planner.api.get_presubmit_report",
			args: { schedule_name: frm.doc.name },
			callback: function (r) {
				if (r.message) {
					state.report = r.message;
					render();
				}
			},
		});
	}

	const $w = dialog.fields_dict.body.$wrapper;
	$w.on("change", ".sp-reconcile-cb", function () {
		state.reconcile_ok = $(this).prop("checked");
		render();
	});
	$w.on("change", ".sp-ack-cb", function () {
		if (!$(this).prop("checked")) return;
		frappe.call({
			method: "schedule_planner.api.acknowledge_presubmit",
			args: { schedule_name: frm.doc.name },
			callback: reload,
		});
	});
	$w.on("click", ".sp-approve-btn", function () {
		const note = ($w.find(".sp-approve-note").val() || "").trim();
		if (!note) {
			frappe.msgprint(__("Write a short reason for approving this forecast."));
			return;
		}
		frappe.call({
			method: "schedule_planner.api.approve_forecast_limit",
			args: { schedule_name: frm.doc.name, note: note },
			callback: reload,
		});
	});

	render();
	dialog.show();
}
