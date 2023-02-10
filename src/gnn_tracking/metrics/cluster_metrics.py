"""Metrics evaluating the quality of clustering/i.e., the usefulness of the
algorithm for tracking.
"""

from __future__ import annotations

import functools
from collections import Counter
from typing import Callable, Iterable, Protocol, TypedDict

import numpy as np
import pandas as pd
from sklearn import metrics

from gnn_tracking.utils.math import zero_division_gives_nan
from gnn_tracking.utils.nomenclature import denote_pt
from gnn_tracking.utils.signature import tolerate_additional_kwargs


class ClusterMetricType(Protocol):
    """Function type that calculates a clustering metric."""

    def __call__(
        self,
        *,
        truth: np.ndarray,
        predicted: np.ndarray,
        pts: np.ndarray,
        reconstructable: np.ndarray,
        pt_thlds: list[float],
    ) -> float | dict[str, float]:
        ...


class TrackingMetrics(TypedDict):
    """Custom cluster metrics for tracking.

    All nominators and denominators only count clusters where the majority particle
    is reconstructable.
    If a pt threshold is applied, the denominator only counts clusters where the
    majority PID's pt is above the threshold.
    """

    #: True number of particles
    n_particles: int
    #: Number of clusters/number of predicted particles. Cleaned means
    n_cleaned_clusters: int
    #: Number of reconstructed tracks (clusters) containing only hits from the same
    #: particle and every hit generated by that particle, divided by the true number
    #: of particles
    perfect: float
    #: The number of reconstructed tracks containing over 50% of hits from the same
    #: particle and over 50% of that particle’s hits, divided by the total number of
    #: true particles
    double_majority: float
    #: The number of reconstructed tracks containing over 75% of hits from the same
    #: particle, divided by the total number reconstructed tracks (clusters)
    lhc: float

    fake_perfect: float
    fake_double_majority: float
    fake_lhc: float


_tracking_metrics_nan_results: TrackingMetrics = {
    "n_particles": 0,
    "n_cleaned_clusters": 0,
    "perfect": float("nan"),
    "lhc": float("nan"),
    "double_majority": float("nan"),
    "fake_perfect": float("nan"),
    "fake_lhc": float("nan"),
    "fake_double_majority": float("nan"),
}


def tracking_metrics(
    *,
    truth: np.ndarray,
    predicted: np.ndarray,
    pts: np.ndarray,
    reconstructable: np.ndarray,
    pt_thlds: Iterable[float],
    predicted_count_thld=3,
) -> dict[float, TrackingMetrics]:
    """Calculate 'custom' metrics for matching tracks and hits.

    Args:
        truth: Truth labels/PIDs for each hit
        predicted: Predicted labels/cluster index for each hit. Negative labels are
            interpreted as noise (because this is how DBSCAN outputs it) and are
            ignored
        pts: pt values of the hits
        reconstructable: Whether the hit belongs to a "reconstructable tracks" (this
            usually implies a cut on the number of layers that are being hit
            etc.)
        pt_thlds: pt thresholds to calculate the metrics for
        predicted_count_thld: Minimal number of hits in a cluster for it to not be
            rejected.

    Returns:
        See `TrackingMetrics`
    """
    for ar in (truth, predicted, pts, reconstructable):
        # Tensors behave differently when counting, so this is absolutely
        # vital!
        assert isinstance(ar, np.ndarray)
    assert predicted.shape == truth.shape == pts.shape, (
        predicted.shape,
        truth.shape,
        pts.shape,
    )
    if len(truth) == 0:
        return {pt: _tracking_metrics_nan_results for pt in pt_thlds}
    df = pd.DataFrame({"c": predicted, "id": truth, "pt": pts, "r": reconstructable})

    # For each cluster, we determine the true PID that is associated with the most
    # hits in that cluster.
    # Here we make use of the fact that `df.value_counts` sorts by the count.
    # That means that if we group by the cluster and take the first line
    # for each of the counts, we have the most popular PID for each cluster.
    # The resulting dataframe now has both the most popular PID ("id" column) and the
    # number of times it appears ("0" column).
    # This strategy is a significantly (!) faster version than doing
    # c_id.groupby("c").agg(lambda x: x.mode()[0]) etc.
    pid_counts = df[["c", "id"]].value_counts().reset_index()
    pid_counts_grouped = pid_counts.groupby("c")
    maj_df = pid_counts_grouped.first()
    # For each cluster: Which true PID has the most hits?
    c_maj_pids = maj_df["id"]
    # For each cluster: How many hits does the PID with the most hits have?
    c_maj_hits = maj_df[0]
    # Number of hits per cluster
    c_sizes = pid_counts_grouped[0].sum()
    # Assume that negative cluster labels mean that the cluster was labeled as
    # invalid
    unique_predicted, predicted_counts = np.unique(predicted, return_counts=True)
    c_valid_cluster = (unique_predicted >= 0) & (
        predicted_counts >= predicted_count_thld
    )

    # Properties associated to PID. This is pretty trivial, but since everything is
    # passed by hit, rather than by PID, we need to get rid of "duplicates"
    pid_to_props = df[["id", "pt", "r"]].groupby("id")[["pt", "r"]].first()
    pid_to_pt = pid_to_props["pt"].to_dict()
    pid_to_r = pid_to_props["r"].to_dict()
    # For each cluster: Of which pt is the PID with the most hits?
    c_maj_pts = c_maj_pids.map(pid_to_pt)
    # For each cluster: Is the PID with the most hits reconstructable?
    c_maj_reconstructable = c_maj_pids.map(pid_to_r)

    # For each PID: Number of hits (in any cluster)
    pid_to_count = Counter(truth)
    # For each cluster: Take most popular PID of that cluster and get number of hits of
    # that PID (in any cluster)
    maj_hits = c_maj_pids.map(pid_to_count)

    result = dict[float, ClusterMetricType]()
    for pt in pt_thlds:
        c_mask = (c_maj_pts >= pt) & c_maj_reconstructable

        # For each cluster: Fraction of hits that have the most popular PID
        c_maj_frac = (c_maj_hits[c_mask] / c_sizes[c_mask]).fillna(0)
        # For each cluster: Take the most popular PID of that cluster. What fraction of
        # the corresponding hits is in this cluster?
        maj_frac = (c_maj_hits[c_mask] / maj_hits[c_mask]).fillna(0)

        perfect_match = np.sum(
            (maj_hits[c_mask] == c_maj_hits[c_mask])
            & (c_maj_frac > 0.99)
            & c_valid_cluster[c_mask]
        ).item()
        double_majority = np.sum(
            (maj_frac > 0.5) & (c_maj_frac > 0.5) & c_valid_cluster[c_mask]
        ).item()
        lhc_match = np.sum((c_maj_frac > 0.75) & c_valid_cluster[c_mask]).item()

        h_pt_mask = pts >= pt
        c_pt_mask = c_maj_pts >= pt
        n_particles = len(np.unique(truth[h_pt_mask]))
        n_clusters = len(unique_predicted[c_pt_mask & c_valid_cluster])

        fake_pm = n_clusters - perfect_match
        fake_dm = n_clusters - double_majority
        fake_lhc = n_clusters - lhc_match

        # breakpoint()

        r: TrackingMetrics = {
            "n_particles": n_particles,
            "n_cleaned_clusters": n_clusters,
            "perfect": zero_division_gives_nan(perfect_match, n_particles),
            "double_majority": zero_division_gives_nan(double_majority, n_particles),
            "lhc": zero_division_gives_nan(lhc_match, n_clusters),
            "fake_perfect": zero_division_gives_nan(fake_pm, n_particles),
            "fake_double_majority": zero_division_gives_nan(fake_dm, n_particles),
            "fake_lhc": zero_division_gives_nan(fake_lhc, n_clusters),
        }
        result[pt] = r  # type: ignore
    return result  # type: ignore


