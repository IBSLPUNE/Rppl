# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

"""Turns an uploaded customer schedule file - PDF, Excel or CSV, with
whatever column names that particular customer happens to use - into a flat
list of row dicts: item_code, item_name, delivery_date (ISO string),
quantity (float), demand_type (Firm / Forecast / Raw).

Two very different problems are bundled behind one dispatcher:

* Excel / CSV -> already tabular. A `Customer Schedule Format` record holds a
  column-name -> field mapping per customer (set up once), or the parser
  falls back to guessing columns by header keywords.

* PDF -> layout varies wildly between vendors. Two strategies are supported,
  chosen per-customer on `Customer Schedule Format.parsing_mode`:
    - "LLM Extraction" (default): the PDF text is sent to a configured LLM
      with a strict JSON-extraction prompt. Works on a new customer's format
      with zero code changes.
    - "Template Mapping" with a `pdf_parser_key`: a hand-written regex parser
      registered in PDF_PARSER_REGISTRY. Deterministic and free, but has to
      be written per layout. "techform_v1" ships as an example/legacy parser.
"""

import json
import os
import re
import subprocess
from datetime import datetime

import frappe
from frappe.utils import getdate

from schedule_planner.utils import layout_parsers


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_header_and_rows(file_url, customer):
	"""Returns (header_dict, rows_list, format_doc_name)."""

	file_path, file_type = _resolve_file(file_url)
	fmt = _get_or_create_format(customer, file_type)

	if file_type == "PDF":
		text = _extract_pdf_text(file_path)
		header = _parse_pdf_header(text, customer)
		if fmt.parsing_mode == "Template Mapping" and fmt.pdf_parser_key:
			rows = _parse_pdf_with_registry(text, fmt.pdf_parser_key)
			known = layout_parsers.parse_by_key(fmt.pdf_parser_key, text)
			if known and known[1]:
				header, rows = known
		else:
			# Known layouts are read by plain code - free, instant, no AI.
			# Only a layout we have never seen goes to the AI.
			known = layout_parsers.parse_known_pdf(text)
			if known:
				_key, label, header, rows = known
				frappe.msgprint(f"Recognised layout: {label}. Read without AI.", alert=True, indicator="green")
			else:
				rows = _parse_pdf_with_llm(text)
	elif fmt.parsing_mode == "LLM Extraction":
		known = _try_known_tabular(file_path, fmt, customer)
		if known:
			header, rows = known
		else:
			header, rows = _llm_infer_and_extract_tabular(file_path, fmt, customer)
	elif fmt.layout == "Wide (Weekly Columns)":
		header, rows = _parse_wide(file_path, fmt, customer)
	else:
		rows = _parse_tabular(file_path, fmt)
		header = _default_header(customer, file_path)

	if not rows:
		frappe.throw("No valid schedule rows could be extracted from this file.")

	return header, rows, fmt.name


# ---------------------------------------------------------------------------
# File resolution
# ---------------------------------------------------------------------------

def _resolve_file(file_url):
	try:
		file_doc = frappe.get_doc("File", {"file_url": file_url})
		file_path = file_doc.get_full_path()
	except Exception:
		frappe.throw(f"File not found or inaccessible: {file_url}")

	ext = os.path.splitext(file_path)[1].lower()
	if ext == ".pdf":
		file_type = "PDF"
	elif ext in (".xlsx", ".xls", ".xlsm"):
		file_type = "Excel"
	elif ext == ".csv":
		file_type = "CSV"
	else:
		frappe.throw(f"Unsupported file type '{ext}'. Upload a PDF, Excel or CSV schedule.")

	return file_path, file_type


def _get_or_create_format(customer, file_type):
	if frappe.db.exists("Customer Schedule Format", customer):
		fmt = frappe.get_doc("Customer Schedule Format", customer)
		if fmt.file_type != file_type:
			frappe.msgprint(
				(
					"{0} is configured for {1} files but a {2} file was uploaded. "
					"Parsing will proceed using {2} rules; update the Customer "
					"Schedule Format record if this customer has changed formats."
				).format(customer, fmt.file_type, file_type),
				alert=True,
				indicator="orange",
			)
		return fmt

	fmt = frappe.get_doc(
		{
			"doctype": "Customer Schedule Format",
			"customer": customer,
			"file_type": file_type,
			"parsing_mode": "LLM Extraction",
			"layout": "Long",
			"header_row": 1,
		}
	)
	fmt.insert(ignore_permissions=True)
	if file_type == "PDF":
		frappe.msgprint(
			f"No parsing format existed for {customer} yet - created one "
			f"automatically (Customer Schedule Format: {fmt.name}). PDFs are read "
			f"with LLM Extraction by default - no setup needed.",
			alert=True,
			indicator="blue",
		)
	else:
		frappe.msgprint(
			f"No parsing format existed for {customer} yet - created one "
			f"automatically (Customer Schedule Format: {fmt.name}). The first "
			f"upload or two will be read with LLM Extraction (an AI figures out "
			f"the columns/layout); once it can reliably reproduce the same result "
			f"without the AI, this customer is switched to fast, free, "
			f"deterministic parsing automatically. Open the format record any "
			f"time to check or override.",
			alert=True,
			indicator="blue",
		)
	return fmt


