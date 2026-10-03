"""Generate the example cases used in the demo: one set of documents per operation.

Every document is synthetic and says so in a banner on the page: fictional patient,
fictional clinics, invented prices. They exist so the document-reading step can be
shown for several different operations without any real medical data.

Each scenario is one patient's whole story:

    referral          the treating doctor's referral naming the operation
    waiting_letter    the home hospital's dated waiting-list notice
    offer_a           the clinic with the earliest date; silent about S2
    offer_b           a later clinic that states it treats S2 patients
    reply_a           the complication: offer_a answers that it does not take S2

The generator also writes `samples/scenarios.json`, the manifest the server and the
UI read. The server never imports this module, so fpdf2 stays a development-only
dependency.

    pip install fpdf2 && python3 samples/make_samples.py

Only Latin-1 characters may appear in the documents: fpdf's built-in Helvetica
cannot encode anything else. Swedish and German text is written normally (a-ring,
umlauts and the like are Latin-1); Polish, Czech and Lithuanian clinic names use
unaccented spellings because their diacritics are not.
"""

import datetime
import json
import os

from fpdf import FPDF

HERE = os.path.dirname(os.path.abspath(__file__))
BANNER = ("SAMPLE - SYNTHETIC DOCUMENT FOR DEMONSTRATION. "
          "Fictional people and clinics. Not a real offer.")

# The documents carry fixed dates, so the manifest also carries the date they were
# written for. The UI passes it back as the case's "today" whenever an example case
# is used, which keeps "days waited" and "days earlier" coherent however long after
# generation the demo is run. A patient's own upload always uses the real date.
TODAY = datetime.date(2026, 10, 3)

