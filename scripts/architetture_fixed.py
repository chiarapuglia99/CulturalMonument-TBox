#!/usr/bin/env python3
"""
Sostituisce i label generici/ripetuti degli individui (Geometry, WebSite,
Telephone, Email, AccessCondition, OnlineContactPoint) con il VALORE REALE
contenuto nelle data/object property, così che nel popolamento appaia
l'informazione effettiva invece dell'etichetta ripetuta.

Approccio deterministico (nessun LLM): per ogni classe si sa quale proprietà
contiene il valore da promuovere a rdfs:label.
"""
import sys
from rdflib import Graph, Literal, RDFS, URIRef
from rdflib.namespace import Namespace

GEO = Namespace("http://www.opengis.net/ont/geosparql#")
NS2 = Namespace("https://w3id.org/italia/onto/CLV/")            # Geometry
NS3 = Namespace("https://w3id.org/italia/onto/SM/")             # WebSite, Telephone, Email, OnlineContactPoint
NS5 = Namespace("https://w3id.org/italia/onto/l0/")             # description
NS6 = Namespace("https://w3id.org/italia/onto/AccessCondition/")# AccessCondition

# Per ogni classe: la proprietà il cui valore diventa il nuovo label.
# (la proprietà può puntare a un literal oppure a un IRI: in entrambi i casi
#  ne usiamo la stringa)
TYPE_TO_VALUE_PROP = {
    NS2.Geometry:        GEO.asWKT,
    NS2.Address:         NS2.fullAddress,
    NS3.WebSite:         NS3.URL,
    NS3.Telephone:       NS3.telephoneNumber,
    NS3.Email:           NS3.emailAddress,
    NS6.AccessCondition: NS5.description,
}

def clean(v):
    """Pulisce literal con virgolette annidate tipo \"\"testo\"\"."""
    s = str(v).strip()
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    return s.strip()

def main(inp, outp):
    g = Graph()
    g.parse(inp, format="turtle")
    changed = 0

    # 1) Classi con un valore diretto
    for cls, prop in TYPE_TO_VALUE_PROP.items():
        for ind in g.subjects(URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"), cls):
            val = g.value(ind, prop)
            if val is None:
                continue
            for old in list(g.objects(ind, RDFS.label)):
                g.remove((ind, RDFS.label, old))
            g.add((ind, RDFS.label, Literal(clean(val))))
            changed += 1

    # 2) OnlineContactPoint ("Recapiti"): nessun literal proprio.
    #    Compone il label dai recapiti collegati (email / tel / sito).
    for ind in g.subjects(URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"),
                          NS3.OnlineContactPoint):
        parts = []
        em = g.value(ind, NS3.hasEmail)
        tel = g.value(ind, NS3.hasTelephoneNumber)
        web = g.value(ind, NS3.hasWebSite)
        if em is not None:
            v = g.value(em, NS3.emailAddress)
            if v: parts.append(clean(v))
        if tel is not None:
            v = g.value(tel, NS3.telephoneNumber)
            if v: parts.append(clean(v))
        if web is not None:
            v = g.value(web, NS3.URL)
            if v: parts.append(clean(v))
        if not parts:
            continue
        for old in list(g.objects(ind, RDFS.label)):
            g.remove((ind, RDFS.label, old))
        g.add((ind, RDFS.label, Literal(" | ".join(parts))))
        changed += 1

    g.serialize(destination=outp, format="turtle")
    print(f"Aggiornati {changed} individui -> {outp}")

if __name__ == "__main__":
    inp = sys.argv[1] if len(sys.argv) > 1 else "architetture_firenze_merged.ttl"
    outp = sys.argv[2] if len(sys.argv) > 2 else "architetture_firenze_fixed.ttl"
    main(inp, outp)