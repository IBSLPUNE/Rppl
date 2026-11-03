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
	// runs after every (re)draw of the grid
	after_datatable_render: function (datatable) {
		sp_setup_grid(datatable);
	},
	onload: function (report) {
		// double-click a Total Demand cell -> where that demand comes from
		report.page.wrapper.off("dblclick.sp-demand").on("dblclick.sp-demand", ".dt-cell", function () {
			const $cell = $(this).find(".sp-demand-cell");
			if (!$cell.length) return;
			sp_show_demand_breakup($cell.attr("data-item"), report.get_filter_value("planning_months"));
		});
	},
	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		if (!data) return value;

		if (column.fieldname === "status") {
			const colors = { Shortfall: "red", Surplus: "orange", Covered: "green" };
			const color = colors[data.status] || "gray";
			return `<span style="color:var(--${color}-600,${color});font-weight:600">${frappe.utils.escape_html(data.status)}</span>`;
		}
		if (column.fieldname === "total_demand") {
			return `<span class="sp-demand-cell" data-item="${frappe.utils.escape_html(data.item_code)}" style="cursor:pointer;text-decoration:underline dotted" title="${__("Double-click for the demand breakup")}">${value}</span>`;
		}
		if (column.fieldname === "reorder_check") {
			const colors = { "Below Reorder Level": "red", "Above Reorder Level": "green", "Not Set": "gray" };
			const color = colors[data.reorder_check] || "gray";
			return `<span style="color:var(--${color}-600,${color});font-weight:600">${frappe.utils.escape_html(data.reorder_check)}</span>`;
		}
		if (column.fieldname === "mr_decision") {
			const raise = data.mr_decision === "Raise MR";
			const wait = (data.mr_decision || "").indexOf("Wait") === 0;
			const color = raise ? "red" : wait ? "orange" : "gray";
			return `<span style="color:var(--${color}-600,${color});font-weight:${raise || wait ? 600 : 400}">${frappe.utils.escape_html(data.mr_decision)}</span>`;
		}
		if (column.fieldname === "cover_after" && data.cover_after !== null && data.cover_after !== undefined) {
			if (data.cover_flag) {
				return `<span style="color:#fff;background:var(--red-600,#d1372a);padding:1px 6px;border-radius:4px;font-weight:700" title="${__("More months of stock than the limit - approval needed")}">&#9888; ${value}</span>`;
			}
			return value;
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


function sp_show_demand_breakup(item_code, planning_months) {
	frappe.call({
		method: "schedule_planner.api.get_demand_breakup",
		args: { item_code: item_code, planning_months: planning_months || null },
		freeze: true,
		callback: function (r) {
			const d = r.message;
			if (!d) return;
			const esc = frappe.utils.escape_html;
			const fmt = (n) => Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });

			let body = "";
			if (d.source === "Customer Schedule") {
				const by_customer = {};
				d.rows.forEach((x) => (by_customer[x.customer] = (by_customer[x.customer] || 0) + x.quantity));
				const by_type = {};
				d.rows.forEach((x) => {
					const key = x.auto_forecast ? __("Auto forecast") : x.demand_type;
					by_type[key] = (by_type[key] || 0) + x.quantity;
				});
				const chips = (obj) =>
					Object.keys(obj)
						.map((k) => `<span style="display:inline-block;margin:0 8px 4px 0;padding:2px 8px;background:var(--bg-light-gray,#f3f3f3);border-radius:4px;font-size:12px">${esc(k)}: <b>${fmt(obj[k])}</b></span>`)
						.join("");
				const lines = d.rows
					.map((x) => {
						const overdue = x.delivery_date < d.today;
						const kind = x.auto_forecast ? __("Auto forecast") : x.demand_type;
						return `<tr>
							<td>${esc(x.delivery_date)}${overdue ? ` <span style="color:var(--orange-600,#c07a1e)">(${__("overdue")})</span>` : ""}</td>
							<td>${esc(x.customer)}</td>
							<td><a href="/app/customer-schedule/${encodeURIComponent(x.schedule)}" target="_blank">${esc(x.schedule)}</a></td>
							<td>${esc(x.release_number || "")}</td>
							<td>${esc(kind)}</td>
							<td style="text-align:right">${fmt(x.quantity)}</td>
						</tr>`;
					})
					.join("");
				body = `
					<div style="margin-bottom:8px">${__("By customer")}: ${chips(by_customer)}</div>
					<div style="margin-bottom:10px">${__("By type")}: ${chips(by_type)}</div>
					<div style="max-height:420px;overflow:auto;border:1px solid var(--border-color,#d1d8dd);border-radius:6px">
					<table class="table table-condensed" style="margin:0;font-size:12px">
						<thead style="position:sticky;top:0;background:var(--fg-color,#fff)"><tr>
							<th>${__("Delivery Date")}</th><th>${__("Customer")}</th><th>${__("Schedule")}</th>
							<th>${__("Release")}</th><th>${__("Type")}</th><th style="text-align:right">${__("Qty")}</th>
						</tr></thead>
						<tbody>${lines}</tbody>
						<tfoot><tr style="font-weight:600"><td colspan="5">${__("Total demand")}</td><td style="text-align:right">${fmt(d.total)}</td></tr></tfoot>
					</table></div>
					<div style="margin-top:8px;color:var(--text-muted);font-size:12px">${__("Only the latest submitted schedule per customer and delivery date is counted.")}</div>`;
			} else if (d.source === "Shipping History (No Schedule)") {
				const f = d.fallback;
				body = `<div style="padding:12px;background:var(--bg-orange,#fff7e6);border-left:4px solid #f39c12">
					${__("No submitted Customer Schedule covers this item in the window, so demand is projected from shipping history.")}<br><br>
					${__("Shipped in last {0} month(s)", [f.months])}: <b>${fmt(f.shipped_qty)}</b><br>
					${__("Projected for {0} month(s)", [d.planning_months])}: <b>${fmt(f.demand)}</b>
					<span style="color:var(--text-muted)">(${fmt(f.shipped_qty)} &times; ${d.planning_months} / ${f.months})</span>
				</div>`;
			} else {
				body = `<div style="padding:12px;color:var(--text-muted)">${__("No schedule rows or shipping history found for this item in the window.")}</div>`;
			}

			const dialog = new frappe.ui.Dialog({
				title: __("Demand breakup - {0}", [item_code]),
				size: "extra-large",
				fields: [
					{
						fieldtype: "HTML",
						fieldname: "body",
						options: `<div style="margin-bottom:10px;font-size:12px;color:var(--text-muted)">
							${__("Window")}: ${esc(d.start_date)} &rarr; ${esc(d.end_date)} (${d.planning_months}M, ${d.grace_days}d ${__("overdue grace")})
							&nbsp;|&nbsp; ${__("Source")}: <b>${esc(d.source)}</b> &nbsp;|&nbsp; ${__("Total")}: <b>${fmt(d.total)}</b>
						</div>${body}`,
					},
				],
			});
			dialog.show();
		},
	});
}