# ---------------------------------------------------------------------------
# Excel / CSV - template mapping or header-keyword auto-detect
# ---------------------------------------------------------------------------

_HEADER_KEYWORDS = {
	"item_code": ["item code", "part number", "part no", "item", "material", "sku"],
	"item_name": ["item name", "description", "part name", "material description"],
	"delivery_date": ["delivery date", "date", "due date", "ship date", "week"],
	"quantity": ["quantity", "qty", "net req", "net requirement", "required qty"],
	"demand_type": ["demand type", "type", "auth type", "category", "status"],
}


def _parse_tabular(file_path, fmt):
	import pandas as pd

	header_row = (fmt.header_row or 1) - 1
	if fmt.file_type == "Excel":
		_require_xlrd(file_path)
		read_kwargs = {"header": header_row}
		if fmt.sheet_name:
			read_kwargs["sheet_name"] = fmt.sheet_name
		df = pd.read_excel(file_path, **read_kwargs)
		if isinstance(df, dict):  # sheet_name=None edge case, not expected here
			df = list(df.values())[0]
	else:
		df = pd.read_csv(file_path, header=header_row)

	df = df.dropna(how="all")
	df.columns = [str(c).strip() for c in df.columns]

	mapping = {row.source_column.strip(): row.target_field for row in (fmt.column_mapping or [])}
	if not mapping:
		mapping = _auto_detect_columns(df.columns)

	required = {"item_code", "delivery_date", "quantity"}
	if not required.issubset(set(mapping.values())):
		frappe.throw(
			"Could not confidently detect Item Code / Delivery Date / Quantity "
			f"columns in this file (found columns: {', '.join(df.columns)}). "
			f"Open Customer Schedule Format '{fmt.name}' and set the Column "
			"Mapping explicitly."
		)

	col_by_target = {}
	for source_col, target in mapping.items():
		match = next((c for c in df.columns if c.strip().lower() == source_col.strip().lower()), None)
		if match:
			col_by_target[target] = match

	rows = []
	for _, r in df.iterrows():
		item_code = str(r.get(col_by_target.get("item_code"), "")).strip()
		if not item_code or item_code.lower() == "nan":
			continue

		qty = _to_float(r.get(col_by_target.get("quantity")))
		if qty <= 0:
			continue

		delivery_date = _to_date(r.get(col_by_target.get("delivery_date")))
		if not delivery_date:
			continue

		demand_type_raw = str(r.get(col_by_target.get("demand_type"), "") or "").strip()
		demand_type = _normalise_demand_type(demand_type_raw)

		item_name = str(r.get(col_by_target.get("item_name"), "") or "").strip() or item_code

		rows.append(
			{
				"item_code": item_code,
				"item_name": item_name,
				"delivery_date": str(delivery_date),
				"quantity": qty,
				"demand_type": demand_type,
			}
		)

	return rows


def _auto_detect_columns(columns):
	mapping = {}
	for target, keywords in _HEADER_KEYWORDS.items():
		for col in columns:
			col_l = str(col).strip().lower()
			if any(kw in col_l for kw in keywords):
				mapping[col] = target
				break
	return mapping


def _normalise_demand_type(raw):
	raw_l = raw.lower()
	if "firm" in raw_l:
		return "Firm"
	if "fabrication" in raw_l or "raw material" in raw_l:
		return "Forecast"  # commitment tiers below Firm are planning demand
	if "forecast" in raw_l or "plan" in raw_l:
		return "Forecast"
	if "raw" in raw_l:
		return "Raw"
	return "Firm" if raw_l in ("f", "confirmed") else "Forecast"


def _to_float(value):
	try:
		if value is None or str(value).strip() == "" or str(value).lower() == "nan":
			return 0.0
		# Strip thousand separators ("9,000") and stray currency/space chars
		# that show up in real-world exports.
		cleaned = str(value).replace(",", "").strip()
		return float(cleaned)
	except (TypeError, ValueError):
		return 0.0


def _to_date(value):
	if value is None:
		return None
	try:
		return getdate(value)
	except Exception:
		pass
	for fmt in ("%m/%d/%y", "%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"):
		try:
			return getdate(datetime.strptime(str(value).strip(), fmt))
		except (ValueError, TypeError):
			continue
	return None


def _default_header(customer, file_path):
	return {
		"release_number": f"AUTO-{os.path.splitext(os.path.basename(file_path))[0]}"[:140],
		"supplier_vendor": customer,
		"schedule_date": str(getdate()),
	}


# ---------------------------------------------------------------------------
# Excel / CSV - "Wide" layout: one row per item, one column per delivery
# week, with a Firm/Forecast band row directly above the header row. Common
# on automotive weekly-bucket schedules (e.g. "SUPPLIERS | PN | Standard
# pack | Past Due | 2/12/24 | 2/19/24 | ..." with a "Firm Firm ... Forecast
# Forecast ..." row above it).
# ---------------------------------------------------------------------------

def _require_xlrd(file_path):
	"""pandas reads old .xls workbooks only through xlrd. Say so plainly
	instead of letting the failure look like an unreadable layout."""
	if not str(file_path).lower().endswith(".xls"):
		return
	try:
		import xlrd  # noqa: F401
	except ImportError:
		frappe.throw(
			"Old Excel files (.xls) need the xlrd package on the server. Run "
			"<code>bench pip install xlrd</code> and restart, or save the file as .xlsx and upload that."
		)


