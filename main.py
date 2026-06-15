"""
Punto di ingresso principale della pipeline ABox → TBox.

Uso:
    python main.py <input.ttl> --output <output.ttl>

Esempio:
    python main.py architettura.ttl --output architetture_firenze_v4.ttl
    python main.py librerie.ttl --output librerie_v2.ttl

I flag --merge --dedup --fix-labels --extract-desc sono attivi per default.
Puoi sovrascriverli aggiungendoli o usando --no-llm, --no-merge, ecc.
"""

import sys
import os

if len(sys.argv) < 2:
    print("Uso: python main.py <input.ttl> --output <output.ttl>")
    sys.exit(1)

# ── Flag di default ─────────────────────────────────────────────
DEFAULT_FLAGS = ["--merge", "--flatten", "--dedup", "--fix-labels", "--extract-desc", "--merge-site", "--refine", "--no-report"]
LLM_MODEL     = "gemma4:31b-cloud"
OLLAMA_URL    = "http://localhost:11434"

os.environ.setdefault("OLLAMA_HOST",        OLLAMA_URL)
os.environ.setdefault("OLLAMA_MODEL",       LLM_MODEL)
os.environ.setdefault("PYTHONIOENCODING",   "utf-8")

# Prendi gli argomenti passati dall'utente e aggiungi i default mancanti
user_args = sys.argv[1:]
for flag in DEFAULT_FLAGS:
    if flag not in user_args:
        user_args.append(flag)

if "--llm-model" not in user_args:
    user_args += ["--llm-model", LLM_MODEL]
if "--ollama-url" not in user_args:
    user_args += ["--ollama-url", OLLAMA_URL]

sys.argv = ["main.py"] + user_args

from scripts.__main__ import main
main()
