"""
Rifinitura ontologica finale (generale, qualsiasi TTL dello schema CIS/CLV/SM/AC).

Trasforma l'output ABox+TBox "grezzo" (es. dopo merge_site) nella forma finale:
  - pulizia letterali (apici CSV) e commenti di provenienza ("confidence");
  - normalizza la proprieta' d'indirizzo (cis:siteAddress / cis:haIndirizzo -> afi:hasAddress);
  - appiattisce clv:Geometry -> geo:asWKT sull'entita' che la referenzia (stringa semplice);
  - tassonomia: sottoclassi di cis:CulturalInstituteOrSite dedotte dall'etichetta;
  - contatti: classe padre afi:Contact con WebSite/Email/Telephone come sottoclassi,
    il sito web "contiene" email/telefono (afi:contains / afi:isContainedIn inverse);
  - condizioni di accesso: collassate in categorie condivise per livello (keyword),
    dettaglio per-monumento spostato in afi:accessibilityNote;
  - mereologia isPartOf dedotta dal pattern "Prefisso - Dettaglio" delle etichette;
  - range rdfs:Literal per le datatype property con valori multilingua (consistenza HermiT);
  - inversi materializzati: ogni individuo ha almeno una object property in uscita;
  - IRI dell'ontologia e metadati.

Tutte le scelte sono guidate dai dati presenti: nessun identificatore fisso.

Uso:
  python -m scripts.refine input.ttl [output.ttl] [--onto-iri IRI]
"""

import sys
import re
from rdflib import Graph, URIRef, BNode, Literal, Namespace
from rdflib.namespace import RDF, RDFS, OWL, XSD

CIS = Namespace("http://dati.beniculturali.it/cis/")
CLV = Namespace("https://w3id.org/italia/onto/CLV/")
SM  = Namespace("https://w3id.org/italia/onto/SM/")
AC  = Namespace("https://w3id.org/italia/onto/AccessCondition/")
L0  = Namespace("https://w3id.org/italia/onto/l0/")
ARCO = Namespace("https://w3id.org/arco/ontology/core/")
GEO = Namespace("http://www.opengis.net/ont/geosparql#")
AFI = Namespace("https://w3id.org/firenze/architetture/")

MAIN_CLASS = CIS.CulturalInstituteOrSite
DEFAULT_ONTO_IRI = "https://w3id.org/firenze/architetture"

# Tassonomia: (keyword nell'etichetta, NomeSottoclasse). Ordine = priorita'.
TAXONOMY_KEYWORDS = [
    ("murale", "Murale"), ("lapide", "OperaCommemorativa"),
    ("tabernacolo", "OperaDevozionale"), ("ponte", "Ponte"), ("torre", "Torre"),
    ("cimitero", "Cimitero"), ("baluardo", "Fortificazione"), ("forte", "Fortificazione"),
    ("porta", "Porta"), ("stazione", "Stazione"), ("loggia", "Loggia"),
    ("biblioteca", "Biblioteca"), ("stadio", "Stadio"), ("villa", "Villa"),
    ("galleria", "Museo"), ("uffizi", "Museo"), ("museo", "Museo"),
    ("palazzo", "Palazzo"), ("piazzale", "Piazza"), ("piazza", "Piazza"),
]
TAXONOMY_LABELS = {
    "Piazza": ("Piazza", "Square"), "Ponte": ("Ponte", "Bridge"),
    "Torre": ("Torre", "Tower"), "Cimitero": ("Cimitero", "Cemetery"),
    "Palazzo": ("Palazzo", "Palace"), "Villa": ("Villa", "Villa"),
    "Biblioteca": ("Biblioteca", "Library"), "Stadio": ("Stadio", "Stadium"),
    "Fortificazione": ("Fortificazione", "Fortification"), "Porta": ("Porta cittadina", "City gate"),
    "Stazione": ("Stazione", "Station"), "Loggia": ("Loggia", "Loggia"),
    "Museo": ("Museo", "Museum"), "Murale": ("Murale", "Mural"),
    "OperaCommemorativa": ("Opera commemorativa", "Commemorative work"),
    "OperaDevozionale": ("Opera devozionale", "Devotional work"),
    "AltroBeneCulturale": ("Altro bene culturale", "Other cultural property"),
}