def _read_raw_no_header(file_path, fmt):
	import pandas as pd

	if fmt.file_type == "Excel":
		_require_xlrd(file_path)
		read_kwargs = {"header": None}
		if fmt.sheet_name:
			read_kwargs["sheet_name"] = fmt.sheet_name
		raw = pd.read_excel(file_path, **read_kwargs)
		if isinstance(raw, dict):
			raw = list(raw.values())[0]
	else:
		raw = pd.read_csv(file_path, header=None)
	return raw


def _find_col_index(header_cells, target_name):
	if not target_name:
		return None
	target_l = str(target_name).strip().lower()
	for idx, val in enumerate(header_cells):
		if str(val).strip().lower() == target_l:
			return idx
	return None


def _parse_wide(file_path, fmt, customer):
	raw = _read_raw_no_header(file_path, fmt)
	header_row_idx = (fmt.header_row or 1) - 1

	if header_row_idx >= len(raw):
		frappe.throw(f"Header Row Number ({fmt.header_row}) is beyond the end of the file.")

	header = _extract_wide_header(raw, header_row_idx, customer)
	rows = _rows_from_wide(raw, header_row_idx, fmt)
	return header, rows


def _extract_wide_header(raw, header_row_idx, customer):
	"""Best-effort: scan the rows above the column-header row for a
	'Date : <value>' style label (common on these letterhead-style sheets)
	to use as the schedule date. Falls back to today() + an auto release
	number when nothing is found - the duplicate-schedule check still works
	fine off the auto-generated release number."""

	schedule_date = None
	for r in range(0, header_row_idx):
		row_vals = list(raw.iloc[r])
		for i, v in enumerate(row_vals):
			label = str(v).strip().rstrip(":").strip().lower()
			if label == "date" and i + 1 < len(row_vals):
				candidate = _to_date(row_vals[i + 1])
				if candidate:
					schedule_date = candidate

	found_date = schedule_date is not None
	schedule_date = schedule_date or getdate()
	return {
		# Last week column printed in the sheet (zero weeks included) - the
		# forecast engine needs it to know where the customer's horizon ends.
		"horizon_end": _wide_horizon_end(raw, header_row_idx),
		# A date printed on the sheet identifies the release, so uploading the
		# same file twice is caught as a duplicate; otherwise a random id.
		"release_number": (
			f"{customer}-{schedule_date:%Y%m%d}"[:140] if found_date else f"AUTO-{frappe.generate_hash(length=8)}"
		),
		"supplier_vendor": customer,
		"schedule_date": str(schedule_date),
	}


def _wide_horizon_end(raw, header_row_idx):
	latest = None
	for value in list(raw.iloc[header_row_idx]):
		text = str(value).strip().lower()
		if not text or text in ("nan", "past due"):
			continue
		parsed = _to_date(value)
		if parsed and (latest is None or parsed > latest):
			latest = parsed
	return str(latest) if latest else None


def _rows_from_wide(raw, header_row_idx, fmt):
	header_cells = list(raw.iloc[header_row_idx])
	band_row_idx = header_row_idx - 1
	band_cells = list(raw.iloc[band_row_idx]) if band_row_idx >= 0 else [None] * len(header_cells)

	# Forward-fill the Firm/Forecast band across merged/blank cells - Excel
	# merged cells only carry a value in their first (leftmost) cell.
	filled_band = []
	last = None
	for v in band_cells:
		v_str = str(v).strip()
		if v_str and v_str.lower() != "nan":
			last = v_str
		filled_band.append(last)

	item_code_col = fmt.item_code_column or "PN"
	item_code_idx = _find_col_index(header_cells, item_code_col)
	if item_code_idx is None:
		frappe.throw(
			f"Could not find an '{item_code_col}' column in row {header_row_idx + 1}. "
			f"Columns found: {', '.join(str(c) for c in header_cells if str(c).strip() and str(c).lower() != 'nan')}"
		)
	item_name_idx = _find_col_index(header_cells, fmt.item_name_column) if fmt.item_name_column else None

	date_cols = []  # (col_idx, delivery_date, demand_type)
	for idx, header_val in enumerate(header_cells):
		if idx in (item_code_idx, item_name_idx):
			continue
		header_str = str(header_val).strip()
		if not header_str or header_str.lower() == "nan":
			continue
		if header_str.lower() == "past due":
			# Already-due quantity - treat as immediate Firm demand.
			date_cols.append((idx, getdate(), "Firm"))
			continue
		parsed_date = _to_date(header_val)
		if not parsed_date:
			continue  # e.g. "Standard pack", "SUPPLIERS" - not a week column, ignore
		band_label = (filled_band[idx] or "").lower()
		demand_type = "Firm" if "firm" in band_label else "Forecast"
		date_cols.append((idx, parsed_date, demand_type))

	if not date_cols:
		frappe.throw(
			"Could not detect any weekly date columns in this file. Check the "
			"Header Row Number on this customer's Schedule Format."
		)

	rows = []
	for row_idx in range(header_row_idx + 1, len(raw)):
		row = list(raw.iloc[row_idx])
		item_code = str(row[item_code_idx]).strip()
		if not item_code or item_code.lower() == "nan":
			continue
		item_name = str(row[item_name_idx]).strip() if item_name_idx is not None else ""
		item_name = item_name if item_name and item_name.lower() != "nan" else item_code

		for col_idx, delivery_date, demand_type in date_cols:
			qty = _to_float(row[col_idx])
			if qty <= 0:
				continue
			rows.append(
				{
					"item_code": item_code,
					"item_name": item_name,
					"delivery_date": str(delivery_date),
					"quantity": qty,
					"demand_type": demand_type,
				}
			)

	return rows


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------

