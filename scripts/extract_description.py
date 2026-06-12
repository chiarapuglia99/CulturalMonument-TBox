"""
Classifica individui con descrizioni testuali tramite LLM.

Priorità configurazione (dal più specifico al più generico):
  1. Argomenti CLI (--class, --property, --values)
  2. postprocess_config.json nella dir del TTL o CWD
  3. Auto-rilevamento: classe con la proprietà testuale più lunga

Uso:
  python extract_description.py input.ttl [output.ttl]
  python extract_description.py input.ttl output.ttl --class <URI> --property <URI>
  python extract_description.py input.ttl output.ttl --values "Val1,Val2,Val3"

Variabili d'ambiente:
  OLLAMA_HOST   (default: http://localhost:11434)
  OLLAMA_MODEL  (default: gemma4:31b-cloud)
  OLLAMA_KEY_FILE  percorso esplicito alla chiave
"""

import sys, os, json, argparse, urllib.request, urllib.error
from pathlib import Path
from rdflib import Graph, Namespace, RDF, RDFS, Literal, URIRef

_META = {
    "http://www.w3.org/2002/07/owl#NamedIndividual",
    "http://www.w3.org/2002/07/owl#Class",
    "http://www.w3.org/2002/07/owl#Ontology",
}

OLLAMA_HOST  = os.environ.get("OLLAMA_HOST",  "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:31b-cloud")


# ── Chiave API ──────────────────────────────────────────────────

def _load_key():
    candidates = []
    env = os.environ.get("OLLAMA_KEY_FILE")
    if env:
        candidates.append(Path(env))
    candidates += [Path(".venv/.key_ollama"), Path(".key_ollama")]
    for p in candidates:
        try:
            if p.is_file():
                key = p.read_text(encoding="utf-8").strip()
                if key:
                    return key
        except OSError:
            pass
    print("[nessun file .key_ollama trovato: procedo senza autenticazione]")
    return None


# ── Config ──────────────────────────────────────────────────────

def _load_config(ttl_path):
    for candidate in [Path(ttl_path).parent / "postprocess_config.json",
                      Path("postprocess_config.json")]:
        if candidate.is_file():
            with open(candidate, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("extract_desc", {})
    return {}


# ── LLM ─────────────────────────────────────────────────────────

def _call_llm(prompt, key):
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0},
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    url = OLLAMA_HOST.rstrip("/") + "/api/chat"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.loads(r.read().decode("utf-8"))
        return body.get("message", {}).get("content", "").strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"[ERRORE HTTP {e.code}] {e.reason}\n[risposta server] {body}")


def _build_prompt(text, allowed_values):
    if allowed_values:
        vals_str = ", ".join(f'"{v}"' for v in allowed_values)
        return (f"Classifica il testo seguente scegliendo UNA categoria tra: {vals_str}.\n"
                f"Rispondi SOLO con la categoria, senza spiegazioni.\n\nTesto:\n{text[:800]}")
    return (f"Riassumi il testo seguente in 3-6 parole chiave separate da virgola.\n"
            f"Rispondi SOLO con le parole chiave.\n\nTesto:\n{text[:800]}")


# ── Auto-rilevamento ─────────────────────────────────────────────

def _auto_detect(g):
    """Trova la classe la cui proprietà testuale ha la lunghezza media più alta."""
    cls_prop_len = defaultdict(lambda: defaultdict(list))
    skip = {str(RDF.type), str(RDFS.label)}
    for s in g.subjects(RDF.type, None):
        if not isinstance(s, URIRef):
            continue
        types = [o for o in g.objects(s, RDF.type)
                 if isinstance(o, URIRef) and str(o) not in _META]
        if not types:
            continue
        cls = types[0]
        for p, o in g.predicate_objects(s):
            if str(p) in skip or not isinstance(o, Literal):
                continue
            cls_prop_len[cls][p].append(len(str(o)))

    best_cls, best_prop, best_avg = None, None, 0
    for cls, props in cls_prop_len.items():
        for prop, lengths in props.items():
            avg = sum(lengths) / len(lengths)
            if avg > best_avg:
                best_avg, best_cls, best_prop = avg, cls, prop
    return best_cls, best_prop


# ── Entry point ─────────────────────────────────────────────────

