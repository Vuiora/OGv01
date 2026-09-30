from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


@dataclass
class SkillStats:
    n: int = 0
    c: int = 0

    def update(self, y: int) -> None:
        self.n += 1
        self.c += int(y == 1)

    def p_correct(self, *, alpha: float, beta: float) -> float:
        # Beta-Bernoulli posterior mean
        return float(self.c + alpha) / float(self.n + alpha + beta)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Train a lightweight grouped skill model (BKT-like) from episodes history. Output: JSON with per-group per-skill p_correct."
    )
    ap.add_argument("--episodes-jsonl", required=True, help="Episodes JSONL (group_id, history with k_id,y).")
    ap.add_argument("--out-model", required=True, help="Output model JSON path.")
    ap.add_argument("--alpha", type=float, default=1.0, help="Beta prior alpha for p_correct.")
    ap.add_argument("--beta", type=float, default=1.0, help="Beta prior beta for p_correct.")
    ap.add_argument("--progress-every", type=int, default=200000, help="Print progress every N episodes.")
    args = ap.parse_args(argv)

    t0 = time.time()
    n_ep = 0
    n_hist = 0

    # group_id -> k_id -> stats
    stats: Dict[str, Dict[str, SkillStats]] = {}

    with Path(args.episodes_jsonl).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ep = json.loads(line)
            except Exception:
                continue
            gid = str(ep.get("group_id") or "unknown")
            hist = ep.get("history") or []
            if not isinstance(hist, list):
                continue
            gmap = stats.setdefault(gid, {})
            for h in hist:
                if not isinstance(h, dict):
                    continue
                kid = str(h.get("k_id") or "")
                if not kid:
                    continue
                try:
                    y = int(h.get("y"))
                except Exception:
                    continue
                gmap.setdefault(kid, SkillStats()).update(y)
                n_hist += 1
            n_ep += 1
            if int(args.progress_every) > 0 and n_ep % int(args.progress_every) == 0:
                dt = max(time.time() - t0, 1e-9)
                print(f"[train] episodes={n_ep:,} hist_events={n_hist:,} speed={n_ep/dt:,.0f} ep/s")

    model: Dict[str, Any] = {
        "kind": "grouped_skill_p_correct_v1",
        "created_at_unix": int(time.time()),
        "alpha": float(args.alpha),
        "beta": float(args.beta),
        "groups": {},
    }
    for gid, gmap in stats.items():
        out_skills: Dict[str, Any] = {}
        for kid, st in gmap.items():
            out_skills[kid] = {"n": int(st.n), "c": int(st.c), "p": float(st.p_correct(alpha=float(args.alpha), beta=float(args.beta)))}
        model["groups"][gid] = {"skills": out_skills}

    out_path = Path(args.out_model)
    _write_json(out_path, model)
    print(json.dumps({"episodes": n_ep, "hist_events": n_hist, "groups": len(stats), "out": str(out_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


