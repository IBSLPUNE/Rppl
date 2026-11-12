# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

"""Maps the part numbers printed in a customer's schedule file to the real
ERPNext Item they stand for.

A customer's file never uses our item codes. Most of the time it carries a
part number that we already store on the Item (Item > Supplier Items >
Supplier Part No); some parts have none, and occasionally a number is wrong
or matches more than one item. So every distinct part number in an upload is
resolved in this order, first hit wins:

  1. Saved Mapping   - a Customer Item Mapping record (a user confirmed it
                       once; it is reused for every later upload)
  2. Item Supplier   - Item > Supplier Items > Supplier Part No
  3. Customer Item   - Item > Customer Items > Ref Code (this customer)
  4. Item Code       - the number is simply our own item code
  5. Numbers in the description - many files print the customer's own part
                       number in the description line next to our code. Each
                       such number is tried against 2-4 above, exactly first,
                       then as a whole number *inside* a longer Supplier Part
                       No (e.g. "M8X18.5 RIVET NUT PART#25367965 DRAWING#...").
  6. otherwise       - "Not mapped", with up to 3 suggestions by description.
                       A suggestion is never picked automatically.

Matching ignores case, spaces, dashes, slashes, dots and underscores, so
"PUR05-06-002179" finds "PUR05 06 002179".

Nothing here creates an Item on its own. A user either picks an existing
Item, explicitly ticks "create new item" for a row, or skips the row."""

import difflib
import re

import frappe
from frappe.utils import today

SRC_SAVED = "Saved Mapping"
SRC_SUPPLIER = "Item Supplier"
SRC_CUSTOMER = "Customer Item"
SRC_CODE = "Item Code"
SRC_MANUAL = "Chosen by user"

MATCHED, REVIEW, MISSING = "Matched", "Review", "Not mapped"

# Same characters stripped in Python (norm) and SQL (_SQL_NORM) so both agree.
_STRIP = re.compile(r"[-\s/._]")


def norm(value):
	return _STRIP.sub("", str(value or "").upper())


def _sql_norm(column):
	expr = f"UPPER({column})"
	for ch in ("-", " ", "/", ".", "_"):
		expr = f"REPLACE({expr}, '{ch}', '')"
	return expr


# ---------------------------------------------------------------------------
# Lookups (each returns {normalised part number: [item_code, ...]})
# ---------------------------------------------------------------------------

def _saved_map(customer):
	out = {}
	for r in frappe.get_all(
		"Customer Item Mapping",
		filters={"customer": customer, "enabled": 1},
		fields=["name", "file_part_no", "item_code"],
	):
		out[norm(r.file_part_no)] = r
	return out


def _group(rows, key, value):
	out = {}
	for r in rows:
		out.setdefault(norm(r[key]), [])
		if r[value] not in out[norm(r[key])]:
			out[norm(r[key])].append(r[value])
	return out


def _item_supplier_map(norms):
	if not norms:
		return {}
	rows = frappe.db.sql(
		f"""
		SELECT isup.parent AS item_code, isup.supplier_part_no AS part_no
		FROM `tabItem Supplier` isup
		INNER JOIN `tabItem` i ON i.name = isup.parent
		WHERE isup.parenttype = 'Item'
		  AND IFNULL(i.disabled, 0) = 0
		  AND IFNULL(isup.supplier_part_no, '') != ''
		  AND {_sql_norm('isup.supplier_part_no')} IN %(norms)s
		""",
		{"norms": tuple(norms)},
		as_dict=True,
	)
	return _group(rows, "part_no", "item_code")


def _customer_item_map(customer, norms):
	if not norms:
		return {}
	rows = frappe.db.sql(
		f"""
		SELECT icd.parent AS item_code, icd.ref_code AS part_no
		FROM `tabItem Customer Detail` icd
		INNER JOIN `tabItem` i ON i.name = icd.parent
		WHERE icd.parenttype = 'Item'
		  AND icd.customer_name = %(customer)s
		  AND IFNULL(i.disabled, 0) = 0
		  AND IFNULL(icd.ref_code, '') != ''
		  AND {_sql_norm('icd.ref_code')} IN %(norms)s
		""",
		{"customer": customer, "norms": tuple(norms)},
		as_dict=True,
	)
	return _group(rows, "part_no", "item_code")