def flatten_track_metrics(
    custom_metrics_result: dict[float, dict[str, float]]
) -> dict[str, float]:
    """Flatten the result of `custom_metrics` by using pt suffixes to arrive at a
    flat dictionary, rather than a nested one.
    """
    return {
        denote_pt(k, pt): v
        for pt, results in custom_metrics_result.items()
        for k, v in results.items()
    }


def count_hits_per_cluster(predicted: np.ndarray) -> np.ndarray:
    """Count number of hits per cluster"""
    _, counts = np.unique(predicted, return_counts=True)
    hist_counts, _ = np.histogram(counts, bins=np.arange(0.5, counts.max() + 1.5))
    return hist_counts


def hits_per_cluster_count_to_flat_dict(
    counts: np.ndarray, min_max=10
) -> dict[str, float]:
    """Turn result array from `count_hits_per_cluster` into a dictionary
    with cumulative counts.

    Args:
        counts: Result from `count_hits_per_cluster`
        min_max: Pad the counts with zeros to at least this length
    """
    cumulative = np.cumsum(
        np.pad(counts, (0, max(0, min_max - len(counts))), "constant")
    )
    total = cumulative[-1]
    return {
        f"hitcountgeq_{i:04}": cumulative / total
        for i, cumulative in enumerate(reversed(cumulative), start=1)
    }


def _sklearn_signature_wrap(func: Callable) -> ClusterMetricType:
    """A decorator to make an sklearn cluster metric function accept/take the
    arguments from ``ClusterMetricType``.
    """

    @functools.wraps(func)
    @tolerate_additional_kwargs
    def wrapped(predicted: np.ndarray, truth: np.ndarray):
        return func(truth, predicted)

    return wrapped


#: Common metrics that we have for clustering/matching of tracks to hits
common_metrics: dict[str, ClusterMetricType] = {
    "v_measure": _sklearn_signature_wrap(metrics.v_measure_score),
    "homogeneity": _sklearn_signature_wrap(metrics.homogeneity_score),
    "completeness": _sklearn_signature_wrap(metrics.completeness_score),
    "trk": lambda *args, **kwargs: flatten_track_metrics(
        tracking_metrics(*args, **kwargs)
    ),
    "adjusted_rand": _sklearn_signature_wrap(metrics.adjusted_rand_score),
    "fowlkes_mallows": _sklearn_signature_wrap(metrics.fowlkes_mallows_score),
    "adjusted_mutual_info": _sklearn_signature_wrap(metrics.adjusted_mutual_info_score),
    # "trkc": lambda **kwargs: hits_per_cluster_count_to_flat_dict(
    #     tolerate_additional_kwargs(count_hits_per_cluster)(**kwargs)
    # ),
}
