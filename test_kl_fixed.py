"""Test the fixed KL divergence computation."""

import numpy as np
from agent_obs.drift.kl_divergence import compute_kl_from_embeddings

# Test with different value ranges
baseline = [[0.1, 0.2, 0.15, 0.3, 0.25], [0.2, 0.1, 0.25, 0.15, 0.3]] * 10
current = [[0.8, 0.9, 0.85, 1.0, 0.95], [0.9, 0.8, 0.95, 0.85, 1.0]] * 5

print("Testing with different value ranges:")
print("Baseline range: 0.1-0.3")
print("Current range: 0.8-1.0")

kl = compute_kl_from_embeddings(baseline, current, bins=50)
print("KL divergence:", kl)

# Test with similar ranges
baseline2 = [[0.1, 0.2, 0.15, 0.3, 0.25], [0.2, 0.1, 0.25, 0.15, 0.3]] * 10
current2 = [[0.12, 0.22, 0.17, 0.32, 0.27], [0.22, 0.12, 0.27, 0.17, 0.32]] * 5

print("\nTesting with similar ranges:")
print("Baseline2 range: 0.1-0.3")
print("Current2 range: 0.12-0.32")

kl2 = compute_kl_from_embeddings(baseline2, current2, bins=50)
print("KL divergence 2:", kl2)

# Test with identical data
baseline3 = [[0.1, 0.2, 0.15, 0.3, 0.25], [0.2, 0.1, 0.25, 0.15, 0.3]] * 10
current3 = [[0.1, 0.2, 0.15, 0.3, 0.25], [0.2, 0.1, 0.25, 0.15, 0.3]] * 5

print("\nTesting with identical data:")
kl3 = compute_kl_from_embeddings(baseline3, current3, bins=50)
print("KL divergence 3 (identical):", kl3)