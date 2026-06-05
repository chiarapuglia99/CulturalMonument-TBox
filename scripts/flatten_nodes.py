"""
Appiattisce nodi intermedi in un grafo RDF.

Per ogni regola in postprocess_config.json["flatten"]:

  PRIMA:  entity --via--> intermediate --sub_prop--> value
  DOPO:   entity --sub_prop--> value

  L'intermediario viene rimosso dall'ABox.
  Dal TBox vengono rimossi: la proprietà via, la classe intermedia,
  le restrizioni che le referenziano, e vengono aggiornati i domini
  delle sub_properties rilevando le nuove classi padre dal grafo.
"""

import sys
import json
from collections import Counter
from pathlib import Path

from rdflib import Graph, RDF, RDFS, OWL, URIRef

_META = {
    str(OWL.NamedIndividual), str(OWL.Class), str(OWL.Ontology),
    str(OWL.ObjectProperty), str(OWL.DatatypeProperty),
}


def _load_rules(ttl_path):
    for candidate in [Path(ttl_path).parent / "postprocess_config.json",
                      Path("postprocess_config.json")]:
        if candidate.is_file():
            with open(candidate, encoding="utf-8") as f:
                return json.load(f).get("flatten", [])
    return []


def _flatten_one(g, via_prop, intermediate_class, sub_props):
    """Applica una regola di appiattimento al grafo."""

    # 1. Sposta sub-proprietà dall'intermedio all'entità padre
    removed = 0
    for parent, _, intermediate in list(g.triples((None, via_prop, None))):
        if not isinstance(intermediate, URIRef):
            continue
        for sub_prop in sub_props:
            for _, _, value in list(g.triples((intermediate, sub_prop, None))):
                g.add((parent, sub_prop, value))
        g.remove((parent, via_prop, intermediate))
        for p, o in list(g.predicate_objects(intermediate)):
            g.remove((intermediate, p, o))
        for s, p in list(g.subject_predicates(intermediate)):
            g.remove((s, p, intermediate))
        removed += 1

    # 2. Rimuovi restrizioni blank-node che referenziano via_prop o
    #    intermediate_class — PRIMA di rimuovere le dichiarazioni TBox,
    #    altrimenti owl:onProperty e owl:onClass vengono cancellati prima
    #    che possiamo controllarli.
    bnodes_to_remove = set()
    for bnode in list(g.subjects(RDF.type, OWL.Restriction)):
        on_prop  = g.value(bnode, OWL.onProperty)
        on_class = g.value(bnode, OWL.onClass)
        if on_prop == via_prop or on_class == intermediate_class:
            bnodes_to_remove.add(bnode)
    for bnode in bnodes_to_remove:
        for cls in list(g.subjects(RDFS.subClassOf, bnode)):
            g.remove((cls, RDFS.subClassOf, bnode))
        for p, o in list(g.predicate_objects(bnode)):
            g.remove((bnode, p, o))

    # 3. Rimuovi via_prop dal TBox
    for p, o in list(g.predicate_objects(via_prop)):
        g.remove((via_prop, p, o))
    for s, p in list(g.subject_predicates(via_prop)):
        g.remove((s, p, via_prop))

    # 4. Rimuovi la classe intermedia dal TBox
    if intermediate_class:
        for p, o in list(g.predicate_objects(intermediate_class)):
            g.remove((intermediate_class, p, o))
        for s, p in list(g.subject_predicates(intermediate_class)):
            g.remove((s, p, intermediate_class))

    # 5. Pulizia finale: rimuovi blank-node Restriction orfani rimasti
    referenced_bnodes = set(g.objects(None, RDFS.subClassOf))
    for bnode in list(g.subjects(RDF.type, OWL.Restriction)):
        if bnode not in referenced_bnodes:
            for p, o in list(g.predicate_objects(bnode)):
                g.remove((bnode, p, o))

    # 7. Aggiorna rdfs:domain delle sub-proprietà rilevando le nuove classi padre
    for sub_prop in sub_props:
        for old_dom in list(g.objects(sub_prop, RDFS.domain)):
            g.remove((sub_prop, RDFS.domain, old_dom))
        cls_count = Counter()
        for s in g.subjects(sub_prop, None):
            if not isinstance(s, URIRef):
                continue
            for cls in g.objects(s, RDF.type):
                if isinstance(cls, URIRef) and str(cls) not in _META:
                    cls_count[cls] += 1
        if cls_count:
            new_dom = cls_count.most_common(1)[0][0]
            g.add((sub_prop, RDFS.domain, new_dom))

    return removed


def main(inp, outp):
    rules = _load_rules(inp)
    if not rules:
        print("[flatten] Nessuna regola trovata in postprocess_config.json — skip.")
        return

    g = Graph()
    g.parse(inp, format="turtle")
    t_before = len(g)
    total = 0

    for rule in rules:
        via_prop = URIRef(rule["via"])
        inter_cls = URIRef(rule["class"]) if "class" in rule else None
        sub_props = [URIRef(p) for p in rule.get("sub_properties", [])]

        via_name = str(via_prop).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        n = _flatten_one(g, via_prop, inter_cls, sub_props)
        total += n
        if n:
            print(f"  {via_name:<35} {n} nodi appiattiti")

    print(f"\n[flatten] Nodi rimossi: {total} | Triple: {t_before} -> {len(g)}")
    g.serialize(destination=outp, format="turtle")
    print(f"[flatten] Salvato: {outp}")


if __name__ == "__main__":
    inp  = sys.argv[1] if len(sys.argv) > 1 else "architetture_firenze_v4.ttl"
    outp = sys.argv[2] if len(sys.argv) > 2 else inp
    main(inp, outp)
