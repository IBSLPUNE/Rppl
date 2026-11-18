# Copyright (c) 2026, Indiba Solutions and contributors
# For license information, please see license.txt

"""PDF text with the columns kept, for when `pdftotext` is not installed."""


def pdfminer_layout_text(file_path):
	"""Text of a PDF with its columns kept, for when `pdftotext` is not installed.

	pdfminer's plain extract_text() reads block by block, so a table comes out as
	one column after another and the known-layout readers cannot recognise it.
	Here every character is put back at its position on the page (like
	`pdftotext -layout`): characters on the same baseline form a line, and the
	gap before each one is filled with spaces in proportion to its x position."""
	from pdfminer.high_level import extract_pages
	from pdfminer.layout import LAParams, LTChar, LTContainer

	def chars_of(node):
		if isinstance(node, LTChar):
			yield node
		elif isinstance(node, LTContainer):
			for child in node:
				yield from chars_of(child)

	pages = []
	for page in extract_pages(file_path, laparams=LAParams(boxes_flow=None)):
		chars = [c for c in chars_of(page) if c.get_text().strip()]
		if not chars:
			pages.append("")
			continue

		# one column of text = this many points; the page's median character width
		widths = sorted(c.width for c in chars if c.width > 0)
		cw = max(widths[len(widths) // 2], 1.0) if widths else 5.0

		# group into lines by baseline (y), top to bottom
		chars.sort(key=lambda c: -((c.y0 + c.y1) / 2))
		lines, cur, cur_y = [], [], None
		for c in chars:
			y = (c.y0 + c.y1) / 2
			tol = max(c.height, 1.0) * 0.45
			if cur_y is None or abs(y - cur_y) <= tol:
				cur.append(c)
				cur_y = y if cur_y is None else (cur_y + y) / 2
			else:
				lines.append(cur)
				cur, cur_y = [c], y
		if cur:
			lines.append(cur)

		out = []
		for line in lines:
			line.sort(key=lambda c: c.x0)
			buf, col, prev = [], 0, None
			for c in line:
				text = c.get_text()
				if prev is not None:
					gap = c.x0 - prev.x1
					# inside a word the glyphs touch; a real space is ~0.25 em wide
					if gap > max(prev.size, 1.0) * 0.17:
						target = int(round(c.x0 / cw))
						pad = max(1, target - col)
						buf.append(" " * pad)
						col += pad
				elif c.x0 > 0:
					pad = int(round(c.x0 / cw))
					buf.append(" " * pad)
					col += pad
				buf.append(text)
				col += len(text)
				prev = c
			out.append("".join(buf).rstrip())
		# pdftotext drops the page's left margin; so do we
		margin = min((len(l) - len(l.lstrip()) for l in out if l.strip()), default=0)
		pages.append("\n".join(l[margin:] for l in out))
	return "\f".join(pages)
