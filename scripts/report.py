"""Report testuale dell'analisi ABox → TBox."""

from rdflib import URIRef

from .constants import TAU_DOM
from .utils import local_name
from .metrics import compute_hierarchy_metrics


def print_report(a: dict, llm_used: bool):
    sep = "=" * 64
    llm_tag = "  [+ LLM]" if llm_used else ""
    print(f"\n{sep}\n  REPORT ABox → TBox{llm_tag}\n{sep}")

    print(f"\n📦 CLASSI ({len(a['classes'])}):")
    for cls in sorted(a["classes"], key=str):
        n    = len(a["class_instances"].get(cls, set()))
        lbl  = a["llm_class_labels"].get(local_name(cls), {}).get("label_it", "")
        tag  = f"  → \"{lbl}\"" if lbl else ""
        print(f"   • {local_name(cls):35s} [{n:4d} ist.]{tag}")

    print(f"\n🔗 OBJECT PROPERTIES ({len(a['object_props'])}):")
    for p, info in sorted(a["object_props"].items(), key=lambda x: str(x[0])):
        flags = []
        if p in a["functional_props"]:      flags.append("Func")
        if p in a["inv_functional_props"]:  flags.append("InvFunc")
        if p in a["symmetric_props"]:       flags.append("Sym")
        if p in a["transitive_props"]:      flags.append("Trans")
        if p in a["inverse_pairs"]:         flags.append(f"inv({local_name(a['inverse_pairs'][p])})")
        lbl  = a["llm_prop_labels"].get(local_name(p), {}).get("label_it", "")
        tag  = f"  [{', '.join(flags)}]" if flags else ""
        ltag = f"  → \"{lbl}\"" if lbl else ""
        print(f"   • {local_name(p)}{tag}{ltag}")
        print(f"       domain → {', '.join(local_name(d) for d in info['domains']) or '—'}")
        print(f"       range  → {', '.join(local_name(r) for r in info['ranges']) or '—'}")

    print(f"\n📝 DATA PROPERTIES ({len(a['data_props'])}):")
    for p, info in sorted(a["data_props"].items(), key=lambda x: str(x[0])):
        ftag = "  [Func]" if p in a["functional_data"] else ""
        lbl  = a["llm_prop_labels"].get(local_name(p), {}).get("label_it", "")
        ltag = f"  → \"{lbl}\"" if lbl else ""
        print(f"   • {local_name(p)}{ftag}{ltag}")
        print(f"       domain → {', '.join(local_name(d) for d in info['domains']) or '—'}")
        print(f"       range  → {', '.join(str(r).split('#')[-1] for r in info['ranges']) or '—'}")

    if a["subclass_candidates"]:
        print(f"\n🌳 SubClassOf:")
        for sub, supers in a["subclass_candidates"].items():
            for sup in supers: print(f"   • {local_name(sub)} ⊆ {local_name(sup)}")

    if a["disjoint_pairs"]:
        print(f"\n⊥  DisjointWith ({len(a['disjoint_pairs'])}):")
        for (a_s, b_s) in sorted(a["disjoint_pairs"]):
            print(f"   • {local_name(URIRef(a_s))} ⊥ {local_name(URIRef(b_s))}")

    if a["restrictions"]:
        print(f"\n🔒 Restrictions ({len(a['restrictions'])}):")
        for (cls, prop), entry in a["restrictions"].items():
            card = f"exact={entry['exact']}" if "exact" in entry else f"min={entry['min']}, max={entry['max']}"
            print(f"   • {local_name(cls)}.{local_name(prop)} → {card}")

    print(f"\n👤 Individui: {len(a['individual_classes'])}")

    if a["subclass_candidates"]:
        metrics = compute_hierarchy_metrics(
            a["subclass_candidates"], a["classes"])
        if "error" not in metrics:
            print(f"\n📊 METRICHE GERARCHIA (Tabella 7):")
            print(f"   • ARC (Absolute Root Cardinality):  {metrics['ARC']}")
            print(f"   • ALC (Absolute Leaf Cardinality):  {metrics['ALC']}")
            print(f"   • AD  (Average Depth):              {metrics['AD']}")
            print(f"   • MD  (Maximum Depth):              {metrics['MD']}")
            print(f"   • AB  (Average Breadth):            {metrics['AB']}")
            print(f"   • MB  (Maximum Breadth):            {metrics['MB']}")
            print(f"   • Nodi: {metrics['nodes']}  Archi: {metrics['edges']}")
        else:
            print(f"\n   ⚠️  Metriche gerarchia: {metrics['error']}")

    axiom_conf = a.get("axiom_confidence", {})
    if axiom_conf:
        low_conf = [(k, v) for k, v in axiom_conf.items() if v[1] < TAU_DOM]
        high_conf = [(k, v) for k, v in axiom_conf.items() if v[1] >= TAU_DOM]
        print(f"\n📈 CONFIDENCE ASSIOMI:")
        print(f"   • Accettati (≥ {TAU_DOM:.0%}): {len(high_conf)}")
        print(f"   • Sotto soglia (< {TAU_DOM:.0%}): {len(low_conf)}")
        if low_conf:
            for (prop, role), (cls, conf) in sorted(low_conf, key=lambda x: x[1][1]):
                print(f"     ⚠️  {local_name(prop)}.{role} → "
                      f"{local_name(cls)} ({conf:.1%})")

    print(sep + "\n")
