"""Trova individui duplicati per rdfs:label e confronta le loro proprietà."""
from rdflib import Graph, RDFS, RDF, URIRef, OWL
from collections import defaultdict

g = Graph()
g.parse("architetture_firenze_v3.ttl", format="turtle")

OWL_NAMED = str(OWL.NamedIndividual)

# Individua tutti i soggetti che sono individui (hanno almeno un rdf:type non-meta)
META = {"http://www.w3.org/2002/07/owl#NamedIndividual",
        "http://www.w3.org/2002/07/owl#Class",
        "http://www.w3.org/2002/07/owl#Ontology"}

def get_types(s):
    return [o for _, _, o in g.triples((s, RDF.type, None))
            if isinstance(o, URIRef) and str(o) not in META]

def short(uri):
    s = str(uri)
    return s.rsplit("/", 1)[-1].rsplit("#", 1)[-1]

# Raggruppa per label normalizzato
label_to_inds = defaultdict(list)
for s in sorted(set(g.subjects()), key=str):
    if not isinstance(s, URIRef):
        continue
    types = get_types(s)
    if not types:
        continue
    labels = list(g.objects(s, RDFS.label))
    if not labels:
        continue
    key = str(labels[0]).strip().lower()
    label_to_inds[key].append(s)

duplicates = {k: v for k, v in label_to_inds.items() if len(v) > 1}

print(f"{'='*65}")
print(f"Gruppi con rdfs:label identico: {len(duplicates)}")
print(f"{'='*65}\n")

for label, inds in sorted(duplicates.items()):
    print(f"LABEL: \"{label}\"  ({len(inds)} individui)")
    prop_sets = []
    for ind in inds:
        types = get_types(ind)
        props = {}
        for _, p, o in g.triples((ind, None, None)):
            pstr = str(p)
            if pstr in (str(RDF.type), str(RDFS.label)):
                continue
            props[short(p)] = str(o)[:80]
        prop_sets.append((ind, types, props))
        print(f"  [{short(ind)}]  tipo: {', '.join(short(t) for t in types)}")
        for pname, val in sorted(props.items()):
            print(f"    {pname:<25} {val}")

    # Confronto: proprietà uguali vs diverse
    if len(prop_sets) == 2:
        p1, p2 = prop_sets[0][2], prop_sets[1][2]
        all_keys = set(p1) | set(p2)
        same = [k for k in all_keys if p1.get(k) == p2.get(k)]
        diff = [k for k in all_keys if p1.get(k) != p2.get(k)]
        only1 = [k for k in all_keys if k in p1 and k not in p2]
        only2 = [k for k in all_keys if k in p2 and k not in p1]
        print(f"  >> Proprieta identiche : {same if same else '-'}")
        print(f"  >> Proprieta diverse   : {diff if diff else '-'}")
        print(f"  >> Solo nel primo      : {only1 if only1 else '-'}")
        print(f"  >> Solo nel secondo    : {only2 if only2 else '-'}")
    print()
