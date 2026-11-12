# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

"""Built-in, deterministic readers for the customer layouts we already know.

A known layout is recognised from the text itself (a fingerprint such as
"SUPPLIER RELEASE PLANNING SCHEDULE"), so nothing has to be configured per
customer and no AI call is made. Each reader returns (header, rows) or None:

* None means "this is not my layout, or I could not account for every line of
  it" - the caller then falls back to AI extraction. A reader never guesses:
  after parsing it re-counts the data lines in the text and refuses to return
  rows if the two counts differ.
* rows are positive-quantity (item_code, item_name, delivery_date, quantity,
  demand_type) dicts, same shape as every other parser in file_parsers.py.

Demand types: Firm stays Firm. Fabrication, Raw Material and Raw
authorisations are treated as Forecast (client decision). Mytox prints no
label at all, so its daily quantities are Firm and weekly/monthly are Forecast.

Known layouts: martinrea_cms (Martinrea Canada and Riverside), techform_portal,
techform_acs (Techform PO064R spool print), marwood_plex, mytox_mrr. Excel/CSV layouts are auto-detected in file_parsers.py.
"""

import re
from datetime import date

_DATE = r"\d{1,2}/\d{1,2}/\d{2,4}"


def _d(text):
	month, day, year = text.split("/")
	year = int(year)
	if year < 100:
		year += 2000
	return date(year, int(month), int(day))


def _num(text):
	text = text.replace(",", "").strip()
	negative = text.endswith("-")
	value = float(text.rstrip("-"))
	return -value if negative else value


def _demand(label):
	return "Firm" if (label or "").strip().lower() == "firm" else "Forecast"


def _row(code, name, when, qty, label):
	return {
		"item_code": code,
		"item_name": name or code,
		"delivery_date": str(when),
		"quantity": qty,
		"demand_type": _demand(label),
	}


def _seen(starts, code, when):
	"""Remember the earliest date printed for a part, zero or not."""
	if code and (code not in starts or when < starts[code]):
		starts[code] = when
	if code and (("end", code) not in starts or when > starts[("end", code)]):
		starts[("end", code)] = when  # the part's last printed date


def _stamp(rows, starts):
	"""Put each part's first printed date (zero or non-zero quantity) on its rows
	as item_start - the forecast months are counted from it."""
	for r in rows:
		when = starts.get(r["item_code"])
		if when:
			r["item_start"] = str(when)
		end = starts.get(("end", r["item_code"]))
		if end:
			r["item_end"] = str(end)
	return rows


def _merge(rows):
	"""Same item + date + type twice (e.g. one part under two POs): add up."""
	merged, order = {}, []
	for r in rows:
		key = (r["item_code"], r["delivery_date"], r["demand_type"])
		if key in merged:
			merged[key]["quantity"] += r["quantity"]
		else:
			merged[key] = dict(r)
			order.append(key)
	return [merged[k] for k in order]


# ---------------------------------------------------------------------------
# Martinrea (CMS "SUPPLIER RELEASE PLANNING SCHEDULE") - Canada and Riverside
# ---------------------------------------------------------------------------

_MARTINREA_ROW = re.compile(
	rf"^\s*({_DATE})\s+(Day|Week|Month)\s+(.+?)\s+([\d,]*\.\d+)\s+([\d,]*\.\d+)\s+([\d,]*\.\d+)\s+([\d,]*\.\d+-?)\s*$"
)
_MARTINREA_LOOSE = re.compile(rf"^\s*{_DATE}\s+(Day|Week|Month)\s")


def detect_martinrea_cms(text):
	return "SUPPLIER RELEASE PLANNING SCHEDULE" in text and "Blanket P/O #" in text