def _item_code_map(norms):
	if not norms:
		return {}
	rows = frappe.db.sql(
		f"""
		SELECT i.name AS item_code, i.name AS part_no
		FROM `tabItem` i
		WHERE IFNULL(i.disabled, 0) = 0 AND {_sql_norm('i.name')} IN %(norms)s
		""",
		{"norms": tuple(norms)},
		as_dict=True,
	)
	return _group(rows, "part_no", "item_code")


_TOKEN = re.compile(r"[A-Za-z0-9-]+")


def description_numbers(description, primary=""):
	"""Part-number-looking words in a file description ("25367965 RIVET NUT"
	-> ["25367965"]): at least 6 characters with a run of 5+ digits, so sizes
	like M8x1.25 or M14X2X65 are never mistaken for part numbers."""
	seen, out = {norm(primary)}, []
	for word in _TOKEN.findall(description or ""):
		n = norm(word)
		if len(n) >= 6 and re.search(r"\d{5,}", n) and n not in seen:
			seen.add(n)
			out.append(word)
	return out[:6]


def _contains_lookup(customer, number):
	"""Items whose Supplier Part No (or this customer's Ref Code) contains the
	number as a whole number - not as part of a longer run of digits.
	Returns [(label, item_code)]."""
	n = norm(number)
	if len(n) < 6:
		return []
	boundary = re.compile(r"(?<!\d)" + re.escape(n) + r"(?!\d)")
	found = []
	for label, table, column, extra, params in (
		(SRC_SUPPLIER, "Item Supplier", "supplier_part_no", "", {}),
		(SRC_CUSTOMER, "Item Customer Detail", "ref_code", "AND t.customer_name = %(customer)s", {"customer": customer}),
	):
		rows = frappe.db.sql(
			f"""
			SELECT t.parent AS item_code, t.{column} AS part_no
			FROM `tab{table}` t
			INNER JOIN `tabItem` i ON i.name = t.parent
			WHERE t.parenttype = 'Item' AND IFNULL(i.disabled, 0) = 0
              {extra}
              AND {_sql_norm('t.' + column)} LIKE %(pat)s
			""",
			dict(params, pat=f"%{n}%"),
			as_dict=True,
		)
		for r in rows:
			if boundary.search(norm(r["part_no"])) and (label, r["item_code"]) not in found:
				found.append((label, r["item_code"]))
	return found


def _item_names(item_codes):
	if not item_codes:
		return {}
	return {
		r.name: r.item_name
		for r in frappe.get_all("Item", filters={"name": ["in", list(item_codes)]}, fields=["name", "item_name"])
	}


# ---------------------------------------------------------------------------
# Suggestions (never selected automatically)
# ---------------------------------------------------------------------------

def _suggestions_for(description, catalogue, limit=3, cutoff=0.55):
	text = (description or "").strip().lower()
	if len(text) < 4 or not catalogue:
		return []
	scored = []
	for code, name in catalogue:
		other = (name or "").lower()
		matcher = difflib.SequenceMatcher(None, text, other)
		if matcher.real_quick_ratio() < cutoff or matcher.quick_ratio() < cutoff:
			continue
		ratio = matcher.ratio()
		if ratio >= cutoff:
			scored.append((ratio, code, name))
	scored.sort(reverse=True)
	return [f"{code} ({name})" for _r, code, name in scored[:limit]]


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------