def _extract_pdf_text(file_path):
	try:
		result = subprocess.run(
			["pdftotext", "-layout", file_path, "-"],
			capture_output=True,
			text=True,
			timeout=60,
		)
		pdf_text = result.stdout
	except Exception:
		pdf_text = ""

	if not pdf_text.strip():
		try:
			from pdfminer.high_level import extract_text

			pdf_text = extract_text(file_path)
		except ImportError:
			frappe.throw("pdfminer.six is not installed. Run: bench pip install pdfminer.six")

	if not pdf_text.strip():
		frappe.throw(
			"This PDF could not be parsed as text. Make sure it is not a "
			"scanned/image-only PDF (OCR is not supported)."
		)

	return pdf_text


def _parse_pdf_header(text, customer):
	release_match = re.search(r"Release\s*(?:#|Number|No\.?)\s*[:\s]*(\S+)", text, re.IGNORECASE)
	vendor_match = re.search(r"Vendor\s*[:#]\s*(\S+)", text, re.IGNORECASE)
	# "Issue Date:", "Release Date :" or the bare "Date:" printed next to the release number
	issue_date_match = (
		re.search(r"Issue Date\s*:\s*([\d/]+)", text, re.IGNORECASE)
		or re.search(r"Release Date\s*:\s*([\d/]+)", text, re.IGNORECASE)
		or re.search(r"Release\s*#[^\n]*?\bDate\s*:\s*([\d/]+)", text, re.IGNORECASE)
	)

	schedule_date = getdate()
	if issue_date_match:
		for fmt in ("%m/%d/%y", "%m/%d/%Y"):
			try:
				schedule_date = getdate(datetime.strptime(issue_date_match.group(1), fmt))
				break
			except ValueError:
				continue

	return {
		"release_number": release_match.group(1)
		if release_match
		else f"UNKNOWN-{frappe.generate_hash(length=6)}",
		"supplier_vendor": vendor_match.group(1) if vendor_match else customer,
		"schedule_date": str(schedule_date),
	}


# ---------------------------------------------------------------------------
# PDF - legacy deterministic regex parsers, keyed by pdf_parser_key
# ---------------------------------------------------------------------------

def _parse_pdf_with_registry(text, parser_key):
	parser = PDF_PARSER_REGISTRY.get(parser_key)
	if not parser:
		frappe.throw(
			f"No legacy PDF parser registered for key '{parser_key}'. "
			f"Known keys: {', '.join(PDF_PARSER_REGISTRY.keys()) or '(none)'}. "
			"Clear the Legacy PDF Parser Key on this customer's Schedule "
			"Format to use LLM Extraction instead."
		)
	return parser(text)


def _parse_pdf_techform_v1(text):
	"""Original hand-written regex parser for the 'Techform' style vendor
	planning schedule layout (Part Number / Date / Net Req. / Auth Type
	blocks). Kept as a free, deterministic option for that one known layout."""

	page_header_re = re.compile(
		r"[ \t]*Report:.*?Page:.*?\n[ \t]*Printed:.*?\n(?:.*?\n){0,2}[ \t]*Release.*?Replace.*?\n",
		re.DOTALL,
	)
	cleaned = page_header_re.sub("\n", text)
	cleaned = re.sub(r"Continued Next Page \.+\s*\n", "\n", cleaned)

	part_header_re = re.compile(
		r"^\s*Part Number\s+Vendor Part Number.*?Cum YTD Required:.*?\n",
		re.IGNORECASE | re.MULTILINE,
	)
	date_re = re.compile(r"^\s*Date\s+((?:\d{1,2}/\d{1,2}/\d{2,4}\s*)+)", re.MULTILINE)
	qty_re = re.compile(r"^\s*Net Req\.\s+((?:[\d.]+\s*)+)", re.MULTILINE)
	auth_re = re.compile(r"^\s*Auth Type\s+((?:(?:Firm|Forecast|Raw)\s*)+)", re.MULTILINE | re.IGNORECASE)
	part_code_re = re.compile(r"^[A-Z][A-Z0-9]{3,7}$")

	blocks = part_header_re.split(cleaned)[1:]
	rows = []

	for block in blocks:
		lines = [l for l in block.split("\n") if l.strip()]
		item_code, item_name = "", ""

		for line in lines[:6]:
			tok = line.strip().split()[0] if line.strip().split() else ""
			if part_code_re.match(tok):
				item_code = tok
				break

		for line in lines[:6]:
			tokens = line.strip().split()
			first_tok = tokens[0] if tokens else ""
			if (
				first_tok
				and re.match(r"^[A-Za-z]", first_tok)
				and not part_code_re.match(first_tok)
				and first_tok not in ("Continued", "Part", "Report", "Printed")
			):
				item_name = re.split(r"\s{2,}", line.strip())[0].strip()
				item_name = re.split(r"\s+Fab\.|\s+Part Status", item_name)[0].strip()
				break

		if not item_code:
			continue

		all_dates, all_qtys, all_auths = [], [], []
		for m in date_re.finditer(block):
			all_dates.extend(m.group(1).strip().split())
		for m in qty_re.finditer(block):
			all_qtys.extend(m.group(1).strip().split())
		for m in auth_re.finditer(block):
			all_auths.extend(m.group(1).strip().split())

		count = min(len(all_dates), len(all_qtys), len(all_auths))
		for i in range(count):
			try:
				qty = float(all_qtys[i])
			except ValueError:
				continue
			if qty <= 0:
				continue

			delivery_date = None
			for fmt in ("%m/%d/%y", "%m/%d/%Y"):
				try:
					delivery_date = getdate(datetime.strptime(all_dates[i], fmt))
					break
				except ValueError:
					continue
			if not delivery_date:
				continue

			rows.append(
				{
					"item_code": item_code,
					"item_name": item_name or item_code,
					"delivery_date": str(delivery_date),
					"quantity": qty,
					"demand_type": all_auths[i].capitalize(),
				}
			)

	return rows