def parse_martinrea_cms(text):
	# Martinrea prints each part under ITS OWN latest release, so one file can
	# carry many releases (a Feb-2024 print held 24 of them, back to 2021). The
	# schedule is identified by the newest release in the file, never the
	# first page's - otherwise the revision history would be dated 2021.
	heads = list(re.finditer(rf"Release#:\s*(\S+)\s+Date:\s*({_DATE})", text))
	if not heads:
		return None
	head = max(heads, key=lambda m: _d(m.group(2)))
	vendor = re.search(r"Vendor:\s*(\S+)", text)

	rows, current, name, loose = [], None, {}, 0
	describing = False
	horizon = None
	starts = {}
	for line in text.splitlines():
		part = re.match(r"\s*Part #:\s*(\S+)", line)
		if part:
			current = part.group(1)
			describing = current not in name
			if describing:
				name[current] = []
			continue
		if describing:
			if "Requirements" in line:
				describing = False
			else:
				# the description sits in the left column; the right column
				# carries Standard Package / Last Receipt / Cum YTD labels
				left = re.split(r"Standard Package|Last Receipt|Cum YTD|Vendor Part|Model Year", line)[0].strip()
				if left:
					name[current].append(left)
		if _MARTINREA_LOOSE.match(line):
			loose += 1
		m = _MARTINREA_ROW.match(line)
		if m and current:
			qty = _num(m.group(4))
			when = _d(m.group(1))
			horizon = when if horizon is None or when > horizon else horizon
			_seen(starts, current, when)
			if qty > 0:
				rows.append(_row(current, name.get(current), when, qty, m.group(3)))

	if not loose or loose != _count_martinrea(text):
		return None
	# e.g. "25367965 RIVET NUT" (the customer's own number is often printed
	# here next to our code; Item Mapping reads it from this text)
	text_by_part = {k: re.sub(r"\s+", " ", " ".join(v)).strip(" ,") for k, v in name.items()}
	for r in rows:
		r["item_name"] = text_by_part.get(r["item_code"]) or r["item_code"]
	_stamp(rows, starts)

	header = {
		"release_number": head.group(1),
		"supplier_vendor": vendor.group(1) if vendor else "",
		"schedule_date": str(_d(head.group(2))),
		"horizon_end": str(horizon) if horizon else None,
	}
	return header, rows


def _count_martinrea(text):
	"""Independent re-count: every dated line must have parsed as a full row."""
	return sum(1 for line in text.splitlines() if _MARTINREA_ROW.match(line))


# ---------------------------------------------------------------------------
# Techform / Magna portal print ("Planning Schedules")
# ---------------------------------------------------------------------------

_TECHFORM_ROW = re.compile(rf"^\s*({_DATE})\s+([\d,]*\.\d+)\s+([\d,]*\.\d+)\s+(Week|Day|Month)\s+(\w+)")
_TECHFORM_LOOSE = re.compile(rf"^\s*{_DATE}\s+[\d,]*\.\d+\s")


def detect_techform_portal(text):
	return "Planning Schedules" in text and "Part Number:" in text and "Net Required" in text


def parse_techform_portal(text):
	head = re.search(r"Release Number:\s*(\S+)", text)
	issued = re.search(rf"Issue Date:\s*({_DATE})", text)
	if not head or not issued:
		return None
	vendor = re.search(r"Supplier:\s*([A-Z]+\d+)", text)

	rows, current, name, loose = [], None, {}, 0
	horizon = None
	starts = {}
	for line in text.splitlines():
		part = re.match(r"\s*Part Number:\s*(\S+)\s+Description:\s*(.*?)\s+Vendor Part", line)
		if part:
			current, name[part.group(1)] = part.group(1), part.group(2).strip()
			continue
		if _TECHFORM_LOOSE.match(line):
			loose += 1
		m = _TECHFORM_ROW.match(line)
		if m and current:
			qty = _num(m.group(2))
			when = _d(m.group(1))
			horizon = when if horizon is None or when > horizon else horizon
			_seen(starts, current, when)
			if qty > 0:
				rows.append(_row(current, name.get(current), when, qty, m.group(5)))

	parsed_lines = sum(1 for line in text.splitlines() if _TECHFORM_ROW.match(line))
	if not loose or loose != parsed_lines:
		return None
	_stamp(rows, starts)

	header = {
		"release_number": head.group(1),
		"supplier_vendor": vendor.group(1) if vendor else "",
		"schedule_date": str(_d(issued.group(1))),
		"horizon_end": str(horizon) if horizon else None,
	}
	return header, rows


# ---------------------------------------------------------------------------
# Techform "Vendor Planning Schedule" (report PO064R, ACS spool print)
# ---------------------------------------------------------------------------
# Per part: a "Part Number" header (code on the next line, description on the
# one after), then bands of up to 10 weeks printed as four lines - Date /
# Net Req. / Period / Auth Type. Bands can run over a page break; the page
# header repeats but the part carries on. Auth Type: Firm, Raw (raw-material
# authorisation) and Forecast; Raw counts as Forecast (client decision).

