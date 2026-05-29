"""Utilità condivise: rilevamento formato, nomi locali, similarità semantica."""

from pathlib import Path

import numpy as np

from rdflib import RDF, XSD

try:
    from sentence_transformers import SentenceTransformer
    HAS_SBERT = True
except ImportError:
    HAS_SBERT = False


# ── Funzioni di base ────────────────────────────────────────────

def detect_format(fp):
    ext = fp.rsplit(".", 1)[-1].lower()
    return {"ttl":"turtle","turtle":"turtle","rdf":"xml","owl":"xml","xml":"xml",
            "nt":"nt","ntriples":"nt","jsonld":"json-ld","json":"json-ld","n3":"n3"
            }.get(ext, "turtle")


def local_name(uri):
    s = str(uri)
    for sep in ("#", "/"):
        if sep in s: return s.rsplit(sep, 1)[-1]
    return s


def xsd_of(lit):
    if lit.datatype: return lit.datatype
    if lit.language: return RDF.langString
    return XSD.string


def auto_output(input_path: str, merge: bool) -> str:
    stem = Path(input_path).stem
    suffix = "_merged.ttl" if merge else "_tbox.ttl"
    return str(Path(input_path).parent / (stem + suffix))


def auto_title(input_path: str) -> str:
    stem = Path(input_path).stem.replace("_", " ").replace("-", " ").title()
    return f"Ontologia {stem}"


# ── Similarità semantica (Sentence Transformers) ────────────────

_embed_model = None


def get_embed_model():
    global _embed_model
    if _embed_model is None:
        if not HAS_SBERT:
            raise ImportError(
                "sentence-transformers non installato. "
                "Installa con: pip install sentence-transformers")
        _embed_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _embed_model


def cosine_sim(a: str, b: str) -> float:
    model = get_embed_model()
    vecs = model.encode([a, b])
    return float(np.dot(vecs[0], vecs[1]) /
                 (np.linalg.norm(vecs[0]) * np.linalg.norm(vecs[1])))
