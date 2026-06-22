# Easybank2OFX
Convert **easybank** (Austria) PDF account statements (*Kontoauszug*) into **OFX**
files for import into personal-finance software such as GnuCash, Nextcloud Budget,
or MoneyMoney.

A single, dependency-light Python script. No web interface, no upload — everything
runs locally on your machine.

## Features

- PDF → **OFX 2.0** (XML); `--legacy-sgml` emits OFX 1.0.2 (SGML)
- **Bulk mode**: point it at a folder (or several files) and get **one merged OFX**
- Correct **year handling** across year boundaries — booking dates in the PDF are only
  `DD.MM`; the year is taken from each statement's date on page 1
- Built-in **validation** (`-v`): per-statement totals, balance-continuity
  (gap detection), and an overall reconciliation against the printed balances
- One `<STMTRS>` block **per statement**, so even multi-year exports import cleanly
  into regex-based parsers (see [Notes](#output--compatibility))
- Readable payee names with full detail in the memo; stable transaction IDs (`FITID`)
  taken from easybank's running document number
- Robust by default: ignores SEPA payment-slip attachments, strips XML-invalid
  characters, respects OFX field-length limits, keeps umlauts as proper UTF-8

## Requirements

- Python 3.8+
- [`pdfplumber`](https://github.com/jsvine/pdfplumber)

```bash
pip install pdfplumber
```

## Usage

```bash
# single statement -> auszug.ofx
python3 easyofx.py auszug.pdf

# whole folder -> one merged OFX (easybank_<YYYYMM>_<YYYYMM>.ofx)
python3 easyofx.py statements/

# mix files and folders, custom output, or stdout
python3 easyofx.py 2024/ jan.pdf feb.pdf -o all.ofx
python3 easyofx.py auszug.pdf -o -

# validation report
python3 easyofx.py statements/ -v

# legacy OFX 1.0.2 (SGML) instead of 2.0 (XML)
python3 easyofx.py auszug.pdf --legacy-sgml
```

### Options

| Argument | Description |
| --- | --- |
| `input` | one or more PDF files and/or folders |
| `-o, --output` | output path, or `-` for stdout (default: `<name>.ofx`, or `easybank_<YYYYMM>_<YYYYMM>.ofx` when merging) |
| `-v, --verbose` | print a validation report |
| `--legacy-sgml` | emit OFX 1.0.2 (SGML) instead of OFX 2.0 (XML) |

## Validation (`-v`)

The converter can check its own output against the totals and balances printed on
each statement, which makes `-v` the quickest way to trust a conversion. Example for
two consecutive statements:

```
Pruefbericht:
  Auszuege:          2
  Buchungen gesamt:  189
  Zeitraum:          03.04.2026 - 02.06.2026

  Pro Auszug:
    ...2026_005.pdf  04.05.2026   108 Buch.  Ein  4369.82  Aus  4193.30  Saldo  823.06  [OK]
    ...2026_006.pdf  02.06.2026    81 Buch.  Ein  5818.18  Aus  5682.23  Saldo  959.01  [OK]

  Lueckenlose Kette: OK  (Endsaldo je Auszug = Anfangssaldo des naechsten)
  Gesamtabgleich:    Anfang 646.54 + Ein 10188.00 - Aus 9875.53 = 959.01   laut letztem Auszug 959.01   OK
```

The **chain check** is the useful one for bulk imports: it confirms each statement's
closing balance equals the next one's opening balance, so a missing month shows up
immediately. If every line says `OK`, nothing was lost.

## How it works

- Each PDF is parsed independently. The **year** comes from the statement date on
  page 1 and is applied to the `DD.MM` booking dates (with year-rollover logic), so
  multi-year merges sort correctly by real date regardless of file name order.
- Transactions are grouped into **one `<STMTRS>` block per statement**, which keeps
  each block small (see [Notes](#output--compatibility)).
- The running document number (`OG/000005176` → `000005176`) becomes the `FITID` —
  stable and unique, which helps de-duplication on re-import.
- The payee `NAME` is derived heuristically (card merchant, transfer counterparty, …);
  the full booking text goes into `MEMO`.

### Safety nets

- Statements from different accounts in one run → it stops (merging would be wrong).
- The same statement twice (same statement date) → the duplicate is skipped.
- A corrupt or unreadable PDF → a warning, and the remaining files still convert.

## Output / compatibility

Output is standard **OFX 2.0** (XML, UTF-8), verified against common OFX parsers and
the [Nextcloud Budget](https://apps.nextcloud.com/apps/budget) importer. Umlauts are
written as real UTF-8 characters rather than numeric entities, which some importers
would otherwise display literally.

> **Note on large files.** Some importers parse OFX with regular expressions and hit a
> backtracking limit on a single very large `<STMTRS>`, then import **0 records** on
> big multi-year files. `easyofx` avoids this by emitting one `<STMTRS>` per statement,
> so even a 5000-transaction export imports cleanly as a single file.

## Limitations

- Parsing is tuned to the easybank statement layout(s) seen so far. If easybank changes
  the layout or you hit an unusual booking type, the `-v` totals are the early-warning
  system — if a line says `ABWEICHUNG` (mismatch), please open an issue and attach the
  statement.
- SEPA payment-slip attachments (*Beilagen*) are intentionally ignored; the counterparty
  printed there is not merged back into the transaction.
- Not affiliated with easybank or BAWAG.

## Privacy

Everything runs locally. The only dependency is `pdfplumber`; the script makes no
network calls and sends your statement data nowhere.

## License

MIT — see `LICENSE` (add one if you haven't yet).