# Livelli di accessibilita': (keyword, slug, etichetta, descrizione generica)
ACCESS_LEVELS = [
    ("non accessibile", "non-accessibile", "Non accessibile",
     "Il sito non è accessibile a persone con ridotta mobilità."),
    ("parzialmente", "parzialmente-accessibile", "Parzialmente accessibile",
     "Il sito è accessibile solo in parte: alcune aree presentano barriere architettoniche."),
    ("totalmente", "totalmente-accessibile", "Totalmente accessibile",
     "Il sito è interamente accessibile, senza barriere architettoniche."),
    ("completamente", "totalmente-accessibile", "Totalmente accessibile",
     "Il sito è interamente accessibile, senza barriere architettoniche."),
]

# Datatype property che portano testo in linguaggio naturale (range rdfs:Literal, lingua @it)
TEXT_PROPS = [ARCO.description, L0.name, L0.description, AFI.accessibilityNote]


def drop_subject(g, s):
    for t in list(g.triples((s, None, None))):
        g.remove(t)


def _remove_from_lists(g, term):
    """Rimuove term da qualsiasi rdf:List (owl:members/unionOf/...) ricucendo la lista."""
    for cell in list(g.subjects(RDF.first, term)):
        rest = g.value(cell, RDF.rest)
        for s, p in list(g.subject_predicates(cell)):
            g.remove((s, p, cell))
            g.add((s, p, rest if rest is not None else RDF.nil))
        for p, o in list(g.predicate_objects(cell)):
            g.remove((cell, p, o))


def purge_term(g, term):
    """Elimina completamente un termine: dichiarazione, usi come oggetto,
    restrizioni che lo referenziano (con il relativo rdfs:subClassOf), liste e disjointWith."""
    for rp in (OWL.onProperty, OWL.onClass, OWL.someValuesFrom,
               OWL.allValuesFrom, OWL.hasValue):
        for b in list(g.subjects(rp, term)):
            for s in list(g.subjects(RDFS.subClassOf, b)):
                g.remove((s, RDFS.subClassOf, b))
            drop_subject(g, b)
    _remove_from_lists(g, term)
    for s, p, o in list(g.triples((None, None, term))):
        g.remove((s, p, o))
    drop_subject(g, term)


# ---------------------------------------------------------------------------
def clean_literals_and_comments(g):
    n_c = 0
    for s, p, o in list(g.triples((None, RDFS.comment, None))):
        if isinstance(o, Literal) and "confidence:" in str(o):
            g.remove((s, p, o)); n_c += 1

    def clean(text):
        s, prev = text, None
        while prev != s:
            prev = s
            s = s.replace('""', '"').strip().strip(';').strip()
            if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
                s = s[1:-1].strip()
        return s

    n_l = 0
    for p in [RDFS.label, ARCO.description, L0.description, L0.name]:
        for s, _, o in list(g.triples((None, p, None))):
            if isinstance(o, Literal) and o.datatype in (None, XSD.string):
                new = clean(str(o))
                if new != str(o):
                    g.remove((s, p, o))
                    g.add((s, p, Literal(new, lang=o.language) if o.language else Literal(new)))
                    n_l += 1
    return n_c, n_l


def remove_orphan_terms(g):
    """Rimuove classi/proprieta' che hanno solo rdfs:label e non sono mai usate."""
    removed = 0
    for s in set(g.subjects()):
        if not isinstance(s, URIRef):
            continue
        preds = set(g.predicates(s, None))
        if preds and preds <= {RDFS.label}:                 # solo label
            used = (any(g.triples((None, None, s))) or
                    (s, RDF.type, None) in g)
            # usato come oggetto (dominio/range/tipo/valore)?
            if not list(g.triples((None, None, s))):
                drop_subject(g, s); removed += 1
    return removed


