"""
Hyper-parameter optimization (HPO) utilities.

This subpackage hosts "guided search" variants (e.g., Guided-ASHA) that reuse
the same pattern as Guided-CART: generate candidates -> score by a learned
reranker -> rerank/replace decisions with an alpha safety mix.
"""

