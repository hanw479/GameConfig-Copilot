"""Evaluation harnesses owned by Worker E (docs/06 §5.2).

Modules (all import-free at package level so ``gcc eval`` never pays for an
optional dependency it does not use):

* :mod:`~gameconfig_copilot.evaluation.retrieval_eval` — S1-07 retrieval baseline
  (Recall@k / MRR / nDCG over ``data/golden/qrels.json``).
* :mod:`~gameconfig_copilot.evaluation.agent_eval` — S3-05/S7-03 golden-set FPVR,
  repair rounds, danger block rate, P95 latency and per-run cost roll-up.
* :mod:`~gameconfig_copilot.evaluation.ragas_eval` — S7-02 lazy ragas harness with
  a pinned judge version; raises ``RagasUnavailable`` instead of faking scores.
* :mod:`~gameconfig_copilot.evaluation.trace_audit` — S7-05 JSONL span attribute
  audit, grouped by the owner who must fill the missing field.
* :mod:`~gameconfig_copilot.evaluation.compare` — S7-07 regression gate
  (FPVR −3pt / Recall@5 −2pt / cost +20%).
"""

from __future__ import annotations

__all__: list[str] = []