def normalize_address_property(g):
    """cis:siteAddress / cis:haIndirizzo -> afi:hasAddress (object property)."""
    moved = 0
    for old in (CIS.siteAddress, CIS.haIndirizzo):
        for s, _, o in list(g.triples((None, old, None))):
            g.add((s, AFI.hasAddress, o)); g.remove((s, old, o)); moved += 1
        drop_subject(g, old)
    if moved or True:
        g.add((AFI.hasAddress, RDF.type, OWL.ObjectProperty))
        g.add((AFI.hasAddress, RDF.type, OWL.FunctionalProperty))
        g.add((AFI.hasAddress, RDFS.label, Literal("ha indirizzo", lang="it")))
        g.add((AFI.hasAddress, RDFS.label, Literal("has address", lang="en")))
        g.add((AFI.hasAddress, RDFS.domain, MAIN_CLASS))
        g.add((AFI.hasAddress, RDFS.range, CLV.Address))
    return moved


def geometry_entity(g):
    """Geometria come ENTITA' visibile: Bene --afi:haCoordinate--> afi:Geometria(geo:asWKT),
    con inverso afi:eCoordinataDi. Sostituisce clv:Geometry/clv:hasGeometry."""
    def local(t):
        return str(t).rstrip("/#").rsplit("/", 1)[-1].rsplit("#", 1)[-1]

    # termini geometrici legacy (clv:Geometry/clv:hasGeometry, qualsiasi namespace/posizione)
    all_terms = set(g.subjects()) | set(g.predicates()) | set(g.objects())
    legacy_props = {t for t in all_terms
                    if isinstance(t, URIRef) and local(t) == "hasGeometry"}
    legacy_classes = {t for t in all_terms
                      if isinstance(t, URIRef) and local(t) == "Geometry"}

    n = 0
    # la geometria resta un'ENTITA' visibile: Bene --afi:haCoordinate--> afi:Geometria
    for hp in legacy_props:
        for holder in set(g.subjects(hp, None)):
            for geom in list(g.objects(holder, hp)):
                g.remove((holder, hp, geom))
                g.add((holder, AFI.haCoordinate, geom))
                for gc in legacy_classes:
                    g.remove((geom, RDF.type, gc))
                g.add((geom, RDF.type, AFI.Geometria))
                g.add((geom, RDF.type, OWL.NamedIndividual))
                n += 1
    # purga i SOLI termini legacy e i loro riferimenti (restrizioni, disjointWith, liste)
    for t in (legacy_props | legacy_classes):
        purge_term(g, t)

    # dichiarazioni: classe Geometria + object property "ha coordinate" e il suo inverso
    g.add((AFI.Geometria, RDF.type, OWL.Class))
    g.add((AFI.Geometria, RDFS.label, Literal("Geometria", lang="it")))
    g.add((AFI.haCoordinate, RDF.type, OWL.ObjectProperty))
    g.add((AFI.haCoordinate, RDFS.label, Literal("ha coordinate", lang="it")))
    g.add((AFI.haCoordinate, RDFS.domain, MAIN_CLASS))
    g.add((AFI.haCoordinate, RDFS.range, AFI.Geometria))
    g.add((AFI.haCoordinate, OWL.inverseOf, AFI.eCoordinataDi))
    g.add((AFI.eCoordinataDi, RDF.type, OWL.ObjectProperty))
    g.add((AFI.eCoordinataDi, RDFS.label, Literal("è coordinata di", lang="it")))
    g.add((AFI.eCoordinataDi, RDFS.domain, AFI.Geometria))
    g.add((AFI.eCoordinataDi, RDFS.range, MAIN_CLASS))
    # geo:asWKT resta come datatype property della Geometria (i valori restano invariati)
    purge_term(g, GEO.asWKT)
    g.add((GEO.asWKT, RDF.type, OWL.DatatypeProperty))
    g.add((GEO.asWKT, RDFS.label, Literal("geometria (WKT)", lang="it")))
    g.add((GEO.asWKT, RDFS.domain, AFI.Geometria))
    g.add((GEO.asWKT, RDFS.range, XSD.string))
    return n


