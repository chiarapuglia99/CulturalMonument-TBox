"""Generazione TBox OWL: classi, proprietà, restrizioni, merge."""

import datetime
from collections import defaultdict

from rdflib import Graph, RDF, RDFS, OWL, XSD, URIRef, Literal, BNode

from .constants import (DC, KNOWN_PREFIXES, SKIP_PROPS, META_CLASSES,
                        TAU_DOM, TBOX_PREDICATES, TBOX_TYPES, PROVENANCE_PROPS)
from .utils import local_name


# ── Utilità domain/range ────────────────────────────────────────

def _add_dr(tbox, prop, domains, ranges, is_data,
            axiom_confidence=None, tau_dom=TAU_DOM):
    if axiom_confidence is not None:
        dom_entry = axiom_confidence.get((prop, "domain"))
        if dom_entry:
            best_dom, conf = dom_entry
            if conf > tau_dom:
                tbox.add((prop, RDFS.domain, best_dom))
                tbox.add((prop, RDFS.comment,
                          Literal(f"domain: {local_name(best_dom)} "
                                  f"(confidence: {conf:.1%})")))
        rng_entry = axiom_confidence.get((prop, "range"))
        if rng_entry:
            best_rng, conf = rng_entry
            if conf > tau_dom:
                tbox.add((prop, RDFS.range, best_rng))
                tbox.add((prop, RDFS.comment,
                          Literal(f"range: {local_name(best_rng)} "
                                  f"(confidence: {conf:.1%})")))
            return
        if not ranges: return
        if len(ranges) == 1:
            tbox.add((prop, RDFS.range, next(iter(ranges))))
        else:
            if is_data:
                all_str = all(r in (XSD.string, RDF.langString) for r in ranges)
                tbox.add((prop, RDFS.range, XSD.string if all_str else XSD.anySimpleType))
            else:
                tbox.add((prop, RDFS.range, OWL.Thing))
    else:
        for d in sorted(domains, key=str): tbox.add((prop, RDFS.domain, d))
        if not ranges: return
        if len(ranges) == 1:
            tbox.add((prop, RDFS.range, next(iter(ranges))))
        else:
            if is_data:
                all_str = all(r in (XSD.string, RDF.langString) for r in ranges)
                tbox.add((prop, RDFS.range, XSD.string if all_str else XSD.anySimpleType))
            else:
                tbox.add((prop, RDFS.range, OWL.Thing))


# ── Classi equivalenti e deduplicazione ─────────────────────────

def detect_equivalent_classes(analysis: dict, src: Graph) -> dict:
    cls_by_lname = defaultdict(list)
    for cls in analysis["classes"]:
        cls_by_lname[local_name(cls)].append(cls)

    PREFER_NS = ["dati.gov.it", "linkedopendata", "example.org"]
    equiv = {}
    for uris in cls_by_lname.values():
        if len(uris) < 2:
            continue
        canonical = uris[0]
        for uri in uris:
            if any(ns in str(uri) for ns in PREFER_NS):
                canonical = uri
                break
        aliases = [u for u in uris if u != canonical]
        equiv[canonical] = aliases

    # SubClassOf reciproco (A ⊆ B e B ⊆ A): tiene solo specifico → generico
    sub_cands = analysis.get("subclass_candidates", {})
    seen = set()
    for a, supers_a in list(sub_cands.items()):
        for b in list(supers_a):
            if b in sub_cands and a in sub_cands[b]:
                pair = tuple(sorted([str(a), str(b)]))
                if pair in seen:
                    continue
                seen.add(pair)
                child, parent = _resolve_hierarchy(a, b, analysis)
                # Tiene solo child ⊆ parent
                sub_cands[child].add(parent)
                sub_cands[child].discard(child)
                sub_cands[parent].discard(child)
                if parent in sub_cands.get(child, set()):
                    pass  # già corretto
                # Rimuove la direzione inversa
                if child in sub_cands.get(parent, set()):
                    sub_cands[parent].discard(child)

    return equiv


