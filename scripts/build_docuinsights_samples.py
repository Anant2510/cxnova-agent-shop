"""Builds the synthetic sample documents of the DocuInsights profiles (src/docuinsights/samples/<profile>/).

    python scripts/build_docuinsights_samples.py        needs reportlab; pdftoppm (poppler) for the one scanned sample

Every company, reference, account and amount below is invented for the demonstration. The documents are layouts to
classify and extract from, aligned into one coherent transaction per profile, with deliberate variants (a missing
code, a wrong code, a short payment, a mismatched waybill) that the sample cases in the profile combine.
The generated files are committed, so the accelerator runs without this script.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parents[1] / "src" / "docuinsights" / "samples"
NOTE = "SYNTHETIC DEMONSTRATION DOCUMENT - invented parties, references and amounts"


def pdf(path: Path, org: str, title: str, lines: list[str | tuple[str, str]], table: list[list[str]] | None = None, widths: list[float] | None = None) -> str:
    """One page: letterhead, title, 'Label: value' lines (a str is a paragraph or a blank line), then an optional table. Returns the text as printed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    c.setTitle(title)
    w, h = A4
    y = h - 22 * mm
    c.setFont("Helvetica-Bold", 11)
    c.drawString(20 * mm, y, org)
    c.setFont("Helvetica-Bold", 17)
    y -= 12 * mm
    c.drawString(20 * mm, y, title)
    c.setLineWidth(0.8)
    c.line(20 * mm, y - 3 * mm, w - 20 * mm, y - 3 * mm)
    y -= 12 * mm
    text = [org, title]
    for ln in lines:
        if isinstance(ln, tuple):
            c.setFont("Helvetica-Bold", 9.5)
            c.drawString(20 * mm, y, f"{ln[0]}:")
            c.setFont("Helvetica", 9.5)
            c.drawString(20 * mm + c.stringWidth(f"{ln[0]}:   ", "Helvetica-Bold", 9.5), y, ln[1])   # a gap wide enough to be read back as a space
            text.append(f"{ln[0]}: {ln[1]}")
        elif ln:
            c.setFont("Helvetica-Oblique", 9)
            c.drawString(20 * mm, y, ln)
            text.append(ln)
        y -= 6.2 * mm
    if table:
        widths = widths or [170 / len(table[0])] * len(table[0])
        y -= 2 * mm
        for r, row in enumerate(table):
            x = 20 * mm
            c.setFont("Helvetica-Bold" if r == 0 else "Helvetica", 8.5)
            for cell, cw in zip(row, widths):
                c.drawString(x, y, cell)
                x += cw * mm
            if r == 0:
                c.line(20 * mm, y - 1.5 * mm, w - 20 * mm, y - 1.5 * mm)
            text.append("   ".join(row))
            y -= 5.6 * mm
    c.setFont("Helvetica", 7)
    c.drawString(20 * mm, 12 * mm, NOTE)
    c.save()
    return "\n".join(text) + "\n"


def scan(pdf_path: Path, png_path: Path, text: str) -> None:
    """A page image of a PDF (as a scan would arrive) and the transcription kept beside it for runs without a model or OCR engine."""
    try:
        subprocess.run(["pdftoppm", "-png", "-r", "100", "-gray", "-singlefile", str(pdf_path), str(png_path.with_suffix(""))], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"  (no scan for {png_path.name}: pdftoppm unavailable: {exc})", file=sys.stderr)
        return
    png_path.with_name(png_path.name + ".ocr.txt").write_text(text, encoding="utf-8")
    pdf_path.unlink()