def build_taxonomy(g):
    def classify(label):
        l = label.lower()
        for kw, cls in TAXONOMY_KEYWORDS:
            if kw in l:
                return cls
        return "AltroBeneCulturale"

    used = set()
    n = 0
    for m in list(g.subjects(RDF.type, MAIN_CLASS)):
        label = str(g.value(m, RDFS.label) or "")
        cls = classify(label)
        g.add((m, RDF.type, AFI[cls])); used.add(cls); n += 1
    for cls in used:
        it, en = TAXONOMY_LABELS.get(cls, (cls, cls))
        g.add((AFI[cls], RDF.type, OWL.Class))
        g.add((AFI[cls], RDFS.subClassOf, MAIN_CLASS))
        g.add((AFI[cls], RDFS.label, Literal(it, lang="it")))
        g.add((AFI[cls], RDFS.label, Literal(en, lang="en")))
    return n, len(used)


def compact_main_class(g):
    """Un'unica classe per i beni culturali: rimuove le sottoclassi (Biblioteca, Stadio, ...)
    e ritipizza i loro individui sotto cis:CulturalInstituteOrSite."""
    n = 0
    for sub in list(g.subjects(RDFS.subClassOf, MAIN_CLASS)):
        if not isinstance(sub, URIRef):
            continue
        for ind in list(g.subjects(RDF.type, sub)):
            g.remove((ind, RDF.type, sub))
            g.add((ind, RDF.type, MAIN_CLASS))
        purge_term(g, sub); n += 1
    return n


def restructure_contacts(g):
    """afi:Contact padre; il sito web 'ha contatti' email/telefono; coppia inversa."""
    if not (list(g.subjects(RDF.type, SM.WebSite)) or
            list(g.subjects(RDF.type, SM.Email)) or
            list(g.subjects(RDF.type, SM.Telephone))):
        return 0
    # classe padre + sottoclassi
    g.add((AFI.Contact, RDF.type, OWL.Class))
    g.add((AFI.Contact, RDFS.label, Literal("Contatto", lang="it")))
    g.add((AFI.Contact, RDFS.label, Literal("Contact", lang="en")))
    g.add((AFI.Contact, RDFS.comment,
           Literal("Recapito di un bene culturale: sito web, email o telefono.", lang="it")))
    for sub in (SM.WebSite, SM.Email, SM.Telephone):
        g.add((sub, RDFS.subClassOf, AFI.Contact))
    # coppia inversa generica: "ha contatti" / "è contatto di" (range/dominio: la classe Contatto).
    # Il sito web NON ha una relazione propria: si raggiunge come Contatto tramite "ha contatti".
    g.add((AFI.haContatti, RDF.type, OWL.ObjectProperty))
    g.add((AFI.haContatti, RDFS.label, Literal("ha contatti", lang="it")))
    g.add((AFI.haContatti, RDFS.range, AFI.Contact))
    g.add((AFI.haContatti, OWL.inverseOf, AFI.eContattoDi))
    g.add((AFI.eContattoDi, RDF.type, OWL.ObjectProperty))
    g.add((AFI.eContattoDi, RDFS.label, Literal("è contatto di", lang="it")))
    g.add((AFI.eContattoDi, RDFS.domain, AFI.Contact))
    n = 0
    for m in list(g.subjects(RDF.type, MAIN_CLASS)):
        web = g.value(m, SM.hasWebSite)
        if web is None:
            continue
        # email/telefono diventano contatti del sito web
        for objp in (SM.hasEmail, SM.hasTelephoneNumber):
            for ent in list(g.objects(m, objp)):
                g.add((web, AFI.haContatti, ent)); g.remove((m, objp, ent)); n += 1
        # il sito web è un contatto del monumento (sostituisce "ha sito web"/"è sito web di")
        g.add((m, AFI.haContatti, web)); g.remove((m, SM.hasWebSite, web)); n += 1
    # eventuali contatti residui gia' su un WebSite -> normalizza
    for objp in (SM.hasEmail, SM.hasTelephoneNumber):
        for w, _, ent in list(g.triples((None, objp, None))):
            if (w, RDF.type, SM.WebSite) in g:
                g.add((w, AFI.haContatti, ent)); g.remove((w, objp, ent)); n += 1
        if not list(g.triples((None, objp, None))):
            drop_subject(g, objp)
    drop_subject(g, SM.hasWebSite)   # proprieta' sostituita da "ha contatti"
    return n