def _resolve_hierarchy(a, b, analysis):
    """Determina chi è figlio e chi è padre tra due classi con SubClassOf reciproco.
    Euristica: la classe con più proprietà esclusive è più specifica (figlio)."""
    obj_props = analysis.get("object_props", {})
    data_props = analysis.get("data_props", {})

    def exclusive_props(cls):
        count = 0
        for info in obj_props.values():
            if cls in info["domains"]:
                count += 1
        for info in data_props.values():
            if cls in info["domains"]:
                count += 1
        return count

    pa, pb = exclusive_props(a), exclusive_props(b)
    if pa != pb:
        # Più proprietà = più specifico = figlio
        return (a, b) if pa > pb else (b, a)
    # Fallback: nome più lungo = più specifico
    if len(local_name(a)) != len(local_name(b)):
        return (a, b) if len(local_name(a)) > len(local_name(b)) else (b, a)
    # Ultimo fallback: ordine alfabetico
    return (a, b) if str(a) > str(b) else (b, a)


def deduplicate_domains(domains: set, equiv_map: dict) -> set:
    alias_to_canonical = {}
    for canonical, aliases in equiv_map.items():
        for alias in aliases:
            alias_to_canonical[alias] = canonical
    result = set()
    for d in domains:
        canonical = alias_to_canonical.get(d, d)
        result.add(canonical)
    return result


_RESTRICTION_CONSTRAINT_PROPS = {
    OWL.someValuesFrom, OWL.allValuesFrom, OWL.hasValue,
    OWL.cardinality, OWL.minCardinality, OWL.maxCardinality,
    OWL.qualifiedCardinality, OWL.minQualifiedCardinality, OWL.maxQualifiedCardinality,
}


def _remove_malformed_restrictions(tbox: Graph) -> int:
    """Rimuove blank-node owl:Restriction privi di onProperty o senza alcun vincolo."""
    removed = 0
    for bnode in list(tbox.subjects(RDF.type, OWL.Restriction)):
        has_prop = tbox.value(bnode, OWL.onProperty) is not None
        has_constraint = any(
            tbox.value(bnode, cp) is not None
            for cp in _RESTRICTION_CONSTRAINT_PROPS
        )
        if not has_prop or not has_constraint:
            for cls in list(tbox.subjects(RDFS.subClassOf, bnode)):
                tbox.remove((cls, RDFS.subClassOf, bnode))
            for p, o in list(tbox.predicate_objects(bnode)):
                tbox.remove((bnode, p, o))
            removed += 1
    return removed


def deduplicate_restrictions(tbox: Graph):
    fingerprint_to_bnodes = defaultdict(list)
    for bnode in list(tbox.subjects(RDF.type, OWL.Restriction)):
        prop  = tbox.value(bnode, OWL.onProperty)
        card  = tbox.value(bnode, OWL.cardinality)
        minc  = tbox.value(bnode, OWL.minCardinality)
        maxc  = tbox.value(bnode, OWL.maxCardinality)
        qcard = tbox.value(bnode, OWL.qualifiedCardinality)
        minqc = tbox.value(bnode, OWL.minQualifiedCardinality)
        maxqc = tbox.value(bnode, OWL.maxQualifiedCardinality)
        some  = tbox.value(bnode, OWL.someValuesFrom)
        allv  = tbox.value(bnode, OWL.allValuesFrom)
        oncls = tbox.value(bnode, OWL.onClass)
        fp = (str(prop), str(card), str(minc), str(maxc),
              str(qcard), str(minqc), str(maxqc),
              str(some), str(allv), str(oncls))
        fingerprint_to_bnodes[fp].append(bnode)

    removed = 0
    for fp, bnodes in fingerprint_to_bnodes.items():
        if len(bnodes) < 2:
            continue
        canonical = bnodes[0]
        for dup in bnodes[1:]:
            for cls in list(tbox.subjects(RDFS.subClassOf, dup)):
                tbox.add((cls, RDFS.subClassOf, canonical))
                tbox.remove((cls, RDFS.subClassOf, dup))
            for p, o in list(tbox.predicate_objects(dup)):
                tbox.remove((dup, p, o))
            removed += 1
    return removed


