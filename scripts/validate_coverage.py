"""
Confronta ABox originale con l'output della pipeline (TBox + ABox merged).

Controlla:
  1. Individui presenti nell'ABox ma mancanti nell'output
  2. Proprietà di individui perse (valore presente nell'ABox ma non nell'output)
  3. Tipi (rdf:type) non preservati
  4. Classi dell'ABox senza dichiarazione owl:Class nel TBox
  5. Proprietà dell'ABox senza dichiarazione nel TBox
  6. Esclusioni intenzionali (PROVENANCE_PROPS, flatten, ecc.)

Uso:
  python -m scripts.validate_coverage <abox.ttl> <output.ttl>
  python -m scripts.validate_coverage architettura.ttl architetture_firenze_v4.ttl
"""

import sys
import json
from collections import defaultdict
from pathlib import Path

from rdflib import Graph, RDF, RDFS, OWL, URIRef, Literal, BNode

# ── Costanti ────────────────────────────────────────────────────

_META_TYPES = {
    str(OWL.NamedIndividual), str(OWL.Class), str(OWL.Ontology),
    str(OWL.ObjectProperty), str(OWL.DatatypeProperty),
    str(OWL.FunctionalProperty), str(OWL.InverseFunctionalProperty),
    str(OWL.TransitiveProperty), str(OWL.SymmetricProperty),
    str(OWL.Restriction),
}
_META_PROPS = {str(RDF.type), str(OWL.sameAs)}

# Proprietà built-in che non richiedono dichiarazione owl:*Property
_BUILTIN_PROPS = {
    str(RDFS.label), str(RDFS.comment), str(RDFS.seeAlso),
    str(RDFS.isDefinedBy), str(RDFS.subClassOf), str(RDFS.domain), str(RDFS.range),
}

SEP  = "─" * 65
SEP2 = "═" * 65


# ── Caricamento config per esclusioni note ──────────────────────

def _load_known_exclusions():
    """Legge esclusioni intenzionali da constants.py e postprocess_config.json."""
    excluded_props   = set()
    excluded_classes = set()   # classi rimosse intenzionalmente (es. da flatten)

    try:
        from .constants import PROVENANCE_PROPS
        excluded_props.update(PROVENANCE_PROPS)
    except ImportError:
        pass

    for candidate in [Path("postprocess_config.json")]:
        if candidate.is_file():
            with open(candidate, encoding="utf-8") as f:
                cfg = json.load(f)
            for rule in cfg.get("flatten", []):
                excluded_props.add(rule["via"])
                if "class" in rule:
                    excluded_classes.add(rule["class"])

    return excluded_props, excluded_classes


# ── Estrazione individui ────────────────────────────────────────

def _get_individuals(g):
    """
    Ritorna {uri_str: {"types": set, "props": {prop_str: set(val_str)}}}
    per tutti gli individui non-TBox del grafo.
    """
    individuals = {}
    for s in set(g.subjects()):
        if not isinstance(s, URIRef):
            continue
        types = {str(o) for o in g.objects(s, RDF.type)
                 if isinstance(o, URIRef) and str(o) not in _META_TYPES}
        if not types:
            continue
        props = defaultdict(set)
        for p, o in g.predicate_objects(s):
            if str(p) in _META_PROPS:
                continue
            if isinstance(o, BNode):
                continue
            props[str(p)].add(str(o))
        individuals[str(s)] = {"types": types, "props": dict(props)}
    return individuals


# ── Utilità ─────────────────────────────────────────────────────

def _short(uri):
    s = str(uri)
    return s.rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def _label(g, uri):
    lbls = list(g.objects(URIRef(uri), RDFS.label))
    if lbls:
        return str(sorted(lbls, key=len)[0])
    return _short(uri)


# ── Confronto principale ────────────────────────────────────────