PDF_PARSER_REGISTRY = {
	"techform_v1": _parse_pdf_techform_v1,
	# Built-in layouts (also recognised automatically, see layout_parsers.py)
	**{key: (lambda text, _key=key: (layout_parsers.parse_by_key(_key, text) or (None, []))[1]) for key, *_ in layout_parsers.KNOWN_PDF_LAYOUTS},
}


# ---------------------------------------------------------------------------
# LLM-based extraction - shared plumbing (used by PDF and, below, Excel/CSV)
# ---------------------------------------------------------------------------

_LLM_PROMPT = """You are extracting rows from an automotive-parts customer delivery \
schedule PDF (converted to plain text below). Return ONLY a JSON array (no \
prose, no markdown fences) where each element is:
{{"item_code": string, "item_name": string, "delivery_date": "YYYY-MM-DD", \
"quantity": number, "demand_type": "Firm" | "Forecast" | "Raw"}}

Rules:
- One element per (item, delivery date) line with a positive quantity.
- "Firm" = confirmed/committed demand the customer will definitely take.
- "Forecast" (or "Raw"/planning data) = tentative future demand. Authorisation \
tiers named "Fabrication" or "Raw Material" are also "Forecast".
- If demand type is not labelled at all, use "Forecast".
- Skip rows with zero or blank quantity.
- Dates must be normalised to YYYY-MM-DD.

TEXT:
{text}
"""


def _ensure_llm_configured(settings):
	if settings.llm_provider == "None" or not settings.llm_provider:
		frappe.throw(
			"No LLM provider is configured in Schedule Planning Settings. "
			"Set one - Local (Ollama) needs no API key/internet at all - "
			"before uploading schedules for a customer that doesn't have a "
			"manually configured parsing template."
		)

	if settings.llm_provider == "Local (Ollama)":
		# Self-hosted - no API key needed. A dummy value is still passed
		# through to the caller so the shared HTTP-call code stays uniform.
		return settings.get_password("llm_api_key") or "ollama"

	api_key = settings.get_password("llm_api_key")
	if not api_key:
		frappe.throw("Set an API Key in Schedule Planning Settings > AI-Based Schedule Extraction.")

	return api_key


def _call_llm(settings, api_key, prompt):
	if settings.llm_provider == "Anthropic":
		return _call_anthropic(api_key, settings, prompt)
	elif settings.llm_provider == "OpenAI":
		return _call_openai(api_key, settings, prompt)
	elif settings.llm_provider == "Google Gemini":
		return _call_gemini(api_key, settings, prompt)
	elif settings.llm_provider == "Groq":
		return _call_groq(api_key, settings, prompt)
	elif settings.llm_provider == "Local (Ollama)":
		return _call_ollama(api_key, settings, prompt)
	else:
		frappe.throw(f"Unsupported LLM provider: {settings.llm_provider}")


def _call_anthropic(api_key, settings, prompt):
	import requests

	endpoint = settings.llm_endpoint or "https://api.anthropic.com/v1/messages"
	model = settings.llm_model or "claude-sonnet-4-5"

	response = requests.post(
		endpoint,
		headers={
			"x-api-key": api_key,
			"anthropic-version": "2023-06-01",
			"content-type": "application/json",
		},
		json={
			"model": model,
			"max_tokens": 8000,
			"messages": [{"role": "user", "content": prompt}],
		},
		timeout=120,
	)
	if response.status_code != 200:
		frappe.throw(f"LLM extraction failed ({response.status_code}): {response.text[:500]}")

	data = response.json()
	return "".join(block.get("text", "") for block in data.get("content", []))


def _call_openai_compatible(api_key, endpoint, model, prompt, timeout=120):
	"""Shared caller for any provider that speaks the OpenAI chat-completions
	wire format (OpenAI itself, Groq, Ollama, and most other OpenAI-compatible
	hosts)."""
	import requests

	response = requests.post(
		endpoint,
		headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
		json={
			"model": model,
			"messages": [{"role": "user", "content": prompt}],
			"temperature": 0,
		},
		timeout=timeout,
	)
	if response.status_code != 200:
		frappe.throw(f"LLM extraction failed ({response.status_code}): {response.text[:500]}")

	data = response.json()
	return data["choices"][0]["message"]["content"]