_ACS_DATES = re.compile(rf"^\s*Date\s+((?:{_DATE}\s*)+)$")
_ACS_NUMS = re.compile(r"^\s*Net Req\.\s+((?:[\d,]*\.?\d+-?\s*)+)$")
_ACS_AUTH = re.compile(r"^\s*Auth Type\s+((?:\S+\s*)+)$")


def detect_techform_acs(text):
	return (
		"Vendor Planning Schedule" in text
		and "Part Number" in text
		and "Net Req." in text
		and "Auth Type" in text
	)


def parse_techform_acs(text):
	head = re.search(rf"Release #:\s*(\S+)\s+Issue Date:\s*({_DATE})", text)
	if not head:
		return None
	vendor = re.search(r"Vendor:\s*([A-Z]+\d+)", text)

	lines = text.splitlines()
	rows, name, horizon = [], {}, None
	starts = {}
	current, cells_expected, cells_read = None, 0, 0
	parts_seen = 0
	i = 0
	while i < len(lines):
		line = lines[i]
		if re.match(r"\s*Part Number\s+Vendor Part Number", line):
			# code on the next line, description on the one after
			code_line = lines[i + 1] if i + 1 < len(lines) else ""
			desc_line = lines[i + 2] if i + 2 < len(lines) else ""
			code = code_line.split()[0] if code_line.split() else None
			if not code:
				return None
			current = code
			parts_seen += 1
			description = re.split(r"\s{2,}|Fab\. Cum", desc_line.strip())[0].strip()
			name.setdefault(code, description or code)
			i += 3
			continue

		m = _ACS_DATES.match(line)
		if m:
			dates = re.findall(_DATE, m.group(1))
			# the next three printed lines must be Net Req. / Period / Auth Type
			block = [l for l in lines[i + 1 : i + 6] if l.strip()][:3]
			if current is None or len(block) < 3:
				return None
			nums = _ACS_NUMS.match(block[0])
			auth = _ACS_AUTH.match(block[2])
			if not nums or not auth or not block[1].strip().startswith("Period"):
				return None
			qtys = nums.group(1).split()
			labels = auth.group(1).split()
			if not (len(dates) == len(qtys) == len(labels)):
				return None
			cells_expected += len(dates)
			for token, qty_text, label in zip(dates, qtys, labels):
				when = _d(token)
				horizon = when if horizon is None or when > horizon else horizon
				_seen(starts, current, when)
				cells_read += 1
				qty = _num(qty_text)
				if qty > 0:
					rows.append(_row(current, name.get(current), when, qty, label))
		i += 1

	# independent re-count of every printed week cell
	printed = sum(len(re.findall(_DATE, l)) for l in lines if _ACS_DATES.match(l))
	if not parts_seen or not rows or printed != cells_read or cells_expected != cells_read:
		return None
	for r in rows:
		r["item_name"] = name.get(r["item_code"]) or r["item_code"]
	_stamp(rows, starts)

	header = {
		"release_number": head.group(1),
		"supplier_vendor": vendor.group(1) if vendor else "",
		"schedule_date": str(_d(head.group(2))),
		"horizon_end": str(horizon) if horizon else None,
	}
	return header, _merge(rows)


# ---------------------------------------------------------------------------
# Marwood / Plex ("ONLINE RELEASES TO SUPPLIERS")
# ---------------------------------------------------------------------------
# One table line per release. The part (and PO) is printed on the first line of
# its group; the following lines carry only the release. The same part can sit
# under two POs, so rows are read per (part, PO) and merged afterwards. Only
# non-zero releases are listed, and the due date is the last date on the line.

_MARWOOD_PART = re.compile(r"^(P\w*)\s+(\d{5,7})\s+.*?\bPlant\s+(\S+)")
_MARWOOD_ROW = re.compile(
	r"\b(Firm|Forecast)\s+([\d,]+)\s+[\d,]+\s*pcs\s+[\d,]+\(\d+\)\s*pcs\s+([\d,]+)\b"
)


def detect_marwood_plex(text):
	return "ONLINE RELEASES TO SUPPLIERS" in text and "Release" in text


