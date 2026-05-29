"""Analisi ABox: estrazione classi, proprietà, SubClassOf, confidence."""

from collections import defaultdict

from rdflib import RDF, URIRef, Literal

from .constants import SKIP_PROPS, META_CLASSES, TAU_SUB, TAU_SIM
from .utils import local_name, xsd_of, cosine_sim, HAS_SBERT


def analyze_abox(g) -> dict:
    classes     = set()
    data_props  = defaultdict(lambda: {"domains": set(), "ranges": set(),
                                       "values_per_subject": []})
    obj_props   = defaultdict(lambda: {"domains": set(), "ranges": set(),
                                       "pairs": set(),
                                       "values_per_subject": defaultdict(set),
                                       "subjects_per_value": defaultdict(set)})
    ind_cls     = defaultdict(set)

    for s, _, o in g.triples((None, RDF.type, None)):
        if isinstance(s, URIRef) and isinstance(o, URIRef) and str(o) not in META_CLASSES:
            classes.add(o); ind_cls[s].add(o)

    for s, p, o in g:
        if str(p) in SKIP_PROPS or not isinstance(s, URIRef): continue
        sc = ind_cls.get(s, set())
        if isinstance(o, Literal):
            data_props[p]["ranges"].add(xsd_of(o))
            data_props[p]["domains"].update(sc)
            data_props[p]["values_per_subject"].append((s, o))
        elif isinstance(o, URIRef):
            oc = ind_cls.get(o, set())
            obj_props[p]["domains"].update(sc)
            obj_props[p]["ranges"].update(oc)
            obj_props[p]["pairs"].add((s, o))
            obj_props[p]["values_per_subject"][s].add(o)
            obj_props[p]["subjects_per_value"][o].add(s)

    cls_insts = defaultdict(set)
    for ind, cs in ind_cls.items():
        for c in cs: cls_insts[c].add(ind)

    # Rule 1 del paper: SubClassOf con soglie τ_sub e τ_sim
    sub_cands = defaultdict(set)
    for a in classes:
        for b in classes:
            if a == b: continue
            ia, ib = cls_insts[a], cls_insts[b]
            if not ia or not ib: continue
            # Overlap reale: quante istanze di A sono anche istanze di B
            overlap = len(ia & ib) / len(ia)
            if overlap > TAU_SUB:
                if HAS_SBERT:
                    sim = cosine_sim(local_name(a), local_name(b))
                    if sim > TAU_SIM:
                        sub_cands[a].add(b)
                else:
                    sub_cands[a].add(b)

    disjoint_pairs = set()

    functional_props     = set()
    inv_functional_props = set()
    symmetric_props      = set()
    transitive_props     = set()
    inverse_pairs        = {}

    for prop, info in obj_props.items():
        pairs = info["pairs"]
        if not pairs: continue
        if all(len(v) <= 1 for v in info["values_per_subject"].values()):
            functional_props.add(prop)
        if all(len(s) <= 1 for s in info["subjects_per_value"].values()):
            inv_functional_props.add(prop)
        if all((o, s) in pairs for s, o in pairs):
            symmetric_props.add(prop)
        if len(pairs) >= 2 and any(
            (a, c) in pairs for (a, b) in pairs for (b2, c) in pairs
            if b == b2 and a != c
        ):
            transitive_props.add(prop)

    prop_list = list(obj_props.keys())
    for i, p in enumerate(prop_list):
        for q in prop_list[i+1:]:
            pp = obj_props[p]["pairs"]
            qp = obj_props[q]["pairs"]
            if pp and qp and {(b, a) for a, b in pp} == qp:
                inverse_pairs[p] = q; inverse_pairs[q] = p

    functional_data = set()
    for prop, info in data_props.items():
        vps = defaultdict(set)
        for (s, o) in info["values_per_subject"]: vps[s].add(str(o))
        if vps and all(len(v) <= 1 for v in vps.values()):
            functional_data.add(prop)

    # Confidence scores per domain/range (Rule 2 del paper)
    axiom_confidence = {}
    for prop, info in obj_props.items():
        total = len(info["pairs"])
        if not total: continue
        dom_counts = defaultdict(int)
        for s, _ in info["pairs"]:
            for cls in ind_cls.get(s, set()):
                dom_counts[cls] += 1
        if dom_counts:
            best_dom = max(dom_counts, key=dom_counts.get)
            conf_dom = dom_counts[best_dom] / total
            axiom_confidence[(prop, "domain")] = (best_dom, conf_dom)
        rng_counts = defaultdict(int)
        for _, o in info["pairs"]:
            for cls in ind_cls.get(o, set()):
                rng_counts[cls] += 1
        if rng_counts:
            best_rng = max(rng_counts, key=rng_counts.get)
            conf_rng = rng_counts[best_rng] / total
            axiom_confidence[(prop, "range")] = (best_rng, conf_rng)

    for prop, info in data_props.items():
        total = len(info["values_per_subject"])
        if not total: continue
        dom_counts = defaultdict(int)
        for s, _ in info["values_per_subject"]:
            for cls in ind_cls.get(s, set()):
                dom_counts[cls] += 1
        if dom_counts:
            best_dom = max(dom_counts, key=dom_counts.get)
            conf_dom = dom_counts[best_dom] / total
            axiom_confidence[(prop, "domain")] = (best_dom, conf_dom)

    restrictions = {}
    for cls in classes:
        insts = cls_insts[cls]
        if not insts: continue
        for prop, info in obj_props.items():
            if not info["domains"].intersection({cls}): continue
            counts = [len(info["values_per_subject"].get(ind, set())) for ind in insts]
            if not counts: continue
            mn, mx = min(counts), max(counts)
            entry = {"min": mn, "max": mx}
            if mn == mx: entry["exact"] = mn
            if mn > 0 or mx < 999:
                restrictions[(cls, prop)] = entry

    return {
        "classes":             classes,
        "data_props":          data_props,
        "object_props":        obj_props,
        "subclass_candidates": sub_cands,
        "class_instances":     cls_insts,
        "individual_classes":  ind_cls,
        "disjoint_pairs":      disjoint_pairs,
        "functional_props":    functional_props,
        "inv_functional_props":inv_functional_props,
        "symmetric_props":     symmetric_props,
        "transitive_props":    transitive_props,
        "inverse_pairs":       inverse_pairs,
        "functional_data":     functional_data,
        "restrictions":        restrictions,
        "axiom_confidence":    axiom_confidence,
        "equiv_classes":       {},
        "llm_class_labels":    {},
        "llm_prop_labels":     {},
        "llm_dc_meta":         {},
    }
