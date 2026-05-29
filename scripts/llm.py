"""Integrazione Ollama: arricchimento LLM e revisione assiomi."""

import json
from pathlib import Path

from rdflib import URIRef

try:
    import requests as _req
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

from .constants import TAU_DOM
from .utils import local_name


# ── Comunicazione Ollama ────────────────────────────────────────

def check_ollama(url: str, model: str) -> bool:
    if not HAS_REQUESTS:
        return False
    try:
        r = _req.get(f"{url}/api/tags", timeout=3)
        if r.status_code != 200:
            return False
        models = [m["name"] for m in r.json().get("models", [])]
        return any(model in m or m in model for m in models)
    except Exception:
        return False


def ask_ollama(url: str, model: str, prompt: str, timeout: int = None) -> str:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.1}
    }
    if timeout is not None and timeout <= 0:
        timeout = None
    r = _req.post(f"{url}/api/chat", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()["message"]["content"].strip()


def _parse_json_response(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(l for l in lines if not l.startswith("```"))
    start = raw.find("{")
    end   = raw.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("LLM non ha restituito JSON valido")
    return json.loads(raw[start:end])


# ── Arricchimento principale ────────────────────────────────────

def enrich_with_llm(analysis: dict, input_path: str,
                    url: str, model: str, timeout: int = None) -> dict:
    classes_info = {local_name(c): len(analysis["class_instances"].get(c, set()))
                    for c in analysis["classes"]}
    obj_props_info = {
        local_name(p): {
            "domain": [local_name(d) for d in info["domains"]],
            "range":  [local_name(r) for r in info["ranges"]],
            "functional":  (p in analysis["functional_props"]),
            "symmetric":   (p in analysis["symmetric_props"]),
            "transitive":  (p in analysis["transitive_props"]),
        }
        for p, info in analysis["object_props"].items()
    }
    data_props_info = {
        local_name(p): {
            "domain": [local_name(d) for d in info["domains"]],
            "range":  [str(r).split("#")[-1] for r in info["ranges"]],
            "functional": (p in analysis["functional_data"]),
        }
        for p, info in analysis["data_props"].items()
    }

    filename = Path(input_path).stem

    prompt = f"""Sei un esperto di ontologie OWL e Web Semantico.
Ti viene fornita l'analisi automatica di una ABox RDF estratta dal file "{filename}".
Devi restituire SOLO un oggetto JSON valido, senza testo aggiuntivo, senza markdown, senza backtick.

## Dati estratti dalla ABox

Classi (nome: numero istanze):
{json.dumps(classes_info, ensure_ascii=False, indent=2)}

Object Properties (nome: dominio, range, caratteristiche rilevate):
{json.dumps(obj_props_info, ensure_ascii=False, indent=2)}

Data Properties (nome: dominio, range, caratteristiche):
{json.dumps(data_props_info, ensure_ascii=False, indent=2)}

SubClassOf già rilevate automaticamente:
{json.dumps({local_name(s): [local_name(p) for p in supers]
             for s, supers in analysis["subclass_candidates"].items()}, ensure_ascii=False)}

DisjointWith già rilevate automaticamente:
{json.dumps([[local_name(URIRef(a)), local_name(URIRef(b))]
             for a, b in analysis["disjoint_pairs"]], ensure_ascii=False)}

## Compito

Restituisci un JSON con questa struttura esatta:
{{
  "dc_meta": {{
    "title": "...",
    "description": "...",
    "creator": "abox_to_tbox.py + LLM"
  }},
  "class_enrichments": {{
    "NomeClasse": {{
      "label_it": "...",
      "label_en": "...",
      "comment": "..."
    }}
  }},
  "property_enrichments": {{
    "nomeProprietà": {{
      "label_it": "...",
      "label_en": "...",
      "comment": "...",
      "functional": true/false,
      "symmetric": true/false,
      "transitive": true/false,
      "inverse_functional": true/false
    }}
  }},
  "extra_subclassof": [
    ["Sottoclasse", "Superclasse"]
  ],
  "extra_disjoint": [
    {{"classA": "NomeClasse", "classB": "NomeClasse", "reason": "motivazione logica"}}
  ]
}}

Regole:
- Usa italiano per label_it e commenti
- I nomi delle classi/proprietà devono corrispondere ESATTAMENTE a quelli forniti
- Non inventare classi o proprietà nuove
- extra_subclassof e extra_disjoint solo se semanticamente giustificato
- Correggi le caratteristiche (functional, symmetric, ecc.) se rilevi errori nell'analisi automatica
"""

    raw = ask_ollama(url, model, prompt, timeout=timeout)
    return _parse_json_response(raw)


def apply_llm_enrichments(analysis: dict, enrichments: dict) -> dict:
    a = dict(analysis)

    cls_map  = {local_name(c): c for c in a["classes"]}
    prop_map = {local_name(p): p
                for p in list(a["object_props"].keys()) + list(a["data_props"].keys())}

    a["llm_class_labels"]  = enrichments.get("class_enrichments", {})
    a["llm_prop_labels"]   = enrichments.get("property_enrichments", {})

    for pname, pdata in enrichments.get("property_enrichments", {}).items():
        uri = prop_map.get(pname)
        if not uri: continue
        if pdata.get("functional")         is False: a["functional_props"].discard(uri)
        if pdata.get("functional")         is True:  a["functional_props"].add(uri)
        if pdata.get("symmetric")          is False: a["symmetric_props"].discard(uri)
        if pdata.get("symmetric")          is True:  a["symmetric_props"].add(uri)
        if pdata.get("transitive")         is False: a["transitive_props"].discard(uri)
        if pdata.get("transitive")         is True:  a["transitive_props"].add(uri)
        if pdata.get("inverse_functional") is True:  a["inv_functional_props"].add(uri)
        if pdata.get("inverse_functional") is False: a["inv_functional_props"].discard(uri)

    for pair in enrichments.get("extra_subclassof", []):
        if len(pair) == 2:
            sub_uri = cls_map.get(pair[0])
            sup_uri = cls_map.get(pair[1])
            if sub_uri and sup_uri:
                a["subclass_candidates"][sub_uri].add(sup_uri)

    for item in enrichments.get("extra_disjoint", []):
        if isinstance(item, dict):
            na, nb = item.get("classA",""), item.get("classB","")
            reason = item.get("reason","")
        elif isinstance(item, list) and len(item) >= 2:
            na, nb, reason = item[0], item[1], ""
        else:
            continue
        ua = cls_map.get(na)
        ub = cls_map.get(nb)
        if ua and ub:
            key = tuple(sorted([str(ua), str(ub)]))
            a["disjoint_pairs"].add(key)
            a.setdefault("disjoint_reasons", {})[key] = reason

    a["llm_dc_meta"] = enrichments.get("dc_meta", {})
    return a


# ── Revisione assiomi a bassa confidence ────────────────────────

def ask_llm_review_low_confidence(analysis: dict, axiom_confidence: dict,
                                   url: str, model: str,
                                   tau: float = TAU_DOM,
                                   timeout: int = None) -> dict:
    low_conf = {
        f"{local_name(prop)}.{role}": {
            "class": local_name(cls),
            "confidence": round(conf, 3),
        }
        for (prop, role), (cls, conf) in axiom_confidence.items()
        if conf < tau
    }

    if not low_conf:
        return {}

    prompt = f"""Sei un ontology engineer esperto.
I seguenti assiomi domain/range hanno confidence bassa (< {tau})
e potrebbero essere semanticamente ambigui.

Assiomi da rivedere:
{json.dumps(low_conf, ensure_ascii=False, indent=2)}

Per ognuno, decidi: ACCEPT o REJECT con una motivazione breve.
Restituisci SOLO JSON valido, senza markdown:
{{"prop.role": {{"decision": "ACCEPT"|"REJECT", "reason": "..."}}}}
"""
    raw = ask_ollama(url, model, prompt, timeout=timeout)
    try:
        return _parse_json_response(raw)
    except (ValueError, json.JSONDecodeError):
        return {}


def apply_llm_review(analysis: dict, review: dict) -> dict:
    axiom_confidence = analysis.get("axiom_confidence", {})
    rejected = set()
    for key, decision in review.items():
        if isinstance(decision, dict) and decision.get("decision") == "REJECT":
            rejected.add(key)

    if rejected:
        new_conf = {}
        for (prop, role), val in axiom_confidence.items():
            k = f"{local_name(prop)}.{role}"
            if k not in rejected:
                new_conf[(prop, role)] = val
        analysis["axiom_confidence"] = new_conf

    return analysis