def collapse_access_conditions(g, data_ns):
    """Collassa le AccessCondition per-monumento in categorie condivise per livello."""
    acs = [s for s in g.subjects(RDF.type, AC.AccessCondition)]
    if not acs:
        return 0
    cats = {}

    def level_of(label):
        l = label.lower()
        for kw, slug, lab, desc in ACCESS_LEVELS:
            if kw in l:
                return slug, lab, desc
        return None

    n = 0
    for ac in list(acs):
        label = str(g.value(ac, RDFS.label) or "")
        lvl = level_of(label)
        if lvl is None:
            continue
        slug, lab, desc = lvl
        if slug not in cats:
            cat = URIRef(data_ns + "access-condition/" + slug)
            g.add((cat, RDF.type, AC.AccessCondition))
            g.add((cat, RDF.type, OWL.NamedIndividual))
            g.add((cat, RDFS.label, Literal(lab, lang="it")))
            g.add((cat, L0.description, Literal(desc, lang="it")))
            cats[slug] = cat
        cat = cats[slug]
        note = g.value(ac, L0.description)
        for m in list(g.subjects(AC.hasAccessCondition, ac)):
            g.remove((m, AC.hasAccessCondition, ac))
            g.add((m, AC.hasAccessCondition, cat))
            if note is not None and str(note).strip():
                g.add((m, AFI.accessibilityNote, Literal(str(note), lang="it")))
            n += 1
        drop_subject(g, ac)
    if cats:
        g.add((AFI.accessibilityNote, RDF.type, OWL.DatatypeProperty))
        g.add((AFI.accessibilityNote, RDFS.label, Literal("nota di accessibilità", lang="it")))
        g.add((AFI.accessibilityNote, RDFS.domain, MAIN_CLASS))
        g.add((AFI.accessibilityNote, RDFS.range, RDFS.Literal))
    return n


def infer_part_of(g):
    """isPartOf dedotta dal pattern 'Prefisso - Dettaglio' delle etichette."""
    label2m = {}
    for m in g.subjects(RDF.type, MAIN_CLASS):
        lab = str(g.value(m, RDFS.label) or "").strip()
        if lab:
            label2m.setdefault(lab, m)
    n = 0
    for m in list(g.subjects(RDF.type, MAIN_CLASS)):
        lab = str(g.value(m, RDFS.label) or "")
        if " - " in lab:
            prefix = lab.split(" - ")[0].strip()
            parent = label2m.get(prefix)
            if parent is not None and parent != m:
                g.add((m, AFI.isPartOf, parent)); n += 1
    if n:
        g.add((AFI.isPartOf, RDF.type, OWL.ObjectProperty))
        g.add((AFI.isPartOf, RDF.type, OWL.TransitiveProperty))
        g.add((AFI.isPartOf, RDFS.label, Literal("fa parte di", lang="it")))
        g.add((AFI.isPartOf, RDFS.label, Literal("is part of", lang="en")))
        g.add((AFI.isPartOf, RDFS.domain, MAIN_CLASS))
        g.add((AFI.isPartOf, RDFS.range, MAIN_CLASS))
    return n


