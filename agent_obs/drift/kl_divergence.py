"""KL-divergence computation for drift detection using histogram-based approach."""

from __future__ import annotations

import numpy as np
from typing import List


def create_histogram(embeddings: List[List[float]], bins: int = 50, bin_edges: np.ndarray | None = None) -> np.ndarray:
    """Create normalized histogram from embedding vectors.
    
    Args:
        embeddings: List of embedding vectors (each is list[float])
        bins: Number of histogram bins (used only if bin_edges is None)
        bin_edges: Optional fixed bin edges to ensure consistent binning
        
    Returns:
        Normalized histogram as numpy array
    """
    if not embeddings:
        return np.zeros(bins)
    
    # Flatten all embeddings into single array
    all_values = np.array(embeddings).flatten()
    
    # Create histogram with fixed bin edges if provided
    if bin_edges is not None:
        hist, _ = np.histogram(all_values, bins=bin_edges, density=False)
    else:
        hist, _ = np.histogram(all_values, bins=bins, density=False)
    
    return hist


def normalize_histogram(hist: np.ndarray) -> np.ndarray:
    """Normalize histogram to probability distribution.
    
    Args:
        hist: Raw histogram counts
        
    Returns:
        Normalized probability distribution
    """
    if np.sum(hist) == 0:
        return np.zeros_like(hist)
    
    # Normalize to probability distribution
    return hist.astype(np.float64) / np.sum(hist)


def compute_kl_divergence(p: np.ndarray, q: np.ndarray, epsilon: float = 1e-10) -> float:
    """Compute KL-divergence between two probability distributions.
    
    KL(P || Q) = sum(p_i * log(p_i / q_i))
    
    Args:
        p: Reference distribution (baseline)
        q: Test distribution (current)
        epsilon: Small value to avoid log(0)
        
    Returns:
        KL-divergence score
    """
    # Add epsilon to avoid log(0)
    p_safe = p + epsilon
    q_safe = q + epsilon
    
    # Normalize to ensure they are proper probability distributions
    p_safe = normalize_histogram(p_safe)
    q_safe = normalize_histogram(q_safe)
    
    # Compute KL-divergence
    kl = np.sum(p_safe * np.log(p_safe / q_safe))
    
    return float(kl)


def compute_kl_from_embeddings(
    baseline_embeddings: List[List[float]], 
    current_embeddings: List[List[float]], 
    bins: int = 50
) -> float:
    """Compute KL-divergence between two sets of embeddings.
    
    Args:
        baseline_embeddings: Baseline period embeddings
        current_embeddings: Current period embeddings
        bins: Number of histogram bins
        
    Returns:
        KL-divergence score
    """
    # Use fixed bin edges that cover the combined range of both datasets
    all_values = np.array(baseline_embeddings + current_embeddings).flatten()
    if len(all_values) == 0:
        return 0.0
    
    min_val, max_val = np.min(all_values), np.max(all_values)
    # Extend range slightly to handle edge cases and ensure consistent binning
    bin_edges = np.linspace(min_val - 0.01, max_val + 0.01, bins + 1)
    
    # Create histograms with the same bin edges
    baseline_hist = create_histogram(baseline_embeddings, bins, bin_edges)
    current_hist = create_histogram(current_embeddings, bins, bin_edges)
    
    # Compute KL-divergence
    kl_score = compute_kl_divergence(baseline_hist, current_hist)
    
    return kl_score