"""Human review of rubric verdicts (mechanism A).

    pull: MLflow labels -> Delta `human_labels`; judge verdicts attached after a human label;
          judge-human agreement -> Delta `judge_agreement`
    push: queue (uncertain, judge-fail, random) -> MLflow traces -> the space's labeling session

Blind review: the judge's verdict lives only in Delta until a human has labelled that
(trace, criterion); reviewers have no grant on the Delta tables.
"""