def resolve(customer, parts, use_saved=True):
	"""parts: [{"file_part_no": str, "file_description": str}, ...] (distinct).
	Returns one dict per part, same order:
	{file_part_no, file_description, item_code, item_name, source, status,
	 suggestions, create_item, skip, remember}"""

	alt_by_part = {
		p["file_part_no"]: description_numbers(p.get("file_description"), p["file_part_no"]) for p in parts
	}
	norms = list(
		{norm(p["file_part_no"]) for p in parts if norm(p["file_part_no"])}
		| {norm(w) for words in alt_by_part.values() for w in words}
	)
	saved = _saved_map(customer) if use_saved else {}
	by_supplier = _item_supplier_map(norms)
	by_customer = _customer_item_map(customer, norms)
	by_code = _item_code_map(norms)

	results, catalogue = [], None
	for p in parts:
		part, desc = (p["file_part_no"] or "").strip(), (p.get("file_description") or "").strip()
		key = norm(part)
		item_code, source, status, note = None, None, MISSING, ""

		hit = saved.get(key)
		if hit and frappe.db.exists("Item", hit.item_code):
			item_code, source, status = hit.item_code, SRC_SAVED, MATCHED
		else:
			for label, table in ((SRC_SUPPLIER, by_supplier), (SRC_CUSTOMER, by_customer), (SRC_CODE, by_code)):
				found = table.get(key) or []
				if len(found) == 1:
					item_code, source, status = found[0], label, MATCHED
					break
				if len(found) > 1:
					source, status = f"{label} (matches {len(found)} items)", REVIEW
					note = ", ".join(found[:5])
					break

		# 5. numbers printed in the description (the customer's own part no)
		if not item_code and status == MISSING:
			item_code, source, status, note = _from_description_numbers(
				customer, part, alt_by_part.get(part) or [], (by_supplier, by_customer, by_code)
			)

		suggestions = note
		if not item_code and not suggestions:
			if catalogue is None:
				catalogue = [
					(r.name, r.item_name)
					for r in frappe.get_all(
						"Item", filters={"disabled": 0}, fields=["name", "item_name"], limit_page_length=5000
					)
				]
			suggestions = ", ".join(_suggestions_for(desc, catalogue))
			if suggestions and status == MISSING:
				status = REVIEW
				source = "Suggestions only"

		results.append(
			{
				"file_part_no": part,
				"file_description": desc,
				"item_code": item_code or "",
				"item_name": "",
				"source": source or "-",
				"status": status,
				"suggestions": suggestions,
				"create_item": 0,
				"skip": 0,
				"remember": 1,
			}
		)

	names = _item_names({r["item_code"] for r in results if r["item_code"]})
	for r in results:
		r["item_name"] = names.get(r["item_code"], "")
	return results


def _from_description_numbers(customer, part, words, tables):
	"""Try each number found in the description: exact match first (Supplier
	Part No, Customer Ref Code, Item Code), then as a whole number inside a
	longer Supplier Part No / Ref Code. One Item -> matched; several -> left
	for the user (REVIEW) with the candidates listed; none -> unchanged."""

	by_supplier, by_customer, by_code = tables
	for stage in ("exact", "inside"):
		hits = []  # (label, number, item_code)
		for word in words:
			if stage == "exact":
				for label, table in ((SRC_SUPPLIER, by_supplier), (SRC_CUSTOMER, by_customer), (SRC_CODE, by_code)):
					hits += [(label, word, code) for code in table.get(norm(word)) or []]
			else:
				hits += [(label, word, code) for label, code in _contains_lookup(customer, word)]
		items = []
		for _label, _word, code in hits:
			if code not in items:
				items.append(code)
		if len(items) == 1:
			label, word, code = hits[0]
			how = f"no. {word} in description" if stage == "exact" else f"{word} inside part no, from description"
			return code, f"{label} ({how})", MATCHED, ""
		if len(items) > 1:
			return "", f"Description number matches {len(items)} items", REVIEW, ", ".join(items[:5])
	return "", None, MISSING, ""


def distinct_parts(rows):
	"""First description seen per distinct file part number, in file order."""
	seen, parts = set(), []
	for r in rows:
		part = (r.get("file_part_no") or r.get("item_code") or "").strip()
		if part and part not in seen:
			seen.add(part)
			parts.append({"file_part_no": part, "file_description": r.get("file_description") or r.get("item_name") or ""})
	return parts


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def remap_rows(rows, item_by_part):
	"""Return new row dicts with item_code replaced by the mapped Item.
	item_by_part: {file_part_no: item_code}. Rows whose part is not in the
	dict keep their original code. Two file part numbers that map to the
	same Item on the same date are merged (quantities added)."""

	merged, order = {}, []
	for r in rows:
		part = (r.get("file_part_no") or r.get("item_code") or "").strip()
		row = dict(r)
		row["file_part_no"] = part
		row.setdefault("file_description", r.get("item_name") or part)
		if part in item_by_part:
			row["item_code"] = item_by_part[part]
		key = (row["item_code"], row["delivery_date"], row.get("demand_type"))
		if key in merged:
			merged[key]["quantity"] = float(merged[key]["quantity"]) + float(row["quantity"])
		else:
			merged[key] = row
			order.append(key)

	item_names = _item_names({k[0] for k in order if frappe.db.exists("Item", k[0])})
	out = []
	for key in order:
		row = merged[key]
		if row["item_code"] in item_names:
			row["item_name"] = item_names[row["item_code"]]
		out.append(row)
	return out