def parse_marwood_plex(text):
	stamp = re.search(r"-\s*(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}):(\d{2})\s*([AP]M)", text)
	if not stamp:
		return None
	hour = int(stamp.group(2)) % 12 + (12 if stamp.group(4) == "PM" else 0)
	when = _d(stamp.group(1))

	planner = re.search(r"Planner\s+(\w+)", text)
	vendor = re.search(r"Supplier\s+(\S+)", text)

	rows, current, name, loose = [], None, {}, 0
	for line in text.splitlines():
		part = _MARWOOD_PART.match(line)
		if part:
			current = part.group(3)
			if planner:
				m = re.search(rf"{planner.group(1)}\s+(.+?)\s+(?:{_DATE}|\d)", line)
				if m and current not in name:
					name[current] = m.group(1).strip()
		if _MARWOOD_ROW.search(line):
			loose += 1
			dates = re.findall(_DATE, line)
			if current and dates:
				m = _MARWOOD_ROW.search(line)
				qty = _num(m.group(3))  # balance still to ship (= quantity until receipts arrive)
				if qty > 0:
					rows.append(_row(current, name.get(current), _d(dates[-1]), qty, m.group(1)))
			else:
				return None  # a release line we could not place

	if not loose or not rows:
		return None
	for r in rows:
		r["item_name"] = name.get(r["item_code"]) or r["item_code"]

	header = {
		# Plex prints no release number: the print time identifies the release
		"release_number": f"PLEX-{when:%Y%m%d}-{hour:02d}{stamp.group(3)}",
		"supplier_vendor": vendor.group(1) if vendor else "",
		"schedule_date": str(when),
	}
	return header, _merge(rows)


# ---------------------------------------------------------------------------
# Mytox ("MATERIAL REQUIREMENTS RELEASE" mainframe report)
# ---------------------------------------------------------------------------
# Per part: DATE / QTY / CUM rows in three bands (daily, then weekly, then
# monthly). No Firm/Forecast label is printed, only "Daily quantities end:
# <date>" - the daily window is Firm, everything after it is Forecast.
# PAST-DUE is already-due quantity: immediate Firm, dated today.

def detect_mytox_mrr(text):
	return "MATERIAL REQUIREMENTS RELEASE" in text and "PART # :" in text and "QTY :" in text


def parse_mytox_mrr(text, today=None):
	if today is None:
		from frappe.utils import getdate

		today = getdate()

	release = re.search(rf"RELEASE DATE\s*:\s*({_DATE})", text)
	number = re.search(r"RELEASE NUMBER\s*:\s*(\S+)", text)
	daily_end = re.search(rf"Daily quantities end:\s*({_DATE})", text)
	if not release or not number or not daily_end:
		return None
	vendor = re.search(r"MATERIAL SUPPLIER\s*:\s*(\S+)", text)
	cutoff = _d(daily_end.group(1))

	entries, cur, dates, pending_name = [], None, None, False
	for line in text.splitlines():
		if "CONTACT:" in line[:40]:
			cur = {"part": None, "name": "", "rows": []}
			entries.append(cur)
		m = re.search(r"PART # : (\S+)", line)
		if m and cur is not None:
			if cur["part"] is None:
				cur["part"] = m.group(1)
				pending_name = True
		elif pending_name and cur is not None and cur["part"] is not None:
			nm = re.match(r"\s*(.*?)\s*DATE:", line)
			if nm and nm.group(1):
				cur["name"] = nm.group(1).strip()
			pending_name = False
		m = re.search(r"\bDATE:\s+(.*)$", line)
		if m and cur is not None and "RELEASE" not in line and "TIME" not in line:
			dates = m.group(1).replace("|", " ").split()
			continue
		m = re.search(r"\bQTY :\s+(.*)$", line)
		if m and dates is not None and cur is not None:
			values = m.group(1).replace("|", " ").split()
			if len(values) != len(dates):
				return None
			cur["rows"].extend(zip(dates, values))
			dates = None

	rows = []
	horizon = None
	starts = {}
	for entry in entries:
		if not entry["part"]:
			return None
		# Days each printed bucket stands for (gap to the next printed date;
		# the last one reuses the gap before it). Daily/weekly/monthly bands
		# share one report, so the grain differs along the same part.
		printed = sorted({_d(tok) for tok, _ in entry["rows"] if tok.upper() != "PAST-DUE"})
		span = {}
		for i, day in enumerate(printed):
			if i + 1 < len(printed):
				span[day] = (printed[i + 1] - day).days
			else:
				span[day] = span[printed[i - 1]] if i else 7
		for token, value in entry["rows"]:
			qty = _num(value)
			if token.upper() != "PAST-DUE":
				seen = _d(token)
				horizon = seen if horizon is None or seen > horizon else horizon
				_seen(starts, entry["part"], seen)
			if qty <= 0:
				continue
			if token.upper() == "PAST-DUE":
				rows.append(_row(entry["part"], entry["name"], today, qty, "Firm"))
				continue
			when = _d(token)
			row = _row(entry["part"], entry["name"], when, qty, "Firm" if when <= cutoff else "Forecast")
			row["bucket_days"] = span[when]
			rows.append(row)

	if not entries:
		return None
	_stamp(rows, starts)
	header = {
		"release_number": number.group(1),
		"supplier_vendor": vendor.group(1) if vendor else "",
		"schedule_date": str(_d(release.group(1))),
		"horizon_end": str(horizon) if horizon else None,
	}
	return header, _merge(rows)