def _call_groq(api_key, settings, prompt):
	endpoint = settings.llm_endpoint or "https://api.groq.com/openai/v1/chat/completions"
	model = settings.llm_model or "llama-3.3-70b-versatile"
	return _call_openai_compatible(api_key, endpoint, model, prompt)


def _call_ollama(api_key, settings, prompt):
	"""Self-hosted, free, no internet/API-key dependency. Ollama
	(https://ollama.com) exposes an OpenAI-compatible /v1/chat/completions
	endpoint - defaults assume it's running on the same machine as the bench
	(`ollama serve`, default port 11434). CPU-only inference on a modest
	machine can genuinely take 30s-2min for a large file - that's expected,
	and it only ever runs for a customer whose layout hasn't been learned yet."""
	endpoint = settings.llm_endpoint or "http://localhost:11434/v1/chat/completions"
	# qwen2.5:3b-instruct is a good small/CPU-friendly default for structured
	# JSON extraction; bump this in Settings > Model if you have more RAM/a GPU
	# (e.g. "llama3.1:8b-instruct-q4_K_M" or "qwen2.5:7b-instruct").
	model = settings.llm_model or "qwen2.5:3b-instruct"

	import requests

	try:
		return _call_openai_compatible(api_key, endpoint, model, prompt, timeout=600)
	except requests.exceptions.RequestException as e:
		frappe.throw(
			f"Could not reach the local Ollama server at {endpoint} ({e}). Make "
			f"sure Ollama is installed and running (`ollama serve`) and that the "
			f"model in Schedule Planning Settings > Model has been pulled "
			f"(`ollama pull {model}`)."
		)


def _call_gemini(api_key, settings, prompt):
	import requests

	# gemini-2.5-flash was retired for new users in late 2025 - flash-lite is
	# the more conservative currently-live free-tier default; gemini-3.6-flash
	# is Google's newest recommended replacement if you want to try it (set it
	# explicitly in the Model field - some users have reported it being less
	# stable under load than the older flash-lite line).
	model = settings.llm_model or "gemini-2.5-flash-lite"
	endpoint = settings.llm_endpoint or (
		f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
	)

	response = requests.post(
		endpoint,
		headers={"content-type": "application/json"},
		params={"key": api_key},
		json={
			"contents": [{"parts": [{"text": prompt}]}],
			"generationConfig": {"temperature": 0},
		},
		timeout=120,
	)
	if response.status_code == 404:
		frappe.throw(
			f"Gemini model '{model}' was not found/is no longer available "
			f"({response.text[:300]}). Google retires old model names from "
			f"time to time - set a current one (e.g. gemini-2.5-flash-lite or "
			f"gemini-3.6-flash) in Schedule Planning Settings > Model."
		)
	if response.status_code != 200:
		frappe.throw(f"LLM extraction failed ({response.status_code}): {response.text[:500]}")

	data = response.json()
	try:
		candidate = data["candidates"][0]
		return "".join(part.get("text", "") for part in candidate["content"]["parts"])
	except (KeyError, IndexError, TypeError):
		frappe.throw(f"Unexpected response shape from Gemini: {json.dumps(data)[:500]}")


def _call_openai(api_key, settings, prompt):
	endpoint = settings.llm_endpoint or "https://api.openai.com/v1/chat/completions"
	model = settings.llm_model or "gpt-4o"
	return _call_openai_compatible(api_key, endpoint, model, prompt)


def _normalise_llm_rows(parsed_list):
	rows = []
	for item in parsed_list:
		try:
			item_code = str(item["item_code"]).strip()
			qty = float(item["quantity"])
			delivery_date = getdate(item["delivery_date"])
		except (KeyError, TypeError, ValueError):
			continue
		if not item_code or qty <= 0:
			continue
		demand_type = str(item.get("demand_type") or "Forecast").strip().capitalize()
		if demand_type not in ("Firm", "Forecast", "Raw"):
			demand_type = "Forecast"
		rows.append(
			{
				"item_code": item_code,
				"item_name": str(item.get("item_name") or item_code).strip(),
				"delivery_date": str(delivery_date),
				"quantity": qty,
				"demand_type": demand_type,
			}
		)
	return rows


def _parse_llm_json_array(raw_text, error_hint=""):
	match = re.search(r"\[.*\]", raw_text, re.DOTALL)
	if not match:
		frappe.throw(f"The LLM did not return parseable JSON rows.{(' ' + error_hint) if error_hint else ''}")

	try:
		return json.loads(match.group(0))
	except json.JSONDecodeError:
		frappe.throw("The LLM's JSON response could not be parsed.")


def _parse_llm_json_object(raw_text):
	match = re.search(r"\{.*\}", raw_text, re.DOTALL)
	if not match:
		frappe.throw(
			"The LLM did not return a parseable JSON object. Try again, or set "
			"up this customer's Column Mapping / Layout manually in Customer "
			"Schedule Format."
		)

	try:
		return json.loads(match.group(0))
	except json.JSONDecodeError:
		frappe.throw("The LLM's JSON response could not be parsed.")


# ---------------------------------------------------------------------------
# PDF - LLM-based extraction (format-agnostic)
# ---------------------------------------------------------------------------

