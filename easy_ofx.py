#!/usr/bin/env python3
"""
easyofx - Konvertiert easybank-Kontoauszuege (PDF) nach OFX 2.0 (XML).

Schlankes CLI ohne Webinterface. Einzige Abhaengigkeit: pdfplumber.
Standard ist OFX 2.0 (XML); mit --legacy-sgml wird OFX 1.0.2 (SGML) erzeugt.

  python3 easyofx.py auszug.pdf                # -> auszug.ofx
  python3 easyofx.py auszug.pdf -o out.ofx     # eigener Dateiname
  python3 easyofx.py auszug.pdf -o -           # nach stdout
  python3 easyofx.py ordner/                   # alle PDFs -> EIN gemergtes OFX
  python3 easyofx.py ordner/ a.pdf b.pdf       # Dateien und Ordner mischbar
  python3 easyofx.py ordner/ -v                # Aggregat-Pruefbericht (gesamtes Input)

Jeder Auszug bringt sein Jahr von Seite 1 mit (Buchungen stehen nur als TT.MM);
das Jahr wird daher pro Auszug aufgeloest. Beim Mergen wird nach echtem Datum
sortiert - die Dateinamen-Reihenfolge ist egal (wichtig ueber Jahreswechsel).
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    sys.exit("Fehler: pdfplumber fehlt.  Installieren mit:  pip install pdfplumber")


# ---------------------------------------------------------------------------
# Muster fuer das easybank-Layout
# ---------------------------------------------------------------------------
# Buchungstag (DD.MM) am Zeilenanfang -> Beginn einer Buchung
RE_START = re.compile(r"^\d{2}\.\d{2}\s")
# Wert (DD.MM) + Betrag (dt. Format) + optionales '-' (= Soll) am Zeilenende.
# Das '*' nach dem Wertdatum (z.B. Echtzeitueberweisung "28.04*") wird toleriert.
RE_AMOUNT = re.compile(r"(\d{2}\.\d{2})[\s*]+(\d{1,3}(?:\.\d{3})*,\d{2})(-?)\s*$")
# Belegreferenz, z.B. "MC/000005177", "OG/000005176" -> die Ziffern dienen als FITID
RE_REF = re.compile(r"\b([A-Z0-9]{2})/(\d{6,})\b")
# IBAN (ohne Leerzeichen, wie in den Buchungszeilen)
RE_IBAN = re.compile(r"\b([A-Z]{2}\d{2}[A-Z0-9]{11,30})\b")
# Zeile, die nur aus Ziffern / Schraegstrichen / Leerzeichen besteht (Kontonr., kein Name)
RE_NUMERIC_ONLY = re.compile(r"^[\d/ ]+$")
# Verkorkster Druck-/Fusszeilencode ohne Leerzeichen, z.B. "3-1P/20P...KMM40D"
RE_FOOTER_CODE = re.compile(r"^[0-9A-Z/\-]{25,}$")
# Beginn eines SEPA-Zahlungsbelegs ("Beilage") am Auszugsende. Keine Buchungszeile
# beginnt je mit vollem Datum (TT.MM.JJJJ); dazu typische Beleg-Schluesselwoerter.
RE_BEILAGE = re.compile(
    r"^\d{2}\.\d{2}\.\d{4}"
    r"|Zahlungsempf|Zahlungspflicht|Zahlungsgrund|LASTSCHRIFT|UEBERW\.\s?AN"
)
# In XML 1.0 unzulaessige Zeichen (Steuerzeichen ausser \t \n \r). Aus aelteren PDFs
# koennen solche in Buchungstexte geraten und wuerden das GESAMTE OFX-Dokument fuer
# strenge Parser ungueltig machen -> Importer zeigt dann "0 Buchungen".
RE_XML_INVALID = re.compile(
    "[^\u0009\u000A\u000D\u0020-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]"
)

# Feste easybank-Boilerplate-Kopf-/Fusszeilen - institutionell, nicht personenbezogen.
# Die auf JEDEM Blatt wiederholte Kopfzeile "<Name> <IBAN> <Whg> <Datum> <Saldo>" wird
# bewusst NICHT hier, sondern dynamisch ueber die eigene IBAN gefiltert (s.
# parse_transactions) - so bleibt das Muster robust gegen Namens-/Adressaenderungen.
RE_NOISE = re.compile(
    r"^(KONTOAUSZUG|IBAN W|Buch\.-Tag|Ihre ?aktuelle|Summe |Beilagen|"
    r"Neuer Kontostand|Bei R\u00fcckfragen|Reklamationen|ihrer Durchf\u00fchrung|"
    r"BIC:|Dieses ?Konto|Ausnahmen)"
)


def de_to_decimal(num: str, sign: str = "") -> Decimal:
    """'2.561,15' -> Decimal('2561.15');  sign '-' macht negativ (Soll)."""
    value = Decimal(num.replace(".", "").replace(",", "."))
    return -value if sign == "-" else value


def de_date(ddmm: str, stmt_month: int, stmt_year: int) -> date:
    """DD.MM ohne Jahr -> Datum. Jahr aus Auszugsdatum, mit Jahreswechsel-Logik."""
    day, month = int(ddmm[:2]), int(ddmm[3:5])
    year = stmt_year if month <= stmt_month else stmt_year - 1
    return date(year, month, day)


# Breite eines Zeichens NACH dem XML-Escapen (&->&amp; = 5, < / > -> 4 Zeichen).
_ESC_WIDTH = {"&": 5, "<": 4, ">": 4}


def clip(s: str, limit: int) -> str:
    """Kuerzt s so, dass die ESCAPTE Form hoechstens 'limit' Zeichen lang ist, ohne
    eine Entity zu zerschneiden. Noetig, weil OFX-Felder Laengengrenzen haben (NAME 32,
    MEMO 255) und strenge Parser '&amp;' woertlich zaehlen - sonst bricht der Import ab."""
    out, used = [], 0
    for ch in s:
        w = _ESC_WIDTH.get(ch, 1)
        if used + w > limit:
            break
        out.append(ch)
        used += w
    return "".join(out)


@dataclass
class Txn:
    posted: date
    amount: Decimal
    fitid: str
    name: str
    memo: str


@dataclass
class Statement:
    iban: str = ""
    bic: str = ""
    currency: str = "EUR"
    stmt_date: date = None
    old_balance: Decimal = None
    new_balance: Decimal = None
    sum_in: Decimal = None          # "Summe Ein" laut Auszug (zum Abgleich)
    sum_out: Decimal = None         # "Summe Aus" laut Auszug (zum Abgleich)
    source: str = ""                # Quelldatei (Pfad) - fuer Bulk-Berichte
    txns: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# PDF -> Zeilen
# ---------------------------------------------------------------------------
def extract_lines(pdf_path: str) -> list:
    lines = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text(layout=False) or ""
            lines.extend(text.splitlines())
    return lines


# ---------------------------------------------------------------------------
# Kopfdaten (Konto, Salden, Summen, Datum)
# ---------------------------------------------------------------------------
def parse_header(lines: list, stmt: Statement) -> None:
    text = "\n".join(lines)

    m = re.search(r"vom (\d{2})\.(\d{2})\.(\d{4})", text)
    if m:
        stmt.stmt_date = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    m = re.search(r"\b[A-Z]{2}\d{2}(?:\s\d{4}){4,5}\b", text)
    if m:
        stmt.iban = m.group(0).replace(" ", "")

    m = re.search(r"BIC:\s*([A-Z0-9]{8,11})", text)
    if m:
        stmt.bic = m.group(1)

    m = re.search(r"4092\s+([A-Z]{3})\s", text) or re.search(r"\b(EUR|USD|CHF)\b", text)
    if m:
        stmt.currency = m.group(1)

    m = re.search(r"Summe Ein:\s*([\d.]+,\d{2})", text)
    if m:
        stmt.sum_in = de_to_decimal(m.group(1))
    m = re.search(r"Summe Aus:\s*([\d.]+,\d{2})", text)
    if m:
        stmt.sum_out = de_to_decimal(m.group(1))

    # Neuer Kontostand: "zu Ihren Gunsten" = Guthaben (+), "zu unseren Gunsten" = Soll (-)
    m = re.search(r"Neuer Kontostand zu (Ihren|unseren)\s*Gunsten\s*(?:EUR)?\s*([\d.]+,\d{2})", text)
    if m:
        bal = de_to_decimal(m.group(2))
        stmt.new_balance = bal if m.group(1) == "Ihren" else -bal

    # Alter Kontostand am Ende der Kopfzeile: "... 04.05.2026 823,06"
    m = re.search(r"\d{2}\.\d{2}\.\d{4}\s+([\d.]+,\d{2})\s*$", text, re.MULTILINE)
    if m:
        stmt.old_balance = de_to_decimal(m.group(1))


# ---------------------------------------------------------------------------
# Name (Empfaenger/Haendler) aus einer Buchung ableiten
# ---------------------------------------------------------------------------
def derive_name(head: str, cont: list) -> str:
    # Karte erkennen: ueber den Buchungstext ODER eine Haendlerzeile mit '\'-Trenner
    # (Letzteres faengt z.B. Karten-Rueckerstattungen, die als "Gutschrift ..." gebucht sind).
    is_card = (head.startswith(("Bezahlung Karte", "Auszahlung Karte"))
               or any("\\" in line for line in cont))

    if is_card:
        # Haendlerzeile nutzt '\' als Trenner:  HAENDLER\\ORT\PLZ
        for line in cont:
            if "\\" in line:
                merchant = re.split(r"\\+", line)[0].strip()
                if merchant:
                    return merchant
        if head.startswith("Auszahlung"):
            return "Bargeldauszahlung"
        return head or "Kartenzahlung"

    # Ueberweisung / Lastschrift: am IBAN ausrichten, Name steht dahinter oder darunter
    all_lines = [head] + cont
    for i, line in enumerate(all_lines):
        m = RE_IBAN.search(line)
        if not m:
            continue
        trailing = line[m.end():].strip(" ,")
        if trailing:
            return trailing
        for nxt in all_lines[i + 1:]:
            cand = nxt.strip()
            if not cand or RE_IBAN.search(cand) or RE_NUMERIC_ONLY.match(cand):
                continue
            return cand
        break

    return head or "Buchung"


# ---------------------------------------------------------------------------
# Buchungen parsen
# ---------------------------------------------------------------------------
def parse_transactions(lines: list, stmt: Statement) -> None:
    stmt_month = stmt.stmt_date.month
    stmt_year = stmt.stmt_date.year

    # Buchungen in Bloecke schneiden (Start = Datumszeile mit Betrag am Ende)
    # Kompakte Kontonummer (aus dem Header) zum Erkennen der wiederholten
    # Blatt-Kopfzeile, die Name + IBAN + Saldo enthaelt - ohne Name/Adresse fest
    # zu verdrahten. Faellt zurueck auf nichts, falls keine IBAN gefunden wurde.
    acct_compact = stmt.iban

    blocks = []
    current = None
    for line in lines:
        # SEPA-Zahlungsbelege ("Beilagen") haengen nach dem letzten Blatt an und
        # enthalten keine Buchungen mehr -> ab hier nicht weiter sammeln.
        if RE_BEILAGE.search(line.strip()):
            break
        # Wiederholte Blatt-Kopfzeile (enthaelt die eigene IBAN) ueberspringen.
        if acct_compact and acct_compact in line.replace(" ", ""):
            continue
        if RE_NOISE.match(line) or RE_FOOTER_CODE.match(line.strip()):
            continue
        if RE_START.match(line) and RE_AMOUNT.search(line):
            current = [line]
            blocks.append(current)
        elif current is not None and line.strip():
            current.append(line)

    for block in blocks:
        start = block[0]
        cont = block[1:]

        m_amt = RE_AMOUNT.search(start)
        value_date, num, sign = m_amt.groups()
        amount = de_to_decimal(num, sign)

        booking_day = start.split()[0]
        posted = de_date(booking_day, stmt_month, stmt_year)

        # Kopftext = Zeilenanfang ohne Buchungstag, ohne Wert/Betrag, ohne Referenz
        head = start[: m_amt.start()].split(None, 1)
        head = head[1] if len(head) > 1 else ""
        head = RE_REF.sub("", head).strip()

        # FITID aus der Belegreferenz (irgendwo im Block)
        ref_match = RE_REF.search("\n".join(block))
        fitid = ref_match.group(2) if ref_match else f"{posted:%Y%m%d}-{num}-{sign}"

        cont_clean = [RE_REF.sub("", l).strip() for l in cont]
        name = clip(derive_name(head, cont_clean), 32)

        memo_parts = [head] + cont_clean
        memo = " | ".join(p for p in memo_parts if p)
        memo = re.sub(r"\\+", " ", memo)
        memo = clip(re.sub(r"\s+", " ", memo).strip(), 255)

        stmt.txns.append(Txn(posted, amount, fitid, name, memo))


# ---------------------------------------------------------------------------
# OFX 1.0.2 (SGML) erzeugen
# ---------------------------------------------------------------------------
def xml_escape(s: str) -> str:
    # Erst in XML unzulaessige Steuerzeichen entfernen (sonst wird das ganze Dokument
    # ungueltig), dann nur XML-Sonderzeichen escapen. Umlaute bleiben echtes UTF-8.
    s = RE_XML_INVALID.sub("", s)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_amt(d: Decimal) -> str:
    return f"{d:.2f}"


def build_ofx(statements, version: int = 200) -> str:
    """OFX erzeugen. 'statements' ist eine Liste von Auszuegen (gleiches Konto) - JE Auszug
    ein eigener <STMTRS>-Block. Das haelt jeden Block klein, damit regex-basierte Importer
    (z.B. die Nextcloud-Budget-App) nicht am PCRE-Backtrack-Limit scheitern und 0 Buchungen
    liefern. Ein Einzelauszug ist auch direkt erlaubt.
    version=200 -> OFX 2.0 (XML, schliessende Tags); 102 -> 1.0.2 (SGML).
    Datumsfelder mit vollem Zeitstempel (12:00)."""
    if isinstance(statements, Statement):
        statements = [statements]
    statements = sorted(statements, key=lambda s: s.stmt_date)
    server = statements[-1].stmt_date

    def dt(d):
        return f"{d:%Y%m%d}120000"

    # Blattelement: in OFX 2.0 mit schliessendem Tag, in 1.0.2 (SGML) ohne.
    def leaf(tag, value):
        v = xml_escape(str(value))
        return f"<{tag}>{v}</{tag}>" if version >= 200 else f"<{tag}>{v}"

    out = []
    if version >= 200:
        out.append('<?xml version="1.0" encoding="UTF-8"?>')
        out.append('<?OFX OFXHEADER="200" VERSION="200" SECURITY="NONE" '
                   'OLDFILEUID="NONE" NEWFILEUID="NONE"?>')
    else:
        out += ["OFXHEADER:100", "DATA:OFXSGML", "VERSION:102", "SECURITY:NONE",
                "ENCODING:UTF-8", "CHARSET:NONE", "COMPRESSION:NONE",
                "OLDFILEUID:NONE", "NEWFILEUID:NONE", ""]

    out.append("<OFX>")
    out.append("<SIGNONMSGSRSV1><SONRS>")
    out.append("<STATUS>" + leaf("CODE", 0) + leaf("SEVERITY", "INFO") + "</STATUS>")
    out.append(leaf("DTSERVER", dt(server)))
    out.append(leaf("LANGUAGE", "GER"))
    out.append("</SONRS></SIGNONMSGSRSV1>")
    out.append("<BANKMSGSRSV1>")

    for idx, stmt in enumerate(statements, 1):
        txns = sorted(stmt.txns, key=lambda t: (t.posted, t.fitid))
        dates = [t.posted for t in txns]
        dtstart = min(dates) if dates else stmt.stmt_date
        dtend = max(dates) if dates else stmt.stmt_date

        out.append("<STMTTRNRS>")
        out.append(leaf("TRNUID", idx))
        out.append("<STATUS>" + leaf("CODE", 0) + leaf("SEVERITY", "INFO") + "</STATUS>")
        out.append("<STMTRS>")
        out.append(leaf("CURDEF", stmt.currency))
        out.append("<BANKACCTFROM>")
        out.append(leaf("BANKID", stmt.bic))
        out.append(leaf("ACCTID", stmt.iban))
        out.append(leaf("ACCTTYPE", "CHECKING"))
        out.append("</BANKACCTFROM>")
        out.append("<BANKTRANLIST>")
        out.append(leaf("DTSTART", dt(dtstart)))
        out.append(leaf("DTEND", dt(dtend)))
        for t in txns:
            out.append("<STMTTRN>")
            out.append(leaf("TRNTYPE", "CREDIT" if t.amount >= 0 else "DEBIT"))
            out.append(leaf("DTPOSTED", dt(t.posted)))
            out.append(leaf("TRNAMT", fmt_amt(t.amount)))
            out.append(leaf("FITID", t.fitid))
            out.append(leaf("NAME", t.name))
            if t.memo:
                out.append(leaf("MEMO", t.memo))
            out.append("</STMTTRN>")
        out.append("</BANKTRANLIST>")
        if stmt.new_balance is not None:
            out.append("<LEDGERBAL>")
            out.append(leaf("BALAMT", fmt_amt(stmt.new_balance)))
            out.append(leaf("DTASOF", dt(stmt.stmt_date)))
            out.append("</LEDGERBAL>")
        out.append("</STMTRS></STMTTRNRS>")

    out.append("</BANKMSGSRSV1>")
    out.append("</OFX>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Pruefbericht (Summenabgleich)
# ---------------------------------------------------------------------------
def validation_report(stmt: Statement) -> str:
    credits = sum((t.amount for t in stmt.txns if t.amount > 0), Decimal(0))
    debits = sum((-t.amount for t in stmt.txns if t.amount < 0), Decimal(0))
    lines = [f"  Buchungen:        {len(stmt.txns)}"]

    def check(label, got, exp):
        if exp is None:
            return f"  {label:<18}{got:>12.2f}   (kein Sollwert im PDF)"
        ok = "OK" if abs(got - exp) < Decimal("0.005") else "ABWEICHUNG!"
        return f"  {label:<18}{got:>12.2f}   laut PDF {exp:>10.2f}   {ok}"

    lines.append(check("Summe Ein:", credits, stmt.sum_in))
    lines.append(check("Summe Aus:", debits, stmt.sum_out))
    if stmt.old_balance is not None and stmt.new_balance is not None:
        computed = stmt.old_balance + credits - debits
        ok = "OK" if abs(computed - stmt.new_balance) < Decimal("0.005") else "ABWEICHUNG!"
        lines.append(
            f"  Endsaldo (berech.){computed:>12.2f}   laut PDF "
            f"{stmt.new_balance:>10.2f}   {ok}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Aggregat-Pruefbericht (mehrere Auszuege)
# ---------------------------------------------------------------------------
def _sums(stmt: Statement):
    c = sum((t.amount for t in stmt.txns if t.amount > 0), Decimal(0))
    d = sum((-t.amount for t in stmt.txns if t.amount < 0), Decimal(0))
    return c, d


def _stmt_ok(stmt: Statement, c: Decimal, d: Decimal) -> bool:
    tol = Decimal("0.005")
    if stmt.sum_in is not None and abs(c - stmt.sum_in) >= tol:
        return False
    if stmt.sum_out is not None and abs(d - stmt.sum_out) >= tol:
        return False
    if stmt.old_balance is not None and stmt.new_balance is not None:
        if abs(stmt.old_balance + c - d - stmt.new_balance) >= tol:
            return False
    return True


def aggregate_report(statements: list) -> str:
    statements = sorted(statements, key=lambda s: s.stmt_date)
    tol = Decimal("0.005")
    total_c = total_d = Decimal(0)

    out = [f"  Auszuege:          {len(statements)}",
           f"  Buchungen gesamt:  {sum(len(s.txns) for s in statements)}",
           f"  Zeitraum:          {min(t.posted for s in statements for t in s.txns):%d.%m.%Y}"
           f" - {max(t.posted for s in statements for t in s.txns):%d.%m.%Y}",
           "",
           "  Pro Auszug:"]
    for s in statements:
        c, d = _sums(s)
        total_c += c
        total_d += d
        bal = f"{s.new_balance:.2f}" if s.new_balance is not None else "?"
        flag = "OK" if _stmt_ok(s, c, d) else "ABWEICHUNG!"
        out.append(f"    {os.path.basename(s.source)[:34]:<34} {s.stmt_date:%d.%m.%Y}"
                   f"  {len(s.txns):>4} Buch.  Ein {c:>9.2f}  Aus {d:>9.2f}"
                   f"  Saldo {bal:>9}  [{flag}]")

    # Lueckenlose Kette: Endsaldo Auszug N == Anfangssaldo Auszug N+1
    if len(statements) > 1:
        out.append("")
        gaps = [(a, b) for a, b in zip(statements, statements[1:])
                if a.new_balance is not None and b.old_balance is not None
                and abs(a.new_balance - b.old_balance) >= tol]
        if not gaps:
            out.append("  Lueckenlose Kette: OK  (Endsaldo je Auszug = Anfangssaldo des naechsten)")
        else:
            out.append("  Lueckenlose Kette: ABWEICHUNG - moeglicher fehlender/falscher Auszug:")
            for a, b in gaps:
                out.append(f"      {a.stmt_date:%d.%m.%Y} (Endsaldo {a.new_balance:.2f})"
                           f" -> {b.stmt_date:%d.%m.%Y} (Anfang {b.old_balance:.2f})")

    # Gesamtabgleich ueber alle Auszuege
    first, last = statements[0], statements[-1]
    if first.old_balance is not None and last.new_balance is not None:
        computed = first.old_balance + total_c - total_d
        ok = "OK" if abs(computed - last.new_balance) < tol else "ABWEICHUNG!"
        out.append("")
        out.append(f"  Gesamtabgleich:    Anfang {first.old_balance:.2f}"
                   f" + Ein {total_c:.2f} - Aus {total_d:.2f} = {computed:.2f}"
                   f"   laut letztem Auszug {last.new_balance:.2f}   {ok}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Einlesen / Sammeln / Mergen
# ---------------------------------------------------------------------------
def parse_pdf(path: str) -> Statement:
    """Ein PDF zu einem Statement parsen. Wirft ValueError bei Problemen."""
    lines = extract_lines(path)
    stmt = Statement(source=path)
    parse_header(lines, stmt)
    if stmt.stmt_date is None:
        raise ValueError("kein Auszugsdatum ('vom TT.MM.JJJJ') gefunden")
    parse_transactions(lines, stmt)
    return stmt


def collect_pdfs(inputs: list) -> list:
    """Eingaben (Dateien und/oder Ordner) zu einer sortierten PDF-Liste aufloesen."""
    pdfs = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            pdfs += sorted(str(x) for x in p.iterdir()
                           if x.is_file() and x.suffix.lower() == ".pdf")
        elif p.is_file():
            pdfs.append(str(p))
        else:
            print(f"Warnung: '{item}' nicht gefunden - uebersprungen", file=sys.stderr)
    return pdfs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="easybank-Kontoauszuege (PDF) -> OFX 1.0.2 konvertieren. "
                    "Mehrere Dateien/Ordner werden zu EINEM OFX gemergt."
    )
    ap.add_argument("input", nargs="+",
                    help="PDF-Datei(en) und/oder Ordner mit easybank-Kontoauszuegen")
    ap.add_argument("-o", "--output",
                    help="Ausgabedatei (.ofx) oder '-' fuer stdout. Standard: bei einer "
                         "Datei gleicher Name mit .ofx, sonst easybank_<von>_<bis>.ofx")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Pruefbericht; bei mehreren Auszuegen aggregiert ueber das gesamte Input")
    ap.add_argument("--legacy-sgml", action="store_true",
                    help="OFX 1.0.2 (SGML) statt des Standards OFX 2.0 (XML) erzeugen")
    args = ap.parse_args()

    pdfs = collect_pdfs(args.input)
    if not pdfs:
        sys.exit("Fehler: keine PDF-Dateien gefunden.")

    statements = []
    for path in pdfs:
        try:
            stmt = parse_pdf(path)
        except Exception as exc:                       # robust: defekte Datei nicht fatal
            print(f"Warnung: {os.path.basename(path)} uebersprungen ({exc})", file=sys.stderr)
            continue
        if not stmt.txns:
            print(f"Warnung: {os.path.basename(path)}: keine Buchungen - uebersprungen",
                  file=sys.stderr)
            continue
        statements.append(stmt)

    if not statements:
        sys.exit("Fehler: keine gueltigen Auszuege erkannt.")

    # Nur ein Konto zulassen (Mergen verschiedener Konten waere falsch)
    ibans = {s.iban for s in statements if s.iban}
    if len(ibans) > 1:
        sys.exit("Fehler: Auszuege gehoeren zu verschiedenen Konten:\n  "
                 + "\n  ".join(sorted(ibans))
                 + "\nBitte Konten getrennt verarbeiten.")

    # Doppelte Auszuege (gleiches Auszugsdatum) entfernen -> keine Doppelbuchungen
    unique, seen = [], {}
    for s in sorted(statements, key=lambda s: s.stmt_date):
        if s.stmt_date in seen:
            print(f"Warnung: {os.path.basename(s.source)}: gleiches Auszugsdatum wie "
                  f"{os.path.basename(seen[s.stmt_date])} - uebersprungen", file=sys.stderr)
            continue
        seen[s.stmt_date] = s.source
        unique.append(s)
    statements = unique

    ofx = build_ofx(statements, version=102 if args.legacy_sgml else 200)

    if args.output == "-":
        sys.stdout.write(ofx)
    else:
        if args.output:
            out_path = args.output
        elif len(statements) == 1:
            out_path = re.sub(r"\.pdf$", "", statements[0].source, flags=re.I) + ".ofx"
        else:
            lo = min(s.stmt_date for s in statements)
            hi = max(s.stmt_date for s in statements)
            out_path = f"easybank_{lo:%Y%m}_{hi:%Y%m}.ofx"
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(ofx)
        print(f"{sum(len(s.txns) for s in statements)} Buchungen aus "
              f"{len(statements)} Auszug(en) -> {out_path}",
              file=sys.stderr)

    if args.verbose:
        print("\nPruefbericht:", file=sys.stderr)
        if len(statements) == 1:
            print(validation_report(statements[0]), file=sys.stderr)
        else:
            print(aggregate_report(statements), file=sys.stderr)


if __name__ == "__main__":
    main()