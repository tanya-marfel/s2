"""Expand Swedish DRG shorthand into readable English.

NordDRG labels are heavily abbreviated clinical shorthand, not plain Swedish:

    "Prim ledprot hoft U"        -> Primary hip replacement, uncomplicated
    "Sek ledprotreplant knafot"  -> Revision knee/foot joint replacement
    "Trakeostomi ej ONHsjd M"    -> Tracheostomy, non-ENT disease, major

So this is abbreviation expansion plus translation, which is why a model does it
rather than a dictionary.

Translations are PRESENTATIONAL ONLY. The DRG code remains the identity of a
procedure; nothing downstream matches, prices or decides on a translated string.
Results are cached so a demo never waits on the network.

    python3 cbh/ingest/translate.py
"""

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from cbh.core.llm import LLMClient  # noqa: E402

IN_FILE = os.path.join("out", "se_tariffs.json")
OUT_FILE = os.path.join("out", "se_labels_en.json")
BATCH = 60
WORKERS = 6

SYSTEM_PROMPT = """You translate Swedish NordDRG procedure labels into English.

These are abbreviated clinical shorthand, not ordinary Swedish. Expand the
abbreviations into a readable English description.

Common conventions:
  Prim = primar (primary)        Sek = sekundar (revision/secondary)
  ledprot = ledprotes (joint replacement)      op/kir = surgery
  U = utan komplikation (uncomplicated)        K = med komplikation (complicated)
  M = major                      O = oppenvard (outpatient)
  ej = not/without               sjd = sjukdom (disease)
  besok = visit                  diagn = diagnostic

Return ONLY a JSON array, one object per input line, same order:
[{"drg": "<code exactly as given>", "en": "<English description>"}]

Keep each description under 60 characters. Do not add commentary.
"""


def _parse(text):
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", cleaned, re.S)
        if not m:
            return []
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return []


def translate_batch(client, rows):
    payload = "\n".join("%s | %s" % (r["drg"], r["label_sv"]) for r in rows)
    try:
        out = _parse(client.chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": payload},
        ]))
    except Exception:  # noqa: BLE001 - one bad batch must not abort the run
        return {}

    wanted = {r["drg"] for r in rows}
    result = {}
    for item in out:
        if not isinstance(item, dict):
            continue
        drg, en = item.get("drg"), (item.get("en") or "").strip()
        # Only accept codes we actually asked about.
        if drg in wanted and en:
            result[drg] = en
    return result


def main():
    if not os.path.exists(IN_FILE):
        raise SystemExit("run: python3 cbh/ingest/se_norddrg.py")
    with open(IN_FILE, encoding="utf-8") as fh:
        rows = json.load(fh)["tariffs"]

    cache = {}
    if os.path.exists(OUT_FILE):
        with open(OUT_FILE, encoding="utf-8") as fh:
            cache = json.load(fh)

    todo = [r for r in rows if r["drg"] not in cache]
    print("%d labels, %d cached, %d to translate" % (len(rows), len(cache), len(todo)))
    if not todo:
        return

    # Translation carries no figures, so the proxy is safe here; but it is also
    # not worth the risk of a lossy transform mangling short labels.
    client = LLMClient(use_condense=False)
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for got in pool.map(lambda b: translate_batch(client, b), batches):
            cache.update(got)
            done += 1
            print("  batch %d/%d  (%d translated)" % (done, len(batches), len(cache)))

    os.makedirs("out", exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=2, sort_keys=True)

    missing = [r["drg"] for r in rows if r["drg"] not in cache]
    print("\ntranslated %d/%d -> %s" % (len(cache), len(rows), OUT_FILE))
    if missing:
        print("missing %d (UI falls back to the Swedish label): %s"
              % (len(missing), ", ".join(missing[:8])))

    for drg in ("H02E", "H01N", "H04N", "T59A"):
        if drg in cache:
            print("  %-6s %s" % (drg, cache[drg]))


if __name__ == "__main__":
    main()
