"""
report_pdf.py
Renders the batch report dict (from report.build_report) as a short,
printable PDF summary -- the kind of thing you'd actually hand to a
finance stakeholder who doesn't want to open a dashboard.

Uses fpdf2 (pure Python, no system dependencies) so it works
anywhere the rest of the app does.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fpdf import FPDF

TITLE = "AI Revenue Recovery Agent -- Batch Report"


def _fmt_currency(value: float) -> str:
    return f"Rs {value:,.2f}"


def build_report_pdf(report: dict, invalid_count: int) -> bytes:
    """Return the PDF as raw bytes, ready for a Streamlit download button."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, TITLE, ln=True)

    pdf.set_font("Helvetica", "", 10)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pdf.cell(0, 8, f"Generated {generated_at}", ln=True)
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Summary", ln=True)
    pdf.set_font("Helvetica", "", 11)

    summary_rows = [
        ("Revenue at risk", _fmt_currency(report["revenue_at_risk"])),
        ("Gross revenue recovered", _fmt_currency(report["gross_revenue_recovered"])),
        ("% of at-risk revenue recovered", f"{report['pct_of_at_risk_recovered']:.1f}%"),
        ("Net revenue preserved", _fmt_currency(report["net_revenue_preserved"])),
        ("Total action cost", _fmt_currency(report["total_action_cost"])),
        ("Net recovery rate", f"{report['net_recovery_rate_pct']:.1f}%"),
        ("Payment links issued", str(report.get("links_issued_count", 0))),
        (
            "  ...of which confirmed (paid)",
            str(report.get("links_issued_count", 0) - report.get("link_issued_not_confirmed_count", 0)),
        ),
        ("Link -> payment conversion rate", f"{report.get('link_confirmation_rate_pct', 0):.1f}%"),
        ("Total cases processed", str(report["total_cases"])),
        ("Invalid events skipped", str(invalid_count)),
        ("Blocked by safety gate", str(report["blocked_case_count"])),
    ]
    for label, value in summary_rows:
        pdf.cell(95, 7, label, border=0)
        pdf.cell(0, 7, value, ln=True)

    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Recovery rate by failure bucket", ln=True)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(50, 7, "Bucket", border=1)
    pdf.cell(35, 7, "Total", border=1)
    pdf.cell(45, 7, "Recovered", border=1)
    pdf.cell(0, 7, "Rate", border=1, ln=True)
    pdf.set_font("Helvetica", "", 10)
    for bucket, stats in report["recovery_by_bucket"].items():
        pdf.cell(50, 7, bucket, border=1)
        pdf.cell(35, 7, str(stats["total"]), border=1)
        pdf.cell(45, 7, str(stats["recovered"]), border=1)
        pdf.cell(0, 7, f"{stats['recovery_rate_pct']:.1f}%", border=1, ln=True)

    if report.get("recovery_by_ltv_tier"):
        pdf.ln(4)
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, "Recovery rate by customer LTV tier", ln=True)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(50, 7, "Tier", border=1)
        pdf.cell(35, 7, "Total", border=1)
        pdf.cell(45, 7, "Recovered", border=1)
        pdf.cell(0, 7, "Rate", border=1, ln=True)
        pdf.set_font("Helvetica", "", 10)
        for tier, stats in report["recovery_by_ltv_tier"].items():
            pdf.cell(50, 7, tier, border=1)
            pdf.cell(35, 7, str(stats["total"]), border=1)
            pdf.cell(45, 7, str(stats["recovered"]), border=1)
            pdf.cell(0, 7, f"{stats['recovery_rate_pct']:.1f}%", border=1, ln=True)

    if report.get("blocked_reasons"):
        pdf.ln(4)
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, "Blocked case reasons", ln=True)
        pdf.set_font("Helvetica", "", 11)
        for reason, count in report["blocked_reasons"].items():
            pdf.cell(0, 7, f"{reason}: {count}", ln=True)

    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 8)
    pdf.multi_cell(
        0, 5,
        "Note: 'recovered'/'confirmed' means a simulated payment-confirmation "
        "check indicated the customer completed payment on an issued link -- "
        "there is no live completion webhook to call in Razorpay TEST mode. "
        "'Links issued' is a separate, purely technical count (link creation "
        "succeeded) and is always >= the confirmed count. Figures are computed "
        "from a synthetic/demo batch. See README.md for the full "
        "real-vs-simulated breakdown."
    )

    return bytes(pdf.output())