def main(inp, outp, target_class=None, text_prop=None, allowed_values=None):
    g = Graph()
    g.parse(inp, format="turtle")
    print(f"[host: {OLLAMA_HOST} | modello: {OLLAMA_MODEL}]")

    # Risolvi parametri: CLI > config (solo se la classe esiste nel grafo) > auto
    if target_class is None or text_prop is None:
        cfg = _load_config(inp)
        if cfg:
            cfg_cls  = URIRef(cfg["class"])    if "class"    in cfg else None
            cfg_prop = URIRef(cfg["property"])  if "property" in cfg else None
            # Usa la config solo se la classe configurata ha effettivamente istanze
            if cfg_cls and any(True for _ in g.subjects(RDF.type, cfg_cls)):
                target_class   = target_class   or cfg_cls
                text_prop      = text_prop      or cfg_prop
                allowed_values = allowed_values or cfg.get("allowed_values")

    if target_class is None or text_prop is None:
        print("[extract_desc] Nessuna classe configurata per questo file — skip.")
        print("               Usa --class e --property per specificare la classe target,")
        print("               oppure aggiungi 'extract_desc' a postprocess_config.json.")
        return

    key = _load_key()

    # Passo 1: classifica ogni individuo tramite LLM
    classification = {}  # URIRef -> str classificazione
    for s in list(g.subjects(RDF.type, target_class)):
        if not isinstance(s, URIRef):
            continue
        texts = list(g.objects(s, text_prop))
        if not texts:
            continue
        prompt = _build_prompt(str(texts[0]), allowed_values)
        try:
            result = _call_llm(prompt, key).strip().strip('"').strip("'")
        except RuntimeError as e:
            print(e, file=sys.stderr)
            sys.exit(1)
        classification[s] = result
        print(f"  {s.split('/')[-1]} → {result}")

    cls_name = str(target_class).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    if not classification:
        print(f"[extract_desc] Nessun individuo '{cls_name}' trovato nel file — step saltato.")
        return

    # Passo 2: raggruppa per valore di classificazione
    groups: dict[str, list] = {}
    for ind, val in classification.items():
        groups.setdefault(val, []).append(ind)

    # Passo 3: per ogni gruppo mantieni un canonico con label pulita;
    #          copia le descrizioni dei duplicati sul canonico, poi rimuovili
    for cls_val, individuals in groups.items():
        canonical = individuals[0]

        for lbl in list(g.objects(canonical, RDFS.label)):
            g.remove((canonical, RDFS.label, lbl))
        g.add((canonical, RDFS.label, Literal(cls_val)))

        existing_descs = {str(o) for o in g.objects(canonical, text_prop)}

        for dup in individuals[1:]:
            # Copia le descrizioni uniche del duplicato sul canonico
            for desc in g.objects(dup, text_prop):
                if str(desc) not in existing_descs:
                    g.add((canonical, text_prop, desc))
                    existing_descs.add(str(desc))

            # Redirige relazioni in ingresso (es. hasAccessCondition) al canonico
            for subj, pred in list(g.subject_predicates(dup)):
                g.remove((subj, pred, dup))
                g.add((subj, pred, canonical))

            # Rimuove tutti i triple con il duplicato come soggetto
            for pred, obj in list(g.predicate_objects(dup)):
                g.remove((dup, pred, obj))

        print(f"  '{cls_val}': {len(individuals)} individui → canonico {canonical.split('/')[-1]}")

    print(f"Classificati {len(classification)} '{cls_name}' → {len(groups)} unici → {outp}")
    g.serialize(destination=outp, format="turtle")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Classifica individui con descrizioni testuali tramite LLM.")
    parser.add_argument("input",    nargs="?", default="architetture_firenze_v3.ttl")
    parser.add_argument("output",   nargs="?", default=None)
    parser.add_argument("--class",    dest="cls",    default=None,
                        help="URI della classe target")
    parser.add_argument("--property", dest="prop",   default=None,
                        help="URI della proprietà testuale")
    parser.add_argument("--values",   dest="values", default=None,
                        help="Valori permessi separati da virgola")
    args = parser.parse_args()

    main(
        args.input,
        args.output or args.input,
        target_class=URIRef(args.cls)  if args.cls    else None,
        text_prop=URIRef(args.prop)    if args.prop   else None,
        allowed_values=[v.strip() for v in args.values.split(",")] if args.values else None,
    )
