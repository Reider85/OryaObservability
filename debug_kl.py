"""Debug KL divergence computation."""

import numpy as np
from agent_obs.drift.kl_divergence import compute_kl_from_embeddings, create_histogram, normalize_histogram

# Test with similar distributions
baseline = [[0.1, 0.2, 0.1], [0.2, 0.1, 0.2], [0.15, 0.25, 0.15]]
current = [[0.1, 0.2, 0.1], [0.2, 0.1, 0.2]]

print("Baseline embeddings:", baseline)
print("Current embeddings:", current)

# Create histograms
baseline_hist = create_histogram(baseline, bins=5)
current_hist = create_histogram(current, bins=5)

print("Baseline histogram:", baseline_hist)
print("Current histogram:", current_hist)

# Normalize
baseline_norm = normalize_histogram(baseline_hist)
current_norm = normalize_histogram(current_hist)

print("Baseline normalized:", baseline_norm)
print("Current normalized:", current_norm)

# Compute KL
kl = compute_kl_from_embeddings(baseline, current)
print("KL divergence:", kl)

# Test with even more similar data
baseline2 = [[0.1, 0.2, 0.1], [0.2, 0.1, 0.2]]
current2 = [[0.1, 0.2, 0.1], [0.2, 0.1, 0.2]]

print("\nMore similar data:")
print("Baseline2:", baseline2)
print("Current2:", current2)

baseline_hist2 = create_histogram(baseline2, bins=5)
current_hist2 = create_histogram(current2, bins=5)
print("Baseline2 histogram:", baseline_hist2)
print("Current2 histogram:", current_hist2)

kl2 = compute_kl_from_embeddings(baseline2, current2)
print("KL divergence 2:", kl2)