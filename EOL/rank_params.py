# EOL/rank_params.py
"""
Ranks Digi-Key (or synthesized) parameter fields from MOST FLEXIBLE → LEAST FLEXIBLE
using the OpenAI API. Prints the ranked list and returns it.

Env:
  OPENAI_API_KEY

CLI examples (PowerShell):
  python -m EOL.rank_params --mpn 4414F --category "DC Fans" --params "[{'id':1,'name':'Voltage - Rated','value':'24VDC'},{'id':2,'name':'Air Flow','value':'100 CFM'}]"
  python -m EOL.rank_params --from_json params.json
"""
from __future__ import annotations
import os, json, sys, argparse
from typing import Any, Dict, List, Optional

try:
    from openai import OpenAI
except ImportError:
    raise SystemExit("Missing dependency. Install with:\n  pip install --upgrade openai")

OPENAI_MODEL = os.getenv("RANKER_MODEL", "gpt-4o-mini")
OPENAI_TEMPERATURE = float(os.getenv("RANKER_TEMPERATURE", "0.2"))

Prompt_GUIDE = """\
You are assisting in component replacement engineering.

GOAL:
Rank ALL of the provided specification parameters from MOST CRUCIAL to preserve (parameters that, if changed, would likely force a redesign or cause the part to not function as intended) 
down to LEAST CRUCIAL (parameters that can usually be varied or dropped with minimal redesign risk).

CRITERIA:
- Treat electrical/electronic ratings fundamental to function (e.g., Voltage - Rated, Current, Power, Frequency, Package Size critical to PCB fit, Connector Pin Count, etc.) as the MOST crucial.
- Treat secondary/mechanical/optional features (e.g., packaging style, lead finish, minor tolerances, marking, weight, cosmetic features) as less crucial.
- You MUST return ALL parameters provided, even if they seem trivial. Do not omit any.

OUTPUT:
Return STRICT JSON with schema:

{
  "ranked": [
    {"id": <ParameterId>, "name": "<ParameterName>"},
    ...
  ]
}

IMPORTANT:
- Include EVERY provided parameter exactly once.
- Order them from MOST CRUCIAL (top of list) → LEAST CRUCIAL (bottom of list).
- Do not invent new parameters.
"""

def _client() -> OpenAI:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set.")
    return OpenAI(api_key=key)

def _try_responses_api(client: OpenAI, user_blob: Dict[str, Any]) -> Optional[str]:
    """
    Try the Responses API. Some SDK/model combos don’t support reasoning.* flags.
    We DO NOT send reasoning params to avoid 'unsupported parameter' errors.
    """
    try:
        resp = client.responses.create(
            model=OPENAI_MODEL,
            temperature=OPENAI_TEMPERATURE,
            input=[
                {"role": "system", "content": Prompt_GUIDE},
                {"role": "user", "content": json.dumps(user_blob, ensure_ascii=False)},
            ],
            # Ask for JSON; older SDKs use this header instead of response_format
            extra_headers={"X-Response-Format": "json"},
        )
        content = getattr(resp, "output_text", None)
        if content:
            return content
        # Fallback: try to reconstruct from content array if output_text missing
        try:
            parts = []
            for item in getattr(resp, "output", []) or []:
                for c in getattr(item, "content", []) or []:
                    if getattr(c, "type", None) == "output_text":
                        parts.append(getattr(c, "text", ""))
            return "".join(parts) if parts else None
        except Exception:
            return None
    except Exception:
        return None

def _try_chat_api(client: OpenAI, user_blob: Dict[str, Any]) -> Optional[str]:
    """
    Fallback to Chat Completions with JSON response_format (supported broadly).
    """
    try:
        chat = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=OPENAI_TEMPERATURE,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": Prompt_GUIDE},
                {"role": "user", "content": json.dumps(user_blob, ensure_ascii=False)},
            ],
        )
        return chat.choices[0].message.content
    except Exception:
        return None