// ---------------------------------------------------------------------------
// Grid behaviour: Item Code / Item Name stay put while scrolling sideways,
// and a clicked row is highlighted (light yellow row, darker clicked cell).
// ---------------------------------------------------------------------------

const SP_FROZEN_COLUMNS = ["item_code", "item_name"];

function sp_setup_grid(datatable) {
	const $root = $(".report-wrapper");
	if (!$root.length || !datatable) return;

	const columns = datatable.datamanager.columns;
	const frozen = SP_FROZEN_COLUMNS.map((id) => columns.findIndex((c) => c.id === id)).filter((i) => i > 0);
	$("#sp-grid-style, #sp-select-style").remove();
	if (!frozen.length) return;

	// left offset of each frozen column = widths of everything before it
	// (column 0 is the row-number column, already frozen by the grid itself)
	const width = (i) => $root.find(".dt-cell--header-" + i).first().outerWidth() || columns[i].width || 100;
	const last = Math.max(...frozen);
	let left = 0;
	const offsets = {};
	for (let i = 0; i <= last; i++) {
		if (frozen.includes(i)) offsets[i] = left;
		left += width(i);
	}

	const body = frozen
		.map(
			(i) => `.report-wrapper .dt-cell--col-${i}:not(.dt-cell--header){position:sticky;left:${offsets[i]}px;z-index:2;background:var(--fg-color,#fff);${i === last ? "box-shadow:2px 0 3px -2px rgba(0,0,0,.3);" : ""}}`
		)
		.join("");
	const head = frozen
		.map((i) => `.report-wrapper .dt-cell--header-${i}{position:relative;z-index:3;background:var(--control-bg,#f3f3f3);${i === last ? "box-shadow:2px 0 3px -2px rgba(0,0,0,.3);" : ""}}`)
		.join("");
	$("<style id='sp-grid-style'>").text(body + head).appendTo("head");

	// the grid moves its header with a transform; keep our frozen header cells
	// still by moving them back by the same amount (what it does for column 0)
	const $scroll = $root.find(".dt-scrollable");
	const pin_headers = function () {
		const x = $scroll.scrollLeft();
		frozen.forEach((i) => $root.find(".dt-cell--header-" + i).css("transform", `translateX(${x}px)`));
	};
	$scroll.off("scroll.sp-freeze").on("scroll.sp-freeze", pin_headers);
	pin_headers();

	// row selection
	const select_row = function (row_index) {
		$("#sp-select-style").remove();
		if (row_index === null || row_index === undefined) return;
		$("<style id='sp-select-style'>")
			.text(
				`.report-wrapper .dt-scrollable .dt-cell[data-row-index="${row_index}"]{background:#fff6b3 !important}` +
					`.report-wrapper .dt-scrollable .dt-cell.dt-cell--focus[data-row-index="${row_index}"]{background:#ffc94d !important}`
			)
			.appendTo("head");
	};
	$root.off("click.sp-select");
	$root.on("click.sp-select", ".dt-scrollable .dt-cell", function () {
		select_row($(this).attr("data-row-index"));
	});
	// sorting reorders the rows, so a remembered row number would point at the wrong one
	$root.on("click.sp-select", ".dt-cell--header", function () {
		select_row(null);
	});
}
