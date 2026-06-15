"""Metriche strutturali gerarchia e validazione reasoner."""

from collections import defaultdict


def compute_hierarchy_metrics(subclass_map: dict, all_classes: set) -> dict:
    try:
        import networkx as nx
    except ImportError:
        return {"error": "networkx non installato (pip install networkx)"}

    G = nx.DiGraph()
    G.add_nodes_from(all_classes)
    for sub, supers in subclass_map.items():
        for sup in supers:
            G.add_edge(sub, sup)

    roots = [n for n in G if G.in_degree(n) == 0]
    leaves = [n for n in G if G.out_degree(n) == 0]

    G_rev = G.reverse()
    depths = {}
    for root in roots:
        reachable = nx.descendants(G_rev, root) | {root}
        for node in reachable:
            try:
                d = nx.shortest_path_length(G_rev, root, node)
                depths[node] = min(depths.get(node, 999), d)
            except nx.NetworkXNoPath:
                pass

    depth_values = list(depths.values()) or [0]
    breadth_per_level = defaultdict(int)
    for node, d in depths.items():
        breadth_per_level[d] += 1
    breadth_values = list(breadth_per_level.values()) or [0]

    return {
        "ARC": len(roots),
        "ALC": len(leaves),
        "AD":  round(sum(depth_values) / len(depth_values), 2),
        "MD":  max(depth_values),
        "AB":  round(sum(breadth_values) / len(breadth_values), 2),
        "MB":  max(breadth_values) if breadth_values else 0,
        "nodes": len(all_classes),
        "edges": G.number_of_edges(),
    }


def validate_with_reasoner(tbox_path: str) -> dict:
    """Verifica la consistenza con HermiT (via owlready2).

    - Java rilevato dinamicamente dal PATH (niente percorso hardcoded).
    - owl:imports rimossi prima del reasoning (niente fetch di rete).
    """
    try:
        import owlready2
        from owlready2 import sync_reasoner
        import tempfile, os, shutil, logging, sys, io
        from rdflib import Graph as RGraph
        from rdflib.namespace import OWL

        java = shutil.which("java")
        if java:
            owlready2.JAVA_EXE = java

        # Converte in RDF/XML, rimuovendo gli owl:imports (evita download esterni)
        g = RGraph()
        g.parse(tbox_path)
        for t in list(g.triples((None, OWL.imports, None))):
            g.remove(t)
        tmp_xml = tempfile.NamedTemporaryFile(
            suffix=".rdf", delete=False, dir=tempfile.gettempdir())
        g.serialize(destination=tmp_xml.name, format="xml")
        tmp_xml.close()

        logging.getLogger("owlready2").setLevel(logging.CRITICAL)

        world = owlready2.World()
        file_uri = "file://" + os.path.abspath(tmp_xml.name).replace(os.sep, "/")
        onto = world.get_ontology(file_uri).load(only_local=True)

        consistent = True
        _old = (sys.stdout, sys.stderr)
        sys.stdout = sys.stderr = io.StringIO()
        try:
            with onto:
                sync_reasoner(world, debug=0)
        except owlready2.base.OwlReadyInconsistentOntologyError:
            consistent = False
        finally:
            sys.stdout, sys.stderr = _old

        result = {
            "consistent": consistent,
            "inconsistent_classes": [str(c) for c in world.inconsistent_classes()],
        }
        os.unlink(tmp_xml.name)
        return result
    except ImportError:
        return {"consistent": None,
                "error": "owlready2 non installato (pip install owlready2)"}
    except Exception as e:
        return {"consistent": None, "error": str(e)}
