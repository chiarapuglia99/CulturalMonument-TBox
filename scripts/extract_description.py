#!/usr/bin/env python3
"""
Per gli individui AccessCondition: la descrizione è testo libero molto lungo,
quindi invece di usarla come label si estrae una PAROLA CHIAVE di sintesi
(livello di accessibilità) tramite LLM, vincolato a un insieme chiuso di valori.

Usa un modello Ollama (default: deepseek-v4-pro:cloud).
Il testo originale resta in ns5:description; cambia solo rdfs:label.

Uso:
  python extract_description.py input.ttl output.ttl
Variabili d'ambiente opzionali:
  OLLAMA_HOST       (default https://ollama.com)
  OLLAMA_MODEL      (default deepseek-v4-pro:cloud)
  OLLAMA_KEY_FILE   percorso esplicito al file con la chiave (sovrascrive la ricerca)

La chiave viene letta dal file '.key_ollama' (cercato in: OLLAMA_KEY_FILE,
./.venv/.key_ollama) e inviata nell'header Authorization: Bearer <chiave>.
Se non si trova nessun file, lo script procede senza header.
"""
import sys, os, json, urllib.request, urllib.error
from pathlib import Path
from rdflib import Graph, Namespace, RDF, RDFS, Literal

NS6 = Namespace("https://w3id.org/italia/onto/AccessCondition/")
NS5 = Namespace("https://w3id.org/italia/onto/l0/")

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "https://ollama.com")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "deepseek-v4-pro:cloud")

def load_key():
    candidates = []
    env = os.environ.get("OLLAMA_KEY_FILE")
    if env:
        candidates.append(Path(env))
    candidates += [
        Path(".venv") / ".key_ollama",   # cerca SOLO qui
    ]
    for p in candidates:
        try:
            if p.is_file():
                key = p.read_text(encoding="utf-8").strip()
                if key:
                    print(f"[chiave letta da {p}]")
                    return key
        except OSError:
            continue
    print("[nessun file .key_ollama trovato: procedo senza autenticazione]")
    return None

OLLAMA_KEY = load_key()
print(f"[host: {OLLAMA_HOST} | modello: {OLLAMA_MODEL}]")

CATEGORIES = [
    "Totalmente accessibile",
    "Parzialmente accessibile",
    "Non accessibile",
    "Accessibilità da verificare",
]

SYSTEM = (
    "Sei un classificatore di testi sull'accessibilità di luoghi culturali. "
    "Leggi la descrizione e rispondi con UNA SOLA delle seguenti etichette, "
    "esattamente come scritta, senza altro testo:\n"
    + "\n".join("- " + c for c in CATEGORIES) +
    "\nScegli 'Parzialmente accessibile' quando l'accesso è garantito solo "
    "ad alcune aree, con accompagnatore, o con eccezioni."
)

def classify(text):
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": text[:4000]},
        ],
    }
    headers = {"content-type": "application/json"}
    if OLLAMA_KEY:
        headers["Authorization"] = "Bearer " + OLLAMA_KEY
    req = urllib.request.Request(
        OLLAMA_HOST.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"\n[ERRORE HTTP {e.code}] {e.reason}")
        print(f"[URL chiamato] {req.full_url}")
        print(f"[lunghezza chiave inviata] {len(OLLAMA_KEY) if OLLAMA_KEY else 0} caratteri")
        print(f"[risposta server] {body}\n")
        raise SystemExit(1)
    out = data.get("message", {}).get("content", "").strip()
    # normalizza al set chiuso (i casi negativi/parziali prima del generico)
    low = out.lower()
    for c in ["Non accessibile", "Parzialmente accessibile",
              "Totalmente accessibile", "Accessibilità da verificare"]:
        if c.lower() in low:
            return c
    return "Accessibilità da verificare"

def main(inp, outp):
    g = Graph(); g.parse(inp, format="turtle")
    n = 0
    for s in g.subjects(RDF.type, NS6.AccessCondition):
        d = g.value(s, NS5.description)
        if d is None:
            continue
        kw = classify(str(d))
        # risali al monumento che riferisce questa AccessCondition
        nome_monumento = None
        for m in g.subjects(NS6.hasAccessCondition, s):
            nome_monumento = g.value(m, NS5.name) or g.value(m, RDFS.label)
            if nome_monumento:
                break
        if nome_monumento:
            label = f"{nome_monumento} - {kw}"
        else:
            label = kw
        for old in list(g.objects(s, RDFS.label)):
            g.remove((s, RDFS.label, old))
        g.add((s, RDFS.label, Literal(label, lang="it")))
        n += 1
        print(f"  {label}")
    g.serialize(destination=outp, format="turtle")
    print(f"Aggiornate {n} AccessCondition -> {outp}")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "architetture_firenze_fixed.ttl",
         sys.argv[2] if len(sys.argv) > 2 else "architetture_firenze_fixed.ttl")