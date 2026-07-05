import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from flask import Flask, abort, jsonify, render_template, request
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS

BASE_DIR = Path(__file__).resolve().parent.parent
TTL_PATH = BASE_DIR / "architetture_firenze_fixed.ttl"
# I monumenti aggiunti da "Aggiungi Monumento" vengono salvati qui, separati
# dal TTL curato originale: evita di riserializzare (e quindi riformattare)
# il file principale ad ogni inserimento.
ADDITIONS_PATH = BASE_DIR / "architetture_firenze_additions.ttl"

AFI = Namespace("https://w3id.org/firenze/architetture/")
CIS = Namespace("http://dati.beniculturali.it/cis/")
ARCO = Namespace("https://w3id.org/arco/ontology/core/")
CLV = Namespace("https://w3id.org/italia/onto/CLV/")
GEO = Namespace("http://www.opengis.net/ont/geosparql#")
L0 = Namespace("https://w3id.org/italia/onto/l0/")
SM = Namespace("https://w3id.org/italia/onto/SM/")
AC = Namespace("https://w3id.org/italia/onto/AccessCondition/")

# Vocabolari esterni usati dall'arricchimento via web (DESCRIBE su DBpedia).
SCHEMA = Namespace("http://schema.org/")
DBO = Namespace("http://dbpedia.org/ontology/")
DBP = Namespace("http://dbpedia.org/property/")
DBR = Namespace("http://dbpedia.org/resource/")
FOAF = Namespace("http://xmlns.com/foaf/0.1/")
DCT = Namespace("http://purl.org/dc/terms/")

INSTITUTE_BASE = "https://linkedopendata.comune.fi.it/data/cultural-institute/"
# Namespace dei dati per i monumenti aggiunti dal form "Aggiungi Monumento":
# deliberatamente diverso da linkedopendata.comune.fi.it (il registro
# open-data ufficiale), per non far passare dati inseriti dagli utenti come
# ID ufficiali del comune.
ADDED_DATA_BASE = "https://w3id.org/firenze/architetture/data/"
ADDED_INSTITUTE_BASE = f"{ADDED_DATA_BASE}cultural-institute/"
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
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""

app = Flask(__name__)

graph = Graph()
graph.parse(TTL_PATH, format="turtle")

# Grafo separato che accumula solo i monumenti aggiunti a runtime: viene
# unito al grafo principale (così è subito interrogabile come tutto il
# resto) e ri-serializzato per intero ad ogni inserimento, per persistere
# le aggiunte tra un riavvio e l'altro senza toccare il TTL curato.
additions_graph = Graph()
if ADDITIONS_PATH.exists():
    additions_graph.parse(ADDITIONS_PATH, format="turtle")
    for triple in additions_graph:
        graph.add(triple)


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


def parse_wkt_point(wkt: str):
    """Estrae (lon, lat) da un letterale WKT 'POINT(lon lat)'. None se non valido."""
    match = WKT_RE.search(wkt)
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def haversine_km(lat1, lon1, lat2, lon2):
    """Distanza in linea d'aria (great-circle) in chilometri tra due punti
    espressi in gradi decimali, con la formula dell'emisenoverso."""
    radius = 6371.0  # raggio medio terrestre in km
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def require_valid_id(inst_id: str) -> None:
    if not INST_ID_RE.match(inst_id):
        abort(404)


def resolve_institute_uri(inst_id: str) -> str:
    """Risolve l'id di un monumento al suo URI completo, cercandolo prima nel
    namespace ufficiale (linkedopendata) e poi in quello dei monumenti aggiunti
    dal form. Va in 404 se non esiste in nessuno dei due."""
    for base in (INSTITUTE_BASE, ADDED_INSTITUTE_BASE):
        uri = URIRef(f"{base}{inst_id}")
        if (uri, RDF.type, CIS.CulturalInstituteOrSite) in graph:
            return str(uri)
    abort(404)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/monuments")
def list_monuments():
    # QUERY: elenco-monumenti
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


@app.route("/api/stats")
def accessibility_stats():
    """Conteggio dei monumenti per condizione di accessibilità.
    Query SPARQL di aggregazione (GROUP BY + COUNT): l'OPTIONAL include anche i
    monumenti senza condizione dichiarata, che COALESCE etichetta come
    'Nessuna informazione' (assenza != falsità, Open World Assumption).
    """
    # QUERY: statistiche-accessibilita
    query = PREFIXES + """
    SELECT ?stato (COUNT(DISTINCT ?inst) AS ?numero) WHERE {
        ?inst a cis:CulturalInstituteOrSite .
        OPTIONAL { ?inst ac:hasAccessCondition ?ac . ?ac rdfs:label ?lbl . }
        BIND(COALESCE(STR(?lbl), "Nessuna informazione") AS ?stato)
    }
    GROUP BY ?stato
    ORDER BY DESC(?numero)
    """
    stats = [
        {"label": fix_mojibake(str(row.stato)), "count": int(row.numero)}
        for row in graph.query(query)
    ]
    return jsonify({"total": sum(s["count"] for s in stats), "stats": stats})


@app.route("/api/monuments/<inst_id>")
def monument_detail(inst_id):
    require_valid_id(inst_id)
    inst_uri = resolve_institute_uri(inst_id)

    # QUERY: dettaglio-monumento
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

    # QUERY: contatti-monumento
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


