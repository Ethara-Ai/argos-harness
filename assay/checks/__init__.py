"""Pytest surface for Channel A.

The checks themselves live in ``assay.deterministic`` so the RL loop can call
them in process. These modules turn the same results into a pytest session, which
is what produces the human-readable report and the JSON artifact.
"""
