import re
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, render_template
from rdflib import Graph, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS

BASE_DIR = Path(__file__).resolve().parent.parent
TTL_PATH = BASE_DIR / "architetture_firenze_fixed.ttl"

AFI = Namespace("https://w3id.org/firenze/architetture/")
CIS = Namespace("http://dati.beniculturali.it/cis/")
ARCO = Namespace("https://w3id.org/arco/ontology/core/")
CLV = Namespace("https://w3id.org/italia/onto/CLV/")
GEO = Namespace("http://www.opengis.net/ont/geosparql#")
L0 = Namespace("https://w3id.org/italia/onto/l0/")
SM = Namespace("https://w3id.org/italia/onto/SM/")
AC = Namespace("https://w3id.org/italia/onto/AccessCondition/")

INSTITUTE_BASE = "https://linkedopendata.comune.fi.it/data/cultural-institute/"
COMMONS_HEADERS = {
    "User-Agent": "ArchitettureFirenzeWebApp/1.0 (educational project; contact: example@example.com)"
}
INST_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
WKT_RE = re.compile(r"POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)\s*\)")

PREFIXES = """
PREFIX afi: <https://w3id.org/firenze/architetture/>
PREFIX cis: <http://dati.beniculturali.it/cis/>
PREFIX arco: <https://w3id.org/arco/ontology/core/>
PREFIX clv: <https://w3id.org/italia/onto/CLV/>
PREFIX geo: <http://www.opengis.net/ont/geosparql#>
PREFIX l0: <https://w3id.org/italia/onto/l0/>
PREFIX sm: <https://w3id.org/italia/onto/SM/>
PREFIX ac: <https://w3id.org/italia/onto/AccessCondition/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
"""

app = Flask(__name__)

graph = Graph()
graph.parse(TTL_PATH, format="turtle")


MOJIBAKE_MARKERS = ("Ã", "â€", "Â")


def _reverse_to_bytes(s):
    """Reverse a string back to the bytes it likely came from: each char is
    encoded as cp1252 where possible (covers € ™ ' " œ … from 'â€™'/'â€œ'
    sequences), otherwise as a raw Latin-1 byte. Returns None if a char is
    outside both (unrecoverable), so the caller leaves the text untouched.
    """
    out = bytearray()
    for ch in s:
        try:
            out.extend(ch.encode("cp1252"))
        except UnicodeEncodeError:
            code = ord(ch)
            if code <= 0xFF:
                out.append(code)
            else:
                return None
    return bytes(out)


def fix_mojibake(text):
    """Repair text that was UTF-8 encoded, wrongly decoded as Windows-1252, and
    re-encoded as UTF-8 upstream in the pipeline (e.g. 'cittÃ ' -> 'città',
    'Lâ€™Associazione' -> 'L'Associazione', 'â€œ...â€' -> '"..."').
    Best-effort: a few characters lost in the process surface as U+FFFD.
    """
    if text is None:
        return None
    # 'à è ì ò ù' a fine parola erano seguite da un nbsp che a monte è diventato
    # spazio normale: i byte 'Ã ' (0xC3 + 0x20) non sono UTF-8 valido. Ripristino
    # il nbsp così che il reverse produca 'à ' invece di un carattere perso.
    s = text.replace("Â ", "Â ").replace("Ã ", "Ã ")
    for _ in range(3):
        if not any(marker in s for marker in MOJIBAKE_MARKERS):
            break
        raw = _reverse_to_bytes(s)
        if raw is None:
            break
        new_s = raw.decode("utf-8", errors="replace")
        if new_s == s:
            break
        s = new_s
    # eventuali byte residui non recuperabili (es. un nbsp finale perso del tutto)
    # diventano U+FFFD: sono solo rumore, li rimuovo per una resa pulita.
    if s != text:
        s = s.replace("�", "")
    return s


def short_id(uri: str) -> str:
    return uri.rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def require_valid_id(inst_id: str) -> None:
    if not INST_ID_RE.match(inst_id):
        abort(404)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/monuments")