@app.route("/api/monuments/<inst_id>/nearby")
def monument_nearby(inst_id):
    """I 5 monumenti più vicini (distanza in linea d'aria) a quello dato.

    La SELECT recupera nome e coordinate (geo:asWKT) di tutti i monumenti
    georeferenziati; la distanza great-circle si calcola poi in Python con la
    formula dell'emisenoverso (haversine), che SPARQL puro non offre senza le
    estensioni GeoSPARQL. Si escludono il monumento stesso e quelli privi di
    coordinate, si ordina per distanza crescente e si restituiscono i primi 5.
    """
    require_valid_id(inst_id)
    inst_uri = resolve_institute_uri(inst_id)

    # QUERY: monumenti-vicini
    coords_query = PREFIXES + """
    SELECT ?inst ?name ?wkt WHERE {
        ?inst a cis:CulturalInstituteOrSite ;
              l0:name ?name ;
              afi:haCoordinate ?geom .
        ?geom geo:asWKT ?wkt .
    }
    """
    points = {}
    for row in graph.query(coords_query):
        coord = parse_wkt_point(str(row.wkt))
        if coord is None:
            continue
        lon, lat = coord
        points[str(row.inst)] = {
            "id": short_id(str(row.inst)),
            "name": fix_mojibake(str(row.name)),
            "lat": lat,
            "lon": lon,
        }

    origin = points.get(inst_uri)
    if origin is None:
        # Il monumento selezionato non ha coordinate: niente con cui calcolare
        # le distanze. Lista vuota, non un errore.
        return jsonify({"nearby": []})

    nearby = []
    for uri, p in points.items():
        if uri == inst_uri:
            continue
        distance = haversine_km(origin["lat"], origin["lon"], p["lat"], p["lon"])
        nearby.append({"id": p["id"], "name": p["name"], "distanceKm": round(distance, 2)})

    nearby.sort(key=lambda m: m["distanceKm"])
    return jsonify({"nearby": nearby[:5]})


@app.route("/api/monuments/<inst_id>/photos")
def monument_photos(inst_id):
    require_valid_id(inst_id)
    inst_uri = resolve_institute_uri(inst_id)

    # QUERY: nome-monumento-foto
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
    inst_uri = URIRef(resolve_institute_uri(inst_id))

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


# Monumenti più iconici di Firenze usati per lo sfondo dinamico della home.
# Nomi scelti tra quelli presenti in l0:name nel TTL (così i link restano
# coerenti con l'ontologia), privilegiando i soggetti più fotografati.
BACKGROUND_MONUMENT_NAMES = [
    "Gli Uffizi",
    "Ponte Vecchio",
    "Piazza del Duomo",
    "Piazza della Signoria",
    "Palazzo Vecchio",
    "Piazzale Michelangiolo",
    "Piazza di Santa Croce",
    "Piazza di Santa Maria Novella",
]

_background_photos_cache = None


def _load_background_photos():
    # Le chiamate a Wikimedia Commons sono indipendenti: eseguirle in parallelo
    # riduce il tempo totale da ~1.5s*N (sequenziale) a ~1.5s (il più lento).
    with ThreadPoolExecutor(max_workers=len(BACKGROUND_MONUMENT_NAMES)) as pool:
        results = pool.map(lambda name: fetch_commons_photos(name, limit=1), BACKGROUND_MONUMENT_NAMES)
    return [found[0] for found in results if found]


@app.route("/api/background-photos")
def background_photos():
    """Una foto per ciascuno dei monumenti più iconici, per lo sfondo della home.
    Cache in-memory di processo: i risultati di Wikimedia Commons non cambiano
    a runtime, niente senso richiamarli ad ogni caricamento di pagina. Viene
    già pre-caricata in background all'avvio (vedi sotto); qui si ricalcola
    solo se quel prefetch non fosse ancora terminato.
    Se un fallimento di rete transitorio lascia la cache vuota, NON viene
    considerata definitiva: si ritenta alla richiesta successiva invece di
    restare vuota per sempre fino al riavvio del processo.
    """
    global _background_photos_cache
    if not _background_photos_cache:
        _background_photos_cache = _load_background_photos()
    return jsonify({"photos": _background_photos_cache})


def _prefetch_background_photos():
    global _background_photos_cache
    _background_photos_cache = _load_background_photos()


threading.Thread(target=_prefetch_background_photos, daemon=True).start()


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


def _ontology_graph_data():
    """Costruisce la vista nodi/archi dell'ontologia di base (TBox): classi,
    gerarchia subClassOf e object property (con fusione delle coppie inverse).
    Nessun individuo: è il grafo dell'ontologia, non il popolamento."""
    # QUERY: ontologia-classi
    classes_query = PREFIXES + """
    SELECT ?cls ?label WHERE {
        ?cls a owl:Class .
        OPTIONAL { ?cls rdfs:label ?label }
        FILTER(isIRI(?cls))
    }
    """
    # QUERY: ontologia-sottoclassi
    subclass_query = PREFIXES + """
    SELECT ?sub ?super WHERE {
        ?sub rdfs:subClassOf ?super .
        FILTER(isIRI(?super))
    }
    """
    # QUERY: ontologia-proprieta
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

    return {"nodes": list(nodes.values()), "edges": edges}


@app.route("/api/graph")
def ontology_graph():
    return jsonify(_ontology_graph_data())


