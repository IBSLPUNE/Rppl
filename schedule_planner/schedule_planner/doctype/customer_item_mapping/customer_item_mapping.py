# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from schedule_planner.utils.item_mapping import norm


class CustomerItemMapping(Document):
	def validate(self):
		self.file_part_no = (self.file_part_no or "").strip()
		if not norm(self.file_part_no):
			frappe.throw("Part Number in Customer File cannot be empty.")

		if self.item_code and frappe.db.get_value("Item", self.item_code, "disabled"):
			frappe.throw(f"Item {self.item_code} is disabled - pick an active item.")

		if self.enabled:
			clash = frappe.db.get_all(
				"Customer Item Mapping",
				filters={"customer": self.customer, "enabled": 1, "name": ["!=", self.name]},
				fields=["name", "file_part_no", "item_code"],
			)
			for row in clash:
				if norm(row.file_part_no) == norm(self.file_part_no):
					frappe.throw(
						f"{self.customer} already has an enabled mapping for part number "
						f"<b>{row.file_part_no}</b> (to {row.item_code}): "
						f"<a href='/app/customer-item-mapping/{row.name}'>{row.name}</a>. "
						"Edit that one, or untick Enabled on it first."
					)
