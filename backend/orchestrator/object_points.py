"""Per-object measurement points received over MQTT (R12).

During its lifecycle an object accumulates geometric data produced by the
subsystems that work on it:

- the **vision** system reports, for every object it discovers, the
  gripper contact points (``[x, y, z]``);
- the **AI analysis** reports, once it has planned from the imaging data,
  the points of interest on the object surface (``[x, y, z, r, p, y]``),
  one per analysis (labeled, e.g. ``xrf`` and ``raman``).

The orchestrator stores these points per object ID as they arrive and,
whenever it commands a subsystem that declared it needs them
(``metadata.points_requests`` in the consumer's YAML), it pushes the
selected coordinates — in order — on the consumer's dedicated points
topic, right after the command itself.

The store is intentionally dumb: it keeps what the producers sent (plain
coordinate lists, plus optional labels) and selects from it; every
semantic (which points a command needs, in which shape) lives in the
subsystem YAML, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class PointEntry:
    """One stored point: optional label + its coordinates."""

    coordinates: List[float]
    label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"coordinates": list(self.coordinates)}
        if self.label:
            out["label"] = self.label
        return out


@dataclass
class ObjectPoints:
    """All the points of one kind received for one object."""

    object_id: str
    kind: str
    source: str
    points: List[PointEntry] = field(default_factory=list)
    received_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "object_id": self.object_id,
            "received_at": self.received_at,
            "points": [p.to_dict() for p in self.points],
        }


class ObjectPointsStore:
    """``object_id -> kind -> ObjectPoints`` with request-based selection."""

    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, ObjectPoints]] = {}

    # -- population ------------------------------------------------------

    def add(
        self,
        object_id: str,
        kind: str,
        source: str,
        raw_points: List[Any],
        labels: Optional[List[str]] = None,
    ) -> Optional[ObjectPoints]:
        """Store a points report for one object (replacing the same kind).

        ``raw_points`` accepts both producer shapes: plain coordinate
        lists (``[x, y, z]``) or ``{label, coordinates}`` objects. Returns
        the stored entry, or ``None`` when the report carried nothing
        usable (no object id or no parseable points).
        """
        if not object_id or not kind:
            return None
        points: List[PointEntry] = []
        for i, raw in enumerate(raw_points):
            label = ""
            coords: Any = raw
            if isinstance(raw, dict):
                label = str(raw.get("label") or "")
                coords = raw.get("coordinates")
            if not isinstance(coords, (list, tuple)) or not coords:
                continue
            try:
                values = [float(v) for v in coords]
            except (TypeError, ValueError):
                continue
            if not label and labels and i < len(labels):
                label = str(labels[i])
            points.append(PointEntry(coordinates=values, label=label))
        if not points:
            return None
        entry = ObjectPoints(
            object_id=object_id, kind=kind, source=source, points=points
        )
        self._data.setdefault(object_id, {})[kind] = entry
        return entry

    # -- lookup ----------------------------------------------------------

    def get(self, object_id: Optional[str], kind: str) -> Optional[ObjectPoints]:
        if not object_id:
            return None
        return self._data.get(object_id, {}).get(kind)

    def select(
        self,
        object_id: Optional[str],
        kind: str,
        label: Optional[str] = None,
        index: Optional[int] = None,
    ) -> Optional[List[List[float]]]:
        """The coordinates a points request selects, in order.

        - ``label``: only the points carrying that label (producer-declared).
        - ``index``: only the point at that position.
        - neither: every stored point of the kind, in report order.
        Returns ``None`` when the object has no such points (or the
        selection matches nothing) so the caller can warn.
        """
        entry = self.get(object_id, kind)
        if entry is None:
            return None
        if label:
            coords = [p.coordinates for p in entry.points if p.label == label]
            return coords or None
        if index is not None:
            if 0 <= index < len(entry.points):
                return [entry.points[index].coordinates]
            return None
        return [p.coordinates for p in entry.points]

    # -- frontend info / lifecycle ----------------------------------------

    def info(self, object_id: str) -> List[Dict[str, Any]]:
        """Serializable snapshot for the object window (stable order)."""
        kinds = self._data.get(object_id) or {}
        return [
            entry.to_dict()
            for _, entry in sorted(kinds.items())
        ]

    def drop(self, object_id: str) -> None:
        self._data.pop(object_id, None)

    def clear(self) -> None:
        self._data.clear()
