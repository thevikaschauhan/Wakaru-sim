"""Small deterministic logistic baseline, separate calibration and diagnostics."""

from __future__ import annotations

import math
import random
import statistics


def sigmoid(value):
    return 1 / (1 + math.exp(-max(-40, min(40, value))))


def fit(x, y, iterations=500, l2=0.01):
    if not x or set(y) != {0, 1}:
        raise ValueError("both_classes_required")
    columns = list(zip(*x))
    means = [statistics.fmean(c) for c in columns]
    scales = [max(statistics.pstdev(c), 1e-6) for c in columns]
    z = [[1.0] + [(v - m) / s for v, m, s in zip(row, means, scales)] for row in x]
    weights = [0.0] * len(z[0])
    for _ in range(iterations):
        gradient = [0.0] * len(weights)
        for row, label in zip(z, y):
            error = sigmoid(sum(w * v for w, v in zip(weights, row))) - label
            for j, value in enumerate(row):
                gradient[j] += error * value
        for j in range(len(weights)):
            weights[j] -= 0.15 * (gradient[j] / len(y) + (l2 * weights[j] if j else 0))
    return {
        "weights": weights,
        "means": means,
        "scales": scales,
        "ranges": [[min(c), max(c)] for c in columns],
    }


def score(model, row):
    z = [1.0] + [(v - m) / s for v, m, s in zip(row, model["means"], model["scales"])]
    return sum(w * v for w, v in zip(model["weights"], z))


def predict(model, rows):
    raw = [score(model, row) for row in rows]
    if "calibrator" in model:
        return [sigmoid(score(model["calibrator"], [s])) for s in raw]
    return [sigmoid(s) for s in raw]


def calibrate(model, x, y):
    model["calibrator"] = fit([[score(model, row)] for row in x], y, l2=0.001)
    return model


def wilson(positive, n):
    if not n:
        return None
    p, z = positive / n, 1.96
    center = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [max(0, center - half), min(1, center + half)]


def metrics(y, p):
    if not y:
        return {
            "n": 0,
            "auc": None,
            "brier": None,
            "log_loss": None,
            "ece": None,
            "reliability": [],
        }
    n, positives = len(y), sum(y)
    # Mann-Whitney AUC with average ranks for ties, O(n log n).
    ordered = sorted(zip(p, y))
    rank_sum, i = 0.0, 0
    while i < n:
        j = i + 1
        while j < n and ordered[j][0] == ordered[i][0]:
            j += 1
        rank_sum += sum(label for _, label in ordered[i:j]) * (i + 1 + j) / 2
        i = j
    auc = (
        (rank_sum - positives * (positives + 1) / 2) / (positives * (n - positives))
        if 0 < positives < n
        else None
    )
    reliability, ece = [], 0.0
    for bucket in range(10):
        pairs = [
            (label, prob)
            for label, prob in zip(y, p)
            if min(int(prob * 10), 9) == bucket
        ]
        if not pairs:
            continue
        count = len(pairs)
        observed = sum(a for a, _ in pairs) / count
        predicted = statistics.fmean(b for _, b in pairs)
        ece += count * abs(observed - predicted) / n
        reliability.append(
            {
                "bucket": bucket,
                "n": count,
                "predicted": predicted,
                "observed": observed,
                "observed_95ci": wilson(sum(a for a, _ in pairs), count),
            }
        )
    return {
        "n": n,
        "positive": positives,
        "auc": auc,
        "brier": statistics.fmean((a - b) ** 2 for a, b in zip(y, p)),
        "log_loss": statistics.fmean(
            -a * math.log(max(b, 1e-12)) - (1 - a) * math.log(max(1 - b, 1e-12))
            for a, b in zip(y, p)
        ),
        "ece": ece,
        "reliability": reliability,
    }


def bootstrap_difference(y, baseline, candidate, merchants, draws=500):
    """Paired merchant-cluster bootstrap preserves within-merchant dependence."""
    groups = {}
    for label, b, c, merchant in zip(y, baseline, candidate, merchants):
        groups.setdefault(merchant, []).append((label - b) ** 2 - (label - c) ** 2)
    if len(groups) < 3:
        return None
    rng = random.Random(1701)
    keys = sorted(groups)
    samples = []
    for _ in range(draws):
        values = [
            value for key in rng.choices(keys, k=len(keys)) for value in groups[key]
        ]
        samples.append(statistics.fmean(values))
    samples.sort()
    return [samples[int(draws * 0.025)], samples[int(draws * 0.975)]]