def _parse_pdf_with_llm(text):
	settings = frappe.get_cached_doc("Schedule Planning Settings")
	api_key = _ensure_llm_configured(settings)

	# Guard against extremely large PDFs blowing the prompt budget.
	truncated_text = text[:120000]
	prompt = _LLM_PROMPT.format(text=truncated_text)
	raw = _call_llm(settings, api_key, prompt)

	parsed = _parse_llm_json_array(raw, error_hint="Try again or use a Legacy PDF Parser Key.")
	return _normalise_llm_rows(parsed)


# ---------------------------------------------------------------------------
# Excel / CSV - recognise ordinary layouts without the AI
# ---------------------------------------------------------------------------

def _try_known_tabular(file_path, fmt, customer):
	"""Weekly-column ("Wide") sheets and plain Long tables are recognised from
	their headers and read by the deterministic parsers - no AI. Returns
	(header, rows) or None when nothing recognisable is found (the caller then
	falls back to AI extraction). Never raises."""

	try:
		raw = _read_raw_no_header(file_path, fmt)
		grid = [list(r) for r in raw.head(60).itertuples(index=False, name=None)]

		wide = layout_parsers.find_wide_layout(grid)
		if wide:
			trial = frappe._dict(
				file_type=fmt.file_type,
				sheet_name=fmt.sheet_name,
				header_row=wide["header_row"],
				item_code_column=wide["item_code_column"],
				item_name_column=wide["item_name_column"],
				column_mapping=[],
			)
			header, rows = _parse_wide(file_path, trial, customer)
			if rows:
				frappe.msgprint("Recognised a weekly-column layout. Read without AI.", alert=True, indicator="green")
				return header, rows

		for header_row in range(1, 11):
			trial = frappe._dict(
				file_type=fmt.file_type,
				sheet_name=fmt.sheet_name,
				header_row=header_row,
				item_code_column=None,
				item_name_column=None,
				column_mapping=[],
			)
			try:
				rows = _parse_tabular(file_path, trial)
			except Exception:
				continue
			if rows:
				frappe.msgprint("Recognised a one-row-per-date layout. Read without AI.", alert=True, indicator="green")
				return _default_header(customer, file_path), rows
	except Exception:
		return None
	return None


# ---------------------------------------------------------------------------
# Excel / CSV - LLM-based extraction with self-learning template promotion.
#
# On a customer's first upload (or any upload while parsing_mode is still
# "LLM Extraction"), the whole grid is sent to the LLM, which returns both
# the extracted rows AND a best-guess reusable layout (column mapping, or
# Wide-layout settings). That inferred layout is immediately re-run through
# the ordinary deterministic parser (_parse_tabular / _rows_from_wide)
# against the SAME file, purely in memory - if the deterministic result
# closely matches what the LLM extracted, the layout is saved onto the real
# Customer Schedule Format and parsing_mode flips to "Template Mapping": all
# future uploads from that customer parse instantly, for free, without
# calling the LLM at all. If it doesn't match closely enough, nothing is
# saved and the next upload just tries the LLM again (safe default).
# ---------------------------------------------------------------------------

_TABULAR_LLM_PROMPT = """You are reading an automotive-parts customer delivery schedule \
exported from Excel/CSV as a numbered grid of cells below (Row 0, Row 1, ...). \
Column layouts vary wildly between customers - there may be one row per \
item+date ("Long"), or one row per item with a separate column per delivery \
week and a Firm/Forecast band row directly above the header row ("Wide").

Return ONLY a JSON object (no prose, no markdown fences) shaped exactly like:
{{
  "header": {{"release_number": string, "schedule_date": "YYYY-MM-DD" or null}},
  "layout": {{
    "type": "Long" or "Wide",
    "header_row": integer (0-based row number of the actual column header row - \
for Wide, this is the row with item code / date columns, NOT the Firm/Forecast \
band row above it),
    "item_code_column": string (exact header text of the item/part code column),
    "item_name_column": string or null (exact header text of the item \
description column, if any),
    "column_mapping": [{{"source_column": string, "target_field": \
"item_code"|"item_name"|"delivery_date"|"quantity"|"demand_type"}}, ...] \
(Long layout only - one entry per relevant column; omit entirely or leave \
empty for Wide)
  }},
  "rows": [{{"item_code": string, "item_name": string, "delivery_date": \
"YYYY-MM-DD", "quantity": number, "demand_type": "Firm"|"Forecast"|"Raw"}}, ...]
}}

Rules:
- One row element per (item, delivery date) with a positive quantity.
- "Firm" = confirmed/committed demand. "Forecast"/"Raw" = tentative/planning \
demand ("Fabrication" and "Raw Material" tiers count as "Forecast"). If not \
labelled at all, use "Forecast".
- A column/cell literally labelled "Past Due" is already-due quantity - treat \
it as an immediate "Firm" row with today's date, not a future week.
- For a Wide layout, a Firm/Forecast band row sits directly above the header \
row and its cells are usually merged (blank = "carry forward the last value \
to the left"); use the nearest non-blank label to its left for that column.
- Ignore columns that are neither an item code/name column nor a date/week \
column (e.g. standard pack size, vendor name, supplier code).
- Skip rows with zero, blank, or negative quantity.
- Dates must be normalised to YYYY-MM-DD.

GRID:
{grid}
"""