def fix_multilingual_ranges(g):
    """Tag @it sul testo + range rdfs:Literal (evita inconsistenza langString/xsd:string)."""
    # tag @it sulle etichette dei monumenti e sulle text property
    def tag(prop, subj=None):
        for s, _, o in list(g.triples((subj, prop, None))):
            if isinstance(o, Literal) and o.language is None and o.datatype in (None, XSD.string):
                g.remove((s, prop, o)); g.add((s, prop, Literal(str(o), lang="it")))
    for m in g.subjects(RDF.type, MAIN_CLASS):
        tag(RDFS.label, m)
    for p in TEXT_PROPS:
        tag(p)
    # qualsiasi datatype property con valori @lang -> range rdfs:Literal
    for p in set(g.subjects(RDF.type, OWL.DatatypeProperty)):
        if any(isinstance(o, Literal) and o.language for _, _, o in g.triples((None, p, None))):
            for r in list(g.objects(p, RDFS.range)):
                g.remove((p, RDFS.range, r))
            g.add((p, RDFS.range, RDFS.Literal))


def materialize_inverses(g):
    """Garantisce che ogni individuo abbia una object property in uscita, via inversi."""
    # inversi strutturali con nomi parlanti (cosi' Address/AccessCondition/WebSite
    # ottengono una object property in uscita)
    NICE = {AC.hasAccessCondition: "isAccessConditionOf", AFI.hasAddress: "isAddressOf"}
    declared = {a for a, _, _ in g.triples((None, OWL.inverseOf, None))} | \
               {b for _, _, b in g.triples((None, OWL.inverseOf, None))}
    for p, local in NICE.items():
        if p in declared or not list(g.triples((None, p, None))):
            continue
        inv = AFI[local]
        g.add((inv, RDF.type, OWL.ObjectProperty))
        g.add((inv, OWL.inverseOf, p))
        g.add((inv, RDFS.label, Literal(local, lang="en")))
    # materializza tutte le coppie inverse in entrambi i versi
    pairs = set()
    for a, _, b in g.triples((None, OWL.inverseOf, None)):
        pairs.add((a, b)); pairs.add((b, a))
    added = 0
    for A, B in pairs:
        for s, _, o in list(g.triples((None, B, None))):
            if isinstance(o, URIRef) and (o, A, s) not in g:
                g.add((o, A, s)); added += 1
    return added


# Nomi chiaramente spuri (es. 'Error1'). I generici (class/ns/node) solo se seguiti da cifre.
SPURIOUS_NAME = re.compile(r'(?i)^(error|errore|unknown|undefined)\d*$|^(class|ns|node|bnode)\d+$')
# Namespace standard: mai toccati dalla potatura dei nomi spuri.
STD_NS = ("http://www.w3.org/2002/07/owl#",
          "http://www.w3.org/2000/01/rdf-schema#",
          "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
          "http://www.w3.org/2001/XMLSchema#",
          "http://www.w3.org/2004/02/skos/core#",
          "http://purl.org/dc/")