@app.route("/api/same-location/ontology")
def same_location_ontology():
    """Il grafo dell'ontologia di base (TBox: classi e proprietà) ARRICCHITO con il
    popolamento della sola relazione afi:stessaUbicazioneDi: si eseguono la
    CONSTRUCT e si aggiungono al grafo i monumenti realmente collegati (le istanze,
    in blu), gli archi rossi della nuova relazione tra di essi e il rispettivo
    rdf:type verso la classe CulturalInstituteOrSite. Così la relazione si vede
    "in azione" sui dati, tra nodi distinti, invece che come self-loop sulla classe
    (che sigma non disegna)."""
    data = _ontology_graph_data()
    cis = str(CIS.CulturalInstituteOrSite)
    if cis not in {n["id"] for n in data["nodes"]}:
        data["nodes"].append({"id": cis, "label": short_id(cis)})

    constructed = graph.query(SAME_LOCATION_CONSTRUCT).graph

    def name_of(term):
        n = graph.value(term, L0.name)
        return fix_mojibake(str(n)) if n else short_id(str(term))

    # toponimo per ogni monumento, per colorare le istanze per zona (la legenda
    # nel frontend mappa colore -> via/piazza, come nel "Grafo delle triple prodotte").
    toponym_by_uri = {}
    for row in graph.query(SAME_LOCATION_SELECT):
        top = fix_mojibake(str(row.fa).split(",")[0].strip())
        if top:
            toponym_by_uri[str(row.inst)] = top

    instances = {}
    rel_edges = []
    for s, _, o in constructed:
        for term in (s, o):
            instances.setdefault(str(term), term)
        rel_edges.append({"from": str(s), "to": str(o),
                          "label": "stessa ubicazione", "added": True})

    for uri, term in instances.items():
        data["nodes"].append({"id": uri, "label": name_of(term), "kind": "Monument",
                              "group": toponym_by_uri.get(uri, ""), "size": 11})
        # rdf:type: l'istanza appartiene alla classe (arco tratteggiato).
        data["edges"].append({"from": uri, "to": cis, "label": "rdf:type", "dashes": True})
    data["edges"].extend(rel_edges)

    return jsonify(data)


# Etichette italiane (rdfs:label) delle 3 condizioni di accesso già presenti
# nel TTL: il form propone queste categorie condivise (coerente con come il
# resto dei dati è modellato). La query CONSTRUCT sotto usa questa etichetta
# come chiave di ricerca nel grafo (FILTER su rdfs:label), così l'URI della
# condizione di accesso è trovato per pattern matching e non hardcoded qui.
ACCESS_CONDITION_LABELS = {
    "non-accessibile": "Non accessibile",
    "parzialmente-accessibile": "Parzialmente accessibile",
    "totalmente-accessibile": "Totalmente accessibile",
}

# Query CONSTRUCT che produce l'unica tripla mancante: il collegamento tra il
# monumento e la condizione di accesso scelta. Coerente con la teoria del
# corso, il WHERE è un vero graph pattern, non un template vuoto:
#  • FILTER NOT EXISTS realizza l'idea della sez. 7.40 (Mondo Aperto vs Chiuso):
#    si interroga *esplicitamente* l'assenza del dato — un monumento senza
#    ac:hasAccessCondition — perché nel Web Semantico l'assenza di una tripla
#    non è di per sé "falso", va cercata di proposito. Se il monumento ha già
#    una condizione la CONSTRUCT non genera nulla: non si sovrascrive mai un
#    dato esistente.
#  • ?access viene *trovata* nel grafo per pattern matching sulla sua
#    rdfs:label (FILTER su confronto di letterali), invece di costruirne l'URI
#    a mano in Python.
# ?inst e ?accessKeyword arrivano via initBindings come termini RDF già
# tipizzati (niente interpolazione di stringhe -> niente SPARQL injection).
# QUERY: assegna-accessibilita
SET_ACCESS_CONSTRUCT = PREFIXES + """
CONSTRUCT {
    ?inst ac:hasAccessCondition ?access .
}
WHERE {
    ?inst a cis:CulturalInstituteOrSite .
    FILTER NOT EXISTS { ?inst ac:hasAccessCondition ?existing }
    ?access a ac:AccessCondition ;
            rdfs:label ?accessLabel .
    FILTER(LCASE(STR(?accessLabel)) = LCASE(STR(?accessKeyword)))
}
"""


def _save_additions():
    for pfx, ns in [("afi", AFI), ("cis", CIS), ("clv", CLV), ("sm", SM),
                    ("ac", AC), ("l0", L0), ("geo", GEO), ("rdfs", RDFS), ("owl", OWL)]:
        additions_graph.bind(pfx, ns, replace=True)
    additions_graph.serialize(destination=ADDITIONS_PATH, format="turtle")


@app.route("/api/monuments/missing-access")
def list_monuments_missing_access():
    """Elenco dei monumenti privi di condizione di accesso, da proporre nella
    sezione "Modifica Accessibilità". La SELECT usa FILTER NOT EXISTS per
    cercare di proposito l'assenza della tripla ac:hasAccessCondition: è il
    risvolto pratico dell'Open World Assumption (l'assenza va interrogata
    esplicitamente, non è un "falso" implicito)."""
    # QUERY: monumenti-senza-accessibilita
    query = PREFIXES + """
    SELECT ?inst ?name WHERE {
        ?inst a cis:CulturalInstituteOrSite ;
              l0:name ?name .
        FILTER NOT EXISTS { ?inst ac:hasAccessCondition ?ac }
    }
    ORDER BY ?name
    """
    results = [
        {"id": short_id(str(row.inst)), "name": fix_mojibake(str(row.name))}
        for row in graph.query(query)
    ]
    return jsonify(results)


@app.route("/api/monuments/<inst_id>/access", methods=["POST"])
def set_access_condition(inst_id):
    """Assegna una condizione di accesso a un monumento che ne è privo,
    eseguendo la query SET_ACCESS_CONSTRUCT e unendo la tripla risultante al
    grafo (in memoria e su file). Se il monumento ha già una condizione la
    query non produce triple (FILTER NOT EXISTS) e la richiesta viene
    rifiutata: questa sezione modifica solo i dati mancanti.
    """
    require_valid_id(inst_id)
    inst_uri = URIRef(resolve_institute_uri(inst_id))

    payload = request.get_json(silent=True) or {}
    access_key = payload.get("accessCondition") or None

    if access_key not in ACCESS_CONDITION_LABELS:
        return jsonify({"errors": {"accessCondition": "Condizione di accesso non valida."}}), 400

    bindings = {
        "inst": inst_uri,
        # Si passa solo l'etichetta italiana, non l'URI: il WHERE della query
        # trova ?access cercandola nel grafo per rdfs:label (pattern matching).
        "accessKeyword": Literal(ACCESS_CONDITION_LABELS[access_key], lang="it"),
    }
    constructed = graph.query(SET_ACCESS_CONSTRUCT, initBindings=bindings).graph

    if len(constructed) == 0:
        # Nessuna tripla generata: il monumento ha già una condizione di
        # accesso (la FILTER NOT EXISTS lo ha escluso). Non c'è nulla da
        # modificare in questa sezione.
        return jsonify({"errors": {"accessCondition": "Questo monumento ha già una condizione di accesso."}}), 409

    for triple in constructed:
        graph.add(triple)
        additions_graph.add(triple)
    _save_additions()

    return jsonify({"id": inst_id, "accessCondition": ACCESS_CONDITION_LABELS[access_key]}), 200


