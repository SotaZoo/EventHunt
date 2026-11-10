"""Evaluation harness for the M0 hard gate (precision@5 ≥ 70%).

- ``load_seed`` parses ``data/taste-seed.txt`` into structured records
  and emits ``tests/eval/seed.jsonl`` for stable downstream tests.
- ``precision`` (M0.8) computes the gate metrics over ranked scores.
"""
