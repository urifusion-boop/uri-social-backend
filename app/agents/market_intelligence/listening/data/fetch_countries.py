"""
Regenerates countries.json from the canonical `world-countries` dataset
(github.com/mledoze/countries) — the same source the frontend's country
picker uses, so both sides of the product agree on one canonical list
without either one hand-typing it. Run this to refresh after a new UN
member/territory change; nothing in this module is meant to be edited
by hand otherwise.

Usage: python3 fetch_countries.py
Requires: npm (uses `npm pack` to fetch the package without installing it
into the backend's own node-free Python environment).
"""
import json
import subprocess
import tarfile
import tempfile
from datetime import date
from pathlib import Path

PACKAGE = "world-countries@5.1.0"
OUT = Path(__file__).parent / "countries.json"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["npm", "pack", PACKAGE, "--silent"], cwd=tmp, check=True)
        tgz = next(Path(tmp).glob("*.tgz"))
        with tarfile.open(tgz) as t:
            t.extractall(tmp)
        raw = json.loads((Path(tmp) / "package" / "dist" / "countries.json").read_text())

    out = []
    for c in raw:
        if c.get("region") == "Antarctic":
            continue
        code, name, region = c.get("cca2"), (c.get("name") or {}).get("common"), c.get("region")
        if not (code and name and region):
            continue
        out.append({"code": code, "name": name, "region": region, "subregion": c.get("subregion") or ""})
    out.sort(key=lambda x: x["code"])

    OUT.write_text(json.dumps({
        "source": f"world-countries npm package (github.com/mledoze/countries), {PACKAGE}",
        "fetched": date.today().isoformat(),
        "note": "Canonical ISO-3166 country + region reference data — not hand-authored. "
                "Re-run this script to refresh.",
        "countries": out,
    }, indent=2))
    print(f"wrote {len(out)} countries to {OUT}")


if __name__ == "__main__":
    main()