# ─────────────────────────────────────────────────────────────────────
# CONSTRUCT — stessa via/piazza (afi:stessaUbicazioneDi)
# ─────────────────────────────────────────────────────────────────────
# Materializza una relazione assente nei dati grezzi: due monumenti condividono
# l'ubicazione se hanno lo stesso toponimo (la via o la piazza). Il toponimo è
# il testo prima della prima virgola in clv:fullAddress (es. "Piazza del Duomo,
# 50122, Firenze, Italia" -> "piazza del duomo"), estratto in pura SPARQL con
# STRBEFORE. Il confronto è in minuscolo (LCASE) per non distinguere maiuscole.
# FILTER(STR(?a) < STR(?b)) tiene una sola coppia per accoppiamento ed esclude
# l'accoppiamento di un monumento con sé stesso.
# QUERY: stessa-ubicazione-construct
SAME_LOCATION_CONSTRUCT = PREFIXES + """
CONSTRUCT {
    ?a afi:stessaUbicazioneDi ?b .
}
WHERE {
    ?a a cis:CulturalInstituteOrSite ; afi:hasAddress ?addrA .
    ?addrA clv:fullAddress ?fa .
    ?b a cis:CulturalInstituteOrSite ; afi:hasAddress ?addrB .
    ?addrB clv:fullAddress ?fb .
    FILTER(STR(?a) < STR(?b))
    BIND(LCASE(STRBEFORE(?fa, ",")) AS ?viaA)
    BIND(LCASE(STRBEFORE(?fb, ",")) AS ?viaB)
    FILTER(?viaA != "" && ?viaA = ?viaB)
}
"""

# SELECT di supporto: monumento -> toponimo, per raggrupparli lato server e
# offrire al frontend la vista a schede (una per via/piazza). La CONSTRUCT sopra
# resta la fonte delle triple RDF mostrate; questa serve solo all'impaginazione.
# QUERY: monumenti-per-toponimo
SAME_LOCATION_SELECT = PREFIXES + """
SELECT ?inst ?name ?fa WHERE {
    ?inst a cis:CulturalInstituteOrSite ;
          l0:name ?name ;
          afi:hasAddress ?addr .
    ?addr clv:fullAddress ?fa .
}
"""


@app.route("/api/same-location-construct")
def same_location_construct():
    """Esegue la CONSTRUCT che collega i monumenti con lo stesso toponimo
    (afi:stessaUbicazioneDi) e restituisce i monumenti raggruppati per via/piazza
    (solo i luoghi condivisi da almeno due monumenti) per la resa a schede, più una
    vista a grafo (nodi/archi) delle triple generate per disegnarle con sigma."""
    constructed = graph.query(SAME_LOCATION_CONSTRUCT).graph

    groups = {}
    for row in graph.query(SAME_LOCATION_SELECT):
        toponym = fix_mojibake(str(row.fa).split(",")[0].strip())
        if not toponym:
            continue
        bucket = groups.setdefault(toponym.lower(),
                                   {"toponym": toponym, "monuments": []})
        bucket["monuments"].append({
            "id": short_id(str(row.inst)),
            "name": fix_mojibake(str(row.name)),
        })

    shared = [g for g in groups.values() if len(g["monuments"]) >= 2]
    for g in shared:
        g["monuments"].sort(key=lambda m: m["name"])
    # luoghi più affollati prima, poi in ordine alfabetico di toponimo.
    shared.sort(key=lambda g: (-len(g["monuments"]), g["toponym"].lower()))

    # Vista a grafo delle triple generate: i monumenti sono i nodi, ogni tripla
    # afi:stessaUbicazioneDi è un arco. Ogni nodo porta il proprio toponimo
    # ("group"), così il frontend può colorare i monumenti dello stesso luogo allo
    # stesso modo (ogni via/piazza diventa un cluster colorato).
    info_by_id = {m["id"]: (m["name"], g["toponym"])
                  for g in shared for m in g["monuments"]}

    nodes = {}
    edges = []
    for s, _, o in constructed:
        for uri in (s, o):
            sid = short_id(str(uri))
            if sid not in nodes and sid in info_by_id:
                name, toponym = info_by_id[sid]
                nodes[sid] = {"id": sid, "label": name, "group": toponym}
        sa, ob = short_id(str(s)), short_id(str(o))
        if sa in nodes and ob in nodes:
            edges.append({"from": sa, "to": ob, "label": "stessa ubicazione"})

    return jsonify({
        "groupCount": len(shared),
        "monumentCount": sum(len(g["monuments"]) for g in shared),
        "groups": shared,
        "graph": {"nodes": list(nodes.values()), "edges": edges},
    })


