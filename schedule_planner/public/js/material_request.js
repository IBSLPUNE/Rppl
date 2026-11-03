// Copyright (c) 2026, Indiba Solutions and contributors
// For license information, please see license.txt

// Months-of-cover warning on the Auto Material Request: after this order
// arrives, how many months of demand will the stock cover? Over the limit
// (Schedule Planning Settings) => red, and Submit needs the approver role plus
// a written reason. utils/safety.py enforces the same rule on the server.

const SP_MR = { limit: 0, can_approve: false, role: "", loaded: false };

function sp_mr_cover(row) {
	const monthly = flt(row.custom_sp_monthly_demand);
	if (monthly <= 0) return null;
	return Math.round(((flt(row.custom_sp_available_qty) + flt(row.qty)) / monthly) * 10) / 10;
}

function sp_mr_apply(frm) {
	if (!frm.doc.custom_sp_auto_generated) return;
	const limit = SP_MR.limit;
	const bad = [];
	(frm.doc.items || []).forEach(function (row) {
		const cover = sp_mr_cover(row);
		const over = limit > 0 && cover !== null && cover > limit + 0.05;
		if (over) bad.push({ item: row.item_code, qty: row.qty, cover: cover, monthly: row.custom_sp_monthly_demand });
		const grid_row = frm.fields_dict.items.grid.grid_rows_by_docname[row.name];
		if (grid_row) {
			grid_row.wrapper.find('[data-fieldname="custom_sp_cover_months"]').css({
				color: over ? "#fff" : "",
				background: over ? "var(--red-600,#d1372a)" : "",
				"font-weight": over ? 700 : "",
			});
		}
	});

	frm.toggle_display("custom_sp_cover_approval_note", bad.length > 0);
	if (bad.length && frm.doc.docstatus === 0) {
		const lines = bad
			.map(
				(b) =>
					`${frappe.utils.escape_html(b.item)}: ${__("order")} ${format_number(b.qty, null, 0)} &rarr; <b>${b.cover.toFixed(1)} ${__("months of cover")}</b> (${__("demand")} ${format_number(b.monthly, null, 0)}/${__("month")})`
			)
			.join("<br>");
		frm.dashboard.set_headline(
			`<span style="color:var(--red-600,#d1372a)"><b>&#9888; ${__("More than {0} months of stock after this order:", [limit])}</b><br>${lines}<br>` +
				(SP_MR.can_approve
					? __("Reduce the quantity, or write the reason in Cover Approval Note to submit.")
					: __("Only a user with the role {0} can submit this. Reduce the quantity or ask them.", [SP_MR.role])) +
				"</span>"
		);
	} else {
		frm.dashboard.clear_headline();
	}
}

frappe.ui.form.on("Material Request", {
	refresh: function (frm) {
		if (!frm.doc.custom_sp_auto_generated || frm.is_new()) return;
		frappe.call({
			method: "schedule_planner.utils.safety.get_mr_cover_check",
			args: { mr_name: frm.doc.name },
			callback: function (r) {
				if (!r.message) return;
				SP_MR.limit = flt(r.message.limit);
				SP_MR.can_approve = !!r.message.can_approve;
				SP_MR.role = r.message.approver_role;
				SP_MR.loaded = true;
				sp_mr_apply(frm);
			},
		});
	},
});

frappe.ui.form.on("Material Request Item", {
	qty: function (frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		const cover = sp_mr_cover(row);
		if (cover !== null) frappe.model.set_value(cdt, cdn, "custom_sp_cover_months", cover);
		if (SP_MR.loaded) sp_mr_apply(frm);
	},
});
