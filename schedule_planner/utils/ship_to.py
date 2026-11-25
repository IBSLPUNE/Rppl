"""Ship-to plants on customer schedules.

Some customer files (the Marwood / Plex release) name the plant each line is
for ("Plant 1", "Plant 4" ...). The customer's Shipping Addresses are kept per
plant in ERPNext, and Sales Orders are made per plant with that plant's address.
Each schedule carries a small table of its plants and their addresses
(`ship_to_addresses`). It is filled when the schedule is created - from what was
used for this customer's earlier schedules, else from a Shipping Address whose
title / lines name the plant - and anything left blank must be filled in before
Submit. Files that print no plant are not affected at all."""

import re

import frappe


def norm(text):
	return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _customer_addresses(customer):
	names = frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Customer", "link_name": customer, "parenttype": "Address"},
		pluck="parent",
	)
	if not names:
		return []
	return frappe.get_all(
		"Address",
		filters={"name": ["in", names], "disabled": 0},
		fields=["name", "address_title", "address_line1", "address_line2", "city"],
	)


def _plant_pattern(ship_to):
	# "Plant 1" must match "Plant 1", "plant-1", "Plant1" but never "Plant 10"
	words = re.split(r"[\s\-_]+", norm(ship_to))
	return re.compile(r"(?<![a-z0-9])" + r"[\s\-_]*".join(re.escape(w) for w in words if w) + r"(?![a-z0-9])")


def _auto_match(ship_to, addresses):
	pattern = _plant_pattern(ship_to)
	hits = [
		a.name
		for a in addresses
		if pattern.search(norm(" ".join(str(a.get(f) or "") for f in ("name", "address_title", "address_line1", "address_line2", "city"))))
	]
	return hits[0] if len(hits) == 1 else None  # only when exactly one address names the plant


def _remembered(customer):
	"""{norm(ship_to): address} from this customer's earlier submitted schedules, newest first."""
	parents = frappe.get_all(
		"Customer Schedule",
		filters={"customer": customer, "docstatus": 1},
		pluck="name",
		order_by="creation desc",
		limit=20,
	)
	found = {}
	for parent in parents:
		for row in frappe.get_all(
			"Customer Schedule Ship To", filters={"parent": parent, "parenttype": "Customer Schedule"}, fields=["ship_to", "shipping_address"]
		):
			if row.shipping_address:
				found.setdefault(norm(row.ship_to), row.shipping_address)
	return found


def resolve(customer, ship_tos):
	"""[(ship_to, address or None)] for the plants named in a file."""
	remembered = _remembered(customer)
	addresses = _customer_addresses(customer)
	valid = {a.name for a in addresses}
	out = []
	for ship_to in ship_tos:
		address = remembered.get(norm(ship_to))
		if address not in valid:
			address = _auto_match(ship_to, addresses)
		out.append((ship_to, address))
	return out


def address_map(doc):
	"""{norm(ship_to): address} chosen on the schedule."""
	return {norm(r.ship_to): r.shipping_address for r in (doc.get("ship_to_addresses") or []) if r.shipping_address}


def check_before_submit(doc):
	"""Every plant in the file needs a Shipping Address of this customer."""
	plants = {}
	for row in doc.schedule_items:
		if row.get("ship_to") and row.demand_type == "Firm":
			plants.setdefault(norm(row.ship_to), row.ship_to)
	if not plants:
		return
	chosen = address_map(doc)
	missing = sorted(label for key, label in plants.items() if key not in chosen)
	if missing:
		frappe.throw(
			"Choose a Shipping Address for each plant of this schedule (table <b>Ship-To Addresses</b>):<br>"
			+ "<br>".join(missing),
			title="Ship-To Address Missing",
		)
	foreign = []
	for key, address in chosen.items():
		if key in plants and not frappe.db.exists(
			"Dynamic Link",
			{"parenttype": "Address", "parent": address, "link_doctype": "Customer", "link_name": doc.customer},
		):
			foreign.append(f"{plants[key]}: {address}")
	if foreign:
		frappe.throw(
			"These addresses are not linked to customer <b>%s</b>:<br>%s" % (doc.customer, "<br>".join(foreign)),
			title="Ship-To Address",
		)