# ─────────────────────────────────────────────────────────────────────
# ASK — completezza di una proprietà su tutti i monumenti
# ─────────────────────────────────────────────────────────────────────
# Whitelist proprietà -> (IRI, descrizione leggibile). La proprietà da
# verificare arriva come termine RDF via initBindings (?prop): nessuna
# interpolazione di stringa nella query.
COMPLETENESS_PROPERTIES = {
    "coordinate": (AFI.haCoordinate, "le coordinate geografiche"),
    "indirizzo": (AFI.hasAddress, "un indirizzo"),
    "descrizione": (ARCO.description, "una descrizione"),
    "accessibilita": (AC.hasAccessCondition, "una condizione di accesso"),
    "contatti": (AFI.haContatti, "almeno un contatto"),
}

# ASK booleana: "esiste un monumento PRIVO della proprietà ?prop?".
# FILTER NOT EXISTS interroga di proposito l'assenza della tripla (Open World
# Assumption: l'assenza va cercata esplicitamente, non è un "falso" implicito).
# Se la ASK è true il dataset è incompleto per quella proprietà; se è false
# tutti i monumenti la possiedono.
# QUERY: completezza-ask
COMPLETENESS_ASK = PREFIXES + """
ASK {
    ?inst a cis:CulturalInstituteOrSite .
    FILTER NOT EXISTS { ?inst ?prop ?value }
}
"""


@app.route("/api/ask-completeness")
def ask_completeness():
    """Risponde con un booleano (query ASK) alla domanda «esiste almeno un
    monumento privo della proprietà scelta?». `property` è una delle chiavi in
    COMPLETENESS_PROPERTIES."""
    prop_key = request.args.get("property", "")
    if prop_key not in COMPLETENESS_PROPERTIES:
        return jsonify({"error": "Proprietà non valida."}), 400

    prop_uri, prop_desc = COMPLETENESS_PROPERTIES[prop_key]
    exists_missing = bool(
        graph.query(COMPLETENESS_ASK, initBindings={"prop": prop_uri}).askAnswer
    )

    return jsonify({
        "property": prop_key,
        "description": prop_desc,
        # risposta grezza della ASK: True = esiste un monumento senza la proprietà
        "existsMissing": exists_missing,
        # interpretazione: il dataset è completo se NON esiste alcun mancante
        "complete": not exists_missing,
    })


# ─────────────────────────────────────────────────────────────────────
# DESCRIBE — arricchimento via web (Linked Data) con DBpedia + schema.org
# ─────────────────────────────────────────────────────────────────────
# Idea: partendo da una risorsa della NOSTRA base di conoscenza (es. la
# Biblioteca Medicea Laurenziana), si va sul web a recuperare le risorse ad
# essa collegate e si costruisce un NUOVO grafo. Lo si fa eseguendo una vera
# query DESCRIBE su un endpoint SPARQL pubblico — DBpedia — che descrive le
# risorse anche con il vocabolario schema.org. schema.org è la chiave per
# trovare "altre risorse": tipizza l'entità (schema:Library, schema:Organization)
# e, con schema:sameAs/owl:sameAs, la collega ad altre risorse del web
# (Wikidata, VIAF, le altre DBpedia di lingua…).
DBPEDIA_SPARQL = "https://dbpedia.org/sparql"
DBPEDIA_HEADERS = {
    "User-Agent": "ArchitettureFirenzeWebApp/1.0 (educational project; contact: example@example.com)"
}
ABSTRACT_LIMIT = 400

# Predicati della DESCRIBE remota da includere nel nuovo grafo: (gruppo, tetto
# massimo di oggetti, qname per l'etichetta dell'arco). Il tetto evita di
# trascinare le decine di wikiPageWikiLink: il grafo deve restare leggibile.
# rdf:type è incluso a parte, solo per i tipi schema.org.
# Si includono solo risorse che parlano DEL monumento (pagina Wikipedia, sito
# ufficiale, immagini, categorie): NON i sameAs verso altre knowledge base
# (Wikidata, VIAF, GND, Freebase…), che porterebbero fuori dall'input di partenza.
RELATED_PREDICATES = {
    FOAF.isPrimaryTopicOf: ("wikipedia", 2, "foaf:isPrimaryTopicOf"),
    FOAF.homepage: ("website", 2, "foaf:homepage"),
    DBP.website: ("website", 2, "dbp:website"),
    FOAF.depiction: ("image", 3, "foaf:depiction"),
    DBO.thumbnail: ("image", 1, "dbo:thumbnail"),
    DCT.subject: ("category", 6, "dct:subject"),
}

# gruppo di risorsa collegata -> "kind" del nodo (per il colore nel grafo)
RELATED_GROUP_KIND = {
    "wikipedia": "Wikipedia",
    "website": "Website",
    "image": "Image",
    "category": "Category",
}

# Astrazione concettuale: quanti concetti generali ricavare dai tipi dell'entità
# e quanti concetti correlati pescare dal concetto generale principale. Tetti
# bassi (LIMIT) di proposito, così i concetti associati restano vicini alla
# risorsa di partenza e non divaghino.
GENERAL_CONCEPT_LIMIT = 3
RELATED_CONCEPT_LIMIT = 4


DBPEDIA_LOOKUP = "https://lookup.dbpedia.org/api/search"

# Parole troppo comuni per identificare il monumento giusto in una ricerca
# fuzzy: articoli/preposizioni e nomi generici di tipologia. Vengono escluse
# dai "token distintivi" usati per validare i candidati del lookup.
_NAME_STOPWORDS = {"di", "del", "dei", "della", "delle", "degli", "il", "lo",
                   "la", "le", "i", "gli", "e", "a", "al", "alla", "o", "da",
                   "in", "con", "san", "santa", "santo", "santi", "ss"}
_NAME_GENERIC = {"palazzo", "piazza", "piazzale", "via", "viale", "ponte",
                 "chiesa", "cimitero", "forte", "museo", "loggia", "monumento",
                 "monumentale", "murale", "lapide", "casa", "baluardo",
                 "complesso", "giardino", "torre", "porta", "villa", "stadio",
                 "stazione", "tabernacolo"}


