// Copyright (c) 2026, Indiba Solutions and contributors
// For license information, please see license.txt

// Months-of-cover warning on the Auto Material Request: after this order
// arrives, how many months of demand will the stock cover (counted week by week
// against the demand of each week)? Over the limit
// (Schedule Planning Settings) => red, and Submit needs the approver role plus
// a written reason. utils/safety.py enforces the same rule on the server.

const SP_MR = { limit: 0, can_approve: false, role: "", loaded: false };

// Same week-by-week rule as safety.cover_from_profile on the server: the
// stock (+ this order) is used up against week 1's demand, then week 2's...,
// weeks become months at 7 / 30.4375. The profile is stored on the line
// (Demand by Month); a line made before profiles existed falls back to the
// flat figure.
const SP_DAYS_PER_MONTH = 30.4375;

function sp_mr_profile(row) {
	try {
		const p = row.custom_sp_demand_buckets ? JSON.parse(row.custom_sp_demand_buckets) : null;
		if (p && Array.isArray(p.b)) return { b: p.b.map((x) => flt(x)), t: flt(p.t) };
	} catch (e) {
		/* fall through to the flat figure */
	}
	return null;
}

function sp_mr_round1(x) {
	return Math.floor(x * 10 + 0.5) / 10;
}

function sp_mr_cover(row) {
	const profile = sp_mr_profile(row);
	if (profile) {
		if (!profile.b.some((x) => x > 0) && profile.t <= 0) return null;
		let remaining = Math.max(flt(row.custom_sp_available_qty) + flt(row.qty), 0);
		if (remaining <= 0) return 0;
		let elapsed = 0;
		for (const demand of profile.b) {
			if (demand <= 0) {
				elapsed += 1;
			} else if (remaining >= demand) {
				remaining -= demand;
				elapsed += 1;
			} else {
				return sp_mr_round1(((elapsed + remaining / demand) * 7) / SP_DAYS_PER_MONTH);
			}
		}
		let months = (elapsed * 7) / SP_DAYS_PER_MONTH;
		if (profile.t > 0) months += remaining / profile.t;
		return sp_mr_round1(months);
	}
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
