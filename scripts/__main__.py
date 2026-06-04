"""Entry point CLI: python -m abox_to_tbox file.ttl"""

import argparse
import sys
import datetime
from pathlib import Path

from rdflib import Graph

from .constants import TAU_DOM
from .utils import detect_format, auto_output, auto_title, local_name
from .analyzer import analyze_abox
from .builder import (detect_equivalent_classes, deduplicate_domains,
                      dedup_restrictions_analysis, build_tbox, build_merged)
from .llm import (check_ollama, enrich_with_llm, apply_llm_enrichments,
                  ask_llm_review_low_confidence, apply_llm_review)
from .metrics import validate_with_reasoner
from .report import print_report
from .architetture_fixed import main as _fix_labels_main
from .extract_description import main as _extract_desc_main


def run_fix_labels(input_path: str, output_path: str | None = None) -> str:
    """Sostituisce i label generici degli individui con i valori reali delle proprietà.

    Restituisce il percorso del file prodotto.
    """
    out = output_path or input_path
    print(f"\n[post] Fix labels: {input_path} -> {out}")
    _fix_labels_main(input_path, out)
    return out


def run_extract_descriptions(input_path: str, output_path: str | None = None) -> str:
    """Classifica le AccessCondition via LLM e aggiorna i loro rdfs:label.

    Restituisce il percorso del file prodotto.
    """
    out = output_path or input_path
    print(f"\n[post] Extract descriptions: {input_path} -> {out}")
    _extract_desc_main(input_path, out)
    return out