# ---------------------------------------------------------------------------
# Registry + dispatcher
# ---------------------------------------------------------------------------

KNOWN_PDF_LAYOUTS = [
	("martinrea_cms", "Martinrea (CMS release planning schedule)", detect_martinrea_cms, parse_martinrea_cms),
	("techform_portal", "Techform / Magna planning schedule", detect_techform_portal, parse_techform_portal),
	("techform_acs", "Techform vendor planning schedule (PO064R)", detect_techform_acs, parse_techform_acs),
	("marwood_plex", "Marwood / Plex online release", detect_marwood_plex, parse_marwood_plex),
	("mytox_mrr", "Mytox material requirements release", detect_mytox_mrr, parse_mytox_mrr),
]


def parse_known_pdf(text):
	"""(key, label, header, rows) for the first known layout that recognises
	the text AND reconciles; None otherwise (caller falls back to AI)."""

	for key, label, detect, parse in KNOWN_PDF_LAYOUTS:
		if not detect(text):
			continue
		try:
			result = parse(text)
		except Exception:
			result = None
		if result and result[1]:
			return key, label, result[0], result[1]
	return None


def parse_by_key(key, text):
	for k, _label, _detect, parse in KNOWN_PDF_LAYOUTS:
		if k == key:
			result = parse(text)
			return result
	return None


# ---------------------------------------------------------------------------
# Excel / CSV: find a weekly-column ("Wide") header row in a raw grid
# ---------------------------------------------------------------------------

_ITEM_HEAD = re.compile(r"^(pn|p/n|part|part\s*(no|number|#)\.?|item(\s*code)?|material(\s*code)?|sku)$", re.I)
_NAME_HEAD = re.compile(r"^(description|desc\.?|part\s*name|item\s*name)$", re.I)


def _is_date_cell(value):
	if value is None or value != value:  # None, NaN, NaT
		return False
	if hasattr(value, "year") and hasattr(value, "month"):
		return True
	text = str(value).strip()
	return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}( 00:00:00)?|" + _DATE, text))


def find_wide_layout(grid, scan_rows=40):
	"""grid: list of row lists. Returns {header_row (1-based), item_code_column,
	item_name_column} when a row has an item/part heading and at least three
	date headings, with numeric data beneath it; else None."""

	for idx, row in enumerate(grid[:scan_rows]):
		dated = sum(1 for v in row if _is_date_cell(v))
		if dated < 3:
			continue
		item_col = name_col = None
		for v in row:
			text = "" if v is None else str(v).strip()
			if text.lower() == "nan":
				continue
			if item_col is None and _ITEM_HEAD.match(text):
				item_col = text
			elif name_col is None and _NAME_HEAD.match(text):
				name_col = text
		if not item_col:
			continue
		below = grid[idx + 1 : idx + 6]
		numbers = 0
		for r in below:
			for v in r:
				try:
					if v is not None and str(v).strip() not in ("", "nan") and float(str(v).replace(",", "")) > 0:
						numbers += 1
				except ValueError:
					pass
		if numbers:
			return {"header_row": idx + 1, "item_code_column": item_col, "item_name_column": name_col}
	return None
