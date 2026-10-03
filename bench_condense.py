"""Measure what the condense proxy actually saves on this workload.

Route reasoning re-sends the same legal corpus on every patient query: the
Directive articles, Regulation 883/2004 Art. 20(2), and per-country prior
authorisation rules. Only the case details change. That is precisely the
repeated context condense compresses.

Runs the same cases twice - direct to Gemini, then through condense - and
reports the measured difference instead of relying on a dashboard.

    python3 bench_condense.py --cases 8
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cbh.core.llm import LLMClient  # noqa: E402

# Static reference material only. Anything the model must OBEY lives in the
# per-case message instead: compression treats the repeated block as reference
# context and can compact instructions out of it, which silently changes
# behaviour. Measured: output grew 6x and the length limit stopped being obeyed.
LEGAL_CORPUS = """
Reference material on entitlement to cross-border healthcare in the EU.

DIRECTIVE 2011/24/EU - Art. 7(4): costs of cross-border healthcare are
reimbursed up to the level the Member State of affiliation would have assumed
had the healthcare been provided in its territory, not exceeding the actual
cost received. Art. 8: prior authorisation may be required for care involving
overnight hospital accommodation or highly specialised and cost-intensive
infrastructure. Art. 8(5): authorisation may not be refused where the patient
is entitled to the treatment and it cannot be provided within a time limit that
is medically justifiable.

REGULATION (EC) 883/2004 - Art. 20(2): a person authorised to travel for
treatment receives benefits in kind provided by the institution of the place of
stay, as though insured there. Authorisation shall be accorded where the
treatment is among the benefits provided by the home Member State and cannot be
given within a time limit which is medically justifiable, taking account of the
current state of health and probable course of the illness.

SWEDEN - vardgaranti (0-7-90-90): contact same day; primary care assessment
within 7 days; first specialist appointment within 90 days; treatment within a
further 90 days after a decision to treat.

REIMBURSEMENT MECHANICS - Under the Directive the patient pays the provider up
front and reclaims from the home system at home tariffs. Under S2 the two
institutions settle directly and the patient pays only the treatment state's
own patient charges. Out-of-pocket cost may be identical while the capital the
patient must float differs by thousands of euros.
"""

# Travels with every case, so it stays outside the compressible repeated block.
TASK = (
    "Using the reference material, state the better route, the statutory basis, "
    "whether an undue-delay argument is available, and the capital the patient "
    "must advance. Answer in at most four sentences.\n\nCASE: "
)

CASES = [
    "Hip replacement (NordDRG H02E). Home tariff EUR 7593. Poland EUR 5116. Home wait 240 days.",
    "Knee replacement (H04N). Home tariff EUR 7376. Lithuania EUR 4200. Home wait 180 days.",
    "Cataract surgery. Home tariff EUR 1850. Estonia EUR 1100. Home wait 150 days.",
    "Inguinal hernia repair. Home tariff EUR 3100. Poland EUR 1700. Home wait 95 days.",
    "Bariatric surgery. Home tariff EUR 9800. Czechia EUR 6400. Home wait 400 days.",
    "Spinal fusion. Home tariff EUR 21400. Germany EUR 19800. Home wait 300 days.",
    "Varicose vein surgery. Home tariff EUR 2400. Latvia EUR 1300. Home wait 120 days.",
    "Shoulder arthroscopy. Home tariff EUR 4100. Poland EUR 2500. Home wait 210 days.",
    "Hysterectomy. Home tariff EUR 6900. Estonia EUR 4300. Home wait 160 days.",
    "Prostatectomy. Home tariff EUR 11200. Germany EUR 10400. Home wait 85 days.",
]


def run(use_condense, cases, label):
    client = LLMClient(use_condense=use_condense)
    print("\n== %s (%s)" % (label, client.report()["route"]))
    lengths = []
    for i, case in enumerate(cases, 1):
        messages = [
            {"role": "system", "content": LEGAL_CORPUS},
            {"role": "user", "content": TASK + case},
        ]
        try:
            reply = client.chat(messages)
            lengths.append(len(reply))
            print("  [%d/%d] ok  %4d chars  %s"
                  % (i, len(cases), len(reply), reply.strip().split("\n")[0][:52]))
        except Exception as exc:  # noqa: BLE001 - benchmark should not abort
            print("  [%d/%d] FAIL %s" % (i, len(cases), str(exc)[:110]))
    report = client.report()
    report["mean_reply_chars"] = int(sum(lengths) / len(lengths)) if lengths else 0
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=5)
    args = ap.parse_args()
    cases = CASES[: max(1, min(args.cases, len(CASES)))]

    direct = run(False, cases, "DIRECT")
    viacond = run(True, cases, "VIA CONDENSE")

    d_in = direct["usage"]["prompt_tokens"]
    c_in = viacond["usage"]["prompt_tokens"]
    d_tot = direct["usage"]["total_tokens"]
    c_tot = viacond["usage"]["total_tokens"]

    print("\n" + "=" * 60)
    print("PROXY BENCHMARK  (%d cases)" % len(cases))
    print("=" * 60)
    print("%-22s %10s %10s" % ("", "direct", "condense"))
    print("%-22s %10d %10d" % ("input tokens", d_in, c_in))
    print(
        "%-22s %10d %10d"
        % ("output tokens", direct["usage"]["completion_tokens"], viacond["usage"]["completion_tokens"])
    )
    print("%-22s %10d %10d" % ("TOTAL tokens", d_tot, c_tot))
    print(
        "%-22s %10d %10d"
        % ("mean reply chars", direct["mean_reply_chars"], viacond["mean_reply_chars"])
    )
    print(
        "%-22s %10.1f %10.1f"
        % ("wall seconds", direct["usage"]["wall_seconds"], viacond["usage"]["wall_seconds"])
    )
    if d_in and c_in:
        print("-" * 60)
        print("input tokens saved   : %d (%.1f%%)" % (d_in - c_in, 100.0 * (d_in - c_in) / d_in))
        net = d_tot - c_tot
        print("NET tokens saved     : %d (%.1f%%)" % (net, 100.0 * net / d_tot))
        if net < 0:
            print("  WARNING: input savings were outweighed by longer replies.")
            print("  Check that instructions are not being compressed away.")
        print("condense session id  : %s" % viacond["session_id"])

    os.makedirs("out", exist_ok=True)
    with open(os.path.join("out", "condense_bench.json"), "w", encoding="utf-8") as fh:
        json.dump({"direct": direct, "condense": viacond}, fh, indent=2)
    print("written: out/condense_bench.json")


if __name__ == "__main__":
    main()
