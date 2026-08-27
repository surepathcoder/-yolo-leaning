"""Constant-velocity Kalman filter for bounding-box tracking.

State is 8-dimensional: ``[cx, cy, aspect, height, vx, vy, v_aspect, v_height]``.

Parameterising by centre, aspect ratio and height (rather than the four corners)
is the standard SORT/ByteTrack choice and it matters: a person's height in the
image changes smoothly as they walk towards or away from the camera, while
their aspect ratio stays roughly constant.  Tracking the corners directly makes
the filter fight itself, because the corners are strongly correlated.

Process and measurement noise scale with box height. A detection 200 px tall is
localised far more precisely than one 20 px tall, and a filter that treats both
as equally trustworthy will either lag on close subjects or jitter on distant
ones.
"""

from __future__ import annotations

import numpy as np


class KalmanBoxFilter:
    """Tracks one box through time.

    All coordinates are normalised 0..1, matching the rest of the engine.
    """

    #: Noise scales, relative to box height. These are the ByteTrack/SORT
    #: defaults and are well-tested across pedestrian datasets.
    STD_WEIGHT_POSITION = 1.0 / 20
    STD_WEIGHT_VELOCITY = 1.0 / 160

    def __init__(self) -> None:
        ndim, dt = 4, 1.0

        # Constant-velocity motion: position += velocity each step.
        self._motion_matrix = np.eye(2 * ndim, 2 * ndim)
        for i in range(ndim):
            self._motion_matrix[i, ndim + i] = dt

        # We observe position only; velocity is inferred.
        self._update_matrix = np.eye(ndim, 2 * ndim)

    # ------------------------------------------------------------- conversion
    @staticmethod
    def to_measurement(bbox: tuple[float, float, float, float]) -> np.ndarray:
        """``[x1, y1, x2, y2]`` -> ``[cx, cy, aspect, height]``."""
        x1, y1, x2, y2 = bbox
        width = max(x2 - x1, 1e-6)
        height = max(y2 - y1, 1e-6)
        return np.array([x1 + width / 2, y1 + height / 2, width / height, height])

    @staticmethod
    def to_bbox(mean: np.ndarray) -> tuple[float, float, float, float]:
        """``[cx, cy, aspect, height, ...]`` -> ``[x1, y1, x2, y2]``."""
        cx, cy, aspect, height = mean[:4]
        height = max(float(height), 1e-6)
        width = max(float(aspect) * height, 1e-6)
        return (cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2)

    # ---------------------------------------------------------------- filter
    def initiate(self, measurement: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Start a track from its first measurement.

        Velocity starts at zero with high variance: we have seen one frame and
        genuinely do not know which way the object is moving.
        """
        mean = np.zeros(8)
        mean[:4] = measurement

        height = measurement[3]
        std = np.array(
            [
                2 * self.STD_WEIGHT_POSITION * height,
                2 * self.STD_WEIGHT_POSITION * height,
                1e-2,
                2 * self.STD_WEIGHT_POSITION * height,
                10 * self.STD_WEIGHT_VELOCITY * height,
                10 * self.STD_WEIGHT_VELOCITY * height,
                1e-5,
                10 * self.STD_WEIGHT_VELOCITY * height,
            ]
        )
        return mean, np.diag(np.square(std))

    def predict(self, mean: np.ndarray, covariance: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Advance the state one frame."""
        height = max(mean[3], 1e-6)
        std_position = np.array(
            [
                self.STD_WEIGHT_POSITION * height,
                self.STD_WEIGHT_POSITION * height,
                1e-2,
                self.STD_WEIGHT_POSITION * height,
            ]
        )
        std_velocity = np.array(
            [
                self.STD_WEIGHT_VELOCITY * height,
                self.STD_WEIGHT_VELOCITY * height,
                1e-5,
                self.STD_WEIGHT_VELOCITY * height,
            ]
        )
        motion_covariance = np.diag(np.square(np.concatenate([std_position, std_velocity])))

        mean = self._motion_matrix @ mean
        covariance = self._motion_matrix @ covariance @ self._motion_matrix.T + motion_covariance
        return mean, covariance

    def project(self, mean: np.ndarray, covariance: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Project the state into measurement space."""
        height = max(mean[3], 1e-6)
        std = np.array(
            [
                self.STD_WEIGHT_POSITION * height,
                self.STD_WEIGHT_POSITION * height,
                1e-1,
                self.STD_WEIGHT_POSITION * height,
            ]
        )
        innovation_covariance = np.diag(np.square(std))
        projected_mean = self._update_matrix @ mean
        projected_covariance = self._update_matrix @ covariance @ self._update_matrix.T
        return projected_mean, projected_covariance + innovation_covariance

    def update(
        self, mean: np.ndarray, covariance: np.ndarray, measurement: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Correct the state with a new measurement."""
        projected_mean, projected_covariance = self.project(mean, covariance)

        # Solve for the Kalman gain instead of inverting: numerically stabler,
        # and the covariance can become near-singular on a stationary object.
        try:
            kalman_gain = np.linalg.solve(
                projected_covariance.T,
                (covariance @ self._update_matrix.T).T,
            ).T
        except np.linalg.LinAlgError:  # pragma: no cover - degenerate covariance
            kalman_gain = covariance @ self._update_matrix.T @ np.linalg.pinv(projected_covariance)

        innovation = measurement - projected_mean
        new_mean = mean + kalman_gain @ innovation
        new_covariance = covariance - kalman_gain @ projected_covariance @ kalman_gain.T

        # Height and aspect must stay positive; a negative one would invert the
        # box and cascade into nonsense IoU values.
        new_mean[2] = max(new_mean[2], 1e-4)
        new_mean[3] = max(new_mean[3], 1e-4)
        return new_mean, new_covariance

    def gating_distance(
        self, mean: np.ndarray, covariance: np.ndarray, measurements: np.ndarray
    ) -> np.ndarray:
        """Squared Mahalanobis distance to each measurement.

        Used to reject associations that are geometrically implausible given
        the track's motion, even when IoU happens to look acceptable.
        """
        projected_mean, projected_covariance = self.project(mean, covariance)
        differences = np.atleast_2d(measurements) - projected_mean
        try:
            cholesky = np.linalg.cholesky(projected_covariance)
            solved = np.linalg.solve(cholesky, differences.T)
            return np.sum(solved**2, axis=0)
        except np.linalg.LinAlgError:  # pragma: no cover
            return np.full(len(differences), np.inf)