def dedup_restrictions_analysis(analysis: dict, equiv_map: dict):
    alias_to_can = {str(a): str(c) for c, aliases in equiv_map.items() for a in aliases}

    new_restrictions = {}
    seen = {}
    for (cls, prop), entry in analysis["restrictions"].items():
        can_cls = URIRef(alias_to_can.get(str(cls), str(cls)))
        key = (str(can_cls), str(prop),
               entry.get("exact", entry.get("min","?"), ),
               entry.get("max","?"))
        if key not in seen:
            seen[key] = True
            new_restrictions[(can_cls, prop)] = entry

    analysis["restrictions"] = new_restrictions


# ── Disgiunzioni semantiche ─────────────────────────────────────

def compute_disjoint_semantic(analysis: dict) -> set:
    classes     = analysis["classes"]
    obj_props   = analysis["object_props"]
    data_props  = analysis["data_props"]
    sub_cands   = analysis["subclass_candidates"]

    cls_props = defaultdict(set)
    for prop, info in obj_props.items():
        for d in info["domains"]: cls_props[d].add(prop)
    for prop, info in data_props.items():
        for d in info["domains"]: cls_props[d].add(prop)

    inheritance = set()
    for sub, supers in sub_cands.items():
        for sup in supers:
            inheritance.add((sub, sup))
            inheritance.add((sup, sub))

    disjoint_pairs = set()
    cls_list = sorted(classes, key=str)
    for i, a in enumerate(cls_list):
        for b in cls_list[i+1:]:
            if (a, b) in inheritance or (b, a) in inheritance:
                continue
            pa, pb = cls_props.get(a, set()), cls_props.get(b, set())
            if not pa or not pb:
                continue
            if pa.isdisjoint(pb):
                disjoint_pairs.add(tuple(sorted([str(a), str(b)])))

    return disjoint_pairs


# ── Generazione TBox principale ─────────────────────────────────

