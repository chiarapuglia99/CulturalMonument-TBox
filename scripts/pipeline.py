"""Pipeline modulare ABox → TBox (Zengeya et al.)."""

import datetime

from rdflib import Graph

from .constants import TAU_SUB, TAU_SIM, TAU_DOM
from .utils import detect_format, auto_title
from .analyzer import analyze_abox
from .builder import (detect_equivalent_classes, deduplicate_domains,
                      dedup_restrictions_analysis, build_tbox, build_merged)
from .llm import (check_ollama, enrich_with_llm, apply_llm_enrichments,
                  ask_llm_review_low_confidence, apply_llm_review)


class ABoxToTBoxPipeline:
    """
    Pipeline che implementa i tre stadi del paper di Zengeya et al.:
      1. Lexical-to-Ontological Mapping  (analisi ABox)
      2. Schema Formation               (induzione gerarchia)
      3. Axiom Generation               (TBox + ABox)
    """

    def __init__(self, tau_sub=TAU_SUB, tau_sim=TAU_SIM, tau_dom=TAU_DOM,
                 use_llm=True, ollama_url="http://localhost:11434",
                 llm_model="gemma4:31b-cloud", llm_timeout=None):
        self.tau_sub = tau_sub
        self.tau_sim = tau_sim
        self.tau_dom = tau_dom
        self.use_llm = use_llm
        self.ollama_url = ollama_url
        self.llm_model = llm_model
        self.llm_timeout = llm_timeout

    def stage1_analyze(self, g: Graph) -> dict:
        return analyze_abox(g)

    def stage2_schema_formation(self, analysis: dict, g: Graph) -> dict:
        equiv_map = detect_equivalent_classes(analysis, g)
        analysis["equiv_classes"] = equiv_map
        if equiv_map:
            alias_set = {a for aliases in equiv_map.values() for a in aliases}
            analysis["classes"] = analysis["classes"] - alias_set
            for prop_dict in [analysis["object_props"], analysis["data_props"]]:
                for info in prop_dict.values():
                    info["domains"] = deduplicate_domains(info["domains"], equiv_map)
                    info["ranges"]  = deduplicate_domains(info["ranges"],  equiv_map)
            dedup_restrictions_analysis(analysis, equiv_map)
        return analysis

    def stage2b_llm_enrichment(self, analysis: dict, input_path: str) -> dict:
        if not self.use_llm:
            return analysis
        if not check_ollama(self.ollama_url, self.llm_model):
            return analysis
        enrichments = enrich_with_llm(
            analysis, input_path, self.ollama_url, self.llm_model,
            timeout=self.llm_timeout)
        return apply_llm_enrichments(analysis, enrichments)

    def stage2c_llm_review(self, analysis: dict) -> dict:
        if not self.use_llm:
            return analysis
        axiom_conf = analysis.get("axiom_confidence", {})
        if not axiom_conf:
            return analysis
        if not check_ollama(self.ollama_url, self.llm_model):
            return analysis
        review = ask_llm_review_low_confidence(
            analysis, axiom_conf, self.ollama_url, self.llm_model,
            tau=self.tau_dom, timeout=self.llm_timeout)
        if review:
            analysis = apply_llm_review(analysis, review)
        return analysis

    def stage3_generate_axioms(self, analysis: dict, src: Graph,
                                dc_meta: dict) -> Graph:
        return build_tbox(analysis, src, dc_meta)

    def run(self, input_path: str, fmt: str = None,
            dc_meta: dict = None, merge: bool = False) -> tuple:
        fmt = fmt or detect_format(input_path)
        g = Graph()
        g.parse(input_path, format=fmt)

        analysis = self.stage1_analyze(g)
        analysis = self.stage2_schema_formation(analysis, g)
        analysis = self.stage2b_llm_enrichment(analysis, input_path)
        analysis = self.stage2c_llm_review(analysis)

        dc = dc_meta or {
            "title": auto_title(input_path),
            "creator": "abox_to_tbox.py",
            "date": str(datetime.date.today().year),
        }
        tbox = self.stage3_generate_axioms(analysis, g, dc)

        if merge:
            out = build_merged(tbox, g,
                               equiv_map=analysis.get("equiv_classes", {}))
        else:
            out = tbox

        return out, analysis, g
