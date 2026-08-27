"""Optimal assignment for data association.

Tracking needs to answer "which detection belongs to which track" optimally
across the whole frame, not greedily one pair at a time.  Greedy matching by
best IoU produces visible id swaps whenever two people pass each other -- the
first pair locks in a match that forces a worse one on everything after it.

``scipy.optimize.linear_sum_assignment`` would do this, but scipy is roughly
40 MB of compiled code that VisionGuard would otherwise never use, and the
edge deployments this targets are storage- and bandwidth-constrained.  The
Jonker-Volgenant shortest-augmenting-path algorithm is about sixty lines, so
it is implemented here instead.

The implementation is verified against brute force in the tests.
"""

from __future__ import annotations

import numpy as np


def linear_sum_assignment(cost: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Solve the rectangular linear assignment problem, minimising total cost.

    Returns ``(row_indices, col_indices)`` such that
    ``cost[row_indices, col_indices].sum()`` is minimal, matching the interface
    of ``scipy.optimize.linear_sum_assignment``.

    Complexity is O(n^2 m). For tracking, n and m are the number of tracks and
    detections in one frame -- tens at most -- so this is not a bottleneck.
    """
    cost = np.asarray(cost, dtype=np.float64)
    if cost.ndim != 2:
        raise ValueError("cost matrix must be 2-dimensional")
    if cost.size == 0:
        return np.array([], dtype=int), np.array([], dtype=int)
    if not np.isfinite(cost).all():
        raise ValueError("cost matrix must contain only finite values")

    # The algorithm requires rows <= columns; transpose and swap back if not.
    transposed = cost.shape[0] > cost.shape[1]
    if transposed:
        cost = cost.T
    n_rows, n_cols = cost.shape

    infinity = np.inf
    # u/v are the dual potentials, p[j] is the row assigned to column j.
    u = np.zeros(n_rows + 1)
    v = np.zeros(n_cols + 1)
    p = np.zeros(n_cols + 1, dtype=int)
    way = np.zeros(n_cols + 1, dtype=int)

    for i in range(1, n_rows + 1):
        p[0] = i
        j0 = 0
        minv = np.full(n_cols + 1, infinity)
        used = np.zeros(n_cols + 1, dtype=bool)

        while True:
            used[j0] = True
            i0 = p[j0]
            delta = infinity
            j1 = -1
            for j in range(1, n_cols + 1):
                if used[j]:
                    continue
                current = cost[i0 - 1, j - 1] - u[i0] - v[j]
                if current < minv[j]:
                    minv[j] = current
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(n_cols + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break

        # Walk the augmenting path back, flipping assignments.
        while j0 != 0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1

    rows: list[int] = []
    cols: list[int] = []
    for j in range(1, n_cols + 1):
        if p[j] > 0:
            rows.append(p[j] - 1)
            cols.append(j - 1)

    row_array = np.array(rows, dtype=int)
    col_array = np.array(cols, dtype=int)
    order = np.argsort(row_array)
    row_array, col_array = row_array[order], col_array[order]

    if transposed:
        # Re-sort by the original row index after swapping back.
        order = np.argsort(col_array)
        return col_array[order], row_array[order]
    return row_array, col_array


def associate(
    cost: np.ndarray, max_cost: float
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Match rows to columns, rejecting pairs above ``max_cost``.

    Returns ``(matches, unmatched_rows, unmatched_cols)``.  Rejecting after the
    optimal assignment rather than before it is deliberate: masking expensive
    pairs out of the matrix first can force the solver into a worse global
    arrangement.
    """
    n_rows, n_cols = cost.shape if cost.size else (0, 0)
    if cost.size == 0:
        return [], list(range(cost.shape[0])), list(range(cost.shape[1]))

    row_indices, col_indices = linear_sum_assignment(cost)

    matches: list[tuple[int, int]] = []
    matched_rows: set[int] = set()
    matched_cols: set[int] = set()
    for row, col in zip(row_indices, col_indices, strict=False):
        if cost[row, col] <= max_cost:
            matches.append((int(row), int(col)))
            matched_rows.add(int(row))
            matched_cols.add(int(col))

    unmatched_rows = [i for i in range(n_rows) if i not in matched_rows]
    unmatched_cols = [j for j in range(n_cols) if j not in matched_cols]
    return matches, unmatched_rows, unmatched_cols


def iou_cost_matrix(track_boxes: np.ndarray, detection_boxes: np.ndarray) -> np.ndarray:
    """Cost matrix of ``1 - IoU`` for every track/detection pair.

    Boxes are ``(N, 4)`` arrays of ``[x1, y1, x2, y2]``.  Vectorised because
    this runs on every frame of every camera.
    """
    if len(track_boxes) == 0 or len(detection_boxes) == 0:
        return np.zeros((len(track_boxes), len(detection_boxes)), dtype=np.float64)

    tracks = np.asarray(track_boxes, dtype=np.float64)
    detections = np.asarray(detection_boxes, dtype=np.float64)

    # Broadcast to (n_tracks, n_detections) for every corner.
    x1 = np.maximum(tracks[:, None, 0], detections[None, :, 0])
    y1 = np.maximum(tracks[:, None, 1], detections[None, :, 1])
    x2 = np.minimum(tracks[:, None, 2], detections[None, :, 2])
    y2 = np.minimum(tracks[:, None, 3], detections[None, :, 3])

    intersection = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    track_area = (tracks[:, 2] - tracks[:, 0]) * (tracks[:, 3] - tracks[:, 1])
    detection_area = (detections[:, 2] - detections[:, 0]) * (detections[:, 3] - detections[:, 1])
    union = track_area[:, None] + detection_area[None, :] - intersection

    with np.errstate(divide="ignore", invalid="ignore"):
        iou = np.where(union > 0, intersection / union, 0.0)
    return 1.0 - iou


def centre_distance_cost_matrix(track_boxes: np.ndarray, detection_boxes: np.ndarray) -> np.ndarray:
    """Centre distance between every track/detection pair, scaled by box size.

    Used only as a fallback when IoU association fails. Distance is divided by
    the mean box diagonal so the result is comparable across a scene where a
    person near the camera is ten times the size of one at the back.
    """
    if len(track_boxes) == 0 or len(detection_boxes) == 0:
        return np.zeros((len(track_boxes), len(detection_boxes)), dtype=np.float64)

    tracks = np.asarray(track_boxes, dtype=np.float64)
    detections = np.asarray(detection_boxes, dtype=np.float64)

    track_centres = np.column_stack(
        [(tracks[:, 0] + tracks[:, 2]) / 2, (tracks[:, 1] + tracks[:, 3]) / 2]
    )
    detection_centres = np.column_stack(
        [
            (detections[:, 0] + detections[:, 2]) / 2,
            (detections[:, 1] + detections[:, 3]) / 2,
        ]
    )
    deltas = track_centres[:, None, :] - detection_centres[None, :, :]
    distances = np.sqrt((deltas**2).sum(axis=2))

    track_diagonals = np.hypot(tracks[:, 2] - tracks[:, 0], tracks[:, 3] - tracks[:, 1])
    detection_diagonals = np.hypot(
        detections[:, 2] - detections[:, 0], detections[:, 3] - detections[:, 1]
    )
    scale = (track_diagonals[:, None] + detection_diagonals[None, :]) / 2
    scale = np.maximum(scale, 1e-6)
    return distances / scale


def size_ratio_matrix(track_boxes: np.ndarray, detection_boxes: np.ndarray) -> np.ndarray:
    """Ratio of the larger box area to the smaller, per pair.

    A value near 1 means the boxes are the same size. Used to stop a distant
    person being matched to a nearby one purely because they are close on
    screen.
    """
    if len(track_boxes) == 0 or len(detection_boxes) == 0:
        return np.zeros((len(track_boxes), len(detection_boxes)), dtype=np.float64)

    tracks = np.asarray(track_boxes, dtype=np.float64)
    detections = np.asarray(detection_boxes, dtype=np.float64)
    track_areas = np.maximum((tracks[:, 2] - tracks[:, 0]) * (tracks[:, 3] - tracks[:, 1]), 1e-9)
    detection_areas = np.maximum(
        (detections[:, 2] - detections[:, 0]) * (detections[:, 3] - detections[:, 1]), 1e-9
    )
    ratio = track_areas[:, None] / detection_areas[None, :]
    return np.maximum(ratio, 1.0 / ratio)