def confirm(customer, rows, mapping):
	"""Validate the user's choices, remember the ones worth remembering, and
	return (remapped_rows, final_mapping_list). Raises with a clear list if
	anything is still unresolved."""

	from schedule_planner.utils.items import ensure_item_exists

	parts = [{"file_part_no": m["file_part_no"], "file_description": m.get("file_description")} for m in mapping]
	automatic = {m["file_part_no"]: m for m in resolve(customer, parts, use_saved=False)}

	chosen, skipped, problems, remember = {}, set(), [], []
	for m in mapping:
		part = (m["file_part_no"] or "").strip()
		if int(m.get("skip") or 0):
			skipped.add(part)
			continue

		item_code = (m.get("item_code") or "").strip()
		created = False
		if not item_code and int(m.get("create_item") or 0):
			ensure_item_exists(part, m.get("file_description") or part, allow_create=True)
			item_code, created = part, True

		if not item_code:
			problems.append(f"{part} - no item chosen")
			continue
		if not frappe.db.exists("Item", item_code):
			problems.append(f"{part} - item '{item_code}' does not exist")
			continue
		if frappe.db.get_value("Item", item_code, "disabled"):
			problems.append(f"{part} - item '{item_code}' is disabled")
			continue

		chosen[part] = item_code
		auto_item = (automatic.get(part) or {}).get("item_code")
		if int(m.get("remember", 1) or 0) and (created or item_code != auto_item):
			remember.append((part, m.get("file_description"), item_code))

	if problems:
		frappe.throw(
			"These part numbers still need an item (pick one, tick <b>Create Item</b>, or tick "
			"<b>Skip</b> to leave them out):<br>" + "<br>".join(problems),
			title="Item Mapping Incomplete",
		)

	for part, description, item_code in remember:
		_save_mapping(customer, part, description, item_code)

	remapped = [r for r in remap_rows(rows, chosen) if (r.get("file_part_no") or "") not in skipped]
	if not remapped:
		frappe.throw("Every row was skipped - nothing left to create a schedule from.")

	_touch_used(customer, chosen.keys())

	final = resolve(customer, parts)
	for r in final:
		if r["file_part_no"] in chosen:
			r["item_code"] = chosen[r["file_part_no"]]
			r["status"] = MATCHED
			if r["source"] == "-" or r["source"].startswith("Suggestions"):
				r["source"] = SRC_MANUAL
	return remapped, [r for r in final if r["file_part_no"] not in skipped]


def _save_mapping(customer, part, description, item_code):
	key = norm(part)
	existing = None
	for r in frappe.get_all(
		"Customer Item Mapping", filters={"customer": customer}, fields=["name", "file_part_no"]
	):
		if norm(r.file_part_no) == key:
			existing = r.name
			break

	if existing:
		doc = frappe.get_doc("Customer Item Mapping", existing)
		doc.item_code, doc.enabled, doc.source = item_code, 1, "Confirmed on Upload"
		if description and not doc.file_description:
			doc.file_description = description
		doc.save(ignore_permissions=True)
	else:
		frappe.get_doc(
			{
				"doctype": "Customer Item Mapping",
				"customer": customer,
				"file_part_no": part,
				"file_description": description,
				"item_code": item_code,
				"enabled": 1,
				"source": "Confirmed on Upload",
			}
		).insert(ignore_permissions=True)


def _touch_used(customer, parts):
	keys = {norm(p) for p in parts}
	for r in frappe.get_all(
		"Customer Item Mapping", filters={"customer": customer, "enabled": 1}, fields=["name", "file_part_no"]
	):
		if norm(r.file_part_no) in keys:
			frappe.db.set_value("Customer Item Mapping", r.name, "last_used_on", today(), update_modified=False)


def apply_saved_automatically(customer, rows):
	"""For code paths with no dialog (the fallback re-parse): map using
	whatever resolves unambiguously and refuse to continue if anything
	doesn't, rather than creating guesses."""

	parts = distinct_parts(rows)
	mapping = resolve(customer, parts)
	loose = [m["file_part_no"] for m in mapping if m["status"] != MATCHED]
	if loose:
		frappe.throw(
			"Item mapping is incomplete for: " + ", ".join(loose[:10]) + (" ..." if len(loose) > 10 else "")
			+ ". Upload the file again and complete the Item Mapping step.",
			title="Item Mapping Incomplete",
		)
	return remap_rows(rows, {m["file_part_no"]: m["item_code"] for m in mapping})