def prune_spurious(g):
    """Rimuove artefatti (anche da LLM non deterministico):
      - classi dal nome spurio (es. 'Error1');
      - superclassi nominali della classe principale;
      - proprieta' object/datatype non utilizzate (es. geometrie residue vuote);
      - classi vuote (0 individui, 0 sottoclassi, non dominio/range).
    """
    n_spur = n_sup = n_prop = n_cls = 0

    # 1) classi/individui/proprieta' dal nome chiaramente spurio (anche solo referenziati),
    #    mai nei namespace standard (owl/rdf/rdfs/xsd/...)
    for term in {t for t in (set(g.subjects()) | set(g.objects())) if isinstance(t, URIRef)}:
        if str(term).startswith(STD_NS):
            continue
        local = str(term).rstrip("/#").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        if SPURIOUS_NAME.match(local):
            purge_term(g, term); n_spur += 1

    # 2) la classe principale non deve avere superclassi nominali (solo owl:Thing implicito)
    for sup in list(g.objects(MAIN_CLASS, RDFS.subClassOf)):
        if isinstance(sup, URIRef) and sup != OWL.Thing:
            g.remove((MAIN_CLASS, RDFS.subClassOf, sup)); n_sup += 1

    # 3) proprieta' non usate nei dati
    for p in (list(g.subjects(RDF.type, OWL.ObjectProperty)) +
              list(g.subjects(RDF.type, OWL.DatatypeProperty))):
        if not list(g.triples((None, p, None))):
            purge_term(g, p); n_prop += 1

    # 4) classi vuote (iterativo: rimuovendone una, altre possono svuotarsi)
    changed = True
    while changed:
        changed = False
        dr = set(g.objects(None, RDFS.domain)) | set(g.objects(None, RDFS.range))
        for c in list(g.subjects(RDF.type, OWL.Class)):
            if c == MAIN_CLASS:
                continue
            if (not list(g.subjects(RDF.type, c)) and
                    not list(g.subjects(RDFS.subClassOf, c)) and c not in dr):
                purge_term(g, c); n_cls += 1; changed = True
    return n_spur, n_sup, n_prop, n_cls


# Etichette italiane per i termini noti (classi, object property, datatype property).
# 'description' NON è qui: le sue due etichette italiane (bene / condizione) sono già distinte.
ITALIAN_LABELS = {
    "CulturalInstituteOrSite": "Istituto culturale o sito", "Site": "Sito",
    "Address": "Indirizzo", "AccessCondition": "Condizione di accesso",
    "Email": "Email", "Telephone": "Telefono", "WebSite": "Sito web",
    "Geometria": "Geometria", "Contact": "Contatto",
    "asWKT": "geometria (WKT)", "name": "nome", "fullAddress": "indirizzo completo",
    "emailAddress": "indirizzo email", "telephoneNumber": "numero di telefono", "URL": "URL",
    "accessibilityNote": "nota di accessibilità",
    "hasAddress": "ha indirizzo", "hasAccessCondition": "ha condizione di accesso",
    "hasSite": "ha sito",
    "haCoordinate": "ha coordinate", "eCoordinataDi": "è coordinata di",
    "haContatti": "ha contatti", "eContattoDi": "è contatto di",
    "isAddressOf": "è indirizzo di", "isAccessConditionOf": "è condizione di accesso di",
    "isPartOf": "fa parte di",
}


def italianize_labels(g):
    """Una sola rdfs:label IN ITALIANO per ogni classe e proprieta' (rimuove l'inglese)."""
    n = 0
    terms = (set(g.subjects(RDF.type, OWL.Class)) |
             set(g.subjects(RDF.type, OWL.ObjectProperty)) |
             set(g.subjects(RDF.type, OWL.DatatypeProperty)))
    for t in terms:
        local = str(t).rstrip("/#").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        italian = ITALIAN_LABELS.get(local)
        if italian is None:                       # usa l'etichetta @it gia' presente
            its = [str(o) for o in g.objects(t, RDFS.label)
                   if isinstance(o, Literal) and o.language == "it"]
            italian = its[0] if its else None
        if italian is None:                       # nessuna etichetta italiana disponibile: invariato
            continue
        for o in list(g.objects(t, RDFS.label)):
            g.remove((t, RDFS.label, o))
        g.add((t, RDFS.label, Literal(italian, lang="it")))
        n += 1
    return n


