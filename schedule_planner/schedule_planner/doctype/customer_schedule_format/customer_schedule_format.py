# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class CustomerScheduleFormat(Document):
	def validate(self):
		if self.file_type == "PDF" and not self.pdf_parser_key:
			self.parsing_mode = "LLM Extraction"

		if self.file_type != "PDF" and self.parsing_mode == "Template Mapping" and not self.column_mapping:
			frappe.msgprint(
				"No column mapping set - the parser will try to auto-detect columns "
				"by header name. Add rows here once you know this customer's exact "
				"column headers for more reliable parsing.",
				alert=True,
				indicator="orange",
			)
