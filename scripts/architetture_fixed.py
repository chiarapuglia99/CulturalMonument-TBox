"""
Fix label generici per individui ABox.

Comportamento:
  1. Cerca postprocess_config.json (nella dir del TTL, poi nella CWD).
     Se trovato, usa la mappatura classe→proprietà definita lì.
  2. Se la config manca (o una classe non è presente), auto-rileva la
     proprietà datatype con il miglior bilanciamento unicità/copertura/brevità.
  3. Per classi senza proprietà datatype (es. aggregatori come OnlineContactPoint),
     compone il label dai label dei nodi referenziati.

Compatibile con qualsiasi ontologia: nessun URI hardcoded.
"""

import sys
import json
from collections import defaultdict
from pathlib import Path

from rdflib import Graph, Literal, RDFS, RDF, OWL, URIRef, XSD

_META = {
    str(OWL.NamedIndividual), str(OWL.Class), str(OWL.Ontology),
    str(OWL.ObjectProperty), str(OWL.DatatypeProperty),
    str(OWL.FunctionalProperty),
}
_SKIP = {str(RDF.type), str(RDFS.label)}
_SEMANTIC_HINTS = {"name", "label", "title", "address", "url", "email",
                   "phone", "telephone", "number", "identifier", "code"}


# ── Caricamento config ──────────────────────────────────────────

