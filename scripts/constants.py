"""Costanti condivise: prefissi, soglie, set di filtraggio."""

from rdflib import RDF, RDFS, OWL

DC = "http://purl.org/dc/elements/1.1/"

KNOWN_PREFIXES = {
    "rdf":      "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs":     "http://www.w3.org/2000/01/rdf-schema#",
    "owl":      "http://www.w3.org/2002/07/owl#",
    "xsd":      "http://www.w3.org/2001/XMLSchema#",
    "dc":       DC,
    "dcterms":  "http://purl.org/dc/terms/",
    "schema":   "http://schema.org/",
    "foaf":     "http://xmlns.com/foaf/0.1/",
    "skos":     "http://www.w3.org/2004/02/skos/core#",
    "geo":      "http://www.w3.org/2003/01/geo/wgs84_pos#",
    "vcard":    "http://www.w3.org/2006/vcard/ns#",
    # Namespace domain-specific con nomi fissi: evita il drift ns1/ns2/ns3/ns4
    "cis":       "http://dati.beniculturali.it/cis/",
    "clv":       "https://w3id.org/italia/onto/CLV/",
    "sm":        "https://w3id.org/italia/onto/SM/",
    "arco":      "https://w3id.org/arco/ontology/core/",
    "l0":        "https://w3id.org/italia/onto/l0/",
    "ac":        "https://w3id.org/italia/onto/AccessCondition/",
    "geosparql": "http://www.opengis.net/ont/geosparql#",
}

# Proprietà di sola provenienza: escluse dall'analisi TBox E dall'output merged
PROVENANCE_PROPS = {
    "http://purl.org/dc/terms/isPartOf",
}

SKIP_PROPS = {
    str(RDF.type), str(RDFS.label), str(RDFS.comment),
    str(RDFS.seeAlso), str(RDFS.isDefinedBy), str(OWL.sameAs),
} | PROVENANCE_PROPS

# Proprietà i cui valori sono URIRef nell'ABox ma devono essere dichiarate
# come owl:DatatypeProperty (xsd:anyURI) nel TBox.
FORCE_DATATYPE_PROPS = {
    "https://w3id.org/italia/onto/SM/URL",
}

META_CLASSES = {
    str(OWL.Class), str(OWL.Ontology), str(OWL.Thing), str(OWL.Nothing),
    str(RDFS.Class), str(RDFS.Resource),
    str(OWL.ObjectProperty), str(OWL.DatatypeProperty),
    str(OWL.AnnotationProperty), str(OWL.FunctionalProperty),
    str(OWL.NamedIndividual),
}

# Soglie paper (Zengeya et al.)
TAU_SUB = 0.7
TAU_SIM = 0.6
TAU_DOM = 0.75

TBOX_PREDICATES = {
    str(RDF.type),
    str(RDFS.subClassOf), str(RDFS.domain), str(RDFS.range),
    str(OWL.equivalentClass), str(OWL.disjointWith), str(OWL.inverseOf),
    str(OWL.onProperty), str(OWL.cardinality),
    str(OWL.minCardinality), str(OWL.maxCardinality),
    str(OWL.someValuesFrom), str(OWL.allValuesFrom), str(OWL.hasValue),
    str(OWL.unionOf), str(OWL.intersectionOf), str(OWL.propertyChainAxiom),
    str(RDFS.comment),
}

TBOX_TYPES = {
    str(OWL.Class), str(OWL.Ontology),
    str(OWL.ObjectProperty), str(OWL.DatatypeProperty),
    str(OWL.AnnotationProperty), str(OWL.FunctionalProperty),
    str(OWL.InverseFunctionalProperty), str(OWL.TransitiveProperty),
    str(OWL.SymmetricProperty), str(OWL.Restriction),
    str(RDFS.Class),
}
