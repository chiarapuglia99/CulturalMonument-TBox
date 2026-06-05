"""
Deduplicazione individui ABox — generico, zero hardcoding.

Algoritmo:
  1. Rileva tutte le classi di dominio dal grafo.
  2. Ordina le classi topologicamente: prima le foglie (referenziate),
     poi gli aggregatori (che referenziano le foglie), garantendo la
     propagazione a cascata delle dedup.
  3. Per ogni classe, raggruppa gli individui per fingerprint
     (tutte le coppie property→value esclusi rdf:type e rdfs:label).
  4. Mantiene l'URI lessicograficamente minima come canonico,
     reindirizza tutti i riferimenti ai duplicati e li rimuove.
"""

import sys
from collections import defaultdict
from rdflib import Graph, RDF, RDFS, OWL, URIRef

_META = {
    str(OWL.NamedIndividual), str(OWL.Class), str(OWL.Ontology),
    str(OWL.ObjectProperty), str(OWL.DatatypeProperty),
    str(OWL.FunctionalProperty), str(OWL.InverseFunctionalProperty),
    str(OWL.TransitiveProperty), str(OWL.SymmetricProperty),
    str(RDFS.Class),
}
_SKIP = {str(RDF.type), str(RDFS.label), str(OWL.sameAs)}


def _domain_classes(g):
    classes = set()
    for s in g.subjects(RDF.type, None):
        if not isinstance(s, URIRef):
            continue
        for o in g.objects(s, RDF.type):
            if isinstance(o, URIRef) and str(o) not in _META:
                classes.add(o)
    return classes


def _instances(g, cls):
    return [s for s in g.subjects(RDF.type, cls) if isinstance(s, URIRef)]


def _fingerprint(g, ind):
    pairs = [(str(p), str(o))
             for p, o in g.predicate_objects(ind)
             if str(p) not in _SKIP]
    return tuple(sorted(pairs))


def _topo_order(g, classes):
    """
    Ordine topologico: una classe X viene processata prima di Y se individui
    di Y hanno object-property che puntano a individui di X (X = foglia).
    """
    inst_cls = {}
    for cls in classes:
        for ind in _instances(g, cls):
            inst_cls.setdefault(ind, cls)

    deps = {c: set() for c in classes}
    for cls in classes:
        for ind in _instances(g, cls):
            for _, o in g.predicate_objects(ind):
                if isinstance(o, URIRef) and o in inst_cls and inst_cls[o] != cls:
                    deps[cls].add(inst_cls[o])

    ordered, remaining = [], set(classes)
    while remaining:
        ready = sorted([c for c in remaining if not deps[c] & remaining], key=str)
        if not ready:
            ready = [min(remaining, key=str)]  # ciclo: forza avanzamento
        for c in ready:
            ordered.append(c)
            remaining.discard(c)
    return ordered


def _dedup_class(g, cls):
    fp_map = defaultdict(list)
    for ind in _instances(g, cls):
        fp = _fingerprint(g, ind)
        if fp:
            fp_map[fp].append(ind)

    removed = 0
    for inds in fp_map.values():
        if len(inds) < 2:
            continue
        canonical = min(inds, key=str)
        for dup in sorted(inds, key=str):
            if dup == canonical:
                continue
            for s, p in list(g.subject_predicates(dup)):
                g.remove((s, p, dup))
                g.add((s, p, canonical))
            for p, o in list(g.predicate_objects(dup)):
                g.remove((dup, p, o))
            removed += 1
    return removed


def main(inp, outp):
    g = Graph()
    g.parse(inp, format="turtle")

    classes = _domain_classes(g)
    order = _topo_order(g, classes)

    n_before = sum(1 for s in set(g.subjects(RDF.type, None)) if isinstance(s, URIRef))
    t_before = len(g)
    print(f"[dedup] Individui prima : {n_before} | Triple prima : {t_before}")

    total = 0
    for cls in order:
        removed = _dedup_class(g, cls)
        if removed:
            cname = str(cls).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            print(f"  {cname:<30} {removed} duplicati rimossi")
        total += removed

    n_after = sum(1 for s in set(g.subjects(RDF.type, None)) if isinstance(s, URIRef))
    print(f"\n[dedup] Totale rimossi   : {total}")
    print(f"[dedup] Individui: {n_before} → {n_after} | Triple: {t_before} → {len(g)}")
    g.serialize(destination=outp, format="turtle")
    print(f"[dedup] Salvato: {outp}")


if __name__ == "__main__":
    inp  = sys.argv[1] if len(sys.argv) > 1 else "architetture_firenze_v3.ttl"
    outp = sys.argv[2] if len(sys.argv) > 2 else inp
    main(inp, outp)