def _load_config(ttl_path):
    for candidate in [Path(ttl_path).parent / "postprocess_config.json",
                      Path("postprocess_config.json")]:
        if candidate.is_file():
            with open(candidate, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("fix_labels", {}), data.get("class_labels", {})
    return {}, {}


# ── Utilità grafo ───────────────────────────────────────────────

def _instances(g, cls):
    return [s for s in g.subjects(RDF.type, cls) if isinstance(s, URIRef)]


def _domain_classes(g):
    classes = set()
    for s in g.subjects(RDF.type, None):
        if not isinstance(s, URIRef):
            continue
        for o in g.objects(s, RDF.type):
            if isinstance(o, URIRef) and str(o) not in _META:
                classes.add(o)
    return classes


# ── Strategia 1: proprietà specificata in config ────────────────

def _apply_config_class(g, cls_uri, cfg_entry, convert_anyuri_set):
    instances = _instances(g, cls_uri)
    if not instances:
        return 0

    # Caso "componi dai label dei nodi referenziati"
    if "compose_from_labels_of" in cfg_entry:
        props = [URIRef(p) for p in cfg_entry["compose_from_labels_of"]]
        sep = cfg_entry.get("separator", " | ")
        updated = 0
        for ind in instances:
            parts = []
            for prop in props:
                for o in g.objects(ind, prop):
                    if isinstance(o, URIRef):
                        lbls = list(g.objects(o, RDFS.label))
                        if lbls:
                            parts.append(str(lbls[0]).strip())
            new_lbl = sep.join(p for p in parts if p)
            if new_lbl:
                for lbl in list(g.objects(ind, RDFS.label)):
                    g.remove((ind, RDFS.label, lbl))
                g.add((ind, RDFS.label, Literal(new_lbl)))
                updated += 1
        return updated

    # Caso "usa proprietà esplicita"
    prop_uri = URIRef(cfg_entry["property"])
    convert_anyuri = cfg_entry.get("convert_to_anyuri", False)
    if convert_anyuri:
        convert_anyuri_set.add(prop_uri)

    updated = 0
    for ind in instances:
        vals = list(g.objects(ind, prop_uri))
        if not vals:
            continue
        raw = vals[0]
        new_lbl = str(raw).strip()
        if not new_lbl:
            continue

        # Converti URIRef → Literal xsd:anyURI se richiesto
        if convert_anyuri and isinstance(raw, URIRef):
            g.remove((ind, prop_uri, raw))
            g.add((ind, prop_uri, Literal(str(raw), datatype=XSD.anyURI)))

        for lbl in list(g.objects(ind, RDFS.label)):
            g.remove((ind, RDFS.label, lbl))
        g.add((ind, RDFS.label, Literal(new_lbl)))
        updated += 1
    return updated


# ── Strategia 2: auto-rilevamento ──────────────────────────────

def _best_data_prop(g, instances):
    """
    Proprietà datatype con il miglior bilanciamento di:
    - copertura >= 50% delle istanze
    - unicità >= 50% dei valori
    - lunghezza media <= 250 char
    Bonus per proprietà con nome semantico (name, url, address …).
    """
    prop_vals = defaultdict(list)
    for ind in instances:
        for p, o in g.predicate_objects(ind):
            if str(p) in _SKIP:
                continue
            if isinstance(o, Literal):
                prop_vals[p].append(str(o))

    best_prop, best_score = None, -1.0
    n = len(instances)
    for prop, vals in prop_vals.items():
        coverage = len(vals) / n
        if coverage < 0.5:
            continue
        avg_len = sum(len(v) for v in vals) / len(vals)
        if avg_len > 250:
            continue
        unique_ratio = len(set(vals)) / len(vals)
        if unique_ratio < 0.4:
            continue
        pname = str(prop).rsplit("/", 1)[-1].rsplit("#", 1)[-1].lower()
        bonus = 0.2 if any(h in pname for h in _SEMANTIC_HINTS) else 0.0
        len_score = max(0.0, 1.0 - avg_len / 250)
        score = coverage * 0.3 + unique_ratio * 0.5 + len_score * 0.1 + bonus
        if score > best_score:
            best_score, best_prop = score, prop
    return best_prop


def _compose_from_refs(g, ind):
    """Compone un label dai label dei nodi referenziati (fallback per aggregatori)."""
    parts = []
    for _, o in sorted(g.predicate_objects(ind), key=lambda x: str(x[0])):
        if not isinstance(o, URIRef) or str(o) in _META:
            continue
        lbls = list(g.objects(o, RDFS.label))
        if lbls:
            val = str(lbls[0]).strip()
            if val and len(val) <= 120:
                parts.append(val)
    return " | ".join(parts) if parts else None



def _apply_auto_class(g, cls):
    instances = _instances(g, cls)
    if len(instances) < 2:
        return 0

    best_prop = _best_data_prop(g, instances)
    updated = 0
    for ind in instances:
        cur_lbls = [str(l).strip() for l in g.objects(ind, RDFS.label)]
        cur_lbl  = cur_lbls[0] if cur_lbls else ""

        if best_prop:
            vals = list(g.objects(ind, best_prop))
            new_lbl = str(vals[0]).strip() if vals else None
        else:
            new_lbl = _compose_from_refs(g, ind)

        if not new_lbl:
            continue
        # Aggiorna se: label assente, uguale al nome della classe,
        # oppure la nuova proprietà dà un valore più corto e diverso
        if (not cur_lbl
                or new_lbl == cur_lbl
                or len(new_lbl) < len(cur_lbl)):
            if new_lbl != cur_lbl:
                for lbl in list(g.objects(ind, RDFS.label)):
                    g.remove((ind, RDFS.label, lbl))
                g.add((ind, RDFS.label, Literal(new_lbl)))
                updated += 1
    return updated


# ── Entry point ─────────────────────────────────────────────────

def main(inp, outp):
    g = Graph()
    g.parse(inp, format="turtle")

    config, class_labels = _load_config(inp)
    if config:
        print(f"[fix_labels] Config trovata: {len(config)} classi configurate")
    else:
        print("[fix_labels] Nessuna config — uso auto-rilevamento")

    # Applica label di classe da config (it + en)
    lbl_fixed = 0
    for cls_str, labels in class_labels.items():
        cls_uri = URIRef(cls_str)
        for _, _, o in list(g.triples((cls_uri, RDFS.label, None))):
            g.remove((cls_uri, RDFS.label, o))
        if labels.get("it"):
            g.add((cls_uri, RDFS.label, Literal(labels["it"], lang="it")))
            lbl_fixed += 1
        if labels.get("en"):
            g.add((cls_uri, RDFS.label, Literal(labels["en"], lang="en")))
    if lbl_fixed:
        print(f"[fix_labels] {lbl_fixed} label di classe aggiornati da config")

    convert_anyuri_set = set()
    updated = 0

    # Applica config per le classi note
    for cls_str, cfg_entry in config.items():
        cls_uri = URIRef(cls_str)
        n = _apply_config_class(g, cls_uri, cfg_entry, convert_anyuri_set)
        updated += n

    # Auto-rilevamento per le classi non in config
    configured_uris = {URIRef(k) for k in config}
    for cls in _domain_classes(g):
        if cls not in configured_uris:
            updated += _apply_auto_class(g, cls)

    print(f"Aggiornati {updated} individui -> {outp}")
    g.serialize(destination=outp, format="turtle")


if __name__ == "__main__":
    inp  = sys.argv[1] if len(sys.argv) > 1 else "architetture_firenze_v3.ttl"
    outp = sys.argv[2] if len(sys.argv) > 2 else inp
    main(inp, outp)
