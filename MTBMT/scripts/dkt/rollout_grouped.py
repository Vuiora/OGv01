from __future__ import annotations

import argparse
import gzip
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _open_out(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if str(path).endswith(".gz"):
        return gzip.open(path, "wt", encoding="utf-8")
    return path.open("w", encoding="utf-8")


def _write_jsonl(f, obj: Dict[str, Any]) -> None:
    f.write(json.dumps(obj, ensure_ascii=False) + "\n")


@dataclass
class SkillState:
    p: float


def _clip01(x: float) -> float:
    return 0.0 if x <= 0 else 1.0 if x >= 1 else float(x)


def _init_state_for_group(model: Dict[str, Any], group_id: str, *, default_p: float) -> Dict[str, SkillState]:
    g = (((model.get("groups") or {}).get(group_id) or {}).get("skills") or {})
    out: Dict[str, SkillState] = {}
    if isinstance(g, dict):
        for kid, rec in g.items():
            try:
                p = float((rec or {}).get("p"))
            except Exception:
                p = float(default_p)
            out[str(kid)] = SkillState(p=_clip01(p))
    return out


def _sample_pm_for_k(k2pm: Dict[str, List[str]], k_id: str, rng: random.Random) -> Optional[str]:
    xs = k2pm.get(k_id) or []
    if not xs:
        return None
    return str(rng.choice(xs))


def _step_skill(state: SkillState, y: int, *, learn_rate: float) -> None:
    # 简化的“学习更新”：答对提升，答错小幅下降（避免发散）
    lr = float(learn_rate)
    if int(y) == 1:
        state.p = _clip01(state.p + lr * (1.0 - state.p))
    else:
        state.p = _clip01(state.p - 0.25 * lr * state.p)


def _choose_k_random(available: List[str], rng: random.Random) -> str:
    return str(rng.choice(available))


def _choose_k_guided(
    *,
    available: List[str],
    state: Dict[str, SkillState],
    exam_skills: List[str],
    rng: random.Random,
) -> str:
    # guided：优先在 exam_skills 里挑“当前掌握概率最低”的技能；若 exam_skills 为空则退化为 random
    cand = [k for k in exam_skills if k in state] or available
    best_k = None
    best_p = 2.0
    for k in cand:
        p = state.get(k, SkillState(p=0.5)).p
        if p < best_p:
            best_p = p
            best_k = k
    if best_k is None:
        return _choose_k_random(available, rng)
    return str(best_k)


def _eval_exam(state: Dict[str, SkillState], exam_skills: List[str]) -> float:
    if not exam_skills:
        return 0.0
    ps = [state.get(k, SkillState(p=0.5)).p for k in exam_skills]
    return float(sum(ps)) / float(len(ps))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Rollout grouped student simulator and compare policies (guided vs random).")
    ap.add_argument("--episodes-jsonl", required=True, help="Episodes JSONL (group_id, history, exam_set).")
    ap.add_argument("--model", required=True, help="Grouped model JSON from train_bkt_grouped.py.")
    ap.add_argument("--k2pm", required=True, help="Knowledge->Problem list mapping JSON (k_id -> [pm_id...]).")
    ap.add_argument("--out-jsonl", required=True, help="Output rollouts JSONL (use .gz to compress).")
    ap.add_argument("--T", type=int, default=20, help="Interaction steps before exam.")
    ap.add_argument("--seed", type=int, default=0, help="RNG seed.")
    ap.add_argument("--learn-rate", type=float, default=0.15, help="Learning rate for skill update.")
    ap.add_argument("--default-p", type=float, default=0.5, help="Default p for unseen skills.")
    ap.add_argument("--max-episodes", type=int, default=0, help="If >0, only rollout first N episodes.")
    ap.add_argument("--progress-every", type=int, default=10000, help="Progress print every N episodes.")
    args = ap.parse_args(argv)

    model = _read_json(args.model)
    k2pm = _read_json(args.k2pm)
    if not isinstance(k2pm, dict):
        raise SystemExit("k2pm 必须是 JSON object: {k_id: [pm_id...]}")

    rng = random.Random(int(args.seed))
    out_path = Path(args.out_jsonl)

    n = 0
    t0 = time.time()
    with _open_out(out_path) as out_f, Path(args.episodes_jsonl).open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ep = json.loads(line)
            except Exception:
                continue
            gid = str(ep.get("group_id") or "unknown")
            eid = str(ep.get("episode_id") or "")
            exam_set = ep.get("exam_set") or []
            if not isinstance(exam_set, list):
                continue
            exam_skills = [str(x.get("k_id") or "") for x in exam_set if isinstance(x, dict) and x.get("k_id")]
            exam_skills = [k for k in exam_skills if k]
            # available skills: union(model skills, exam skills, k2pm keys in group?) -> we use (model group skills + exam skills)
            base_state = _init_state_for_group(model, gid, default_p=float(args.default_p))
            for k in exam_skills:
                base_state.setdefault(k, SkillState(p=float(args.default_p)))
            available = list(base_state.keys())
            if not available:
                continue

            def _run_one(policy: str) -> Tuple[float, Dict[str, Any]]:
                st = {k: SkillState(p=v.p) for k, v in base_state.items()}
                actions: List[Dict[str, Any]] = []
                for t in range(int(args.T)):
                    if policy == "guided":
                        k = _choose_k_guided(available=available, state=st, exam_skills=exam_skills, rng=rng)
                    else:
                        k = _choose_k_random(available, rng)
                    pm = _sample_pm_for_k(k2pm, k, rng)
                    p = st.get(k, SkillState(p=float(args.default_p))).p
                    y = 1 if rng.random() < p else 0
                    _step_skill(st.setdefault(k, SkillState(p=float(args.default_p))), y, learn_rate=float(args.learn_rate))
                    actions.append({"t": t, "k_id": k, "pm_id": pm, "y": y, "p_before": p, "p_after": st[k].p})
                j = _eval_exam(st, exam_skills)
                return j, {"actions": actions}

            j_rand, trace_rand = _run_one("random")
            j_guided, trace_guided = _run_one("guided")

            _write_jsonl(
                out_f,
                {
                    "group_id": gid,
                    "episode_id": eid,
                    "T": int(args.T),
                    "J_random": float(j_rand),
                    "J_guided": float(j_guided),
                    "delta": float(j_guided - j_rand),
                    "exam_size": len(exam_skills),
                    "policy_traces": {"random": trace_rand, "guided": trace_guided},
                },
            )
            n += 1
            if int(args.progress_every) > 0 and n % int(args.progress_every) == 0:
                dt = max(time.time() - t0, 1e-9)
                print(f"[rollout] episodes={n:,} speed={n/dt:,.1f} ep/s out={out_path}")
            if int(args.max_episodes) > 0 and n >= int(args.max_episodes):
                break

    print(json.dumps({"episodes": n, "out": str(out_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