def differentiate_description_labels(g):
    if (ARCO.description, None, None) in g:
        for o in list(g.objects(ARCO.description, RDFS.label)):
            g.remove((ARCO.description, RDFS.label, o))
        g.add((ARCO.description, RDFS.label, Literal("descrizione del bene culturale", lang="it")))
        g.add((ARCO.description, RDFS.label, Literal("cultural property description", lang="en")))
    if (L0.description, None, None) in g:
        for o in list(g.objects(L0.description, RDFS.label)):
            g.remove((L0.description, RDFS.label, o))
        g.add((L0.description, RDFS.label, Literal("descrizione della condizione di accesso", lang="it")))
        g.add((L0.description, RDFS.label, Literal("access condition description", lang="en")))


def set_metadata(g, onto_iri):
    new = URIRef(onto_iri)
    olds = [s for s in g.subjects(RDF.type, OWL.Ontology) if s != new]
    for old in olds:
        for p, o in list(g.predicate_objects(old)):
            g.add((new, p, o)); g.remove((old, p, o))
    g.add((new, RDF.type, OWL.Ontology))
    g.set((new, OWL.versionInfo, Literal("refined")))
    g.set((new, OWL.versionIRI, URIRef(onto_iri.rstrip("/") + "/1.0")))


def _data_ns(g):
    """Deduce il namespace dati (.../data/) dagli individui presenti."""
    for s in g.subjects(RDF.type, MAIN_CLASS):
        m = re.match(r"(.*/data/)", str(s))
        if m:
            return m.group(1)
    return "https://example.org/data/"


def refine(g, onto_iri=DEFAULT_ONTO_IRI):
    data_ns = _data_ns(g)
    rep = []
    c, l = clean_literals_and_comments(g); rep.append(f"commenti provenienza rimossi={c}, letterali puliti={l}")
    rep.append(f"termini orfani rimossi={remove_orphan_terms(g)}")
    rep.append(f"indirizzi normalizzati->afi:hasAddress={normalize_address_property(g)}")
    rep.append(f"geometrie come entità (haCoordinate)={geometry_entity(g)}")
    rep.append(f"sottoclassi rimosse (entità unica)={compact_main_class(g)}")
    rep.append(f"contatti (haContatti)={restructure_contacts(g)}")
    rep.append(f"condizioni di accesso collassate={collapse_access_conditions(g, data_ns)}")
    rep.append(f"isPartOf dedotte={infer_part_of(g)}")
    fix_multilingual_ranges(g); differentiate_description_labels(g)
    rep.append(f"inversi materializzati={materialize_inverses(g)}")
    sp, su, pp, cl = prune_spurious(g)
    rep.append(f"potatura: classi spurie={sp}, superclassi indebite={su}, "
               f"prop. inutilizzate={pp}, classi vuote={cl}")
    rep.append(f"etichette rese italiane (no inglese)={italianize_labels(g)}")
    set_metadata(g, onto_iri)
    return rep


def main(inp, outp=None, onto_iri=DEFAULT_ONTO_IRI):
    outp = outp or inp
    g = Graph(); g.parse(inp, format="turtle")
    before = len(g)
    print(f"\n[refine] {inp}  ({before} triple)")
    for line in refine(g, onto_iri):
        print(f"  - {line}")
    for pfx, ns in [("afi", AFI), ("cis", CIS), ("clv", CLV), ("sm", SM), ("ac", AC),
                    ("l0", L0), ("arco", ARCO), ("geo", GEO), ("owl", OWL),
                    ("rdfs", RDFS), ("xsd", XSD)]:
        g.bind(pfx, ns, replace=True)
    g.serialize(destination=outp, format="turtle")
    print(f"[refine] Triple: {before} -> {len(g)}  | Salvato: {outp}")


if __name__ == "__main__":
    inp = sys.argv[1] if len(sys.argv) > 1 else "architetture_firenze_merged.ttl"
    outp = sys.argv[2] if len(sys.argv) > 2 else inp
    iri = DEFAULT_ONTO_IRI
    if "--onto-iri" in sys.argv:
        iri = sys.argv[sys.argv.index("--onto-iri") + 1]
    main(inp, outp, iri)
