"""Debug KL divergence with more bins."""

import numpy as np
from agent_obs.drift.kl_divergence import compute_kl_from_embeddings, create_histogram, normalize_histogram

# Test with different value ranges but similar patterns
baseline = [[0.1, 0.2, 0.15, 0.3, 0.25], [0.2, 0.1, 0.25, 0.15, 0.3]] * 10
current = [[0.8, 0.9, 0.85, 1.0, 0.95], [0.9, 0.8, 0.95, 0.85, 1.0]] * 5

print("Baseline embeddings (first few):", baseline[:2])
print("Current embeddings (first few):", current[:2])

# Create histograms with different bin counts
for bins in [10, 50, 100]:
    baseline_hist = create_histogram(baseline, bins=bins)
    current_hist = create_histogram(current, bins=bins)
    
    print(f"\nWith {bins} bins:")
    print("Baseline histogram (first 10):", baseline_hist[:10])
    print("Current histogram (first 10):", current_hist[:10])
    print("Sum baseline:", sum(baseline_hist))
    print("Sum current:", sum(current_hist))
    
    # Normalize
    baseline_norm = normalize_histogram(baseline_hist)
    current_norm = normalize_histogram(current_hist)
    
    print("Baseline normalized (first 10):", baseline_norm[:10])
    print("Current normalized (first 10):", current_norm[:10])
    
    # Compute KL
    kl = compute_kl_from_embeddings(baseline, current, bins=bins)
    print("KL divergence:", kl)

# Test with more extreme differences
baseline2 = [[0.1, 0.2, 0.1, 0.2, 0.1]] * 20
current2 = [[0.9, 0.8, 0.9, 0.8, 0.9]] * 10

print("\n\nMore extreme differences:")
kl2 = compute_kl_from_embeddings(baseline2, current2, bins=100)
print("KL divergence with extreme differences:", kl2)