# ------------------------------------------------------------------------------------------------ customs claim
def customs() -> None:
    out = OUT / "customs_claim"
    seller = "Nordlager Precision GmbH, Industriestrasse 14, 70565 Stuttgart, Germany"
    buyer = "Brightwell Motion Components Ltd, Unit 4 Canal Road, Coventry CV6 5QT, United Kingdom"
    agent = "Kestrel Freight Forwarding Ltd, Cargo Centre, Birmingham B26 3QJ, United Kingdom"
    goods = "Deep groove ball bearings, single row, sealed, designation 6206-2RS"
    rows = [["Line", "Description", "Qty", "Unit price", "Amount"], ["1", "Deep groove ball bearing 6206-2RS (30 x 62 x 16 mm)", "2,400", "6.25", "15,000.00"],
            ["", "Air freight and handling", "", "", "420.00"]]

    def invoice(name: str, code: str | None) -> None:
        lines: list[str | tuple[str, str]] = [("Invoice No", "NP-2026-1187"), ("Invoice Date", "21 October 2026"), ("Seller", seller), ("Buyer", buyer), "",
                                              ("Description of Goods", goods)]
        if code:
            lines.append(("Commodity Code", code))
        lines += [("Country of Origin", "Germany"), ("Quantity", "2,400 pieces"), ("Unit Price", "GBP 6.25"), ("Goods Value", "GBP 15,000.00"), ("Freight", "GBP 420.00"),
                  ("Invoice Total", "GBP 15,420.00"), ("Terms of Sale", "CPT Birmingham Airport (Incoterms 2020)"), ("Payment Terms", "Bank transfer within 14 days"), ""]
        pdf(out / name, "Nordlager Precision GmbH", "COMMERCIAL INVOICE", lines, rows, [14, 96, 18, 22, 20])

    invoice("invoice.pdf", "8482 10 90 00")
    invoice("invoice_no_code.pdf", None)
    invoice("invoice_wrong_code.pdf", "8482 20 00 00")

    pdf(out / "declaration.pdf", "Customs declaration service - trader copy", "IMPORT DECLARATION EXTRACT", [
        ("Movement Reference Number", "26GB7H3K9Q2M4X1A08"), ("Acceptance Date", "3 November 2026"), ("Declaration Item", "1 of 1"), ("Procedure Code", "4000 000"),
        ("Importer", buyer), ("Exporter", seller), "", ("Goods Description", "Ball bearings, deep groove, single row, 6206-2RS"), ("Commodity Code", "8482109000"),
        ("Country of Origin", "Germany"), ("Customs Value", "GBP 15,420.00"), ("Duty Paid", "GBP 1,233.60"), ("Invoice Reference", "NP-2026-1187"),
        ("Packages", "12 cartons"), ("Gross Mass", "486.0 kg")])

    def waybill(name: str, consignee: str, pieces: str) -> None:
        pdf(out / name, "Aerolane Cargo", "AIR WAYBILL - NOT NEGOTIABLE", [
            ("Master AWB No", "417-20264471"), ("Shipper", seller), ("Consignee", consignee), ("Issuing Carrier's Agent", agent), "",
            ("Airport of Departure", "Stuttgart (STR)"), ("Airport of Destination", "Birmingham (BHX)"), ("No. of Pieces", pieces), ("Gross Weight", "486.0 kg"),
            ("Chargeable Weight", "486.0 kg"), ("Nature and Quantity of Goods", f"Ball bearings 6206-2RS, {pieces} cartons, said to contain 2,400 pieces"),
            ("Handling Information", "Keep dry. Notify consignee on arrival."), ("Executed on", "28 October 2026"), ("Place of Execution", "Stuttgart")])

    waybill("air_waybill.pdf", buyer, "12")
    waybill("air_waybill_mismatch.pdf", "Brightwell Motion Parts Ltd, Unit 4 Canal Road, Coventry CV6 5QT, United Kingdom", "10")

    text = pdf(out / "packing_list.pdf", "Nordlager Precision GmbH", "PACKING LIST", [
        ("Packing List No", "PL-2026-1187"), ("Invoice No", "NP-2026-1187"), ("Shipper", seller), ("Consignee", buyer), "",
        ("Description of Goods", "Deep groove ball bearings 6206-2RS, sealed"), ("Total Packages", "12 cartons"), ("Total Quantity", "2,400 pieces"),
        ("Total Gross Weight", "486.0 kg"), ("Total Net Weight", "451.2 kg"), ("Dimensions per Carton", "400 x 300 x 250 mm"), ("Marks and Numbers", "BMC / NP-1187 / 1-12"), ""],
        [["Carton no", "Contents", "Pieces", "Gross kg"], ["1 to 12", "6206-2RS ball bearings, 200 per carton", "2,400", "486.0"]], [30, 90, 25, 25])
    scan(out / "packing_list.pdf", out / "packing_list.png", text)

    def payment(name: str, amount: str) -> None:
        pdf(out / name, "Wessex Commercial Bank", "PAYMENT ADVICE - STATEMENT OF ACCOUNT EXTRACT", [
            ("Account Holder", buyer), ("Account", "Business current account ending 4417"), ("Beneficiary", seller), ("Transaction Date", "30 October 2026"),
            ("Value Date", "30 October 2026"), ("Amount Paid", amount), ("Payment Reference", "INV NP-2026-1187"), ("Payment Type", "International transfer (debit)"), ""],
            [["Date", "Narrative", "Debit", "Credit"], ["30 Oct 2026", "TRANSFER NORDLAGER PRECISION GMBH INV NP-2026-1187", amount.replace("GBP ", ""), ""]], [26, 96, 26, 22])

    payment("proof_of_payment.pdf", "GBP 15,420.00")
    payment("proof_of_payment_short.pdf", "GBP 14,520.00")

    pdf(out / "data_sheet.pdf", "Nordlager Precision GmbH", "TECHNICAL DATA SHEET", [
        ("Manufacturer", "Nordlager Precision GmbH, Stuttgart, Germany"), ("Product Name", "Deep groove ball bearing 6206-2RS"), ("Designation", "6206-2RS"),
        ("Bearing Type", "Deep groove ball bearing, single row, sealed both sides"), ("Material", "Chrome steel 100Cr6 rings and balls, steel cage, nitrile seals"), "",
        "Dimensions", ("Bore Diameter", "30 mm"), ("Outside Diameter", "62 mm"), ("Width", "16 mm"), "",
        "Performance", ("Basic Dynamic Load Rating", "19.5 kN"), ("Limiting Speed", "7,500 rpm"), ("Operating Temperature", "-30 to +110 degrees C"),
        ("Intended Use", "General industrial machinery, pumps, gearboxes and conveyors"), ("Standards", "ISO 15, ISO 492 tolerance class Normal")])