SCENARIOS = [
    {
        "id": "cataract",
        "title": "Cataract surgery, one eye",
        "operation": "Cataract surgery with lens implant",
        "blurb": "Day surgery. On the list since April, offered a date in January.",
        "patient": "Anna Testsson",
        "home": "SE",
        "statement": ("My doctor recommended a cataract operation on my right eye. "
                      "I have been waiting since April and I cannot afford a large "
                      "upfront payment."),
        "referral": {
            "title": "Remiss / Referral",
            "lines": [
                "Datum: 2026-04-14",
                "Avsändare: Ögonmottagningen, Exempelsjukhuset (fiktivt)",
                "Patient: Anna Testsson (fiktiv)",
                "",
                "Åtgärd: Kataraktoperation med linsimplantation, höger öga (grå starr)",
                "Läkarens bedömning: operation rekommenderas.",
                "",
                "Remissen avser planerad dagkirurgi.",
            ],
        },
        "waiting_letter": {
            "title": "Besked om väntelista / Waiting-list notice",
            "lines": [
                "Datum: 2026-04-20",
                "Avsändare: Exempelsjukhuset, ögonkliniken (fiktivt)",
                "Patient: Anna Testsson (fiktiv)",
                "",
                "Du har registrerats på väntelista för kataraktoperation, höger öga,",
                "den 2026-04-20.",
                "Beräknat operationsdatum: 2027-01-12.",
                "Vi kan tyvärr inte ge ett tidigare datum.",
            ],
        },
        "offer_a": {
            "clinic": "Okulistyczna Klinika Przykladowa",
            "country": "Poland",
            "lines": [
                "Procedure: Cataract surgery with intraocular lens implant, one eye, day case",
                "Price: PLN 3,600 (all inclusive: surgery, standard lens, check-up visit)",
                "Earliest admission date: 2026-11-03",
                "Offer valid until: 2026-12-31",
                "Payment: in full on the day of surgery.",
                "S2 authorisation: not stated in this offer.",
            ],
        },
        "offer_b": {
            "clinic": "Voorbeeld Oogkliniek",
            "country": "Netherlands",
            "lines": [
                "Procedure: Cataract surgery with intraocular lens implant, one eye, day case",
                "Price: EUR 1,850 (self-pay, all inclusive)",
                "Earliest admission date: 2026-11-24",
                "Offer valid until: 2027-01-15",
                "We treat patients holding an S2 authorisation as public patients.",
                "Patient charges under S2: none stated; to be confirmed on request.",
            ],
        },
        "reply_a": {
            "lines": [
                "Date: 2026-10-03",
                "Re: your enquiry about S2 authorisation",
                "",
                "We confirm that Okulistyczna Klinika Przykladowa does not accept S2",
                "authorisations for elective eye surgery. Treatment is available as",
                "self-pay only.",
            ],
        },
    },
    {
        "id": "hip",
        "title": "Hip replacement",
        "operation": "Primary total hip replacement",
        "blurb": "Inpatient surgery. Sixteen months on the list and a five-figure bill abroad.",
        "patient": "Erik Provsson",
        "home": "SE",
        "statement": ("My doctor recommended a hip replacement. I have been on the "
                      "waiting list since February and I cannot pay thousands of euros "
                      "up front."),
        "referral": {
            "title": "Remiss / Referral",
            "lines": [
                "Datum: 2026-01-28",
                "Avsändare: Ortopedmottagningen, Exempelsjukhuset (fiktivt)",
                "Patient: Erik Provsson (fiktiv)",
                "",
                "Åtgärd: Total höftledsplastik, vänster höft (primär höftprotes)",
                "Läkarens bedömning: operation rekommenderas.",
                "",
                "Remissen avser planerad slutenvård, beräkna 3-4 vårddygn.",
            ],
        },
        "waiting_letter": {
            "title": "Besked om väntelista / Waiting-list notice",
            "lines": [
                "Datum: 2026-02-10",
                "Avsändare: Exempelsjukhuset, ortopedkliniken (fiktivt)",
                "Patient: Erik Provsson (fiktiv)",
                "",
                "Du har registrerats på väntelista för höftprotesoperation den 2026-02-10.",
                "Beräknat operationsdatum: 2027-06-15.",
                "Väntetiden beror på hög belastning inom ortopedin.",
            ],
        },
        "offer_a": {
            "clinic": "Klaipedos Ortopedijos Centras",
            "country": "Lithuania",
            "lines": [
                "Procedure: Primary total hip replacement, cementless implant, one side",
                "Price: EUR 9,200 (surgery, implant, 4 nights, physiotherapy)",
                "Earliest admission date: 2026-12-08",
                "Offer valid until: 2027-02-28",
                "Payment: 30% on booking, balance on admission.",
                "S2 authorisation: not stated in this offer.",
            ],
        },
        "offer_b": {
            "clinic": "Ortopedyczna Klinika Przykladowa",
            "country": "Poland",
            "lines": [
                "Procedure: Primary total hip replacement, one side, including implant",
                "Price: PLN 39,500 (surgery, implant, 5 nights, rehabilitation plan)",
                "Earliest admission date: 2027-01-19",
                "Offer valid until: 2027-03-31",
                "We treat patients holding an S2 authorisation as public patients.",
                "Patient charges under S2: none stated; to be confirmed on request.",
            ],
        },
        "reply_a": {
            "lines": [
                "Date: 2026-10-03",
                "Re: your enquiry about S2 authorisation",
                "",
                "Thank you for your enquiry. Klaipedos Ortopedijos Centras does not accept",
                "S2 authorisations. Our joint replacement programme is private only and",
                "must be paid directly by the patient.",
            ],
        },
    },
    {
        "id": "hernia",
        "title": "Inguinal hernia repair",
        "operation": "Unilateral inguinal hernia repair, day case",
        "blurb": "Day surgery. A cheap clinic abroad, but the money is needed first.",
        "patient": "Johan Exempelsson",
        "home": "SE",
        "statement": ("My surgeon says I need an operation for an inguinal hernia. "
                      "I have been waiting since March and I cannot pay the clinic "
                      "first and wait months for a refund."),
        "referral": {
            "title": "Remiss / Referral",
            "lines": [
                "Datum: 2026-03-05",
                "Avsändare: Kirurgmottagningen, Exempelsjukhuset (fiktivt)",
                "Patient: Johan Exempelsson (fiktiv)",
                "",
                "Åtgärd: Operation av ljumskbråck, höger sida (öppen nätplastik)",
                "Läkarens bedömning: operation rekommenderas.",
                "",
                "Remissen avser planerad dagkirurgi.",
            ],
        },
        "waiting_letter": {
            "title": "Besked om väntelista / Waiting-list notice",
            "lines": [
                "Datum: 2026-03-12",
                "Avsändare: Exempelsjukhuset, kirurgkliniken (fiktivt)",
                "Patient: Johan Exempelsson (fiktiv)",
                "",
                "Du har registrerats på väntelista för ljumskbråcksoperation",
                "den 2026-03-12.",
                "Beräknat operationsdatum: 2027-02-08.",
                "Vi kan inte erbjuda ett tidigare datum i nuläget.",
            ],
        },
        "offer_a": {
            "clinic": "Vzorova Chirurgicka Klinika",
            "country": "Czechia",
            "lines": [
                "Procedure: Unilateral inguinal hernia repair with mesh, day case",
                "Price: CZK 48,000 (surgery, mesh, anaesthesia, one follow-up)",
                "Earliest admission date: 2026-11-17",
                "Offer valid until: 2027-01-31",
                "Payment: in full on the day of surgery.",
                "S2 authorisation: not stated in this offer.",
            ],
        },
        "offer_b": {
            "clinic": "Voorbeeld Dagkliniek",
            "country": "Netherlands",
            "lines": [
                "Procedure: Unilateral inguinal hernia repair with mesh, day case",
                "Price: EUR 2,250 (self-pay, all inclusive)",
                "Earliest admission date: 2026-12-15",
                "Offer valid until: 2027-02-28",
                "We treat patients holding an S2 authorisation as public patients.",
                "Patient charges under S2: none stated; to be confirmed on request.",
            ],
        },
        "reply_a": {
            "lines": [
                "Date: 2026-10-03",
                "Re: your enquiry about S2 authorisation",
                "",
                "We regret to inform you that Vzorova Chirurgicka Klinika does not accept",
                "S2 authorisations. Our day-surgery prices apply to self-paying patients",
                "only.",
            ],
        },
    },
    {
        "id": "spine",
        "title": "Lumbar spinal fusion",
        "operation": "Two-level lumbar spinal fusion",
        "blurb": "Major inpatient surgery. The gap left after reimbursement is the story.",
        "patient": "Maria Testsdotter",
        "home": "SE",
        "statement": ("My doctor has recommended a spinal fusion in my lower back. "
                      "I have been waiting more than a year and I cannot put up a "
                      "large sum of money in advance."),
        "referral": {
            "title": "Remiss / Referral",
            "lines": [
                "Datum: 2026-01-09",
                "Avsändare: Ryggmottagningen, Exempelsjukhuset (fiktivt)",
                "Patient: Maria Testsdotter (fiktiv)",
                "",
                "Åtgärd: Ryggradsfusion ländrygg (lumbal fusion), två nivåer",
                "Läkarens bedömning: operation rekommenderas.",
                "",
                "Remissen avser planerad slutenvård.",
            ],
        },
        "waiting_letter": {
            "title": "Besked om väntelista / Waiting-list notice",
            "lines": [
                "Datum: 2026-01-16",
                "Avsändare: Exempelsjukhuset, ryggkirurgiska enheten (fiktivt)",
                "Patient: Maria Testsdotter (fiktiv)",
                "",
                "Du har registrerats på väntelista för ryggfusionsoperation",
                "den 2026-01-16.",
                "Beräknat operationsdatum: 2027-05-10.",
                "Ett tidigare datum kan inte erbjudas.",
            ],
        },
        "offer_a": {
            "clinic": "Beispiel Wirbelsäulenklinik",
            "country": "Germany",
            "lines": [
                "Procedure: Two-level lumbar spinal fusion with pedicle screw fixation",
                "Price: EUR 18,400 (surgery, implants, 6 nights, discharge report)",
                "Earliest admission date: 2026-11-30",
                "Offer valid until: 2027-01-31",
                "Payment: full amount due before admission.",
                "S2 authorisation: not stated in this offer.",
            ],
        },
        "offer_b": {
            "clinic": "Klinika Kregoslupa Przykladowa",
            "country": "Poland",
            "lines": [
                "Procedure: Two-level lumbar spinal fusion with pedicle screw fixation",
                "Price: PLN 62,000 (surgery, implants, 7 nights, rehabilitation plan)",
                "Earliest admission date: 2027-02-02",
                "Offer valid until: 2027-04-30",
                "We treat patients holding an S2 authorisation as public patients.",
                "Patient charges under S2: none stated; to be confirmed on request.",
            ],
        },
        "reply_a": {
            "lines": [
                "Date: 2026-10-03",
                "Re: your enquiry about S2 authorisation",
                "",
                "Beispiel Wirbelsäulenklinik does not accept S2 authorisations for",
                "elective spinal surgery. Treatment at the quoted price is available to",
                "self-paying patients only.",
            ],
        },
    },
]

