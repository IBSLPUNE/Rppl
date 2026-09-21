# Schedule Planner

A Frappe app for automotive-parts suppliers who receive recurring
(weekly/monthly) delivery schedules from customers - PDF, Excel or CSV, each
customer with its own column layout - and need to turn Firm demand into
Sales Orders, Forecast demand into a Sales Forecast, and both into an
automatic Material Request, with full amendment + revision tracking when a
customer's next schedule changes what an earlier one said.

Everything ships as part of the app - Doctypes, custom fields, reports,
client scripts. `bench install-app` is all that's required; nothing has to
be created by hand in the UI.

## What it does

1. **Upload a schedule file** on a new Customer Schedule (PDF / Excel / CSV -
   auto-detected, any column layout - see *Handling every customer's own
   format* below). The file is parsed into item / delivery date / quantity /
   demand type (Firm or Forecast) rows.
2. **Auto-forecast top-up**: if the customer only gave you 3 months but parts
   lead time is 7, pick a target month count and one of two methods -
   *Average of Last 4 Weeks* or *Average Weekly Shipment - Last 3 Months*
   (real Delivery Note history). Untick any item you don't want auto-forecast
   for (e.g. one that's closing).
3. **Create as Draft, review, then Submit.** Submitting is what triggers
   everything downstream:
   - Firm rows -> **one Sales Order per delivery date** (multiple items,
     one date, per SO).
   - If a date/item combination already has a submitted Sales Order from an
     earlier schedule and the quantity changed, that SO is **cancelled and
     amended** rather than duplicated.
   - Forecast/Raw rows -> the customer's **Sales Forecast is wholesale
     replaced** (cancel + amend) - a new schedule makes the old forecast
     horizon obsolete.
   - Every changed/added/removed Firm or Forecast line is written to the
     **Revision Log** tab, and rolled up into **Customer Revision Stats**
     (how many times this customer has revised Firm vs Forecast, ever).
   - The live **Auto Material Request is reconciled** - either immediately
     for this customer, or later as part of a scheduled batch across all
     customers, depending on the toggle in Schedule Planning Settings. See
     *Material Request reconciliation* below for exactly what this means.
4. **Cancelling a schedule adjusts the Auto Material Request too** - this
   always runs regardless of the Immediate/Scheduled toggle, since a
   cancellation is exactly the kind of event that should shrink the live MR
   promptly rather than risk over-buying on stale demand.
5. **A shipment with no schedule behind it still gets planned for** - any
   Delivery Note (submit or cancel) also reconciles the live MR, and an item
   with real shipping history but no active Customer Schedule falls back to
   a shipping-history-based forecast so it doesn't silently disappear from
   planning.
6. **Material Planning Dashboard** report - the live, per-item transparency
   view: total demand (and where it came from), stock, open PO, other open
   MR, net shortfall/surplus, and what's currently on the live Auto Material
   Request - see *Material Request reconciliation* below.
7. **Forecast Revision Analysis** report - a per-item pivot across every
   submitted schedule for a customer, colour-coded green/red for
   increase/decrease, with a colour-preserving CSV export.

## Material Request reconciliation (timely purchase, never over-buy)

The core business problem this solves: parts have a 7-month lead time, so
buying has to be timely - but with dozens of customers each revising Firm
and Forecast numbers on their own schedule, it's easy to either miss a
shortfall (stockout risk) or double up on material that's already covered
(cash tied up in excess inventory). This app keeps **one continuously-synced
Draft Material Request** ("Auto Material Request") instead of creating a new
MR document every time something changes:

- Every reconciliation run (a schedule submit/cancel, a Delivery Note
  submit/cancel, the scheduled batch job, or the manual **Generate Material
  Request** button) recomputes net requirement **from scratch, across every
  customer**, for every item that has any demand at all.
- **Demand** = Firm + Forecast/Raw from the latest submitted schedule per
  customer+item+date (a Firm date up to **Overdue Grace Period** days in the
  past still counts, in case it genuinely hasn't shipped yet), **plus** - only
  for an item with no active schedule coverage at all - a forecast derived
  from real shipping history, so a customer who never sends a formal
  schedule doesn't fall out of planning.
- **Supply** already netted against demand = current stock **+ open Purchase
  Order quantity not yet received + any other open Material Request** for
  that item. This is what stops the app from asking for material that's
  already on order or already stocked.
- The live Auto Material Request's lines are then **added, quantity-adjusted,
  or removed** to match the new shortfall picture - and if nothing is short
  any more, the whole MR is removed rather than left sitting there empty.
  Once a human submits/converts it into a real requisition or PO, it's out
  of the app's hands - the next reconciliation starts a fresh one for
  whatever is short after that.
- Every line records **why** it exists (Customer Schedule vs Shipping
  History fallback) in its Demand Source field and description, and the
  **Material Planning Dashboard** report shows the full breakdown (demand,
  stock, open PO, other open MR, net shortfall, what's on the live MR right
  now) for every item - it reads from exactly the same calculation the
  reconciler itself uses, so the dashboard can never show a different answer
  than what the live MR reflects.

Tune **Overdue Grace Period**, **Fall Back to Shipping History...**, and
**Shipping History Window** in Schedule Planning Settings > Material Request
Reconciliation.

### If a Material Request doesn't seem to be created

This is almost always one of two things, both visible immediately since a
failed reconciliation now shows a persistent (not a fleeting toast) message
with the actual reason:

1. **Default Company / Default Warehouse aren't set** in Schedule Planning
   Settings - reconciliation throws immediately and nothing gets created.
   Set both, then use the **Generate Material Request** button to retry.
2. **Nothing is actually short right now** - stock, open POs, or another
   open Material Request already cover the demand. Open the **Material
   Planning Dashboard** report to see the live numbers per item.

## Install

```bash
# from your bench directory
bench get-app schedule_planner /path/to/schedule_planner   # or a git URL
bench --site your-site install-app schedule_planner
bench --site your-site migrate
```

`bench pip install -r apps/schedule_planner/requirements.txt` if bench
doesn't pick up `pandas` / `openpyxl` / `pdfminer.six` / `requests`
automatically - `pdftotext` (poppler-utils) should also be on the server's
PATH for fast PDF text extraction (falls back to pdfminer if missing).

## One-time setup after install

Open **Schedule Planning Settings** (single doctype, System Manager only)
and set:

- **Default Company** / **Default Warehouse** - used for every Sales Order,
  Sales Forecast and Material Request this app creates.
- **Default Lead Time (Months)** / **Default Planning Horizon (Months)** -
  your parts lead time (7, per the brief) and how far ahead the Material
  Request shortfall check should look.
- **Material Request Trigger Mode** - *Immediate* (fires right after each
  customer's schedule is submitted, scoped to that customer) or *Scheduled*
  (one daily/weekly batch job across every customer). Flip this any time -
  no code change needed.
- **AI-Based Schedule Extraction** - set Provider, Model and API Key. This is
  what reads every new customer's file automatically - see the next section.
  Default is **Local (Ollama)** - it runs entirely on your own bench server:
  free, works offline, and no cloud provider can ever retire a model name out
  from under you (see *Running the AI extraction fully locally (Ollama)*
  below for setup). If you'd rather not self-host, **Google Gemini** and
  **Groq** both currently offer a genuinely free hosted API tier (no card
  required, at aistudio.google.com / console.groq.com); Anthropic and OpenAI
  are paid from the first call. Whichever you pick, note that a customer
  stops needing any AI calls at all once their layout is learned (see below)
  - so this only runs occasionally, not on every upload.

A **Customer Schedule Format** record is auto-created the first time you
upload a file for a customer that doesn't have one yet, defaulted to
**Parsing Mode: LLM Extraction**. You never have to create or configure it
yourself, but it's there to review or override.

## Running the AI extraction fully locally (Ollama)

No API key, no internet dependency, nothing that a cloud provider can change
or retire under you - this is the default. It needs [Ollama](https://ollama.com)
installed on the same machine as your bench (works fine inside WSL):

```bash
# inside WSL, same machine as your bench
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:3b-instruct     # small, CPU-friendly - good for 8GB RAM / no GPU
ollama serve                        # leave this running (or let it run as a service)
```

Then in **Schedule Planning Settings**, Provider = `Local (Ollama)`, leave
Model and API Key blank (defaults to `qwen2.5:3b-instruct`, no key needed),
leave Endpoint Override blank too (defaults to
`http://localhost:11434/v1/chat/completions`).

Notes on running CPU-only with limited RAM:

- Extraction can genuinely take **30 seconds to a couple of minutes** per
  file on a modest CPU-only machine (8GB RAM, no GPU) - this is expected.
  Since it only runs for a customer whose layout hasn't been learned yet
  (see *Handling every customer's own format* below), it's an occasional
  cost per customer, not something that slows down every upload.
- If you have more RAM or an NVIDIA GPU, a bigger model gives better
  extraction quality - pull it (`ollama pull llama3.1:8b-instruct-q4_K_M` or
  `ollama pull qwen2.5:7b-instruct`) and set it as the Model in Settings.
- If extraction fails with "Could not reach the local Ollama server",
  `ollama serve` isn't running, or the model in Settings hasn't been pulled
  yet (`ollama pull <model-name>`).
- You can still switch Provider to Google Gemini/Groq/Anthropic/OpenAI any
  time if you'd rather use a hosted API instead.

## Handling every customer's own format (no per-customer setup required)

Every customer sends their schedule in their own shape - some Excel, some
CSV, some PDF, and no two column layouts match. Rather than hand-building a
parser or a column-mapping for every one of them, this app defaults every
new customer to AI-based extraction and *teaches itself* a fast,
deterministic parser once it's confident it can replace the AI safely:

1. **First upload(s) - LLM Extraction.** The file (PDF text, or the raw
   Excel/CSV grid) is sent to the configured LLM, which returns both the
   extracted item/date/quantity/demand-type rows **and** its best guess at a
   reusable layout - which row is the header, which column is the item code,
   which columns are Long-format field mappings or Wide-format weekly date
   buckets with a Firm/Forecast band row above them.
2. **Self-verification, automatically, on the same upload.** That inferred
   layout is immediately re-run through the ordinary free/deterministic
   parser against the *same* file, entirely in memory. If the deterministic
   result closely matches what the AI extracted (row count and total
   quantity both within a tight tolerance), the layout is saved onto that
   customer's Customer Schedule Format and **Parsing Mode flips to Template
   Mapping** - you'll see a green notification confirming it.
3. **Every upload after that is instant, free, and doesn't call the AI at
   all** - it's just the ordinary column-mapping / Wide-layout parser
   running on the learned settings. You can open the format record any time
   to see what was learned, or correct it by hand.
4. If the self-check doesn't match closely enough (an unusual or
   inconsistent layout), nothing is saved - Parsing Mode stays on LLM
   Extraction, so the next upload safely tries the AI again rather than
   locking in a bad guess.

This means **no manual configuration is required for any customer,
including a brand new one you've never uploaded for before** - it works
out of the box the moment an API key is set in Schedule Planning Settings.
Manual configuration remains fully available if you'd rather set it up
yourself up front, or override what was learned:

- **Excel / CSV**: switch Parsing Mode to Template Mapping and either leave
  Column Mapping empty (the parser then guesses columns by header keywords)
  or fill in the mapping table / Wide Layout Settings once you know the
  exact column names.
- **PDF**: switch Parsing Mode to Template Mapping and register a function
  in `PDF_PARSER_REGISTRY` inside `utils/file_parsers.py`, then set that
  customer's **Legacy PDF Parser Key** to match (`techform_v1` ships as a
  worked example for one known automotive-schedule layout). Note PDF layouts
  are not currently self-learned the way Excel/CSV are - a PDF customer
  either uses LLM Extraction on every upload, or a hand-written parser.

## Assumptions worth checking against your site

- **Sales Forecast** field names (`company`, `posting_date`, `from_date`,
  `frequency`, `demand_number`, `parent_warehouse` on the parent;
  `item_code`, `item_name`, `uom`, `delivery_date`, `demand_qty`,
  `warehouse` on both its `selected_items` and `items` child tables) were
  carried over from the original prototype as-is, since that's the only
  ground truth available for what your site's Sales Forecast doctype looks
  like. If it doesn't match, adjust `_append_forecast_items` and
  `_build_sales_forecast_doc` in `utils/sales_documents.py`.
- **Duplicate schedule check** blocks a second upload with the same
  Customer + Release Number + Vendor code while a Draft or Submitted one
  already exists. If you need to re-upload a corrected file for the same
  release, cancel/delete the existing Draft first (or ask and I'll add an
  explicit "replace" flow).
- **Cancelling a Customer Schedule** does *not* auto-cancel the Sales
  Orders/Sales Forecast it generated - by the time someone cancels a
  schedule those may already be invoiced or delivered, so it just warns and
  leaves them for a human to handle. The **Material Request IS adjusted**
  though (see *Material Request reconciliation* above) - unlike a Sales
  Order or Sales Forecast, the live Auto MR is always safe to shrink since
  it's never anything but a Draft the app itself manages.
- **Weekly Material Request runs** are gated inside a single daily scheduler
  hook (only actually executes on Mondays when frequency = Weekly) rather
  than a separate cron entry, so the frequency stays a live Settings toggle.
- **Open PO / open MR netting** uses each line's `qty`/`received_qty` and
  `qty`/`ordered_qty` fields as-is (the transaction UOM, not converted to
  stock UOM) - fine for the common case of one UOM per item, but worth a
  second look if you buy/sell the same item in different units.

## Build a Dashboard/Workspace (2 minutes, one-time, no risk)

A hand-authored Workspace fixture is deliberately **not** shipped in this
app - Frappe's Workspace JSON schema is intricate enough, and version-
sensitive enough, that a hand-rolled one carries a real risk of breaking
`bench migrate` the same way the earlier `tabContact` schema mismatch did.
Building it yourself in the UI is quick and can't corrupt anything:

1. Go to the Awesome Bar and open any existing Workspace (e.g. "Selling"),
   or use the **+** at the bottom of the left sidebar to create a new one
   called **"Schedule Planner"**.
2. Click **Edit**, then **Add Block > Shortcut** and add one shortcut each
   for:
   - **Customer Schedule** (list) - day-to-day upload/review/submit.
   - **Material Planning Dashboard** (report) - the live shortfall/surplus
     view described above - this is the main "what's happening, what's
     coming" screen.
   - **Material Request**, filtered to `Auto-Generated By Schedule Planner
     = Yes` - the live Auto MR itself, and its history once submitted.
   - **Customer Revision Stats** (list) - who keeps changing Firm/Forecast,
     and how often.
   - **Forecast Revision Analysis** (report) - per-item revision pivot.
   - **Customer Schedule Format** (list) - per-customer parsing config.
   - **Schedule Planning Settings** - global defaults.
3. **Save**. That's the whole dashboard - Frappe keeps it as a normal
   Workspace record from here on (editable, shareable, safe to tweak).

## Files

```
schedule_planner/
  hooks.py                          doc_events, scheduler_events, fixtures
  api.py                             whitelisted endpoints for the client script
  tasks.py                           daily scheduler entry point
  fixtures/custom_field.json         custom fields on Sales Order / Sales Forecast /
                                      Material Request / Material Request Item
  utils/
    file_parsers.py                  PDF/Excel/CSV -> row dicts (hybrid parsing)
    forecast_engine.py               the 2 auto-forecast methods + extension logic
    sales_documents.py               SO per-date + amendment, SF wholesale replace
    material_request.py              live Auto MR reconciler + planning snapshot
    revision.py                      diff vs previous schedule, revision stats
    items.py                         auto-create Item if a schedule references a new one
  schedule_planner/doctype/
    customer_schedule/                main submittable document (+ client script)
    customer_schedule_item/           child: item/date/qty/demand rows
    schedule_revision_log/            child: what changed vs the previous schedule
    customer_schedule_format/         per-customer parsing config
    schedule_column_mapping/          child: source column -> target field
    schedule_planning_settings/       single: global defaults & MR trigger mode
    customer_revision_stats/          per-customer revision counters
  schedule_planner/report/
    forecast_revision_analysis/       pivot report + colour CSV export
    material_planning_dashboard/      live demand/supply/shortfall per item
```

## Before going live

Test the full loop on a staging site first: upload one customer's real
schedule file, walk through Draft -> Submit, confirm the Sales Order(s) and
Sales Forecast look right, then upload that same customer's *next* schedule
with a couple of quantities changed and confirm the previous SO/Forecast get
cancelled and amended rather than duplicated, and that Revision Log /
Customer Revision Stats pick up the change.

Also test the Material Request side specifically, since it's the part
purchasing actually acts on: submit a schedule that creates a shortfall and
confirm the Auto Material Request appears with the right quantity; cancel
that schedule and confirm the same MR shrinks or disappears rather than
being left stale; raise a Purchase Order (or another Material Request) for
one of the items and re-run reconciliation (the **Generate Material
Request** button) to confirm the shortfall nets down instead of asking for
the same material twice; and submit a Delivery Note for an item that has no
active schedule at all, to confirm the shipping-history fallback picks it
up. The **Material Planning Dashboard** report is the fastest way to see
all of this at once.