# ------------------------------------------------------------------------------------------------ trade finance
def trade_finance() -> None:
    out = OUT / "trade_finance"
    applicant = "Harlow & Finch Home Textiles Ltd, 18 Mill Lane, Leeds LS11 9AT, United Kingdom"
    beneficiary = "Sundara Weaves Exports Pvt Ltd, Plot 22 Textile Park, Karur 639002, India"
    goods = "100% cotton woven bed linen sets, 300 thread count, assorted colours"
    pdf(out / "letter_of_credit.pdf", "Wessex Commercial Bank - Trade Services", "DOCUMENTARY CREDIT ADVICE", [
        ("Documentary Credit Number", "WCB-LC-2026-00731"), ("Date of Issue", "2 September 2026"), ("Applicant", applicant), ("Beneficiary", beneficiary), "",
        ("Credit Amount", "USD 84,000.00"), ("Tolerance", "0 percent"), ("Latest Shipment Date", "20 October 2026"), ("Expiry Date", "10 November 2026"),
        ("Presentation Period", "21 days after the date of shipment"), ("Port of Loading", "Chennai, India"), ("Port of Discharge", "Felixstowe, United Kingdom"),
        ("Description of Goods", goods), ("Partial Shipments", "Not allowed"),
        ("Documents Required", "Commercial invoice, bill of lading, packing list, insurance certificate, certificate of origin")])
    pdf(out / "invoice.pdf", "Sundara Weaves Exports Pvt Ltd", "COMMERCIAL INVOICE", [
        ("Invoice No", "SWE/26-27/0458"), ("Invoice Date", "19 October 2026"), ("Seller", beneficiary), ("Buyer", applicant), ("Documentary Credit Number", "WCB-LC-2026-00731"), "",
        ("Description of Goods", goods), ("Quantity", "4,300 sets"), ("Unit Price", "USD 20.00"), ("Invoice Total", "USD 86,000.00"), ("Terms of Sale", "CIF Felixstowe (Incoterms 2020)"),
        ("Country of Origin", "India")])
    pdf(out / "bill_of_lading.pdf", "Coromandel Ocean Lines", "BILL OF LADING", [
        ("B/L No", "COL-CHE-FXT-261044"), ("Carrier", "Coromandel Ocean Lines, Chennai, India"), ("Shipper", beneficiary), ("Consignee", "To the order of Wessex Commercial Bank"),
        ("Notify Party", applicant), ("Vessel / Voyage", "MV Bay Meridian / 044W"), ("Port of Loading", "Chennai, India"), ("Port of Discharge", "Felixstowe, United Kingdom"),
        ("Container No", "COLU 482913-6"), ("No. of Packages", "215 cartons"), ("Gross Weight", "6,880.0 kg"),
        ("Description of Goods", "215 cartons cotton woven bed linen sets"), ("Shipped on Board", "24 October 2026"), ("Freight", "Prepaid")])
    # the same shipment under a carrier and a vessel that match the invented demonstration watchlist (the carrier through a different spelling)
    pdf(out / "bill_of_lading_listed.pdf", "Quelmar Shipping Line", "BILL OF LADING", [
        ("B/L No", "QSL-CHE-FXT-261107"), ("Carrier", "Quelmar Shipping Line Ltd, Harbour Road, Chennai, India"), ("Shipper", beneficiary), ("Consignee", "To the order of Wessex Commercial Bank"),
        ("Notify Party", applicant), ("Vessel / Voyage", "MV Tiberon Star / 117W"), ("Port of Loading", "Chennai, India"), ("Port of Discharge", "Felixstowe, United Kingdom"),
        ("Container No", "QSLU 771204-3"), ("No. of Packages", "215 cartons"), ("Gross Weight", "6,880.0 kg"),
        ("Description of Goods", "215 cartons cotton woven bed linen sets"), ("Shipped on Board", "24 October 2026"), ("Freight", "Prepaid")])
    pdf(out / "packing_list.pdf", "Sundara Weaves Exports Pvt Ltd", "PACKING LIST", [
        ("Packing List No", "SWE/PL/0458"), ("Invoice No", "SWE/26-27/0458"), ("Shipper", beneficiary), ("Consignee", applicant), "",
        ("Description of Goods", "Cotton woven bed linen sets, 300 thread count"), ("Total Packages", "215 cartons"), ("Total Quantity", "4,300 sets"),
        ("Total Gross Weight", "6,880.0 kg"), ("Total Net Weight", "6,450.0 kg")])
    pdf(out / "certificate_of_origin.pdf", "Karur Chamber of Commerce and Industry", "CERTIFICATE OF ORIGIN", [
        ("Certificate No", "KCCI/CO/2026/11873"), ("Exporter", beneficiary), ("Consignee", applicant), ("Country of Origin", "India"),
        ("Description of Goods", "Cotton woven bed linen sets"), ("Invoice No", "SWE/26-27/0458"), ("Date of Issue", "22 October 2026"),
        "The undersigned authority certifies that the goods described above originate in the country shown."])


