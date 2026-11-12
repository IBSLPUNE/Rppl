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


// dates are always shown as 02-Nov-2026 (never 11/02 or 02/11) so nobody has to guess day vs month
function sp_rep_date(x) {
	return x ? moment(x).format("DD-MMM-YYYY") : "";
}
function sp_rep_month(m) {
	return moment(m + "-01").format("MMM-YYYY");
}

// "How was the auto forecast worked out?" - method, period, base total and the
// weekly average, step by step, for the client to follow.
function sp_basis_html(d) {
	const esc = frappe.utils.escape_html;
	const fmt = (n) => Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
	const dt = sp_rep_date;
	const bases = d.forecast_basis || [];
	if (!bases.length) return "";

	const cell = "padding:3px 10px;border-bottom:1px solid var(--border-color,#e2e6e9)";
	const blocks = bases.map(function (b) {
		let steps = "";
		if (b.kind === "shipping") {
			steps = `
				<div>${__("Period")}: <b>${__("last {0} months", [b.months])}</b> (${dt(b.from)} &rarr; ${dt(b.to)}, ${__("up to the day the schedule was created")})</div>
				<div>${__("Total shipped in this period (Delivery Notes of this customer, Nos)")}: <b>${fmt(b.total)}</b></div>
				<div>${__("Weeks in the period")}: ${b.months} &times; 13 &divide; 3 = <b>${fmt(b.weeks)}</b></div>
				<div>${__("Weekly average")}: ${fmt(b.total)} &divide; ${fmt(b.weeks)} = <b>${fmt(b.weekly_avg)}</b></div>`;
			if (b.by_month && b.by_month.length) {
				steps += `<div style="margin-top:6px;color:var(--text-muted)">${__("Shipped month by month")}: ` +
					b.by_month.map((m) => `${esc(sp_rep_month(m.month))}: <b>${fmt(m.qty)}</b>`).join(" &nbsp;|&nbsp; ") + `</div>`;
			}
			if (b.total <= 0) {
				steps += `<div style="margin-top:6px;color:var(--orange-600,#c07a1e)">${__("No shipments in this period, so the shipment-based forecast is 0 (the customer document average is used when it is higher).")}</div>`;
			}
		} else if (b.kind === "last4" && b.last4) {
			const l = b.last4;
			steps = `
				<div>${__("Period")}: <b>${__("last 4 weeks of the customer's file")}</b> (${dt(l.from)} &rarr; ${dt(l.to)})</div>`;
			if (l.weeks && l.weeks.length) {
				steps += `<table style="margin:4px 0;font-size:12px"><tr>${l.weeks
					.map((w) => `<td style="${cell}">${dt(w.from)} &rarr; ${dt(w.to)}</td>`).join("")}<td style="${cell};font-weight:600">${__("Total")}</td></tr>
					<tr>${l.weeks.map((w) => `<td style="${cell};text-align:right">${fmt(w.qty)}</td>`).join("")}<td style="${cell};text-align:right;font-weight:600">${fmt(l.total)}</td></tr></table>
					<div style="color:var(--text-muted)">${__("A week with no quantity counts as 0.")}</div>`;
			}
			steps += `<div>${__("Weekly average")}: ${fmt(l.total)} &divide; 4 = <b>${fmt(l.weekly_avg)}</b></div>`;
		} else if (b.kind === "document") {
			steps = `<div>${__("This item's weekly qty is the customer document's own average (worked out below).")}</div>`;
		} else {
			steps = `<div style="color:var(--text-muted)">${__("The method details were not recorded on this schedule.")}</div>`;
		}

		let compare = "";
		if (b.document && b.document_weekly !== undefined && b.document_weekly !== null) {
			const doc = b.document;
			const used_doc = b.source === "Document Average";
			const base = b.base_method_label || b.method_label;
			let verdict;
			if (b.auto_switched) {
				verdict = __("The document average ({0}) was higher than {1} ({2}), so this item was switched to the method <b>Average of Customer Document</b>.", [fmt(b.document_weekly), esc(base), fmt(b.method_weekly)]);
			} else if (used_doc) {
				verdict = __("Method <b>Average of Customer Document</b> was chosen for this item, so the document average is used.");
			} else {
				verdict = __("The method chosen for this item ({0}) is used; the document average ({1}) was not higher than it, or the method was picked by the user.", [fmt(b.method_weekly), fmt(b.document_weekly)]);
			}
			compare = `<div style="margin-top:6px;padding:6px 8px;background:var(--fg-color,#fff);border:1px solid var(--border-color,#e2e6e9);border-radius:4px">
				<div style="font-weight:600">${__("Customer document's own average")}</div>
				<div>${__("Period")}: ${dt(doc.from)} &rarr; ${dt(doc.to)} (<b>${fmt(doc.weeks)}</b> ${__("weeks")}) &nbsp;|&nbsp; ${__("Total quantity in the document for this item")}: <b>${fmt(doc.total)}</b></div>
				<div>${__("Document weekly average")}: ${fmt(doc.total)} &divide; ${fmt(doc.weeks)} = <b>${fmt(b.document_weekly)}</b></div>
				<div style="margin-top:4px">${verdict}</div>
			</div>`;
		} else if (b.source === "Document Average") {
			compare = `<div style="margin-top:6px"><b>${__("The customer document's own average was used for this item.")}</b></div>`;
		}

		const computed = b.source === "Document Average" ? null : b.kind === "shipping" ? b.weekly_avg : b.last4 ? b.last4.weekly_avg : null;
		const moved = computed !== null && computed !== undefined && b.total !== 0 && Math.abs(computed - b.weekly_used) > Math.max(1, b.weekly_used * 0.01);
		const note = moved
			? `<div style="margin-top:6px;color:var(--orange-600,#c07a1e)">${__("Shipment records have changed since this schedule was created, so today's figure ({0}) differs slightly from the weekly qty that was used.", [fmt(computed)])}</div>`
			: "";

		const auto_months = {};
		(d.rows || []).forEach((x) => {
			if (x.auto_forecast && x.schedule === b.schedule) {
				const k = x.delivery_date.slice(0, 7);
				auto_months[k] = (auto_months[k] || 0) + x.quantity;
			}
		});
		const auto_line = Object.keys(auto_months).length
			? `<div style="margin-top:4px;color:var(--text-muted)">${__("Auto forecast month by month")}: ` +
				Object.keys(auto_months).sort().map((k) => `${esc(sp_rep_month(k))}: <b>${fmt(auto_months[k])}</b>`).join(" &nbsp;|&nbsp; ") + `</div>`
			: "";

		return `<div style="padding:10px 12px;margin-bottom:8px;background:var(--bg-light-gray,#f7f7f7);border-radius:6px;font-size:12.5px;line-height:1.6">
			<div>${__("Forecast method")}: <b>${esc(b.method_label)}</b> &nbsp;|&nbsp; ${__("Customer")}: <b>${esc(b.customer || "")}</b> &nbsp;|&nbsp; ${__("Schedule")}: <a href="/app/customer-schedule/${encodeURIComponent(b.schedule)}" target="_blank">${esc(b.schedule)}</a></div>
			${steps}${note}${compare}
			<div style="margin-top:6px;padding-top:6px;border-top:1px dashed var(--border-color,#d1d8dd)">
				${__("Weekly forecast qty used")}: <b>${fmt(b.weekly_used)}</b> &times; ${b.auto_weeks} ${__("weeks")}
				(${dt(b.auto_from)} &rarr; ${dt(b.auto_to)}) = <b>${fmt(b.auto_total)}</b> ${__("auto forecast in this window")}
				${auto_line}
			</div>
		</div>`;
	});
	return `<div style="margin:0 0 10px"><div style="font-weight:600;margin-bottom:6px">${__("How the auto forecast was worked out")}</div>${blocks.join("")}</div>`;
}