def _grid_to_text(file_path, fmt, max_rows=150):
	raw = _read_raw_no_header(file_path, fmt)
	lines = []
	total_len = 0
	limit = 60000
	for row_idx in range(min(len(raw), max_rows)):
		cells = [str(v).strip() for v in list(raw.iloc[row_idx])]
		cells = ["" if c.lower() == "nan" else c for c in cells]
		line = f"Row {row_idx}: " + " | ".join(cells)
		total_len += len(line)
		if total_len > limit:
			break
		lines.append(line)
	return "\n".join(lines)


def _llm_infer_and_extract_tabular(file_path, fmt, customer):
	settings = frappe.get_cached_doc("Schedule Planning Settings")
	api_key = _ensure_llm_configured(settings)

	grid_text = _grid_to_text(file_path, fmt)
	prompt = _TABULAR_LLM_PROMPT.format(grid=grid_text)
	raw = _call_llm(settings, api_key, prompt)
	parsed = _parse_llm_json_object(raw)

	llm_rows = _normalise_llm_rows(parsed.get("rows") or [])
	if not llm_rows:
		frappe.throw(
			"The AI could not find any valid schedule rows in this file. Try "
			"again, or set up this customer's Layout / Column Mapping manually "
			"in Customer Schedule Format."
		)

	header_in = parsed.get("header") or {}
	schedule_date = _to_date(header_in.get("schedule_date")) or getdate()
	header = {
		"release_number": str(header_in.get("release_number") or f"AUTO-{frappe.generate_hash(length=8)}")[:140],
		"supplier_vendor": customer,
		"schedule_date": str(schedule_date),
	}

	layout = parsed.get("layout") or {}
	try:
		_try_verify_layout_and_upgrade(file_path, fmt, layout, llm_rows)
	except Exception:
		# Self-learning is a nice-to-have; never let a verification bug break
		# the actual upload the user is waiting on.
		frappe.log_error(title="Schedule Planner: layout self-learning failed")

	return header, llm_rows


def _make_trial_fmt(fmt, layout):
	layout_type = layout.get("type") or "Long"
	column_mapping = [
		frappe._dict(source_column=c.get("source_column"), target_field=c.get("target_field"))
		for c in (layout.get("column_mapping") or [])
		if c.get("source_column") and c.get("target_field")
	]
	return frappe._dict(
		file_type=fmt.file_type,
		sheet_name=fmt.sheet_name,
		header_row=(layout.get("header_row") or 0) + 1,  # LLM gives 0-based, our parsers use 1-based
		item_code_column=layout.get("item_code_column"),
		item_name_column=layout.get("item_name_column"),
		column_mapping=column_mapping,
	), layout_type


def _rows_roughly_match(det_rows, llm_rows):
	if not det_rows or not llm_rows:
		return False

	count_ratio = min(len(det_rows), len(llm_rows)) / max(len(det_rows), len(llm_rows))
	if count_ratio < 0.95:
		return False

	det_sum = sum(r["quantity"] for r in det_rows)
	llm_sum = sum(r["quantity"] for r in llm_rows)
	if max(det_sum, llm_sum) == 0:
		return False
	sum_ratio = min(det_sum, llm_sum) / max(det_sum, llm_sum)
	return sum_ratio >= 0.98


def _try_verify_layout_and_upgrade(file_path, fmt, layout, llm_rows):
	if not layout or not layout.get("item_code_column") and not layout.get("column_mapping"):
		return

	trial_fmt, layout_type = _make_trial_fmt(fmt, layout)

	try:
		if layout_type == "Wide":
			_, det_rows = _parse_wide(file_path, trial_fmt, fmt.customer)
		else:
			det_rows = _parse_tabular(file_path, trial_fmt)
	except Exception:
		return  # inferred layout doesn't even run cleanly - stay on LLM Extraction

	if not _rows_roughly_match(det_rows, llm_rows):
		return  # deterministic replay disagrees with the LLM - stay on LLM Extraction, safer

	_save_layout_to_format(fmt, trial_fmt, layout_type)


def _save_layout_to_format(fmt, trial_fmt, layout_type):
	real_fmt = frappe.get_doc("Customer Schedule Format", fmt.name)
	real_fmt.layout = "Wide (Weekly Columns)" if layout_type == "Wide" else "Long"
	real_fmt.header_row = trial_fmt.header_row
	real_fmt.parsing_mode = "Template Mapping"

	if layout_type == "Wide":
		real_fmt.item_code_column = trial_fmt.item_code_column or "PN"
		real_fmt.item_name_column = trial_fmt.item_name_column
	else:
		real_fmt.set("column_mapping", [])
		for c in trial_fmt.column_mapping:
			real_fmt.append("column_mapping", {"source_column": c.source_column, "target_field": c.target_field})

	note = (
		f"[{frappe.utils.now_datetime()}] Auto-learned {real_fmt.layout} layout from "
		f"AI extraction after the deterministic replay matched closely enough - "
		f"switched to Template Mapping (fast, free, no AI call) for future uploads."
	)
	real_fmt.notes = ((real_fmt.notes or "") + "\n" + note).strip()
	real_fmt.save(ignore_permissions=True)

	frappe.msgprint(
		f"This file's layout was learned automatically and saved to Customer "
		f"Schedule Format '{real_fmt.name}' - future uploads from this customer "
		f"will parse instantly without calling the AI. Open the format record "
		f"any time to review or correct it.",
		alert=True,
		indicator="green",
	)
