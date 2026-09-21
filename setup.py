from setuptools import setup, find_packages

with open("requirements.txt") as f:
	install_requires = [line.strip() for line in f if line.strip() and not line.startswith("#")]

setup(
	name="schedule_planner",
	version="0.1.0",
	description="Customer schedule (PDF/Excel/CSV) ingestion, auto-forecast and Sales Order / Sales Forecast / Material Request automation for Frappe.",
	author="Indiba Solutions",
	author_email="rohit@indibasolutions.com",
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires,
)