def rank_parameter_ids(
    params: List[Dict[str, Any]],
    *,
    mpn: Optional[str] = None,
    category: Optional[str] = None,
    console_preview: bool = True,
) -> List[Dict[str, Any]]:
    """
    Send ONLY ParameterIds/Names to OpenAI for ranking, then REMAP the returned
    order back onto the original ParameterValues. Returns a list of dicts:
      [{"id": "...", "name": "...", "value": "<original value text if any>"}, ...]
    ordered from MOST → LEAST critical.

    This ensures downstream replacement search uses VALUE TEXTS for filtering,
    not the raw ParameterIds.
    """
    if not params:
        return []

    # Map original inputs by id so we can restore values post-ranking
    by_id: Dict[str, Dict[str, Any]] = {}
    compact: List[Dict[str, Any]] = []
    for p in params:
        pid = p.get("id")
        if pid is None:
            continue
        sid = str(pid)
        name = p.get("name")
        val  = p.get("value")
        by_id[sid] = {"id": sid, "name": name, "value": val}
        # Send only id+name to the model (include value for context if present, but ranking must be by field importance)
        item = {"id": sid, "name": name}
        if val:
            # context only; the LLM still outputs {"id","name"} per schema
            item["value"] = val
        compact.append(item)

    user_blob = {"mpn": mpn, "category": category, "parameters": compact}

    client = _client()

    content = _try_responses_api(client, user_blob)
    if not content:
        content = _try_chat_api(client, user_blob)

    # Parse and enforce completeness
    if not content:
        ranked_min = [{"id": p["id"], "name": p["name"]} for p in compact]
    else:
        try:
            data = json.loads(content)
            ranked_min = data.get("ranked") or []
        except Exception:
            ranked_min = []

        # Ensure every provided id appears once
        seen_ids = {str(p.get("id")) for p in ranked_min}
        for p in compact:
            if str(p.get("id")) not in seen_ids:
                ranked_min.append({"id": p["id"], "name": p["name"]})

    # --- NEW: Remap VALUES back onto ranked ids ---
    ranked_with_values: List[Dict[str, Any]] = []
    for row in ranked_min:
        sid = str(row.get("id"))
        base = by_id.get(sid, {"id": sid, "name": row.get("name"), "value": None})
        # Keep id+name from the model row, but always carry through the ORIGINAL value text
        ranked_with_values.append({
            "id": sid,
            "name": row.get("name") if row.get("name") else base.get("name"),
            "value": base.get("value"),  # <- the important part
        })

    if console_preview:
        print("[RANK] Ranked ParameterIds (with re-attached values):")
        for i, r in enumerate(ranked_with_values, 1):
            nm = (r.get("name") or "").strip()
            val = (r.get("value") or "").strip()
            print(f"[RANK]   {i:02d}. {nm}  =>  {val or '(no value)'}  (id={r.get('id')})")

    return ranked_with_values

# ---- CLI -------------------------------------------------------------------
def _load_params_from_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return {"params": data}
    if isinstance(data, dict) and "params" in data:
        return {"params": data["params"], "mpn": data.get("mpn"), "category": data.get("category")}
    raise ValueError("JSON must be a list of params or an object with 'params' key.")

def _main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Rank ParameterIds: MOST FLEXIBLE → LEAST FLEXIBLE")
    p.add_argument("--mpn", type=str, default=None)
    p.add_argument("--category", type=str, default=None)
    p.add_argument("--params", type=str, default=None, help="JSON list of {id,name[,value]}")
    p.add_argument("--from_json", type=str, default=None, help="Path to JSON file")
    args = p.parse_args(argv)

    if not args.params and not args.from_json:
        p.print_help()
        return 2

    if args.from_json:
        loaded = _load_params_from_json(args.from_json)
        params = loaded["params"]
        mpn = args.mpn or loaded.get("mpn")
        category = args.category or loaded.get("category")
    else:
        params = json.loads(args.params)
        mpn, category = args.mpn, args.category

    rank_parameter_ids(params, mpn=mpn, category=category, console_preview=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(_main())