def build_tbox(analysis: dict, src: Graph, dc_meta: dict) -> Graph:
    tbox = Graph()
    for pf, ns in src.namespaces(): tbox.bind(pf, ns)
    for pf, uri in KNOWN_PREFIXES.items(): tbox.bind(pf, URIRef(uri))
    tbox.bind("owl", OWL)

    DC_TITLE       = URIRef(DC + "title")
    DC_CREATOR     = URIRef(DC + "creator")
    DC_DATE        = URIRef(DC + "date")
    DC_DESCRIPTION = URIRef(DC + "description")

    llm_dc = analysis.get("llm_dc_meta", {})
    final_dc = {**dc_meta, **{k: v for k, v in llm_dc.items() if v}}

    onto = URIRef("https://example.org/tbox/generated")
    tbox.add((onto, RDF.type,        OWL.Ontology))
    tbox.add((onto, DC_TITLE,        Literal(final_dc.get("title", "TBox"))))
    tbox.add((onto, DC_CREATOR,      Literal(final_dc.get("creator", "abox_to_tbox.py"))))
    tbox.add((onto, DC_DATE,         Literal(final_dc.get("date", str(datetime.date.today().year)))))
    tbox.add((onto, DC_DESCRIPTION,  Literal(final_dc.get("description", ""))))
    tbox.add((onto, RDFS.comment,    Literal("Generata con abox_to_tbox.py — corso Intelligent Web")))

    classes      = analysis["classes"]
    data_props   = analysis["data_props"]
    obj_props    = analysis["object_props"]
    sub_cands    = analysis["subclass_candidates"]
    cls_insts    = analysis["class_instances"]
    if analysis["disjoint_pairs"]:
        disjoint_prs = analysis["disjoint_pairs"]
    else:
        disjoint_prs = compute_disjoint_semantic(analysis)
    func_op      = analysis["functional_props"]
    inv_func_op  = analysis["inv_functional_props"]
    sym_op       = analysis["symmetric_props"]
    trans_op     = analysis["transitive_props"]
    inv_pairs    = analysis["inverse_pairs"]
    func_dp      = analysis["functional_data"]
    restrictions = analysis["restrictions"]
    llm_cls      = analysis.get("llm_class_labels", {})
    llm_prp      = analysis.get("llm_prop_labels", {})

    equiv_map = analysis.get("equiv_classes", {})

    # Proprietà non-simple (OWL 2 §11.2)
    KNOWN_TRANSITIVE_URIS = {
        "http://purl.org/dc/terms/isPartOf",
        "http://purl.org/dc/terms/hasPart",
        "http://purl.org/dc/terms/isFormatOf",
        "http://purl.org/dc/terms/hasFormat",
        "http://purl.org/dc/terms/isVersionOf",
        "http://purl.org/dc/terms/isReferencedBy",
        "http://www.w3.org/2004/02/skos/core#broader",
        "http://www.w3.org/2004/02/skos/core#narrower",
        "http://www.w3.org/2004/02/skos/core#broaderTransitive",
        "http://www.w3.org/2004/02/skos/core#narrowerTransitive",
        "http://www.w3.org/2000/01/rdf-schema#subClassOf",
        "http://www.w3.org/2000/01/rdf-schema#subPropertyOf",
        "http://www.w3.org/2002/07/owl#sameAs",
        "http://schema.org/isPartOf",
        "http://schema.org/hasPart",
        "http://schema.org/containedInPlace",
        "http://schema.org/containsPlace",
        "http://www.w3.org/2006/time#intervalBefore",
        "http://www.w3.org/2006/time#intervalAfter",
        "http://www.w3.org/ns/org#subOrganizationOf",
        "http://www.opengis.net/ont/geosparql#sfContains",
        "http://www.opengis.net/ont/geosparql#sfWithin",
    }
    non_simple = set(trans_op)
    for s, _, o in src.triples((None, RDF.type, OWL.TransitiveProperty)):
        non_simple.add(s)
    for s, _, o in src.triples((None, OWL.propertyChainAxiom, None)):
        non_simple.add(s)
    non_simple.update(URIRef(u) for u in KNOWN_TRANSITIVE_URIS)
    changed = True
    while changed:
        changed = False
        for p, q in inv_pairs.items():
            if q in non_simple and p not in non_simple:
                non_simple.add(p); changed = True

    # 1. Classi
    for cls in sorted(classes, key=str):
        tbox.add((cls, RDF.type, OWL.Class))
        lname = local_name(cls)
        llm = llm_cls.get(lname, {})

        if llm.get("label_it"):
            tbox.add((cls, RDFS.label, Literal(llm["label_it"], lang="it")))
        if llm.get("label_en"):
            tbox.add((cls, RDFS.label, Literal(llm["label_en"], lang="en")))
        if not llm.get("label_it") and not llm.get("label_en"):
            for lbl in src.objects(cls, RDFS.label): tbox.add((cls, RDFS.label, lbl))
            if not list(tbox.objects(cls, RDFS.label)):
                tbox.add((cls, RDFS.label, Literal(lname, lang="en")))

        comment = llm.get("comment") or f"Istanze osservate nella ABox: {len(cls_insts.get(cls, set()))}"
        tbox.add((cls, RDFS.comment, Literal(comment)))

    # 2. SubClassOf
    for sub, supers in sub_cands.items():
        for sup in supers: tbox.add((sub, RDFS.subClassOf, sup))

    # 3. DisjointWith
    disjoint_reasons = analysis.get("disjoint_reasons", {})
    for (a_s, b_s) in disjoint_prs:
        ua, ub = URIRef(a_s), URIRef(b_s)
        tbox.add((ua, OWL.disjointWith, ub))
        key = tuple(sorted([a_s, b_s]))
        reason = disjoint_reasons.get(key)
        if reason:
            existing = str(tbox.value(ua, RDFS.comment) or "")
            note = f"Disgiunta da {local_name(ub)}: {reason}"
            if note not in existing:
                tbox.set((ua, RDFS.comment,
                          Literal(str(tbox.value(ua, RDFS.comment) or "") + " | " + note)))

    # 4. ObjectProperty
    for prop, info in sorted(obj_props.items(), key=lambda x: str(x[0])):
        tbox.add((prop, RDF.type, OWL.ObjectProperty))
        lname = local_name(prop)
        llm = llm_prp.get(lname, {})

        if llm.get("label_it"): tbox.add((prop, RDFS.label, Literal(llm["label_it"], lang="it")))
        if llm.get("label_en"): tbox.add((prop, RDFS.label, Literal(llm["label_en"], lang="en")))
        if not llm.get("label_it") and not llm.get("label_en"):
            for lbl in src.objects(prop, RDFS.label): tbox.add((prop, RDFS.label, lbl))
            if not list(tbox.objects(prop, RDFS.label)):
                tbox.add((prop, RDFS.label, Literal(lname, lang="en")))
        if llm.get("comment"): tbox.add((prop, RDFS.comment, Literal(llm["comment"])))

        _add_dr(tbox, prop, info["domains"], info["ranges"], False,
                axiom_confidence=analysis.get("axiom_confidence"))
        if prop in func_op    and prop not in non_simple:
            tbox.add((prop, RDF.type, OWL.FunctionalProperty))
        if prop in inv_func_op and prop not in non_simple:
            tbox.add((prop, RDF.type, OWL.InverseFunctionalProperty))
        if prop in sym_op:      tbox.add((prop, RDF.type, OWL.SymmetricProperty))
        if prop in trans_op:    tbox.add((prop, RDF.type, OWL.TransitiveProperty))
        if prop in inv_pairs:   tbox.add((prop, OWL.inverseOf, inv_pairs[prop]))

    # 5. DatatypeProperty
    for prop, info in sorted(data_props.items(), key=lambda x: str(x[0])):
        tbox.add((prop, RDF.type, OWL.DatatypeProperty))
        lname = local_name(prop)
        llm = llm_prp.get(lname, {})

        if llm.get("label_it"): tbox.add((prop, RDFS.label, Literal(llm["label_it"], lang="it")))
        if llm.get("label_en"): tbox.add((prop, RDFS.label, Literal(llm["label_en"], lang="en")))
        if not llm.get("label_it") and not llm.get("label_en"):
            for lbl in src.objects(prop, RDFS.label): tbox.add((prop, RDFS.label, lbl))
            if not list(tbox.objects(prop, RDFS.label)):
                tbox.add((prop, RDFS.label, Literal(lname, lang="en")))
        if llm.get("comment"): tbox.add((prop, RDFS.comment, Literal(llm["comment"])))

        _add_dr(tbox, prop, info["domains"], info["ranges"], True,
                axiom_confidence=analysis.get("axiom_confidence"))
        if prop in func_dp: tbox.add((prop, RDF.type, OWL.FunctionalProperty))

    # 6. Restrictions
    for (cls, prop), entry in restrictions.items():
        if entry.get("min", 0) >= 1 and prop not in non_simple:
            ranges = list(obj_props[prop]["ranges"])
            if len(ranges) == 1:
                sv = BNode()
                tbox.add((sv, RDF.type, OWL.Restriction))
                tbox.add((sv, OWL.onProperty, prop))
                tbox.add((sv, OWL.someValuesFrom, ranges[0]))
                tbox.add((cls, RDFS.subClassOf, sv))

        if prop in non_simple:
            continue

        # Usa cardinalità qualificata (owl:onClass) se la proprietà ha un range
        # specifico già dichiarato nel TBox — evita "max N owl:Thing" in Protégé.
        declared_range = tbox.value(prop, RDFS.range)
        use_qualified = (isinstance(declared_range, URIRef)
                         and declared_range != OWL.Thing)

        node = BNode()
        tbox.add((node, RDF.type, OWL.Restriction))
        tbox.add((node, OWL.onProperty, prop))
        has_constraint = False
        if use_qualified:
            tbox.add((node, OWL.onClass, declared_range))
            if "exact" in entry and entry["exact"] > 0:
                tbox.add((node, OWL.qualifiedCardinality,
                          Literal(entry["exact"], datatype=XSD.nonNegativeInteger)))
                has_constraint = True
            else:
                if entry["min"] > 0:
                    tbox.add((node, OWL.minQualifiedCardinality,
                              Literal(entry["min"], datatype=XSD.nonNegativeInteger)))
                    has_constraint = True
                if entry["max"] < 999:
                    tbox.add((node, OWL.maxQualifiedCardinality,
                              Literal(entry["max"], datatype=XSD.nonNegativeInteger)))
                    has_constraint = True
        else:
            if "exact" in entry and entry["exact"] > 0:
                tbox.add((node, OWL.cardinality,
                          Literal(entry["exact"], datatype=XSD.nonNegativeInteger)))
                has_constraint = True
            else:
                if entry["min"] > 0:
                    tbox.add((node, OWL.minCardinality,
                              Literal(entry["min"], datatype=XSD.nonNegativeInteger)))
                    has_constraint = True
                if entry["max"] < 999:
                    tbox.add((node, OWL.maxCardinality,
                              Literal(entry["max"], datatype=XSD.nonNegativeInteger)))
                    has_constraint = True
        if has_constraint:
            tbox.add((cls, RDFS.subClassOf, node))

    removed = deduplicate_restrictions(tbox)
    if removed:
        print(f"      ✂️  {removed} restriction duplicate rimosse")
    malformed = _remove_malformed_restrictions(tbox)
    if malformed:
        print(f"      🗑️  {malformed} restriction malformate rimosse (Error* in Protege)")

    return tbox