# ------------------------------------------------------------------------------------------------ supplier invoice (three-way match)
def supplier_invoice() -> None:
    out = OUT / "supplier_invoice"
    buyer = "Calder Vale Foods Ltd, 4 Riverside Park, Halifax HX3 6TR, United Kingdom"
    supplier = "Penrose Packaging Supplies Ltd, Unit 12 Tannery Court, Leeds LS10 2RU, United Kingdom"
    cartons = "Corrugated shipping cartons, double wall, 400 x 300 x 250 mm, printed"
    account = ("Bank Account (IBAN)", "GB55 PENR 2041 5500 7731 94")   # invented; the check digits are right
    contact = ("Accounts Contact", "accounts@penrose-packaging.example, tel: +44 113 496 0142")   # a reserved example domain and a number from a range kept for drama
    pdf(out / "purchase_order.pdf", "Calder Vale Foods Ltd - Procurement", "PURCHASE ORDER", [
        ("PO Number", "PO-2026-08821"), ("Order Date", "14 September 2026"), ("Buyer", buyer), ("Supplier", supplier), ("Ordered By", "Packaging buyer, Halifax site"), "",
        ("Goods Ordered", cartons), ("Quantity Ordered", "2,400 cartons"), ("Unit Price", "GBP 7.60"), ("Order Total", "GBP 18,240.00"),
        ("Deliver To", "Goods-in, 4 Riverside Park, Halifax"), ("Required By", "8 October 2026"), ("Payment Terms", "30 days from the invoice date")])
    pdf(out / "invoice.pdf", "Penrose Packaging Supplies Ltd", "TAX INVOICE", [
        ("Invoice Number", "PPS-104471"), ("Invoice Date", "9 October 2026"), ("Supplier", supplier), ("Bill To", buyer), ("PO Reference", "PO-2026-08821"), "",
        ("Goods Supplied", cartons), ("Quantity Invoiced", "2,400 cartons"), ("Unit Price", "GBP 7.60"), ("Invoice Total", "GBP 18,240.00"),
        ("Tax", "Included at the standard rate"), ("Payment Due", "8 November 2026"), account, contact])
    pdf(out / "goods_received_note.pdf", "Calder Vale Foods Ltd - Goods-in", "GOODS RECEIVED NOTE", [
        ("GRN Number", "GRN-55102"), ("PO Reference", "PO-2026-08821"), ("Received Date", "6 October 2026"), ("Supplier", supplier), ("Received At", buyer), "",
        ("Goods Received", "Corrugated shipping cartons, double wall, printed"), ("Quantity Received", "2,400 cartons"), ("Condition on Receipt", "Good; 12 pallets, wrapping intact"),
        ("Checked By", "Goods-in supervisor")])
    pdf(out / "delivery_note.pdf", "Penrose Packaging Supplies Ltd - Despatch", "DELIVERY NOTE", [
        ("Delivery Note Number", "DN-778214"), ("PO Reference", "PO-2026-08821"), ("Delivery Date", "6 October 2026"), ("Supplier", supplier), ("Delivered To", buyer), "",
        ("Goods", "Corrugated shipping cartons on 12 pallets"), ("Quantity Delivered", "2,400 cartons"), ("Signed For By", "Goods-in supervisor, Halifax site")])
    film = "Stretch wrap film, 500 mm x 300 m, 23 micron, clear"
    pdf(out / "purchase_order_b.pdf", "Calder Vale Foods Ltd - Procurement", "PURCHASE ORDER", [
        ("PO Number", "PO-2026-08907"), ("Order Date", "5 October 2026"), ("Buyer", buyer), ("Supplier", supplier), ("Ordered By", "Packaging buyer, Halifax site"), "",
        ("Goods Ordered", film), ("Quantity Ordered", "1,800 rolls"), ("Unit Price", "GBP 6.40"), ("Order Total", "GBP 11,520.00"),
        ("Deliver To", "Goods-in, 4 Riverside Park, Halifax"), ("Required By", "27 October 2026"), ("Payment Terms", "30 days from the invoice date")])
    pdf(out / "invoice_over.pdf", "Penrose Packaging Supplies Ltd", "TAX INVOICE", [
        ("Invoice Number", "PPS-104539"), ("Invoice Date", "28 October 2026"), ("Supplier", supplier), ("Bill To", buyer), ("PO Reference", "PO-2026-08907"), "",
        ("Goods Supplied", film), ("Quantity Invoiced", "2,000 rolls"), ("Unit Price", "GBP 6.40"), ("Invoice Total", "GBP 12,800.00"),
        ("Tax", "Included at the standard rate"), ("Payment Due", "27 November 2026"), account, contact])
    pdf(out / "goods_received_note_short.pdf", "Calder Vale Foods Ltd - Goods-in", "GOODS RECEIVED NOTE", [
        ("GRN Number", "GRN-55190"), ("PO Reference", "PO-2026-08907"), ("Received Date", "26 October 2026"), ("Supplier", supplier.replace("Ltd", "Limited")), ("Received At", buyer), "",
        ("Goods Received", "Stretch wrap film, 500 mm, clear"), ("Quantity Received", "1,650 rolls"), ("Condition on Receipt", "Good; one pallet short against the delivery paperwork"),
        ("Checked By", "Goods-in supervisor")])
    # a third transaction: within tolerance of its order, but the invoice's own figures do not add up and it names an account the supplier has not used before
    labels = "Pallet labels, thermal, 100 x 150 mm, rolls of 500"
    pdf(out / "purchase_order_c.pdf", "Calder Vale Foods Ltd - Procurement", "PURCHASE ORDER", [
        ("PO Number", "PO-2026-09114"), ("Order Date", "3 November 2026"), ("Buyer", buyer), ("Supplier", supplier), ("Ordered By", "Packaging buyer, Halifax site"), "",
        ("Goods Ordered", labels), ("Quantity Ordered", "600 rolls"), ("Unit Price", "GBP 9.50"), ("Order Total", "GBP 5,700.00"),
        ("Deliver To", "Goods-in, 4 Riverside Park, Halifax"), ("Required By", "16 November 2026"), ("Payment Terms", "30 days from the invoice date")])
    pdf(out / "invoice_new_account.pdf", "Penrose Packaging Supplies Ltd", "TAX INVOICE", [
        ("Invoice Number", "PPS-104688"), ("Invoice Date", "17 November 2026"), ("Supplier", supplier), ("Bill To", buyer), ("PO Reference", "PO-2026-09114"), "",
        ("Goods Supplied", labels), ("Quantity Invoiced", "600 rolls"), ("Unit Price", "GBP 9.50"), ("Invoice Total", "GBP 5,790.00"),
        ("Tax", "Included at the standard rate"), ("Payment Due", "17 December 2026"), ("Bank Account (IBAN)", "GB65 QLMR 6092 2144 0877 31"),
        "Please note that our bank details have changed. Use the account above for this and all future payments.", contact])
    pdf(out / "goods_received_note_c.pdf", "Calder Vale Foods Ltd - Goods-in", "GOODS RECEIVED NOTE", [
        ("GRN Number", "GRN-55307"), ("PO Reference", "PO-2026-09114"), ("Received Date", "14 November 2026"), ("Supplier", supplier), ("Received At", buyer), "",
        ("Goods Received", "Pallet labels, thermal, 100 x 150 mm"), ("Quantity Received", "600 rolls"), ("Condition on Receipt", "Good; two cartons, seals intact"),
        ("Checked By", "Goods-in supervisor")])