# filename suffix, document title, source key
PARTS = [
    ("1_referral", lambda s: s["referral"]["title"], "referral"),
    ("2_waiting_list_letter", lambda s: s["waiting_letter"]["title"], "waiting_letter"),
    ("3_clinic_offer_A", lambda s: "Offer from %s (fictional clinic, %s)" % (
        s["offer_a"]["clinic"], s["offer_a"]["country"]), "offer_a"),
    ("4_clinic_offer_B", lambda s: "Offer from %s (fictional clinic, %s)" % (
        s["offer_b"]["clinic"], s["offer_b"]["country"]), "offer_b"),
    ("5_clinic_reply_A", lambda s: "Reply from %s (fictional clinic, %s)" % (
        s["offer_a"]["clinic"], s["offer_a"]["country"]), "reply_a"),
]


def build(name, title, lines):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(180, 0, 0)
    pdf.multi_cell(0, 5, BANNER, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 15)
    pdf.multi_cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_font("Helvetica", "", 12)
    for line in lines:
        pdf.multi_cell(0, 7, line, new_x="LMARGIN", new_y="NEXT")
    pdf.output(os.path.join(HERE, name))


def main():
    manifest = []
    for s in SCENARIOS:
        files = {}
        for suffix, title_of, key in PARTS:
            name = "%s_%s_SAMPLE.pdf" % (s["id"], suffix)
            build(name, title_of(s), s[key]["lines"])
            files[key] = name
            print("wrote", name)
        manifest.append({
            "id": s["id"], "title": s["title"], "operation": s["operation"],
            "blurb": s["blurb"], "patient": s["patient"], "home": s["home"],
            "statement": s["statement"], "today": TODAY.isoformat(),
            "documents": [files["referral"], files["waiting_letter"],
                          files["offer_a"], files["offer_b"]],
            "reply": files["reply_a"], "reply_clinic": s["offer_a"]["clinic"],
        })
    path = os.path.join(HERE, "scenarios.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    print("wrote", os.path.basename(path), "(%d example cases)" % len(manifest))


if __name__ == "__main__":
    main()