def list_monuments():
    query = PREFIXES + """
    SELECT ?inst ?name WHERE {
        ?inst a cis:CulturalInstituteOrSite ;
              l0:name ?name .
    }
    ORDER BY ?name
    """
    results = [
        {"id": short_id(str(row.inst)), "name": fix_mojibake(str(row.name))}
        for row in graph.query(query)
    ]
    return jsonify(results)


@app.route("/api/monuments/<inst_id>")
def monument_detail(inst_id):
    require_valid_id(inst_id)
    inst_uri = f"{INSTITUTE_BASE}{inst_id}"

    detail_query = PREFIXES + f"""
    SELECT ?name ?description ?accessibilityNote ?fullAddress ?wkt ?accessLabel ?accessDesc WHERE {{
        BIND(<{inst_uri}> AS ?inst)
        ?inst l0:name ?name .
        OPTIONAL {{ ?inst arco:description ?description }}
        OPTIONAL {{ ?inst afi:accessibilityNote ?accessibilityNote }}
        OPTIONAL {{ ?inst afi:hasAddress ?addr . ?addr clv:fullAddress ?fullAddress }}
        OPTIONAL {{ ?inst afi:haCoordinate ?geom . ?geom geo:asWKT ?wkt }}
        OPTIONAL {{
            ?inst ac:hasAccessCondition ?ac .
            ?ac rdfs:label ?accessLabel .
            OPTIONAL {{ ?ac l0:description ?accessDesc }}
        }}
    }}
    """
    rows = list(graph.query(detail_query))
    if not rows:
        abort(404)
    row = rows[0]

    lat = lon = None
    if row.wkt:
        match = WKT_RE.search(str(row.wkt))
        if match:
            lon, lat = float(match.group(1)), float(match.group(2))

    contacts_query = PREFIXES + f"""
    SELECT DISTINCT ?c ?type ?value WHERE {{
        BIND(<{inst_uri}> AS ?inst)
        ?inst afi:haContatti+ ?c .
        ?c a ?type .
        FILTER(?type IN (sm:WebSite, sm:Email, sm:Telephone))
        OPTIONAL {{ ?c sm:URL ?value }}
        OPTIONAL {{ ?c sm:emailAddress ?value }}
        OPTIONAL {{ ?c sm:telephoneNumber ?value }}
    }}
    """
    websites, emails, phones = [], [], []
    seen = set()
    for crow in graph.query(contacts_query):
        if crow.value is None:
            continue
        key = (str(crow.type), str(crow.value))
        if key in seen:
            continue
        seen.add(key)
        value = str(crow.value)
        if crow.type == SM.WebSite:
            websites.append(value)
        elif crow.type == SM.Email:
            emails.append(value)
        elif crow.type == SM.Telephone:
            phones.append(value)

    access_condition = None
    if row.accessLabel:
        access_condition = {
            "label": fix_mojibake(str(row.accessLabel)),
            "description": fix_mojibake(str(row.accessDesc)) if row.accessDesc else None,
        }

    return jsonify({
        "id": inst_id,
        "name": fix_mojibake(str(row.name)),
        "description": fix_mojibake(str(row.description)) if row.description else None,
        "accessibilityNote": fix_mojibake(str(row.accessibilityNote)) if row.accessibilityNote else None,
        "address": fix_mojibake(str(row.fullAddress)) if row.fullAddress else None,
        "lat": lat,
        "lon": lon,
        "accessCondition": access_condition,
        "contacts": {
            "websites": sorted(set(websites)),
            "emails": sorted(set(emails)),
            "phones": sorted(set(phones)),
        },
    })


@app.route("/api/monuments/<inst_id>/photos")
def monument_photos(inst_id):
    require_valid_id(inst_id)
    inst_uri = f"{INSTITUTE_BASE}{inst_id}"

    name_query = PREFIXES + f"""
    SELECT ?name WHERE {{
        BIND(<{inst_uri}> AS ?inst)
        ?inst l0:name ?name .
    }}
    """
    rows = list(graph.query(name_query))
    if not rows:
        abort(404)
    name = fix_mojibake(str(rows[0].name))

    return jsonify({"name": name, "photos": fetch_commons_photos(name)})