# ------------------------------------------------------------------------------------------------ insurance claim
def insurance_claim() -> None:
    out = OUT / "insurance_claim"
    insurer = "Marlowe Mutual Insurance plc, 1 Castle Quay, Norwich NR1 3EE, United Kingdom"
    holder = "Elena Marsh, 27 Orchard Close, Ipswich IP4 2QD, United Kingdom"
    repairer = "Kingsway Auto Body Ltd, 61 Foundry Lane, Ipswich IP3 9BX, United Kingdom"
    pdf(out / "policy_schedule.pdf", "Marlowe Mutual Insurance plc", "MOTOR POLICY SCHEDULE", [
        ("Policy Number", "MMI-MTR-4471902"), ("Policyholder", holder), ("Insurer", insurer), ("Period of Insurance", "12 months"),
        ("Cover Start Date", "1 October 2025"), ("Cover End Date", "30 September 2026"), "",
        ("Cover", "Comprehensive"), ("Sum Insured", "GBP 14,500.00"), ("Policy Excess", "GBP 350.00"), ("Vehicle Registration", "KV21 RMO"),
        ("Vehicle", "Five-door hatchback, 1.5 litre petrol, first registered 2021")])
    pdf(out / "claim_form.pdf", "Marlowe Mutual Insurance plc - Claims", "MOTOR CLAIM FORM", [
        ("Claim Number", "CL-2026-0090417"), ("Policy Number", "MMI-MTR-4471902"), ("Claimant", holder), ("Date of Loss", "14 August 2026"), "",
        ("Amount Claimed", "GBP 2,860.00"), ("Vehicle Registration", "KV21 RMO"),
        ("Circumstances of Loss", "Reversing collision with a bollard in a car park; rear bumper and tailgate damaged"), ("Injuries", "None"), ("Third Party", "None")])
    pdf(out / "repair_estimate.pdf", "Kingsway Auto Body Ltd", "REPAIR ESTIMATE", [
        ("Estimate Number", "KAB-E-22718"), ("Repairer", repairer), ("Customer", holder), ("Vehicle Registration", "KV21 RMO"), ("Estimate Date", "17 August 2026"), "",
        ("Work Required", "Replace rear bumper, repair and repaint tailgate, refit parking sensors"), ("Labour", "GBP 1,140.00"), ("Parts", "GBP 1,650.00"),
        ("Estimate Total", "GBP 2,790.00"), ("Valid For", "30 days")])
    pdf(out / "claim_form_late.pdf", "Marlowe Mutual Insurance plc - Claims", "MOTOR CLAIM FORM", [
        ("Claim Number", "CL-2026-0093152"), ("Policy Number", "MMI-MTR-4471902"), ("Claimant", holder), ("Date of Loss", "4 October 2026"), "",
        ("Amount Claimed", "GBP 4,150.00"), ("Vehicle Registration", "KV21 RMO"),
        ("Circumstances of Loss", "Collision with another vehicle at a junction; rear bumper and tailgate damaged"), ("Injuries", "None"), ("Third Party", "Yes, one other vehicle")])