def main():
    # Fix UTF-8 su console Windows
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")

    pr = argparse.ArgumentParser(
        description="ABox RDF → TBox OWL  (uso: python -m abox_to_tbox file.ttl)"
    )
    pr.add_argument("input",
                    help="File ABox di input (ttl, rdf, nt, json-ld)")
    pr.add_argument("--format",    "-f", default=None,
                    help="Formato RDF (auto-rilevato dall'estensione)")
    pr.add_argument("--output",    "-o", default=None,
                    help="File di output (default: <input>_tbox.ttl o <input>_merged.ttl)")
    pr.add_argument("--out-format",      default="turtle",
                    help="Formato output: turtle / xml / nt  (default: turtle)")
    pr.add_argument("--merge",           action="store_true", default=False,
                    help="Genera file unico TBox+ABox (individui visibili in Protege)")
    pr.add_argument("--no-merge",        action="store_true",
                    help="Forza solo TBox anche se --merge non specificato")
    pr.add_argument("--no-llm",          action="store_true",
                    help="Salta arricchimento LLM (Ollama)")
    pr.add_argument("--llm-model",       default="gemma4:31b-cloud",
                    help="Modello Ollama (default: gemma4:31b-cloud)")
    pr.add_argument("--ollama-url",      default="http://localhost:11434",
                    help="URL Ollama (default: http://localhost:11434)")
    pr.add_argument("--llm-timeout",     type=int, default=0,
                    help="Timeout LLM in secondi (0 = nessun timeout)")
    pr.add_argument("--dc-title",        default=None)
    pr.add_argument("--dc-creator",      default=None)
    pr.add_argument("--dc-date",         default=None)
    pr.add_argument("--dc-description",  default=None)
    pr.add_argument("--no-report",       action="store_true")
    pr.add_argument("--fix-labels",      action="store_true",
                    help="Esegui architetture_fixed.py sull'output (sostituisce label generici)")
    pr.add_argument("--extract-desc",    action="store_true",
                    help="Esegui extract_description.py sull'output (classifica AccessCondition via LLM)")
    args = pr.parse_args()

    merge = args.merge and not args.no_merge

    output = args.output or auto_output(args.input, merge)

    dc_meta = {
        "title":       args.dc_title       or auto_title(args.input),
        "creator":     args.dc_creator     or "abox_to_tbox.py",
        "date":        args.dc_date        or str(datetime.date.today().year),
        "description": args.dc_description or f"TBox OWL derivata da {Path(args.input).name}",
    }

    # 1. Caricamento ABox
    fmt = args.format or detect_format(args.input)
    print(f"\n[1/5] Caricamento: {args.input}  (formato: {fmt})")
    g = Graph()
    try: g.parse(args.input, format=fmt)
    except Exception as e: print(f"ERRORE: {e}", file=sys.stderr); sys.exit(1)
    print(f"      → {len(g)} triple")

    # 2. Analisi
    print("[2/5] Analisi ABox...")
    analysis = analyze_abox(g)

    equiv_map = detect_equivalent_classes(analysis, g)
    analysis["equiv_classes"] = equiv_map
    if equiv_map:
        print(f"      ✂️  {len(equiv_map)} gruppi di classi equivalenti rilevati")
        for canonical, aliases in equiv_map.items():
            print(f"         {local_name(canonical)} ≡ {[local_name(a) for a in aliases]}")
        alias_set = {a for aliases in equiv_map.values() for a in aliases}
        analysis["classes"] = analysis["classes"] - alias_set
        for prop_dict in [analysis["object_props"], analysis["data_props"]]:
            for info in prop_dict.values():
                info["domains"] = deduplicate_domains(info["domains"], equiv_map)
                info["ranges"]  = deduplicate_domains(info["ranges"],  equiv_map)
        dedup_restrictions_analysis(analysis, equiv_map)

    # 3. LLM enrichment
    llm_used = False
    llm_timeout = None if args.llm_timeout <= 0 else args.llm_timeout
    if not args.no_llm:
        print(f"[3/5] Verifica Ollama ({args.ollama_url}, modello: {args.llm_model})...")
        if check_ollama(args.ollama_url, args.llm_model):
            print(f"      ✅ Ollama disponibile — invio analisi al modello...")
            try:
                enrichments = enrich_with_llm(
                    analysis, args.input, args.ollama_url, args.llm_model,
                    timeout=llm_timeout
                )
                analysis  = apply_llm_enrichments(analysis, enrichments)
                dc_meta   = {**dc_meta, **{k: v for k, v in
                              analysis.get("llm_dc_meta", {}).items() if v}}
                llm_used  = True
                print(f"      ✅ Arricchimento LLM applicato")

                axiom_conf = analysis.get("axiom_confidence", {})
                low_conf_count = sum(
                    1 for _, (_, c) in axiom_conf.items() if c < TAU_DOM)
                if low_conf_count > 0:
                    print(f"      🔍 {low_conf_count} assiomi sotto soglia "
                          f"— revisione LLM...")
                    try:
                        review = ask_llm_review_low_confidence(
                            analysis, axiom_conf,
                            args.ollama_url, args.llm_model,
                            timeout=llm_timeout)
                        if review:
                            analysis = apply_llm_review(analysis, review)
                            n_rejected = sum(
                                1 for d in review.values()
                                if isinstance(d, dict)
                                and d.get("decision") == "REJECT")
                            print(f"      ✅ Review: {n_rejected} "
                                  f"assiomi rimossi")
                    except Exception as e:
                        print(f"      ⚠️  Errore review LLM ({e})")
            except Exception as e:
                print(f"      ⚠️  Errore LLM ({e}) — continuo senza arricchimento")
        else:
            print(f"      ⚠️  Ollama non raggiungibile o modello non trovato — procedo senza LLM")
            print(f"         (usa --no-llm per saltare questo controllo)")
    else:
        print("[3/5] LLM saltato (--no-llm)")

    # Report
    if not args.no_report:
        print_report(analysis, llm_used)

    # 4. Generazione TBox
    print("[4/5] Generazione TBox...")
    tbox = build_tbox(analysis, g, dc_meta)
    print(f"      → {len(tbox)} assiomi TBox")

    if merge:
        print("[4b]  Merge TBox+ABox...")
        out = build_merged(tbox, g, equiv_map=analysis.get('equiv_classes', {}))
        print(f"      → {len(out)} triple totali")
    else:
        out = tbox

    # 5. Salvataggio
    print(f"[5/5] Salvataggio: {output}")
    out.serialize(destination=output, format=args.out_format)
    print(f"      ✅ Fatto!\n")
    if merge:
        print(f"  → Apri {output} direttamente in Protege")
    else:
        print(f"  → In Protege: File > Open  ({output})")
        print(f"     poi File > Import  ({args.input})\n")

    # Validazione con Reasoner
    print("[+] Validazione con reasoner...")
    abs_output = str(Path(output).resolve())
    result = validate_with_reasoner(abs_output)
    if result.get("consistent") is True:
        print(f"      ✅ Ontologia consistente (zero inconsistencies)")
    elif result.get("consistent") is False:
        print(f"      ❌ Ontologia INCONSISTENTE!")
        for c in result.get("inconsistent_classes", []):
            print(f"         • {c}")
    else:
        print(f"      ⚠️  Validazione non disponibile: {result.get('error', '?')}")

    # Post-processing opzionale
    if args.fix_labels:
        run_fix_labels(output)
    if args.extract_desc:
        run_extract_descriptions(output)


if __name__ == "__main__":
    main()
