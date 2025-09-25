# EOL/replacement_search.py
from __future__ import annotations
import os
import re
import time
import json
import unicodedata  # NEW: for robust MPN normalization (unicode dashes, etc.)
from typing import Any, Dict, List, Optional, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# Reuse your existing EOL extractor + Digi-Key request/token helpers
# (keeps auth/locale/retry consistent with the rest of EOL).
# ──────────────────────────────────────────────────────────────────────────────
from EOL.eol_attr_extractor import (          # noqa: F401
    fetch_attributes_for_mpn,                 # structured base + ranked_parameters
    _get_digikey_token,                       # DK OAuth helper
    _headers as _dk_headers,                  # DK headers with client + locale
    _request as _dk_request,                  # resilient requests with backoff
)

# Prefer your API clients for normalization (fall back if unavailable).
try:
    from API.digikey import normalize_digikey_result as _normalize_dk  # type: ignore
except Exception:  # pragma: no cover
    _normalize_dk = None  # type: ignore

try:
    from API.mouser import normalize_mouser_result as _normalize_mouser  # type: ignore
except Exception:  # pragma: no cover
    _normalize_mouser = None  # type: ignore

DK_KEYWORD_URL = "https://api.digikey.com/products/v4/search/keyword"
DEBUG = (os.getenv("DK_REPL_DEBUG") or "").lower() in ("1", "true", "yes", "on")


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
_PLACEHOLDER = {"", "-", "—", "n/a", "na", "none", "null"}

def _is_placeholder(val: Optional[str]) -> bool:
    if val is None:
        return True
    s = str(val).strip().lower()
    return s in _PLACEHOLDER

def _norm_mpn(s: Optional[str]) -> str:
    """Normalize an MPN for comparison."""
    return re.sub(r"[^A-Za-z0-9]", "", (s or "").upper())

def _base_tokens(original_mpn: str) -> List[str]:
    """
    Produce conservative 'base' tokens used to detect base-family parts.
    Strategy:
      - normalize to A–Z0–9 and extract numeric runs (len>=3), e.g., '4414' from '4414F'
      - also include the longest leading numeric run (if any), de-duplicated
    """
    s = _norm_mpn(original_mpn)
    tokset = set(re.findall(r"\d{3,}", s))
    m = re.match(r"\d{3,}", s)
    if m:
        tokset.add(m.group(0))
    # Longer tokens first for more selective matching
    return sorted(tokset, key=lambda t: (-len(t), t))