# ------------------------------------------------------------------------------------------------ loan application
def loan_application() -> None:
    out = OUT / "loan_application"
    applicant = "Daniel Okafor, 9 Lark Rise, Reading RG2 7HN, United Kingdom"
    employer = "Hartwell Logistics Ltd, Thames Gate Business Park, Reading RG1 8DB, United Kingdom"
    pdf(out / "application_form.pdf", "Northgate Bank - Personal Lending", "PERSONAL LOAN APPLICATION", [
        ("Application Number", "APP-2026-30518"), ("Application Date", "10 October 2026"), ("Applicant", applicant), ("Employer", employer), "",
        ("Declared Monthly Income", "GBP 3,250.00"), ("Amount Requested", "GBP 15,000.00"), ("Repayment Term", "48 months"), ("Purpose", "Home improvement")])
    pdf(out / "payslip.pdf", "Hartwell Logistics Ltd - Payroll", "PAYSLIP", [
        ("Employee", applicant), ("Employer", employer), ("Pay Date", "30 September 2026"), ("Pay Period", "Month 6"), "",
        ("Gross Pay", "GBP 4,310.00"), ("Deductions", "GBP 1,060.00"), ("Net Pay", "GBP 3,250.00"), ("Tax Code", "Standard")])
    pdf(out / "bank_statement.pdf", "Northgate Bank", "BANK STATEMENT", [
        ("Account Holder", applicant), ("Statement Period", "1 September 2026 to 30 September 2026"), ("Statement Period End", "30 September 2026"), "",
        ("Salary Credit", "GBP 3,250.00"), ("Salary Paid By", "Hartwell Logistics Ltd"), ("Closing Balance", "GBP 2,184.36"),
        "Account number and sort code withheld from this demonstration document."])
    pdf(out / "employment_letter.pdf", "Hartwell Logistics Ltd - People Team", "CONFIRMATION OF EMPLOYMENT", [
        "Employment letter. To whom it may concern:", ("Employer", employer), ("Employee", applicant), ("Letter Date", "6 October 2026"), "",
        ("Position Held", "Transport planner"), ("Employed Since", "3 March 2021"), ("Annual Salary", "GBP 51,720.00"), ("Contract", "Permanent, full time")])
    pdf(out / "application_form_high.pdf", "Northgate Bank - Personal Lending", "PERSONAL LOAN APPLICATION", [
        ("Application Number", "APP-2026-30677"), ("Application Date", "19 October 2026"), ("Applicant", applicant),
        ("Employer", "Hartwell Freight Services Ltd, Thames Gate Business Park, Reading RG1 8DB, United Kingdom"), "",
        ("Declared Monthly Income", "GBP 4,400.00"), ("Amount Requested", "GBP 22,000.00"), ("Repayment Term", "60 months"), ("Purpose", "Debt consolidation")])
    pdf(out / "payslip_old.pdf", "Hartwell Logistics Ltd - Payroll", "PAYSLIP", [
        ("Employee", applicant), ("Employer", employer), ("Pay Date", "30 June 2026"), ("Pay Period", "Month 3"), "",
        ("Gross Pay", "GBP 4,310.00"), ("Deductions", "GBP 1,060.00"), ("Net Pay", "GBP 3,250.00"), ("Tax Code", "Standard")])


if __name__ == "__main__":
    only = set(sys.argv[1:])   # name the profiles to rebuild; none rebuilds all
    for build in (customs, trade_finance, supplier_invoice, insurance_claim, loan_application):
        if not only or build.__name__ in only:
            build()
    for p in sorted(OUT.rglob("*")):
        if p.is_file():
            print(f"{p.relative_to(OUT)}  {p.stat().st_size:,} bytes")
