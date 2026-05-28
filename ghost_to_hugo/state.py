"""Resume-on-failure state file.

Tracks which posts have been processed (success or quarantined) so that
re-running the migration only retries the un-done posts. R2 uploads are
already idempotent (hash-keyed), so resuming is safe even mid-flight.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class State:
    path: Path
    completed: set[str] = field(default_factory=set)
    quarantined: dict[str, str] = field(default_factory=dict)  # slug → reason
    asset_map: dict[str, str] = field(default_factory=dict)    # ghost_url → r2_url

    @classmethod
    def load(cls, path: Path | str) -> "State":
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return cls(
                path=p,
                completed=set(data.get("completed", [])),
                quarantined=dict(data.get("quarantined", {})),
                asset_map=dict(data.get("asset_map", {})),
            )
        except Exception:
            return cls(path=p)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "completed": sorted(self.completed),
            "quarantined": self.quarantined,
            "asset_map": self.asset_map,
        }
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                             encoding="utf-8")

    def is_done(self, slug: str) -> bool:
        return slug in self.completed