# Predicati object-property da seguire per costruire il grafo ABox del monumento.
MONUMENT_GRAPH_PREDS = [AFI.hasAddress, AFI.haCoordinate, AC.hasAccessCondition, AFI.haContatti]


def node_label_and_kind(uri):
    """Etichetta breve e 'kind' (per colore) di un individuo del grafo monumento."""
    types = set(graph.objects(uri, RDF.type))

    def first(prop):
        for v in graph.objects(uri, prop):
            return str(v)
        return None

    if CIS.CulturalInstituteOrSite in types:
        return fix_mojibake(first(L0.name) or first(RDFS.label) or short_id(str(uri))), "Monument"
    if SM.WebSite in types:
        return first(SM.URL) or short_id(str(uri)), "WebSite"
    if SM.Email in types:
        return first(SM.emailAddress) or short_id(str(uri)), "Email"
    if SM.Telephone in types:
        return first(SM.telephoneNumber) or short_id(str(uri)), "Telephone"
    if CLV.Address in types:
        return fix_mojibake(first(CLV.fullAddress) or short_id(str(uri))), "Address"
    if AC.AccessCondition in types:
        return fix_mojibake(first(RDFS.label) or "Condizione di accesso"), "AccessCondition"
    if AFI.Geometria in types:
        return "Coordinate", "Geometria"
    return short_id(str(uri)), "Other"


def predicate_label(pred):
    for o in graph.objects(pred, RDFS.label):
        return str(o)
    return short_id(str(pred))


def short_label(text, limit=42):
    return text if len(text) <= limit else text[: limit - 1] + "…"


@app.route("/api/monuments/<inst_id>/graph")
def monument_graph(inst_id):
    require_valid_id(inst_id)
    inst_uri = URIRef(f"{INSTITUTE_BASE}{inst_id}")
    if (inst_uri, RDF.type, CIS.CulturalInstituteOrSite) not in graph:
        abort(404)

    nodes = {}
    edges = []
    seen_edges = set()

    def ensure_node(uri):
        key = str(uri)
        if key not in nodes:
            label, kind = node_label_and_kind(uri)
            nodes[key] = {"id": key, "label": short_label(label), "kind": kind}
        return key

    ensure_node(inst_uri)

    # BFS a 2 salti: monumento -> (indirizzo/coordinate/accesso/contatti);
    # sito web -> email/telefono (secondo salto).
    frontier = [inst_uri]
    visited = {inst_uri}
    for _hop in range(2):
        next_frontier = []
        for subj in frontier:
            for pred in MONUMENT_GRAPH_PREDS:
                for obj in graph.objects(subj, pred):
                    ensure_node(subj)
                    ensure_node(obj)
                    edge_key = (str(subj), str(pred), str(obj))
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        edges.append({
                            "from": str(subj),
                            "to": str(obj),
                            "label": predicate_label(pred),
                        })
                    if obj not in visited:
                        visited.add(obj)
                        next_frontier.append(obj)
        frontier = next_frontier

    return jsonify({"nodes": list(nodes.values()), "edges": edges})


def fetch_commons_photos(query: str, limit: int = 8):
    try:
        search_resp = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "list": "search",
                "srsearch": f"{query} Firenze",
                "srnamespace": 6,
                "srlimit": limit,
            },
            headers=COMMONS_HEADERS,
            timeout=5,
        )
        search_resp.raise_for_status()
        titles = [
            item["title"]
            for item in search_resp.json().get("query", {}).get("search", [])
        ]
        if not titles:
            return []

        info_resp = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "format": "json",
                "titles": "|".join(titles),
                "prop": "imageinfo",
                "iiprop": "url",
                "iiurlwidth": 480,
            },
            headers=COMMONS_HEADERS,
            timeout=5,
        )
        info_resp.raise_for_status()
        pages = info_resp.json().get("query", {}).get("pages", {})

        photos = []
        for page in pages.values():
            for info in page.get("imageinfo", []):
                thumb = info.get("thumburl") or info.get("url")
                full = info.get("url")
                if thumb:
                    photos.append({"thumb": thumb, "full": full})
        return photos
    except requests.RequestException:
        return []


