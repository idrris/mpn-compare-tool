
import os
from flask import Flask, render_template, request
from dotenv import load_dotenv

# Load .env if present
load_dotenv()

# Use the user's extractor (Digi-Key first, fallback to Mouser)
from EOL.eol_attr_extractor import fetch_attributes_for_mpn

app = Flask(__name__)

def normalize_col(raw_key: str) -> str:
    k = (raw_key or "").strip().lower()
    def has(*frags):
        return all(f in k for f in frags)
    if has("size") or has("dimension"):
        return "Size / Dimension"
    if has("width"):
        return "Width"
    if has("height") or "119mm h" in k:
        return "Height"
    if has("air") and (has("flow") or "cfm" in k):
        return "Air Flow"
    if has("static") and has("pressure"):
        return "Static Pressure"
    if has("bearing"):
        return "Bearing Type"
    if has("fan") and has("type"):
        return "Fan Type"
    if has("feature"):
        return "Features"
    if has("noise") or "db" in k:
        return "Noise"
    if (has("power") and "w" in k) or "watts" in k:
        return "Power (Watts)"
    if "rpm" in k:
        return "RPM"
    if has("termination") or has("lead"):
        return "Termination"
    if has("ingress") or has("ip "):
        return "Ingress Protection"
    if (has("operating") and has("temp")) or "temperature" in k:
        return "Operating Temperature"
    if (has("voltage") and has("rated")) or (has("rated") and has("voltage")):
        return "Voltage - Rated"
    if has("approval") or k == "agency approvals":
        return "Approval Agency"
    if has("weight"):
        return "Weight"
    if has("depth") or has("length"):
        return "Depth"
    # Title-case fallback
    return " ".join(w.capitalize() for w in raw_key.split())

def to_map(attrs):
    out = {}
    if not isinstance(attrs, dict):
        return out
    for k, v in attrs.items():
        if v is None or v == "":
            continue
        col = normalize_col(str(k))
        out.setdefault(col, str(v) if not isinstance(v, list) else ", ".join(map(str, v)))
    return out

def norm_val(s: str) -> str:
    return (s or "").strip().lower().replace("  ", " ")

@app.route("/", methods=["GET", "POST"])
def index():
    mpn1 = ""
    mpn2 = ""
    result = None

    if request.method == "POST":
        mpn1 = (request.form.get("mpn1") or "").strip()
        mpn2 = (request.form.get("mpn2") or "").strip()
        if mpn1 and mpn2:
            data1 = fetch_attributes_for_mpn(mpn1) or {}
            data2 = fetch_attributes_for_mpn(mpn2) or {}
            a1 = to_map(data1.get("attributes") or {})
            a2 = to_map(data2.get("attributes") or {})

            # Union of parameter names (normalized)
            keys = sorted(set(list(a1.keys()) + list(a2.keys())))

            rows = []
            for k in keys:
                v1 = a1.get(k, "—")
                v2 = a2.get(k, "—")
                match = (v1 != "—" and v2 != "—" and norm_val(v1) == norm_val(v2))
                rows.append({"param": k, "v1": v1, "v2": v2, "match": match})

            result = {
                "mpn1": mpn1,
                "mpn2": mpn2,
                "rows": rows,
                "url1": data1.get("product_url", ""),
                "url2": data2.get("product_url", ""),
            }

    return render_template("index.html", mpn1=mpn1, mpn2=mpn2, result=result)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False)