// Monthly demand = the sum of each customer's own monthly rate
function sp_rates_html(d) {
	const rates = d.customer_rates || [];
	if (!rates.length) return "";
	const esc = frappe.utils.escape_html;
	const fmt = (n) => Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
	const cell = "padding:3px 10px;border-bottom:1px solid var(--border-color,#e2e6e9)";
	const body = rates
		.map(
			(c) => `<tr><td style="${cell}">${esc(c.customer || "")}</td>
				<td style="${cell}">${sp_rep_date(c.from)} &rarr; ${sp_rep_date(c.to)}</td>
				<td style="${cell};text-align:right">${fmt(c.total)}</td>
				<td style="${cell};text-align:right">${fmt(c.months)}</td>
				<td style="${cell};text-align:right;font-weight:600">${fmt(c.rate)}</td></tr>`
		)
		.join("");
	return `<div style="margin:0 0 10px;font-size:12.5px">
		<div style="font-weight:600;margin-bottom:6px">${__("Monthly demand = sum of each customer's own monthly rate")}</div>
		<table class="table table-condensed" style="font-size:12px;margin-bottom:4px">
			<thead><tr><th>${__("Customer")}</th><th>${__("Demand from - to")}</th><th style="text-align:right">${__("Quantity")}</th><th style="text-align:right">${__("Months")}</th><th style="text-align:right">${__("Per month")}</th></tr></thead>
			<tbody>${body}</tbody>
			<tfoot><tr style="font-weight:600"><td colspan="4">${__("Monthly demand (total of all customers)")}</td><td style="text-align:right">${fmt(d.monthly_demand)}</td></tr></tfoot>
		</table>
		<div style="color:var(--text-muted)">${__("Months of cover is worked out week by week: the available stock is used up against each week's demand, so a customer whose schedule is shorter stops counting once it ends.")}</div>
	</div>`;
}