def resolve_class_targets(term):
    """Espande un termine usato come rdfs:domain/range in classi IRI concrete.
    Se è una classe anonima owl:unionOf, restituisce le classi membro; se è un
    IRI, lo restituisce così com'è; altrimenti (es. owl:Restriction) lo ignora.
    """
    if isinstance(term, URIRef):
        return [str(term)]
    union_list = graph.value(term, OWL.unionOf)
    if union_list is not None:
        return [str(m) for m in Collection(graph, union_list) if isinstance(m, URIRef)]
    return []


@app.route("/api/graph")
def ontology_graph():
    classes_query = PREFIXES + """
    SELECT ?cls ?label WHERE {
        ?cls a owl:Class .
        OPTIONAL { ?cls rdfs:label ?label }
        FILTER(isIRI(?cls))
    }
    """
    subclass_query = PREFIXES + """
    SELECT ?sub ?super WHERE {
        ?sub rdfs:subClassOf ?super .
        FILTER(isIRI(?super))
    }
    """
    props_query = PREFIXES + """
    SELECT ?prop ?label ?domain ?range ?inverse WHERE {
        ?prop a owl:ObjectProperty .
        OPTIONAL { ?prop rdfs:label ?label }
        OPTIONAL { ?prop rdfs:domain ?domain }
        OPTIONAL { ?prop rdfs:range ?range }
        OPTIONAL { ?prop owl:inverseOf ?inverse }
    }
    """

    nodes = {}
    for row in graph.query(classes_query):
        cid = str(row.cls)
        nodes[cid] = {
            "id": cid,
            "label": str(row.label) if row.label else short_id(cid),
        }

    edges = []
    for row in graph.query(subclass_query):
        edges.append({
            "from": str(row.sub),
            "to": str(row.super),
            "label": "subClassOf",
            "dashes": True,
        })

    # Raccoglie le object property; le coppie inverse (owl:inverseOf) vengono fuse
    # in un unico arco bidirezionale con label "p / q" per non disegnare due archi
    # opposti con etichette sovrapposte (es. "ha contatti" / "è contatto di").
    props = {}
    for row in graph.query(props_query):
        p = str(row.prop)
        entry = props.setdefault(p, {
            "label": str(row.label) if row.label else short_id(p),
            "domains": set(), "ranges": set(), "inverse": None,
        })
        if row.domain:
            entry["domains"].update(resolve_class_targets(row.domain))
        if row.range:
            entry["ranges"].update(resolve_class_targets(row.range))
        if row.inverse:
            entry["inverse"] = str(row.inverse)

    # owl:inverseOf è spesso dichiarato su un solo lato: rendi la relazione simmetrica.
    for p, info in props.items():
        inv = info["inverse"]
        if inv and inv in props and not props[inv]["inverse"]:
            props[inv]["inverse"] = p

    emitted = set()
    for p, info in props.items():
        if p in emitted or not (info["domains"] and info["ranges"]):
            continue
        inv = info["inverse"]
        bidirectional = bool(inv and inv in props)
        if bidirectional:
            emitted.add(inv)
            label = f"{info['label']} / {props[inv]['label']}"
        else:
            label = info["label"]
        for src in info["domains"]:
            for dst in info["ranges"]:
                edges.append({"from": src, "to": dst, "label": label,
                              "bidirectional": bidirectional})

    used_ids = {e["from"] for e in edges} | {e["to"] for e in edges}
    for node_id in used_ids:
        nodes.setdefault(node_id, {"id": node_id, "label": short_id(node_id)})

    return jsonify({"nodes": list(nodes.values()), "edges": edges})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