def _dbpedia_get(sparql, accept):
    return requests.get(
        DBPEDIA_SPARQL,
        params={"query": sparql, "format": accept},
        headers=DBPEDIA_HEADERS,
        timeout=20,
    )


def _name_tokens(text):
    return [t for t in re.split(r"[^0-9a-zà-ÿ]+", text.lower()) if t]


def _distinctive_tokens(text):
    """Token "forti" del nome (lunghi, non generici): identificano il monumento."""
    return {t for t in _name_tokens(text)
            if len(t) >= 4 and t not in _NAME_STOPWORDS and t not in _NAME_GENERIC}


def _clean_name(name):
    """Toglie i suffissi esplicativi (dopo ' - ' o ':') e le virgolette, che
    confondono la ricerca fuzzy (es. 'Palazzo Vecchio - Quartieri Monumentali'
    -> 'Palazzo Vecchio')."""
    base = re.split(r"\s[-–]\s|:", name)[0]
    return base.replace('"', "").replace("“", "").replace("”", "").strip()


def _exact_label_candidates(name):
    """Candidati per match esatto di rdfs:label (italiano, poi inglese), limitati
    al dominio dbpedia.org."""
    esc = name.replace("\\", "\\\\").replace('"', '\\"')
    uris = []
    for lang in ("it", "en"):
        query = ('SELECT ?s WHERE { ?s rdfs:label "%s"@%s . '
                 'FILTER(STRSTARTS(STR(?s), "http://dbpedia.org/resource/")) } LIMIT 1'
                 % (esc, lang))
        try:
            resp = _dbpedia_get(query, "application/sparql-results+json")
            resp.raise_for_status()
            bindings = resp.json().get("results", {}).get("bindings", [])
            if bindings:
                uris.append(bindings[0]["s"]["value"])
        except (requests.RequestException, ValueError, KeyError):
            continue
    return uris


