"""One-off: generate scrubbed demo fixtures from the current local-storage.

Reads the newest succeeded run + its BOM + snapshot blob, replaces the real
subscription GUID / username / customer label with demo-safe values, and writes
fixtures/demo/{bom.json,run.json,snapshot.json}. Run once; the fixtures are then
committed and shipped so DEMO_MODE can seed a populated dashboard offline.
"""
import json
import os
import sqlite3

REAL_SUB = "928886d3-3b36-405d-a248-8756a129ef7f"
DEMO_SUB = "d3305e11-0000-4a00-b000-0000c0ffee01"
BOM_ID = "0c04d0143c354ea8adf7537872e92f97"
RUN_ID = "2026-08-05T16-29-02Z-2c077257"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "local-storage", "app.db")
BLOB = os.path.join(ROOT, "local-storage", "blobs", "snapshots", BOM_ID, RUN_ID + ".json")
OUT = os.path.join(ROOT, "fixtures", "demo")


def scrub(text: str) -> str:
    return (text.replace(REAL_SUB, DEMO_SUB)
                .replace("f7751401-05a2-4725-b4ac-2837578f5733", DEMO_SUB)
                .replace("bbabcock", "demo-user"))


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    c = sqlite3.connect(DB)

    pk, rk, data = c.execute(
        "select pk,rk,data from tbl_subscriptionmetadata where rk=?", (BOM_ID,)
    ).fetchone()
    bom = json.loads(scrub(data))
    bom["tag"] = "Contoso — Sample BOM"
    bom["customer_name"] = "Contoso Ltd (sample)"
    with open(os.path.join(OUT, "bom.json"), "w", encoding="utf-8") as f:
        json.dump({"pk": pk, "rk": rk, "data": bom}, f, indent=2)

    prk, rrk, rdata = c.execute(
        "select pk,rk,data from tbl_runs where rk=?", (RUN_ID,)
    ).fetchone()
    run = json.loads(scrub(rdata))
    with open(os.path.join(OUT, "run.json"), "w", encoding="utf-8") as f:
        json.dump({"pk": prk, "rk": rrk, "data": run}, f, indent=2)

    with open(BLOB, encoding="utf-8") as f:
        snap_text = scrub(f.read())
    # validate JSON after scrub
    json.loads(snap_text)
    with open(os.path.join(OUT, "snapshot.json"), "w", encoding="utf-8") as f:
        f.write(snap_text)

    print("wrote fixtures to", OUT)
    print("bom_id:", BOM_ID, "run_id:", RUN_ID, "sub:", DEMO_SUB)


if __name__ == "__main__":
    main()