// Demand month by month: customer Firm / Forecast and the auto forecast, side by side
function sp_monthly_html(d) {
	const fmt = (n) => (Number(n || 0) ? Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 }) : "-");
	const months = d.monthly || [];
	if (!months.length) return "";
	const tot = { firm: 0, forecast: 0, auto: 0, total: 0 };
	const rows = months
		.map(function (m) {
			["firm", "forecast", "auto", "total"].forEach((k) => (tot[k] += m[k]));
			return `<tr><td>${frappe.utils.escape_html(sp_rep_month(m.month))}</td>
				<td style="text-align:right">${fmt(m.firm)}</td><td style="text-align:right">${fmt(m.forecast)}</td>
				<td style="text-align:right">${fmt(m.auto)}</td><td style="text-align:right;font-weight:600">${fmt(m.total)}</td></tr>`;
		})
		.join("");
	return `<div style="margin:0 0 10px"><div style="font-weight:600;margin-bottom:6px">${__("Demand month by month")}</div>
		<div style="max-height:240px;overflow:auto;border:1px solid var(--border-color,#d1d8dd);border-radius:6px">
		<table class="table table-condensed" style="margin:0;font-size:12px">
			<thead style="position:sticky;top:0;background:var(--fg-color,#fff)"><tr><th>${__("Month")}</th>
				<th style="text-align:right">${__("Customer Firm")}</th><th style="text-align:right">${__("Customer Forecast")}</th>
				<th style="text-align:right">${__("Auto Forecast")}</th><th style="text-align:right">${__("Total")}</th></tr></thead>
			<tbody>${rows}</tbody>
			<tfoot><tr style="font-weight:600"><td>${__("Total")}</td><td style="text-align:right">${fmt(tot.firm)}</td>
				<td style="text-align:right">${fmt(tot.forecast)}</td><td style="text-align:right">${fmt(tot.auto)}</td>
				<td style="text-align:right">${fmt(tot.total)}</td></tr></tfoot>
		</table></div></div>`;
}

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
							<td>${esc(sp_rep_date(x.delivery_date))}${overdue ? ` <span style="color:var(--orange-600,#c07a1e)">(${__("overdue")})</span>` : ""}</td>
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
					${sp_basis_html(d)}
					${sp_rates_html(d)}
					${sp_monthly_html(d)}
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
							${__("Window")}: ${esc(sp_rep_date(d.start_date))} &rarr; ${esc(sp_rep_date(d.end_date))} (${d.planning_months}M, ${d.grace_days}d ${__("overdue grace")})
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