# ── Utilità per label individui ─────────────────────────────────

def _is_hash_uri(lname: str) -> bool:
    parts = lname.split("_", 1)
    segment = parts[1] if len(parts) == 2 and len(parts[0]) <= 8 else lname
    clean = segment.lower().replace("-", "").replace("_", "")
    if len(clean) < 16:
        return False
    hex_count = sum(1 for c in clean if c in "0123456789abcdef")
    return hex_count / len(clean) >= 0.90


def enrich_individual_labels(g: Graph) -> Graph:
    ind_cls = defaultdict(set)
    for s, _, o in g.triples((None, RDF.type, None)):
        if isinstance(s, URIRef) and isinstance(o, URIRef) and str(o) not in META_CLASSES:
            ind_cls[s].add(o)

    def best_label(node):
        lbls = list(g.objects(node, RDFS.label))
        if lbls:
            return str(sorted(lbls, key=lambda l: len(str(l)), reverse=True)[0])
        return None

    def distinctive_value(node):
        PREF = ["fullAddress","label","name","nome","email","mbox",
                "telephone","telefono","title","identifier"]
        candidates = []
        for p, o in g.predicate_objects(node):
            if isinstance(o, Literal):
                pname = local_name(p).lower()
                val   = str(o).strip()
                if not val or len(val) > 80: continue
                score = next((i for i, k in enumerate(PREF) if k in pname), 99)
                candidates.append((score, len(val), val))
        if candidates:
            candidates.sort()
            return candidates[0][2]
        return None

    pointed_by = defaultdict(list)
    for s, p, o in g:
        if (isinstance(s, URIRef) and isinstance(o, URIRef)
                and str(p) not in SKIP_PROPS
                and str(p) != str(RDF.type)
                and str(p) != str(OWL.NamedIndividual)):
            pointed_by[o].append((s, p))

    siblings = defaultdict(list)
    for ind, classes in ind_cls.items():
        for parent, _ in pointed_by.get(ind, []):
            for cls in classes:
                siblings[(parent, cls)].append(ind)

    for ind, classes in ind_cls.items():
        lname   = local_name(ind)
        cur_lbl = best_label(ind)

        if cur_lbl and not _is_hash_uri(cur_lbl) and len(cur_lbl) > 2:
            continue
        if not _is_hash_uri(lname):
            continue

        parent_lbl  = None
        for (parent, prop) in pointed_by.get(ind, []):
            pl = best_label(parent)
            if pl and not _is_hash_uri(pl) and len(pl) > 1:
                parent_lbl = pl
                break

        cls_names = sorted([local_name(c) for c in classes
                            if str(c) not in META_CLASSES])
        cls_name  = cls_names[0] if cls_names else "Entità"

        if parent_lbl:
            base = f"{cls_name} — {parent_lbl}"
        else:
            base = cls_name

        needs_detail = False
        if parent_lbl:
            for cls in classes:
                siblings_list = siblings.get((parent, cls), [])
                if len(siblings_list) > 1:
                    needs_detail = True
                    break

        if needs_detail:
            val = distinctive_value(ind)
            if val:
                base = f"{base} ({val})"

        for lbl in list(g.objects(ind, RDFS.label)):
            g.remove((ind, RDFS.label, lbl))
        g.add((ind, RDFS.label, Literal(base)))

    return g