def validate(abox_path, output_path):
    print(f"\n{SEP2}")
    print(f"  VALIDAZIONE COPERTURA ABox → Output")
    print(f"{SEP2}")
    print(f"  ABox   : {abox_path}")
    print(f"  Output : {output_path}\n")

    abox = Graph()
    abox.parse(abox_path, format="turtle")
    out  = Graph()
    out.parse(output_path, format="turtle")

    excluded_props, excluded_classes = _load_known_exclusions()

    abox_inds = _get_individuals(abox)
    out_inds  = _get_individuals(out)

    # ── 1. Individui mancanti ────────────────────────────────────
    print(f"{SEP}")
    print("  1. INDIVIDUI")
    print(SEP)
    missing_inds = [u for u in abox_inds if u not in out_inds]
    if missing_inds:
        print(f"  [!] {len(missing_inds)} individui ABox NON trovati nell'output:")
        for u in sorted(missing_inds):
            types = ", ".join(_short(t) for t in abox_inds[u]["types"])
            print(f"      • {_short(u):<40} [{types}]")
    else:
        print(f"  [OK] Tutti i {len(abox_inds)} individui ABox sono presenti nell'output.")
    print()

    # ── 2. Proprietà perse per individuo ────────────────────────
    print(SEP)
    print("  2. PROPRIETA' PERSE (per individuo presente)")
    print(SEP)
    prop_losses = []
    intentional = []

    for uri, abox_data in abox_inds.items():
        if uri not in out_inds:
            continue
        out_data = out_inds[uri]
        for prop, abox_vals in abox_data["props"].items():
            if prop in excluded_props:
                intentional.append((uri, prop, abox_vals))
                continue
            out_vals = out_data["props"].get(prop, set())
            lost = abox_vals - out_vals
            if lost:
                prop_losses.append((uri, prop, lost))

    if prop_losses:
        print(f"  [!] {len(prop_losses)} casi di proprietà con valori persi:\n")
        for uri, prop, lost in sorted(prop_losses, key=lambda x: x[0]):
            print(f"      Individuo : {_short(uri)}")
            print(f"      Proprietà : {_short(prop)}")
            for v in sorted(lost):
                print(f"      Valore    : {v[:80]}")
            print()
    else:
        print("  [OK] Nessuna perdita di proprietà rilevata.\n")

    if intentional:
        print(f"  [i] {len(intentional)} triple escluse intenzionalmente (PROVENANCE/flatten):")
        shown_props = set()
        for uri, prop, vals in intentional:
            if prop not in shown_props:
                print(f"      • {_short(prop)}")
                shown_props.add(prop)
        print()

    # ── 3. Tipi (rdf:type) non preservati ───────────────────────
    print(SEP)
    print("  3. TIPI (rdf:type)")
    print(SEP)
    type_losses = []
    for uri, abox_data in abox_inds.items():
        if uri not in out_inds:
            continue
        out_types = out_inds[uri]["types"]
        lost_types = abox_data["types"] - out_types
        if lost_types:
            type_losses.append((uri, lost_types))

    if type_losses:
        print(f"  [!] {len(type_losses)} individui con tipi non preservati:")
        for uri, lost in sorted(type_losses):
            print(f"      • {_short(uri):<40} tipi persi: {', '.join(_short(t) for t in lost)}")
        print()
    else:
        print("  [OK] Tutti i tipi rdf:type sono preservati.\n")

    # ── 4. Classi ABox senza dichiarazione owl:Class nel TBox ───
    print(SEP)
    print("  4. CLASSI ABox → DICHIARAZIONE owl:Class nel TBox")
    print(SEP)
    abox_classes = set()
    for ind_data in abox_inds.values():
        abox_classes.update(ind_data["types"])
    tbox_classes = {str(s) for s in out.subjects(RDF.type, OWL.Class)}
    intentional_cls = abox_classes & excluded_classes
    undeclared_cls  = abox_classes - tbox_classes - excluded_classes
    if undeclared_cls:
        print(f"  [!] {len(undeclared_cls)} classi ABox senza owl:Class nel TBox:")
        for c in sorted(undeclared_cls):
            print(f"      • {_short(c):<40} {c}")
    else:
        print(f"  [OK] Tutte le {len(abox_classes)} classi ABox sono dichiarate nel TBox.")
    if intentional_cls:
        print(f"  [i] {len(intentional_cls)} classi rimosse intenzionalmente (flatten):")
        for c in sorted(intentional_cls):
            print(f"      • {_short(c)}")
    print()

    # ── 5. Proprietà ABox senza dichiarazione nel TBox ──────────
    print(SEP)
    print("  5. PROPRIETA' ABox → DICHIARAZIONE nel TBox")
    print(SEP)
    abox_props = set()
    for ind_data in abox_inds.values():
        abox_props.update(ind_data["props"].keys())
    abox_props -= excluded_props
    abox_props -= _BUILTIN_PROPS

    tbox_props = (
        {str(s) for s in out.subjects(RDF.type, OWL.ObjectProperty)} |
        {str(s) for s in out.subjects(RDF.type, OWL.DatatypeProperty)}
    )
    undeclared_props = abox_props - tbox_props
    if undeclared_props:
        print(f"  [!] {len(undeclared_props)} proprietà ABox senza dichiarazione nel TBox:")
        for p in sorted(undeclared_props):
            print(f"      • {_short(p):<40} {p}")
    else:
        print(f"  [OK] Tutte le {len(abox_props)} proprietà ABox sono dichiarate nel TBox.")
    print()

    # ── 6. Riepilogo ─────────────────────────────────────────────
    print(SEP2)
    print("  RIEPILOGO")
    print(SEP2)
    ok = (not missing_inds and not prop_losses and not type_losses
          and not undeclared_cls and not undeclared_props)
    # Individui "mancanti" che sono in realtà duplicati rimossi o nodi appiattiti
    flatten_cls_uris = excluded_classes
    expected_missing = [u for u in missing_inds
                        if any(t in flatten_cls_uris
                               for t in abox_inds[u]["types"])]
    unexpected_missing = [u for u in missing_inds if u not in expected_missing]
    print(f"  Individui ABox          : {len(abox_inds)}")
    print(f"  Individui nell'output   : {len(out_inds)}")
    print(f"  Individui mancanti      : {len(missing_inds)}"
          f"  (flatten: {len(expected_missing)}, dedup: {len(missing_inds)-len(expected_missing)})")
    print(f"  Perdite di proprietà    : {len(prop_losses)}")
    print(f"  Perdite di tipo         : {len(type_losses)}")
    print(f"  Classi non dichiarate   : {len(undeclared_cls)}")
    print(f"  Proprietà non dichiarate: {len(undeclared_props)}")
    print(f"  Esclusioni intenzionali : {len(set(p for _, p, _ in intentional))} proprietà")
    print()
    if ok:
        print("  RISULTATO: NESSUNA PERDITA RILEVATA")
    else:
        print("  RISULTATO: ATTENZIONE — verificare i punti segnalati sopra")
    print(SEP2 + "\n")


# ── Entry point ──────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print("Uso: python -m scripts.validate_coverage <abox.ttl> <output.ttl>")
        sys.exit(1)
    validate(sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    main()
