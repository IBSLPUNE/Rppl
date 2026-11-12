import frappe


def execute():
	"""Backfill Customer Name on Customer Schedules created before the field existed."""
	if not frappe.db.has_column("Customer Schedule", "customer_name"):
		return
	frappe.db.sql(
		"""
		UPDATE `tabCustomer Schedule` cs
		INNER JOIN `tabCustomer` c ON c.name = cs.customer
		SET cs.customer_name = c.customer_name
		WHERE IFNULL(cs.customer_name, '') = ''
		"""
	)
