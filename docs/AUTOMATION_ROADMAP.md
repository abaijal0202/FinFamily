# FinFamily — Automation Roadmap

What's automated today, why it's built the way it is, and the realistic path to
automating more of your portfolio (other banks, stocks, mutual funds) as this
hobby project grows.

---

## What's live now: PDF statement import

**Where:** Sidebar → "Import Statement", or `/statements/import`.

**What it does:** Upload an HDFC consolidated statement PDF → it's OCR'd,
parsed into structured account/FD/transaction data → you review it on screen →
confirm → it creates or updates the matching Bank/FD assets and inserts the
transactions (skipping any already imported). Net worth updates immediately.

**Why OCR instead of reading the PDF's text layer:** this particular export
("Microsoft: Print To PDF") embeds a font without a usable character map — the
PDF's real text layer is empty even though it looks normal on screen. Rasterizing
each page and running Tesseract was the only reliable way to get the data out.
Whole-page OCR with `--psm 6` (force "single uniform text block") turned out to
extract the data reliably — cropping the page into columns is unnecessary. This
was validated end-to-end against a real statement: every account's closing
balance and both FDs matched the source exactly, and the total across accounts
matched the statement's own "Account Relationship Summary" total.

**Why it's a review-then-confirm flow, not silent auto-import:** OCR on a
financial document will occasionally misread a run-together number, and a
wrapped, multi-line transaction narration can confuse column alignment. Rather
than risk a silently wrong balance, any transaction whose amounts couldn't be
confidently read is flagged in red on the preview screen. The account's overall
balance, though, comes from the statement's own SUMMARY block, which is a very
regular fixed layout — that number is not sensitive to the transaction-parsing
edge cases and has been reliable in testing.

**Extending to more banks:** `statement_import/registry.py` maps a bank name
to a parser function. Each bank gets its own `<bank>_parser.py` (a "one
per format" plugin, not a universal parser — bank statement layouts differ too
much for one regex set to handle all of them well). Adding a new bank is:
write a parser returning the same dict shape as `hdfc_parser.parse_hdfc_statement`,
register it, done — the routes, preview UI, and database logic don't change.

---

## Near-term: reducing the "upload a PDF" manual step

These don't require any licensing or business registration — they're just
removing clicks, since this is your own data.

1. **Email-attachment ingestion.** Most Indian banks email the monthly
   e-statement to a registered address automatically. A small script using
   Gmail's API (OAuth, read-only, filtered to a specific label/sender) could
   pull the attachment and POST it to `/statements/import` on a schedule —
   turning "log in monthly and upload" into "runs by itself." This is the
   highest-leverage next step for the bank side specifically.
2. **A folder-watch script.** If you save statements to a fixed folder
   (e.g. via your bank's auto-download or your email client's rules), a
   cron job on the VM can watch that folder and call the import endpoint
   for any new file, then move it to a `processed/` folder.
3. **Scheduling via cron + a CLI import command.** Wrap `apply_parsed_statement`
   in a small `scripts/import_cli.py` that takes a PDF path + owner + bank
   as arguments, so both a cron job and a manual terminal run can use the
   same code path as the web UI.

None of these need Flask-Login or a browser session — they'd call the same
parser/importer modules directly, bypassing the web upload form.

---

## Automating stocks and mutual funds

This is a different kind of automation than bank statements: instead of
periodically importing a document, it's about pulling a **live price/NAV**
and applying it to holdings you already track. Three tiers, roughly in order
of effort:

### Tier 1 — Daily valuation refresh (lowest effort, no broker account needed)
- **Mutual funds:** AMFI publishes a daily NAV file for every scheme in India,
  free and public: `https://www.amfiindia.com/spages/NAVAll.txt`. A daily
  scheduled job can download it, match your folios by scheme name, and update
  each MF asset's `current_value` = units × NAV. This requires you to track
  **units held** rather than just a rupee value, which is a small model change
  (add a `units` field to the Asset model for MF-category rows).
- **Direct equity:** NSE/BSE publish end-of-day price data, and libraries like
  `yfinance` or `nsepython` can fetch a quote for a given ticker without any
  broker account or API key. A daily job could revalue your equity holdings
  the same way (quantity × last price), if you track quantity + average buy
  price rather than just a lump current value.

### Tier 2 — Broker-linked holdings (some setup, still no licensing needed for personal use)
- **Zerodha Kite Connect** (and similar broker APIs) let *you* — as the
  account holder — pull your own holdings, positions, and trade history via
  API with your own API key. This is explicitly designed for individual users
  and third-party apps, not just institutions, and doesn't require the RBI
  Account Aggregator registration that a commercial product would need
  (that requirement is about a company pulling *other people's* data at
  scale — it doesn't apply to you pulling your own data with your own
  credentials). This would replace manual equity entry entirely: holdings,
  quantity, average price, and even realized P&L on sale.
- Kite Connect has a modest annual API subscription fee; some brokers (e.g.
  Upstox) offer free API access. Worth comparing before committing to one.

### Tier 3 — Full account aggregation (the BRD's original vision, not needed for a hobby project)
- The RBI Account Aggregator framework, NSDL/CDSL CAS, and NPS CRA API access
  are what a *commercial* product would need to pull bank/MF/NPS data at
  scale across many users' accounts under a data-sharing license. For a
  single-family hobby tool, this is disproportionate — Tier 1 and 2 get you
  most of the practical benefit (live valuations, no manual re-entry) without
  any registration or compliance overhead. Worth revisiting only if this ever
  became something other users relied on.

---

## Suggested build order

1. ✅ HDFC PDF import (done)
2. Add `units` to the Asset model for MF rows; write the AMFI NAV daily-refresh
   script — this is the best effort-to-value ratio next step, since it needs
   no API keys or broker accounts at all.
3. Add a second bank's PDF parser (whichever bank you use most after HDFC) to
   validate the plugin architecture actually generalizes.
4. Gmail-attachment auto-ingestion for statements, so the "upload" step
   disappears for banks entirely.
5. Kite Connect (or equivalent) integration for equity holdings, once the
   above feels solid.

## A note on running OCR on the VM

The statement-import feature needs two **system** packages the Python
dependencies can't provide — `tesseract-ocr` and `poppler-utils` (for
`pdftoppm`). If deploying this feature to the GCP VM, add this to the
existing setup steps:

```bash
sudo apt install -y tesseract-ocr poppler-utils
```

OCR is CPU-bound and briefly memory-hungry per page; on the `e2-small` VM
size from the deployment guide, expect ~20-40 seconds for an 8-page
statement. If imports feel slow under real use, `e2-medium` gives more
headroom without much added cost.