# ── Strip TBox e merge ──────────────────────────────────────────

def strip_tbox_from_abox(abox: Graph, tbox: Graph, equiv_map: dict = None) -> Graph:
    tbox_subjects = {str(s) for s, p, o in tbox if isinstance(s, URIRef)}

    alias_to_canonical = {}
    if equiv_map:
        for canonical, aliases in equiv_map.items():
            for alias in aliases:
                alias_to_canonical[str(alias)] = str(canonical)

    ind_canonical_types = defaultdict(set)
    for s, p, o in abox:
        if (str(p) == str(RDF.type) and isinstance(s, URIRef)
                and isinstance(o, URIRef) and str(o) not in TBOX_TYPES):
            canonical = alias_to_canonical.get(str(o))
            if canonical:
                ind_canonical_types[str(s)].add(canonical)

    clean = Graph()
    for pf, ns in abox.namespaces():
        clean.bind(pf, ns)

    for s, p, o in abox:
        p_str, o_str = str(p), str(o)

        if p_str in PROVENANCE_PROPS:
            continue

        if p_str == str(RDF.type) and o_str in TBOX_TYPES:
            continue

        if isinstance(s, URIRef) and str(s) in tbox_subjects:
            if p_str in TBOX_PREDICATES:
                continue

        if isinstance(s, BNode):
            if any(True for _ in abox.triples((s, OWL.onProperty, None))):
                continue
            if any(True for _ in abox.triples((s, OWL.unionOf, None))):
                continue
            if any(True for _ in abox.triples((s, OWL.intersectionOf, None))):
                continue

        if (p_str == str(RDF.type) and isinstance(s, URIRef)
                and isinstance(o, URIRef) and str(o) in alias_to_canonical):
            canonical = alias_to_canonical[str(o)]
            if canonical in ind_canonical_types.get(str(s), set()):
                continue

        clean.add((s, p, o))

    return clean


def build_merged(tbox: Graph, abox: Graph, equiv_map: dict = None) -> Graph:
    clean_abox = strip_tbox_from_abox(abox, tbox, equiv_map=equiv_map or {})

    merged = Graph()
    for pf, ns in list(tbox.namespaces()) + list(abox.namespaces()):
        merged.bind(pf, ns)
    for t in tbox: merged.add(t)
    for t in clean_abox: merged.add(t)

    for s, _, o in clean_abox.triples((None, RDF.type, None)):
        if isinstance(s, URIRef) and isinstance(o, URIRef) and str(o) not in META_CLASSES:
            merged.add((s, RDF.type, OWL.NamedIndividual))

    merged = enrich_individual_labels(merged)
    return merged