def _dbpedia_lookup(query, max_results=3):
    """Ricerca fuzzy con l'API DBpedia Lookup; restituisce gli IRI candidati."""
    try:
        resp = requests.get(
            DBPEDIA_LOOKUP,
            params={"query": query, "maxResults": max_results, "format": "json"},
            headers={**DBPEDIA_HEADERS, "Accept": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        uris = []
        for doc in resp.json().get("docs", []):
            res = doc.get("resource")
            if isinstance(res, list):
                res = res[0] if res else None
            if res:
                uris.append(res)
        return uris
    except (requests.RequestException, ValueError):
        return []


def _is_florence_related(resource_uri):
    """Verifica con una ASK remota che la risorsa sia davvero collegata a Firenze
    (link a dbr:Florence, oppure 'Florence'/'Firenze' nei suoi letterali o negli
    IRI collegati, es. le categorie). È il filtro che scarta gli omonimi non
    fiorentini (la Piazza San Marco di Venezia, l'Aston Villa, …)."""
    query = ('ASK { <%s> ?p ?o . FILTER( '
             '?o IN (<http://dbpedia.org/resource/Florence>, '
             '<http://dbpedia.org/resource/Metropolitan_City_of_Florence>, '
             '<http://dbpedia.org/resource/Province_of_Florence>) '
             '|| (isLiteral(?o) && (CONTAINS(STR(?o), "Florence") || CONTAINS(STR(?o), "Firenze"))) '
             '|| (isIRI(?o) && CONTAINS(STR(?o), "Florence")) ) }' % resource_uri)
    try:
        resp = _dbpedia_get(query, "application/sparql-results+json")
        resp.raise_for_status()
        return bool(resp.json().get("boolean"))
    except (requests.RequestException, ValueError):
        return False


def find_dbpedia_resource(name):
    """Risolve un monumento qualsiasi alla sua risorsa DBpedia *fiorentina*.
    Raccoglie i candidati — match esatto per label (it/en) e, in fallback,
    DBpedia Lookup fuzzy sul nome ripulito — e restituisce il primo che risulta
    collegato a Firenze (verifica con _is_florence_related). I candidati fuzzy
    devono inoltre condividere un token distintivo col nome, per escludere match
    assurdi (es. 'Villa Favard' -> 'Aston Villa'). Restituisce l'IRI o None."""
    seen = set()

    # 1) match esatto (alta precisione) — ma va comunque verificato per le
    #    omonimie con altre città.
    for uri in _exact_label_candidates(name):
        if uri not in seen:
            seen.add(uri)
            if _is_florence_related(uri):
                return uri

    # 2) fallback fuzzy: lookup sul nome ripulito, filtro per token distintivo,
    #    poi verifica Firenze.
    cleaned = _clean_name(name)
    distinctive = _distinctive_tokens(cleaned)
    for uri in _dbpedia_lookup(cleaned):
        if uri in seen:
            continue
        seen.add(uri)
        cand_tokens = set(_name_tokens(unquote(uri.rsplit("/", 1)[-1])))
        if distinctive and not (distinctive & cand_tokens):
            continue
        if _is_florence_related(uri):
            return uri

    return None


def fetch_dbpedia_describe(resource_uri):
    """Esegue DESCRIBE <resource> sull'endpoint DBpedia e ritorna il grafo RDF
    risultante (o None in caso di errore di rete/parsing)."""
    try:
        resp = _dbpedia_get(f"DESCRIBE <{resource_uri}>", "text/turtle")
        resp.raise_for_status()
        external = Graph()
        external.parse(data=resp.text, format="turtle")
        return external
    except (requests.RequestException, Exception):  # parsing incluso
        return None


def _dbpedia_select(query):
    """Esegue una SELECT su DBpedia e ritorna le bindings (lista di dict), [] su errore."""
    try:
        resp = _dbpedia_get(query, "application/sparql-results+json")
        resp.raise_for_status()
        return resp.json().get("results", {}).get("bindings", [])
    except (requests.RequestException, ValueError):
        return []


# Concetti troppo generici per essere un'astrazione utile: una biblioteca "è un
# Agente / una Organizzazione" non aggiunge nulla di tematico, quindi si scartano.
GENERIC_CONCEPTS = {"Agent", "Organisation", "Organization", "Person",
                    "Place", "Location", "Thing", "Entity"}
# Concetti corretti ma ampi: restano come nodi, ma per l'espansione nei concetti
# correlati si preferisce un concetto più specifico (es. Library prima di Building).
BROAD_CONCEPTS = {"Building", "Structure", "ArchitecturalStructure", "Work"}


def fetch_general_concepts(resource_uri, limit=GENERAL_CONCEPT_LIMIT):
    """Astrae la risorsa specifica ai suoi concetti GENERALI: per ogni tipo
    dell'ontologia DBpedia dell'entità (dbo:Library, dbo:Building…) costruisce la
    risorsa omonima (dbo:Library -> dbr:Library, cioè "la biblioteca in generale"),
    tenendo solo quelle che esistono davvero come risorsa con etichetta inglese.

    Condizione di non-divagazione: i concetti sono per costruzione i *tipi* della
    risorsa principale (non possono allontanarsi dal tema), meno una stoplist di
    concetti troppo generici (Agent, Organisation…). I concetti più specifici sono
    messi in testa, così l'espansione successiva parte dal più pertinente. LIMIT.
    """
    query = (
        'SELECT DISTINCT ?concept ?label WHERE { '
        '<%(res)s> rdf:type ?cls . '
        'FILTER(STRSTARTS(STR(?cls), "http://dbpedia.org/ontology/")) '
        'BIND(IRI(CONCAT("http://dbpedia.org/resource/", '
        'STRAFTER(STR(?cls), "http://dbpedia.org/ontology/"))) AS ?concept) '
        '?concept rdfs:label ?label . '
        'FILTER(LANG(?label) = "en") '
        '} LIMIT 10' % {"res": resource_uri}
    )
    concepts = []
    for b in _dbpedia_select(query):
        uri = b["concept"]["value"]
        local = uri.rsplit("/", 1)[-1]
        if local in GENERIC_CONCEPTS:
            continue
        concepts.append((uri, b["label"]["value"], local in BROAD_CONCEPTS))
    # i concetti specifici (is_broad=False) ordinano prima di quelli ampi.
    concepts.sort(key=lambda c: c[2])
    return [(uri, label) for uri, label, _ in concepts[:limit]]


def fetch_related_concepts(concept_uri, limit=RELATED_CONCEPT_LIMIT):
    """Dalla risorsa-concetto generale (es. dbr:Library) ricava i concetti che la
    compongono o le sono fortemente legati (es. dbr:Book). Condizione di
    correlazione: si tengono solo i collegamenti *reciproci* — A wikilink B **e**
    B wikilink A — indice di forte vicinanza concettuale, con LIMIT perché restino
    concetti adiacenti e non una catena che si allontana dalla risorsa principale.
    """
    query = (
        'SELECT DISTINCT ?r ?label WHERE { '
        '<%(c)s> dbo:wikiPageWikiLink ?r . '
        '?r dbo:wikiPageWikiLink <%(c)s> . '
        '?r rdfs:label ?label . '
        'FILTER(LANG(?label) = "en") '
        'FILTER(STRSTARTS(STR(?r), "http://dbpedia.org/resource/")) '
        'FILTER(!CONTAINS(STR(?r), "Category:")) '
        '} LIMIT %(limit)d' % {"c": concept_uri, "limit": limit}
    )
    return [(b["r"]["value"], b["label"]["value"]) for b in _dbpedia_select(query)]


def readable_label(uri):
    """Etichetta leggibile per una risorsa esterna: ultimo segmento dell'IRI,
    de-quotato e con gli underscore trasformati in spazi (es.
    .../resource/Laurentian_Library -> 'Laurentian Library')."""
    tail = uri.rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    return unquote(tail).replace("_", " ") or uri


# Host del tipo "xx.dbpedia.org" / "xx.wikipedia.org": edizioni in lingua.
_LANG_EDITION_HOST = re.compile(r"^([a-z]{2})\.(dbpedia\.org|wikipedia\.org)$")


def _is_allowed_language_resource(uri):
    """True se la risorsa NON è un'edizione in una lingua diversa da italiano o
    inglese. Scarta le DBpedia/Wikipedia di altre lingue (es. de.dbpedia.org,
    zh.wikipedia.org) ma tiene quelle it/en, la DBpedia inglese canonica
    (dbpedia.org, senza prefisso di lingua) e le fonti non legate a una lingua
    (Wikidata, VIAF, GND…)."""
    host = urlparse(uri).netloc.lower()
    match = _LANG_EDITION_HOST.match(host)
    if match:
        return match.group(1) in ("it", "en")
    return True


@app.route("/api/monuments/<inst_id>/describe")
def monument_describe(inst_id):
    """Arricchimento via web: trova su DBpedia la risorsa corrispondente al
    monumento, esegue lì una query DESCRIBE e costruisce un nuovo grafo con le
    risorse collegate al concetto di partenza, sfruttando schema.org per la
    classificazione e i collegamenti. Restituisce il grafo in Turtle più una
    vista strutturata (tipi schema.org + risorse collegate per categoria)."""
    require_valid_id(inst_id)
    inst_uri = URIRef(resolve_institute_uri(inst_id))

    name = graph.value(inst_uri, L0.name)
    name = fix_mojibake(str(name)) if name else short_id(str(inst_uri))

    resource = find_dbpedia_resource(name)
    if resource is None:
        return jsonify({"id": inst_id, "name": name, "found": False})

    external = fetch_dbpedia_describe(resource)
    if external is None:
        return jsonify({"id": inst_id, "name": name, "found": False,
                        "error": "Risorsa trovata ma DESCRIBE remota non riuscita."})

    res_ref = URIRef(resource)

    # ── Costruzione del NUOVO grafo ──────────────────────────────────
    result = Graph()
    for pfx, ns in [("afi", AFI), ("schema", SCHEMA), ("dbo", DBO), ("dbp", DBP),
                    ("dbr", DBR), ("foaf", FOAF), ("dct", DCT),
                    ("rdfs", RDFS), ("rdf", RDF), ("owl", OWL)]:
        # replace=True: forza il prefisso anche se rdflib ne ha già uno di default
        # (es. "schema" -> https://schema.org/), così il Turtle resta leggibile.
        result.bind(pfx, ns, replace=True)

    # 1) il ponte: la nostra risorsa è la stessa cosa di quella su DBpedia.
    result.add((inst_uri, OWL.sameAs, res_ref))

    # In parallelo al grafo RDF si costruisce la vista a nodi/archi (nodes/edges)
    # che il frontend disegna con sigma/graphology, come "Grafico dell'ontologia".
    nodes = {}
    edges = []

    def ensure_node(node_id, label, kind):
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "label": short_label(label), "kind": kind}

    # nodo centrale = il nostro monumento; ad esso si lega la risorsa DBpedia.
    ensure_node(str(inst_uri), name, "Monument")
    ensure_node(resource, readable_label(resource), "DBpedia")
    edges.append({"from": str(inst_uri), "to": resource, "label": "owl:sameAs"})

    # 2) classificazione schema.org dell'entità (i "tipi" che la qualificano).
    schema_types = []
    for o in external.objects(res_ref, RDF.type):
        if str(o).startswith(str(SCHEMA)):
            result.add((res_ref, RDF.type, o))
            qname = str(o).replace(str(SCHEMA), "schema:")
            schema_types.append(qname)
            ensure_node(str(o), qname, "SchemaType")
            edges.append({"from": resource, "to": str(o), "label": "rdf:type"})

    # 3) etichetta e abstract (it/en) per contesto, abstract troncato.
    for lang in ("it", "en"):
        for o in external.objects(res_ref, RDFS.label):
            if isinstance(o, Literal) and o.language == lang:
                result.add((res_ref, RDFS.label, o))
                break
    for lang in ("it", "en"):
        for o in external.objects(res_ref, DBO.abstract):
            if isinstance(o, Literal) and o.language == lang:
                text = str(o)
                if len(text) > ABSTRACT_LIMIT:
                    text = text[:ABSTRACT_LIMIT].rstrip() + "…"
                result.add((res_ref, DBO.abstract, Literal(text, lang=lang)))
                break

    # 4) risorse collegate, raggruppate per categoria e con tetto per predicato.
    related = {"wikipedia": [], "website": [], "image": [], "category": []}
    seen = set()
    for pred, (group, cap, pqname) in RELATED_PREDICATES.items():
        count = 0
        for o in external.objects(res_ref, pred):
            if not isinstance(o, URIRef) or count >= cap:
                continue
            # solo fonti in italiano/inglese (o non legate a una lingua):
            # niente DBpedia/Wikipedia di altre lingue.
            if not _is_allowed_language_resource(str(o)):
                continue
            key = (group, str(o))
            if key in seen:
                continue
            seen.add(key)
            result.add((res_ref, pred, o))
            label = readable_label(str(o))
            related[group].append({
                "uri": str(o),
                "label": label,
                "source": urlparse(str(o)).netloc,
            })
            ensure_node(str(o), label, RELATED_GROUP_KIND[group])
            edges.append({"from": resource, "to": str(o), "label": pqname})
            count += 1

    # 5) astrazione concettuale: non ci si ferma all'entità specifica, si risale ai
    #    concetti GENERALI che la inquadrano (una biblioteca -> il concetto
    #    "Library") e da quello ai concetti CORRELATI che la compongono
    #    ("Book"). Reciprocità dei wikilink + LIMIT (vedi le funzioni) tengono i
    #    concetti vicini alla risorsa principale, senza divagare.
    related["concept"] = []
    general = fetch_general_concepts(resource)
    for concept_uri, concept_label in general:
        if concept_uri == resource:
            continue
        result.add((res_ref, AFI.concettoGenerale, URIRef(concept_uri)))
        ensure_node(concept_uri, concept_label, "Concept")
        edges.append({"from": resource, "to": concept_uri, "label": "afi:concettoGenerale"})
        related["concept"].append({"uri": concept_uri, "label": concept_label,
                                   "source": urlparse(concept_uri).netloc})

    # Solo il concetto generale più rilevante viene espanso nei suoi correlati,
    # per non moltiplicare le chiamate remote (e restare aderenti al tema).
    if general:
        top_uri = general[0][0]
        for rel_uri, rel_label in fetch_related_concepts(top_uri):
            if rel_uri in (resource, top_uri):
                continue
            result.add((URIRef(top_uri), AFI.concettoCorrelato, URIRef(rel_uri)))
            ensure_node(rel_uri, rel_label, "Concept")
            edges.append({"from": top_uri, "to": rel_uri, "label": "afi:concettoCorrelato"})
            related["concept"].append({"uri": rel_uri, "label": rel_label,
                                       "source": urlparse(rel_uri).netloc})

    return jsonify({
        "id": inst_id,
        "name": name,
        "found": True,
        "resource": resource,
        "resourceLabel": readable_label(resource),
        "schemaTypes": schema_types,
        "related": related,
        "tripleCount": len(result),
        "turtle": result.serialize(format="turtle"),
        "graph": {"nodes": list(nodes.values()), "edges": edges},
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
