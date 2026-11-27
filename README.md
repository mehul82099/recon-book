# recon-book

Offline bank statement reconciliation ledger for small businesses. Import a
bank statement CSV and your own business records (invoices you expect money in
for, expenses you expect money out for), auto-match them one-to-one, and see
exactly what is left unmatched on both sides: the bank line nobody can
explain, and the invoice nobody has paid.

Zero dependencies. Python 3.9+. Single file. Data lives in one JSON store
written with atomic replace.

## Why this exists

Razorpay's [Fix My Itch](https://razorpay.com/m/fix-my-itch/) list includes
the problem "How do users reconcile bank statement transactions monthly?"
(Payment Issues, itch score 79.5, entry re-verified live on the itch page on
2026-11-27). The list is AI-curated from crowdsourced problems; inclusion is
not proof of market demand.

Most reconciliation options for Indian small businesses are full accounting
suites or paid SaaS. This repo is the smaller thing: a free, offline command
line tool for someone who already exports statements and keeps their own
records in a spreadsheet, and just wants the monthly "what does not match"
answer without signing up for anything.

## Landscape (checked 2026-11-27)

A web search on 2026-11-27 for bank reconciliation software for Indian small
businesses surfaced several commercial options: TallyPrime (connected banking
/ auto bank reconciliation inside a full accounting suite), Busy, WIMOnline,
XoomAI Books, Finkitaabh, Autroid, and others. Reconciliation as a feature is
well covered by paid and suite products; this repo makes no uniqueness claim.
It differs only in scope: free, offline, single file, no account aggregation,
no GST or bookkeeping features, CSV in and CSV out.

## Install

```sh
curl -O https://raw.githubusercontent.com/mehul82099/recon-book/main/recon_book.py
python3 recon_book.py --help
```

No pip install, no dependencies. Python 3.9 or newer.

## Input formats

Statement CSV (money in is positive, money out is negative):

```csv
date,description,amount,reference
2026-11-02,NEFT from Sharma Traders,5000.00,
2026-11-03,UPI from walk-in customer,350.00,
2026-11-04,Payment to Verma Suppliers,-2200.00,
2026-11-20,UNKNOWN UPI CREDIT,777.00,
```

Records CSV (`direction` is `in` for receivables, `out` for payables; amounts
are always positive):

```csv
date,party,amount,direction,reference
2026-11-01,Sharma Traders,5000.00,in,INV-101
2026-11-03,Walk-in sales,350.00,in,DAY-BOOK
2026-11-04,Verma Suppliers,2200.00,out,PO-77
2026-11-05,Unpaid Customer,9000.00,in,INV-300
```

The `reference` column is optional in both files.

## Usage

The demo below was run verbatim against the two CSVs above.

```console
$ python3 recon_book.py --db demo.json import-statement statement.csv
imported 4 statement line(s) into demo.json
$ python3 recon_book.py --db demo.json import-records records.csv
imported 4 record(s) into demo.json
$ python3 recon_book.py --db demo.json match
created 3 new match(es)
$ python3 recon_book.py --db demo.json status
statements: 4 (3 matched, 1 unmatched)
records: 4 (3 matched, 1 unmatched)
$ python3 recon_book.py --db demo.json report
recon-book report
matches: 3
  M1: S2 2026-11-03 (350.00 UPI from walk-in customer) <-> R2 2026-11-03 (Walk-in sales, 350.00)
  M2: S3 2026-11-04 (-2200.00 Payment to Verma Suppliers) <-> R3 2026-11-04 (Verma Suppliers, 2200.00)
  M3: S1 2026-11-02 (5000.00 NEFT from Sharma Traders) <-> R1 2026-11-01 (Sharma Traders, 5000.00)
unmatched statement lines: 1
  S4 2026-11-20 777.00 UNKNOWN UPI CREDIT
unmatched records: 1
  R4 2026-11-05 in Unpaid Customer (9000.00)
```

Export every line with its match status for a spreadsheet:

```console
$ python3 recon_book.py --db demo.json export out.csv
wrote out.csv
```

A wrong automatic match can be undone; both sides become unmatched again:

```console
$ python3 recon_book.py --db demo.json unmatch M1
removed match M1
```

## How matching works

Matching is greedy and one-to-one. A statement line and a record are eligible
when:

- direction agrees (money in vs `in`, money out vs `out`),
- absolute amounts differ by at most `--tolerance` rupees (default `0`),
- dates are at most `--days` apart on either side (default `3`).

Among eligible pairs, a record whose reference appears inside the statement
description wins; then the smaller amount gap, then the smaller date gap, then
the lower record id (so ties are deterministic). Each side is used at most
once per run.

Looser runs are opt-in, for example bank charges that shave a few rupees:

```sh
python3 recon_book.py --db demo.json match --tolerance 10 --days 5
```

## Limits

- One-to-one matches only. A single bank line that settles several invoices
  (or a split payment across two bank lines) will not auto-match; match what
  you can and read the unmatched lists.
- No bank, UPI or accounting-software integration. You export the CSVs
  yourself.
- The greedy matcher optimizes each pair locally, not the best global
  assignment; ambiguous batches deserve a look at the unmatched lists.
- This is an organizer, not accounting, tax or GST advice.
- Durability is limited to the tested behavior: the JSON store is written by
  atomic replace, so a failed write does not truncate the previous store. Keep
  your own backups; nothing else is guaranteed.

## Tests

```sh
python3 -m unittest -v
```

31 tests cover exact/tolerance/window matching, direction handling, the
reference boost and deterministic tie-breaks, greedy one-to-one assignment,
unmatch and idempotent re-matching, status/report/export output, CSV
validation errors, paise precision, corrupt-store rejection and atomic-write
behavior. CI runs the same suite on Ubuntu and Windows with Python 3.9 and
3.13.

## License

MIT. Copyright (c) 2026 Mehul Jhabak. See [LICENSE](LICENSE).
