"""
Confronta e unisce individui cis:Site in cis:CulturalInstituteOrSite.

Un Site può essere condiviso da più istituti (stesso sito fisico).
Le proprietà del Site vengono copiate su tutti gli istituti che vi puntano.

Migrazione delle object property assertions:
  cis:siteAddress  →  cis:haIndirizzo  (nuova ObjectProperty)
  tutte le altre   →  stessa property (es. sm:hasOnlineContactPoint)

TBox: cis:Site, cis:hasSite e cis:siteAddress vengono rimossi;
      cis:haIndirizzo viene dichiarata con domain/range corretti.

Uso:
  python -m scripts.merge_site input.ttl [output.ttl]
  python merge_site.py input.ttl [output.ttl]
"""

import sys
from rdflib import Graph, RDF, RDFS, OWL, URIRef, Literal

CIS = "http://dati.beniculturali.it/cis/"
CLV = "https://w3id.org/italia/onto/CLV/"

CIS_CulturalInstituteOrSite = URIRef(CIS + "CulturalInstituteOrSite")
CIS_Site         = URIRef(CIS + "Site")
CIS_hasSite      = URIRef(CIS + "hasSite")
CIS_siteAddress  = URIRef(CIS + "siteAddress")
CIS_haIndirizzo  = URIRef(CIS + "haIndirizzo")
CLV_Address      = URIRef(CLV + "Address")

_SKIP = {str(RDF.type), str(RDFS.label), str(OWL.sameAs)}


def _collect_by_site(g):
    """Raggruppa gli istituti per site: {site_uri: [inst_uri, ...]}."""
    by_site = {}
    for inst, _, site in g.triples((None, CIS_hasSite, None)):
        if isinstance(inst, URIRef) and isinstance(site, URIRef):
            by_site.setdefault(site, []).append(inst)
    return by_site


def _report(g, by_site):
    """Stampa il confronto; segnala i site condivisi da più istituti."""
    total_pairs = sum(len(insts) for insts in by_site.values())
    print(f"\n[merge_site] {len(by_site)} Site distinti, {total_pairs} coppie totali:")

    shared = []
    for site, insts in by_site.items():
        site_id   = str(site).rsplit("/", 1)[-1]
        site_props = [str(p).rsplit("/", 1)[-1]
                      for p, _ in g.predicate_objects(site)
                      if str(p) not in _SKIP]

        if len(insts) == 1:
            inst    = insts[0]
            inst_id = str(inst).rsplit("/", 1)[-1]
            label   = next(g.objects(inst, RDFS.label), "?")
            marker  = "✓" if inst_id == site_id else "~"
            print(f"  {marker}  cultural-institute/{inst_id} ↔ site/{site_id}"
                  f"  [{label}]  props: {site_props or '-'}")
        else:
            # Site condiviso da più istituti — caso legittimo
            shared.append((site, site_id, insts, site_props))
            print(f"  ⬡  site/{site_id} condiviso da {len(insts)} istituti"
                  f"  props: {site_props or '-'}")
            for inst in insts:
                inst_id = str(inst).rsplit("/", 1)[-1]
                label   = next(g.objects(inst, RDFS.label), "?")
                print(f"       → cultural-institute/{inst_id}  [{label}]")

    if shared:
        print(f"\n  ℹ  {len(shared)} Site condivisi: le proprietà verranno copiate su tutti gli istituti.")
    else:
        print(f"\n  → Nessun Site condiviso. cis:Site è ridondante e verrà eliminata.")


def _merge(g, by_site):
    """Copia le proprietà di ogni Site su tutti gli istituti che vi puntano, poi rimuove il Site."""
    n_sites = 0
    for site, insts in by_site.items():
        site_props = list(g.predicate_objects(site))

        for inst in insts:
            for pred, obj in site_props:
                if str(pred) in _SKIP:
                    continue
                new_pred = CIS_haIndirizzo if pred == CIS_siteAddress else pred
                g.add((inst, new_pred, obj))
            g.remove((inst, CIS_hasSite, site))

        # Rimuovi il Site dopo aver servito tutti gli istituti
        for s, p in list(g.subject_predicates(site)):
            g.remove((s, p, site))
        for p, o in list(g.predicate_objects(site)):
            g.remove((site, p, o))

        n_sites += 1

    # Pulizia TBox: prima rimuovi le owl:Restriction che usano queste proprietà,
    # poi le dichiarazioni delle proprietà stesse
    for prop_iri in (CIS_hasSite, CIS_siteAddress):
        for bnode in list(g.subjects(OWL.onProperty, prop_iri)):
            for cls in list(g.subjects(RDFS.subClassOf, bnode)):
                g.remove((cls, RDFS.subClassOf, bnode))
            for p, o in list(g.predicate_objects(bnode)):
                g.remove((bnode, p, o))

    for iri in (CIS_Site, CIS_hasSite, CIS_siteAddress):
        for p, o in list(g.predicate_objects(iri)):
            g.remove((iri, p, o))
        for s, p in list(g.subject_predicates(iri)):
            g.remove((s, p, iri))

    # Rimuovi blank-node owl:Restriction orfani (non più collegati a nessuna classe)
    referenced = set(g.objects(None, RDFS.subClassOf))
    for bnode in list(g.subjects(RDF.type, OWL.Restriction)):
        if bnode not in referenced:
            for p, o in list(g.predicate_objects(bnode)):
                g.remove((bnode, p, o))

    # Aggiungi dichiarazione TBox di cis:haIndirizzo
    g.add((CIS_haIndirizzo, RDF.type,    OWL.ObjectProperty))
    g.add((CIS_haIndirizzo, RDF.type,    OWL.FunctionalProperty))
    g.add((CIS_haIndirizzo, RDFS.label,  Literal("haIndirizzo", lang="it")))
    g.add((CIS_haIndirizzo, RDFS.label,  Literal("hasAddress",  lang="en")))
    g.add((CIS_haIndirizzo, RDFS.domain, CIS_CulturalInstituteOrSite))
    g.add((CIS_haIndirizzo, RDFS.range,  CLV_Address))
    g.add((CIS_haIndirizzo, RDFS.comment, Literal(
        "Collega l'istituto culturale al proprio indirizzo fisico. "
        "Incorpora le informazioni precedentemente modellate in cis:Site tramite cis:siteAddress."
    )))

    return n_sites


def main(inp, outp=None):
    outp = outp or inp

    g = Graph()
    g.parse(inp, format="turtle")
    t_before = len(g)

    by_site = _collect_by_site(g)
    if not by_site:
        print("[merge_site] Nessun Site trovato — skip.")
        return

    _report(g, by_site)

    n = _merge(g, by_site)
    print(f"\n[merge_site] Rimossi {n} Site | Triple: {t_before} → {len(g)}")
    g.serialize(destination=outp, format="turtle")
    print(f"[merge_site] Salvato: {outp}")


if __name__ == "__main__":
    inp  = sys.argv[1] if len(sys.argv) > 1 else "architetture_firenze_merged.ttl"
    outp = sys.argv[2] if len(sys.argv) > 2 else inp
    main(inp, outp)