def _values_from_ranked(ranked: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Convert ranked rows into a list of dicts (most→least critical):
      [{"name": <ParameterText>, "value": <ValueText>, "id": <ParameterId?>, "value_id": <ValueId?>}, ...]
    Falls back to 'name' if 'value' is blank (useful for booleans/enums).
    Skips placeholders like '-'/'N/A'.
    """
    out: List[Dict[str, Any]] = []
    for p in ranked or []:
        name = str(p.get("name") or "").strip()
        val_text = str(p.get("value") or "").strip()
        if _is_placeholder(val_text):
            # for boolean/enum style, try using the name as the value token
            val_text = name
        val_id = p.get("value_id") or p.get("ValueId") or p.get("ValueID")
        pid = p.get("id") or p.get("ParameterId") or p.get("Id")
        if not name or _is_placeholder(val_text):
            continue
        out.append({
            "name": name,
            "value": val_text,
            "id": str(pid) if pid is not None else None,
            "value_id": str(val_id) if val_id is not None else None,
        })
    return out


def _extract_products(j: Dict[str, Any]) -> List[Dict[str, Any]]:
    # DK responses vary: Products / products
    prods = j.get("Products") or j.get("products") or []
    return prods if isinstance(prods, list) else []


def _brief_fallback(p: Dict[str, Any]) -> Dict[str, Any]:
    """Minimal, robust normalization if API.digikey.normalize_digikey_result is unavailable."""
    def _as_text(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            for k in ("Value", "value", "Text", "text", "Name", "name", "Url", "URL"):
                vv = v.get(k)
                if isinstance(vv, str) and vv.strip():
                    return vv
        return str(v)

    mpn = _as_text(p.get("ManufacturerPartNumber")).strip().upper()
    manu = _as_text(p.get("Manufacturer")).strip()
    url  = _as_text(p.get("ProductUrl") or p.get("ProductDetailUrl")).strip()
    desc = _as_text(p.get("DetailedDescription") or p.get("ProductDescription") or p.get("ShortDescription")).strip()
    status = _as_text(p.get("ProductStatus")).strip() or None

    return {
        "mpn": mpn,
        "manufacturer": manu,
        "product_url": url,
        "description": desc,
        "lifecycle": status,
        "price": None,
        "availability": _as_text(
            p.get("Availability") or p.get("QuantityAvailable") or p.get("Stock")
        ).strip() or None,
        "datasheet_url": _as_text(p.get("DatasheetUrl")).strip() or "",
        "image_url": None,
        "score": 0.0,
        "match_reasons": [],
        "attributes": {},
    }


def _normalize_products_dk(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in items or []:
        try:
            out.append(_normalize_dk(p) if _normalize_dk else _brief_fallback(p))
        except Exception:
            out.append(_brief_fallback(p))
    return out


def _pick_base_keywords(base: Dict[str, Any]) -> str:
    """
    Choose the main search keywords (the base item). Prefer category/family.
    If it looks like a fan, bias to 'DC Fans' or 'Fans'.
    """
    cand = (
        base.get("category")
        or base.get("family")
        or base.get("Category")
        or base.get("Family")
        or base.get("product_family")
        or ""
    )
    s = str(cand).strip()
    if not s:
        # fallback: try description tokens
        desc = str(base.get("description") or "").lower()
        if "fan" in desc:
            return "DC Fans" if "dc" in desc else "Fans"
        return ""
    # normalize some fan-y variants
    low = s.lower()
    if "fan" in low and "dc" in low:
        return "DC Fans"
    if "fan" in low:
        return "Fans"
    return s


def _keywords_from_values(base_kw: str, values: List[Dict[str, Any]], max_tokens: int = 3) -> str:
    """
    Build a keyword string like "DC Fans 24VDC tubeaxial 119mm".
    Pull a few top remaining values (most critical first).
    """
    tokens: List[str] = []
    for v in values:
        t = (v.get("value") or "").strip()
        if not t:
            continue
        # prefer compact tokens (strip units/commas if possible but keep value semantics)
        compact = re.sub(r"\s+", " ", t)
        tokens.append(compact)
        if len(tokens) >= max_tokens:
            break
    if base_kw:
        return " ".join([base_kw] + tokens)
    return " ".join(tokens)


# ──────────────────────────────────────────────────────────────────────────────
# Digi-Key parameter search
#   We try id-based shapes FIRST (best chance to match), then value-text fallbacks.
#   We ALWAYS include a base Keyword string (e.g., "DC Fans") to anchor the class.
# ──────────────────────────────────────────────────────────────────────────────
def _search_dk_with_filters(
    token: str,
    base_keywords: str,
    triples: List[Dict[str, Any]],  # [{name, value, id?, value_id?}] in rank order
    *,
    record_count: int = 50,
) -> List[Dict[str, Any]]:
    hdrs = _dk_headers(token)

    # Build variants in order of strictness:
    #  1) ParameterId + ValueId   (strongest)
    #  2) ParameterId + ValueText
    #  3) ParameterId + Value
    #  4) ParameterText + ValueText (fallback)
    p_id_valId = [{"ParameterId": t["id"], "ValueId": t["value_id"]}
                  for t in triples if t.get("id") and t.get("value_id")]
    p_id_valTxt = [{"ParameterId": t["id"], "ValueText": t["value"]}
                   for t in triples if t.get("id") and t.get("value")]
    p_id_val    = [{"ParameterId": t["id"], "Value": t["value"]}
                   for t in triples if t.get("id") and t.get("value")]
    p_txt_val   = [{"ParameterText": t["name"], "ValueText": t["value"]}
                   for t in triples if t.get("name") and t.get("value")]

    candidates: List[Dict[str, Any]] = []
    if p_id_valId:
        candidates.append({"Keywords": base_keywords, "RecordCount": record_count,
                           "Filters": {"ParameterFilters": p_id_valId}})
    if p_id_valTxt:
        candidates.append({"Keywords": base_keywords, "RecordCount": record_count,
                           "Filters": {"ParameterFilters": p_id_valTxt}})
    if p_id_val:
        candidates.append({"Keywords": base_keywords, "RecordCount": record_count,
                           "ParameterValueFilters": p_id_val})
    if p_txt_val:
        candidates.append({"Keywords": base_keywords, "RecordCount": record_count,
                           "Filters": {"ParameterFilters": p_txt_val}})

    if DEBUG:
        print(f"[REPL] 🔎 Base Keywords: {base_keywords or '(none)'}")
        print("[REPL] 🔎 DK search payloads (ID-first). Using filters (rank order):")
        for t in triples:
            print(f"        - {t.get('name')} => {t.get('value')}  "
                  f"(id={t.get('id') or 'n/a'}, value_id={t.get('value_id') or 'n/a'})")

    for idx, body in enumerate(candidates, 1):
        if DEBUG:
            filt_summary = []
            if "Filters" in body:
                for k, v in body["Filters"].items():
                    if isinstance(v, list):
                        filt_summary.append(f"{k}[{len(v)}]")
            elif "ParameterValueFilters" in body:
                filt_summary.append(f"ParameterValueFilters[{len(body['ParameterValueFilters'])}]")
            print(f"[REPL] → variant {idx} | keys: {', '.join(sorted(body.keys()))} "
                  f"| filters: {', '.join(filt_summary) or 'n/a'}")

        r = _dk_request("POST", DK_KEYWORD_URL, headers=hdrs, json_body=body)
        if not r:
            continue
        try:
            j = r.json() or {}
        except Exception:
            j = {}
        prods = _extract_products(j)
        if DEBUG:
            print(f"[REPL]   variant {idx} returned {len(prods)} product(s)")
            if not prods:
                msg = (j.get("Message") or j.get("message") or "")[:200]
                if msg:
                    print("[REPL]   DK says:", msg)
        if prods:
            return prods
    return []


def _keyword_fallback(
    token: str, base_keywords: str, remaining: List[Dict[str, Any]], *, record_count: int = 50
) -> List[Dict[str, Any]]:
    """
    When param filters fail, try a plain keyword search:
      - First: base keywords only (e.g., "DC Fans")
      - Then: base + a few top remaining values (e.g., "DC Fans 24VDC Tubeaxial 119mm")
    """
    hdrs = _dk_headers(token)
    attempts: List[str] = []

    # 1) base only
    if base_keywords:
        attempts.append(base_keywords)

    # 2) base + top value tokens
    attempts.append(_keywords_from_values(base_keywords, remaining, max_tokens=3))
    # 3) single top values (just to be thorough)
    for v in remaining[:3]:
        attempts.append(_keywords_from_values(base_keywords, [v], max_tokens=1))

    seen = set()
    for idx, kw in enumerate(attempts, 1):
        kw = kw.strip()
        if not kw or kw.lower() in seen:
            continue
        seen.add(kw.lower())
        body = {"Keywords": kw, "RecordCount": record_count}
        if DEBUG:
            print(f"[REPL] 🔎 Keyword fallback {idx}: '{kw}'")
        r = _dk_request("POST", DK_KEYWORD_URL, headers=hdrs, json_body=body)
        if not r:
            continue
        try:
            j = r.json() or {}
        except Exception:
            j = {}
        prods = _extract_products(j)
        if DEBUG:
            print(f"[REPL]   keyword '{kw}' → {len(prods)} product(s)")
        if prods:
            return prods
    return []


# ──────────────────────────────────────────────────────────────────────────────
# Public API: iterative replacement search (drop least-critical VALUE each try)
# Adds optional 'base_mode' filtering:
#   base_mode=None            -> no base filtering
#   base_mode='exclude_base'  -> filter OUT parts whose MPN contains any base token from the searched MPN
#   base_mode='only_base'     -> keep ONLY parts whose MPN contains a base token from the searched MPN
# ──────────────────────────────────────────────────────────────────────────────
def find_replacements_for_mpn(
    mpn: str,
    *,
    require_results: bool = True,
    base_mode: Optional[str] = None,  # 'exclude_base' | 'only_base' | None
) -> Dict[str, Any]:
    mpn = (mpn or "").strip()
    if not mpn:
        return {"ok": False, "error": "missing mpn"}

    # --- Silence the ranker's console preview around attribute fetches ----
    import contextlib, sys
    def _fetch_attrs_quiet(_mpn: str) -> Dict[str, Any]:
        try:
            with contextlib.redirect_stdout(open(os.devnull, "w")):
                return fetch_attributes_for_mpn(_mpn) or {}
        except Exception:
            return fetch_attributes_for_mpn(_mpn) or {}
    # ----------------------------------------------------------------------

    # 1) Pull attributes + ranked parameters
    base = _fetch_attrs_quiet(mpn)
    ranked = base.get("ranked_parameters") or []

    # If extractor didn't include ranks, synthesize from attributes (best-effort)
    if not ranked:
        attrs = (base.get("attributes") or {}) if isinstance(base, dict) else {}
        ranked = [{"id": f"attr:{k}", "name": k, "value": str(v)} for k, v in attrs.items()]

    # Build ordered (name, value, id?, value_id?) list — strip placeholders
    triples = _values_from_ranked(ranked)

    # Compute base keywords up-front (fans, etc.)
    base_keywords = _pick_base_keywords(base) or ""

    if DEBUG:
        print(f"[REPL] ===== Replacement search for {mpn} =====")
        print(f"[REPL] Base Keywords: {base_keywords or '(none)'}")
        if triples:
            print(f"[REPL] starting with {len(triples)} value(s) (most→least critical):")
            for i, t in enumerate(triples, 1):
                print(f"[REPL]   {i:02d}. {t['name']} => {t['value']} "
                      f"(id={t.get('id') or 'n/a'}, value_id={t.get('value_id') or 'n/a'})")
        else:
            print("[REPL] no usable parameter values available")

    if not triples:
        return {
            "ok": True,
            "iterations": [],
            "used_parameters": [],
            "dropped_parameters": [],
            "products": [],
            "base": base,
            "note": "No usable parameter values available for filtering.",
            "base_mode": base_mode,
            "base_tokens": _base_tokens(mpn),
        }

    # 2) Digi-Key token
    token = _get_digikey_token()
    if not token:
        return {"ok": False, "error": "Digi-Key token not available; check DIGIKEY_* envs."}

    # 3) Iteratively drop least-critical VALUE and retry
    used: List[Dict[str, Any]] = triples[:]
    dropped: List[Dict[str, Any]] = []
    iterations: List[Dict[str, Any]] = []
    products_norm: List[Dict[str, Any]] = []

    original_mpn_norm = _norm_mpn(mpn)
    base_tokens_for_filter = _base_tokens(mpn)

    def contains_base(subj_norm: str) -> bool:
        return any(tok and tok in subj_norm for tok in base_tokens_for_filter)

    while True:
        if DEBUG:
            preview = ", ".join([f"{t['name']}='{t['value']}'" for t in used]) or "(none)"
            print(f"[REPL] attempt #{len(iterations)+1}: searching with {len(used)} value(s): {preview}")

        raw = _search_dk_with_filters(token, base_keywords, used, record_count=50)
        products_norm = _normalize_products_dk(raw)
        if not products_norm:
            if DEBUG:
                print("[REPL]   → no param-filter matches; trying keyword fallbacks…")
            raw_kw = _keyword_fallback(token, base_keywords, used, record_count=50)
            products_norm = _normalize_products_dk(raw_kw)

        before_ct = len(products_norm)
        products_norm = [p for p in products_norm if _norm_mpn(p.get("mpn")) != original_mpn_norm]
        if DEBUG and before_ct != len(products_norm):
            print(f"[REPL]   hard-filter removed {before_ct - len(products_norm)} exact MPN match(es)")

        if base_mode == "exclude_base":
            products_norm = [p for p in products_norm if not contains_base(_norm_mpn(p.get("mpn")))]
        elif base_mode == "only_base":
            products_norm = [p for p in products_norm if contains_base(_norm_mpn(p.get("mpn")))]

        if DEBUG:
            names = [f"{p.get('manufacturer','?')} {p.get('mpn','?')}" for p in products_norm[:5]]
            print(f"[REPL]   → results (post-filter): {len(products_norm)}"
                  f"{' | top: ' + '; '.join(names) if names else ''}")

        iterations.append({
            "attempt": len(iterations) + 1,
            "used_value_count": len(used),
            "dropped_value_count": len(dropped),
            "results": len(products_norm),
            "used_values": [{"name": t["name"], "value": t["value"], "id": t.get("id"), "value_id": t.get("value_id")} for t in used],
        })

        if products_norm or (not require_results):
            break
        if not used:
            break
        t = used[-1]
        if DEBUG:
            print(f"[REPL]   no matches → dropping lowest-ranked value: {t['name']} = {t['value']}")
        dropped.append(used.pop())
        time.sleep(0.2)

    match_reasons = [f"{t['name']} = {t['value']}" for t in used[:6]]
    for p in products_norm[:10]:
        p.setdefault("match_reasons", match_reasons)

    # ──────────────────────────────────────────────────────────────────────────
    # Enrich ALL alternatives with attributes/parameters IN PARALLEL
    #   (Enhanced: try MPN variants tolerant to hyphens/Unicode dashes/spaces)
    # ──────────────────────────────────────────────────────────────────────────
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Dash-ish characters (ASCII '-' plus the usual Unicode suspects)
    DASHES_RX = re.compile(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212\-]+")

    def _mpn_variants(mpn_raw: str) -> List[str]:
        s = (mpn_raw or "").strip()
        if not s:
            return []
        s = unicodedata.normalize("NFC", s)
        variants: List[str] = [s]
        no_dash = DASHES_RX.sub("", s)
        if no_dash and no_dash not in variants:
            variants.append(no_dash)
        spaced = DASHES_RX.sub(" ", s)
        spaced = re.sub(r"\s{2,}", " ", spaced).strip()
        if spaced and spaced not in variants:
            variants.append(spaced)
        first_tok = s.split()[0]
        if first_tok and first_tok not in variants:
            variants.append(first_tok)
        return variants

    def _enrich(p: Dict[str, Any]) -> None:
        try:
            alt_mpn_raw = (p.get("mpn") or "").strip()
            if not alt_mpn_raw:
                return

            add: Dict[str, Any] = {}
            # Try exact, then hyphen/space-tolerant fallbacks
            for mv in _mpn_variants(alt_mpn_raw):
                add = _fetch_attrs_quiet(mv)
                if isinstance(add, dict) and (add.get("attributes") or add.get("ranked_parameters")):
                    break  # success

            # Attach attribute map so the frontend can display the table
            if isinstance(add.get("attributes"), dict) and add["attributes"]:
                p["attributes"] = dict(add["attributes"])  # overwrite/ensure map

            # Also surface the raw parameter rows (id+name+value+value_id)
            ranked = add.get("ranked_parameters") or []
            if ranked:
                p["parameters"] = _values_from_ranked(ranked)

            # Keep helpful links if present and missing on the normalized record
            if add.get("product_url") and not p.get("product_url"):
                p["product_url"] = add["product_url"]
            if add.get("dk_part") and not p.get("dk_part"):
                p["dk_part"] = add["dk_part"]
            if add.get("mouser_part") and not p.get("mouser_part"):
                p["mouser_part"] = add["mouser_part"]

        except Exception as e:
            if DEBUG:
                print("[REPL] enrich alt attributes err:", e)
            # continue enriching remaining items even if one fails
            return

    if products_norm:
        # Allow override, else use 8 workers by default
        try:
            workers = int(os.getenv("REPL_ENRICH_WORKERS", "8"))
        except Exception:
            workers = 8
        workers = max(1, min(32, workers))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_enrich, p) for p in products_norm]
            for _ in as_completed(futs):
                pass  # best-effort; errors already logged when DEBUG

    return {
        "ok": True,
        "iterations": iterations,
        "used_parameters": [{"name": t["name"], "value": t["value"], "id": t.get("id"), "value_id": t.get("value_id")} for t in used],
        "dropped_parameters": [{"name": t["name"], "value": t["value"], "id": t.get("id"), "value_id": t.get("value_id")} for t in dropped],
        "products": products_norm,
        "base": base,
        "base_mode": base_mode,
        "base_tokens": base_tokens_for_filter,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Simple CLI for local testing
#   Example:
#     python -m EOL.replacement_search 4414F --dump
#     python -m EOL.replacement_search 4414F --base-mode exclude_base --dump
# ──────────────────────────────────────────────────────────────────────────────
def _main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Iterative Digi-Key replacement search (keywords anchored + id-first filters + keyword fallback).")
    p.add_argument("mpn", help="Original MPN to replace")
    p.add_argument("--dump", action="store_true", help="Pretty-print JSON")
    # NEW: CLI flag to exercise base filtering
    p.add_argument("--base-mode", choices=["exclude_base", "only_base"], help="Filter base-family parts")
    args = p.parse_args()

    res = find_replacements_for_mpn(args.mpn, base_mode=args.base_mode)
    if args.dump:
        print(json.dumps(res, indent=2))
    else:
        print(f"ok={res.get('ok')} results={len(res.get('products', []))} base_mode={res.get('base_mode')}")
        for it in res.get("iterations", []):
            print(f"  try {it['attempt']}: used={it.get('used_value_count', 0)} dropped={it.get('dropped_value_count', 0)} → results={it['results']}")
        for p in res.get("products", [])[:5]:
            print("  -", p.get("mpn"), "by", p.get("manufacturer"), "→", (p.get("product_url") or "")[:88])
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
