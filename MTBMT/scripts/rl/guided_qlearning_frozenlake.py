from __future__ import annotations

import argparse
import csv
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np


def _try_import_gym():
    try:
        import gymnasium as gym  # type: ignore

        return gym
    except Exception as e:
        raise SystemExit(
            "未安装 gymnasium，无法运行该 RL 示例。\n"
            "请先安装：\n"
            "  py -3 -m pip install gymnasium\n"
            f"原始错误：{e}"
        )


@dataclass(frozen=True)
class GuidePolicy:
    """
    A simple "expert" for FrozenLake: shortest-path on the known map (desc).
    If a state has no path (e.g. isolated), fallback to a random valid action.
    """

    best_action: Dict[int, int]  # state -> action

    def act(self, s: int, *, rng: np.random.Generator, n_actions: int) -> int:
        a = self.best_action.get(int(s), None)
        if a is None:
            return int(rng.integers(0, int(n_actions)))
        return int(a)


def _neighbors(desc: np.ndarray, r: int, c: int) -> List[Tuple[int, int, int]]:
    """
    Return neighbor cells with corresponding FrozenLake action:
    0: LEFT, 1: DOWN, 2: RIGHT, 3: UP
    """
    H, W = desc.shape
    out: List[Tuple[int, int, int]] = []
    if c - 1 >= 0:
        out.append((r, c - 1, 0))
    if r + 1 < H:
        out.append((r + 1, c, 1))
    if c + 1 < W:
        out.append((r, c + 1, 2))
    if r - 1 >= 0:
        out.append((r - 1, c, 3))
    return out


def build_shortest_path_guide(env) -> GuidePolicy:
    """
    Build a guide by BFS from every non-terminal state to the goal.
    We treat:
    - 'H' as forbidden
    - 'G' as goal
    - 'S'/'F' as passable
    """
    desc = np.asarray(env.unwrapped.desc)  # bytes array shape (H,W)
    H, W = desc.shape

    # locate goal
    goal: Optional[Tuple[int, int]] = None
    for r in range(H):
        for c in range(W):
            if desc[r, c] == b"G":
                goal = (r, c)
                break
        if goal is not None:
            break
    if goal is None:
        raise SystemExit("无法在 FrozenLake 地图中找到目标格 'G'。")

    # Reverse BFS from goal to all cells (avoid holes)
    q: Deque[Tuple[int, int]] = deque()
    q.append(goal)
    dist = {goal: 0}

    def passable(rr: int, cc: int) -> bool:
        return desc[rr, cc] != b"H"

    while q:
        r, c = q.popleft()
        for rr, cc, _a in _neighbors(desc, r, c):
            if not passable(rr, cc):
                continue
            if (rr, cc) in dist:
                continue
            dist[(rr, cc)] = dist[(r, c)] + 1
            q.append((rr, cc))

    # For each state (r,c), choose action that decreases dist (greedy)
    best_action: Dict[int, int] = {}
    for r in range(H):
        for c in range(W):
            if not passable(r, c):
                continue
            if (r, c) not in dist:
                continue
            if (r, c) == goal:
                continue
            best: Optional[Tuple[int, int]] = None  # (next_dist, action)
            for rr, cc, a in _neighbors(desc, r, c):
                if (rr, cc) not in dist:
                    continue
                nd = dist[(rr, cc)]
                cand = (nd, a)
                if best is None or cand < best:
                    best = cand
            if best is None:
                continue
            state = int(r * W + c)
            best_action[state] = int(best[1])

    return GuidePolicy(best_action=best_action)


def q_learning(
    *,
    env,
    episodes: int,
    seed: int,
    alpha_guided0: float,
    alpha_guided_final: float,
    epsilon0: float,
    epsilon_final: float,
    lr: float,
    gamma: float,
    guide: Optional[GuidePolicy],
    guided_mode: str,
    eval_every: int,
    eval_episodes: int,
    out_csv: Path,
    save_q_path: Optional[Path] = None,
) -> None:
    rng = np.random.default_rng(int(seed))
    nS = int(env.observation_space.n)
    nA = int(env.action_space.n)

    Q = np.zeros((nS, nA), dtype=float)

    def linear_schedule(t: int, T: int, a0: float, a1: float) -> float:
        if T <= 1:
            return float(a1)
        x = float(t) / float(max(T - 1, 1))
        return float(a0 + (a1 - a0) * x)

    def eval_policy() -> float:
        wins = 0
        for _ in range(int(eval_episodes)):
            s, _info = env.reset()
            done = False
            while not done:
                a = int(np.argmax(Q[int(s)]))
                s, r, terminated, truncated, _info = env.step(a)
                done = bool(terminated) or bool(truncated)
                if done and float(r) > 0:
                    wins += 1
        return float(wins) / float(max(1, int(eval_episodes)))

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "episode",
                "epsilon",
                "alpha_guided",
                "train_return",
                "eval_win_rate_raw",
                "eval_win_rate",
            ]
        )

        t0 = perf_counter()
        train_returns: Deque[float] = deque(maxlen=100)
        last_eval_win_rate = float("nan")

        for ep in range(1, int(episodes) + 1):
            eps = linear_schedule(ep - 1, episodes, float(epsilon0), float(epsilon_final))
            a_guided = linear_schedule(ep - 1, episodes, float(alpha_guided0), float(alpha_guided_final))

            s, _info = env.reset(seed=int(seed) + ep)
            done = False
            ep_ret = 0.0
            while not done:
                # base: epsilon-greedy on Q
                if float(rng.random()) < float(eps):
                    a_base = int(rng.integers(0, nA))
                else:
                    a_base = int(np.argmax(Q[int(s)]))

                a = a_base
                if guide is not None and guided_mode != "none" and float(a_guided) > 0.0:
                    if float(rng.random()) < float(a_guided):
                        if guided_mode == "guide_only":
                            a = guide.act(int(s), rng=rng, n_actions=nA)
                        else:
                            # mix: guide provides a "hint", but we still allow Q to override if it strongly prefers
                            a_hint = guide.act(int(s), rng=rng, n_actions=nA)
                            a = int(a_hint)

                s2, r, terminated, truncated, _info = env.step(a)
                done = bool(terminated) or bool(truncated)
                ep_ret += float(r)

                # Q-learning update
                td_target = float(r) + float(gamma) * float(np.max(Q[int(s2)])) * (0.0 if done else 1.0)
                Q[int(s), int(a)] = (1.0 - float(lr)) * Q[int(s), int(a)] + float(lr) * td_target

                s = s2

            train_returns.append(float(ep_ret))

            do_eval = (ep % max(1, int(eval_every)) == 0) or (ep == 1) or (ep == int(episodes))
            win_rate_raw = eval_policy() if do_eval else float("nan")
            if np.isfinite(win_rate_raw):
                last_eval_win_rate = float(win_rate_raw)

            # eval_win_rate is forward-filled for convenience in plotting.
            w.writerow([ep, eps, a_guided, float(ep_ret), win_rate_raw, last_eval_win_rate])

            if do_eval:
                avg100 = float(np.mean(np.asarray(train_returns, dtype=float))) if train_returns else 0.0
                elapsed = perf_counter() - t0
                sys.stderr.write(
                    f"\r[progress] ep={ep}/{episodes} eps={eps:.3f} a_guided={a_guided:.3f} "
                    f"avg_return(last100)={avg100:.3f} eval_win_rate={last_eval_win_rate if np.isfinite(last_eval_win_rate) else -1:.3f} "
                    f"elapsed={elapsed:.1f}s"
                )
                sys.stderr.flush()

        sys.stderr.write("\n")
        sys.stderr.flush()

    if save_q_path is not None:
        save_q_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(save_q_path, Q)
        print("saved_q:", str(save_q_path.resolve()))


def evaluate_q_table(
    *,
    env,
    Q: np.ndarray,
    episodes: int,
    seed: int,
    render_steps: str = "episode",  # none|episode|step
) -> float:
    """
    Evaluate a learned Q-table with a greedy policy.
    Returns win rate (mean episode return) for FrozenLake-like 0/1 reward tasks.
    """
    wins = 0
    rs = (render_steps or "episode").strip().lower()
    for i in range(int(episodes)):
        s, _info = env.reset(seed=int(seed) + i)
        if rs in {"step", "episode"}:
            try:
                frame0 = env.render()
                if frame0 is not None:
                    print(frame0)
            except Exception:
                pass
        done = False
        ep_ret = 0.0
        while not done:
            a = int(np.argmax(Q[int(s)]))
            s, r, terminated, truncated, _info = env.step(a)
            if rs == "step":
                try:
                    frame = env.render()
                    if frame is not None:
                        print(frame)
                except Exception:
                    pass
            done = bool(terminated) or bool(truncated)
            ep_ret += float(r)
        if rs == "episode":
            # Print final frame once per episode to reduce spam
            try:
                frame_end = env.render()
                if frame_end is not None:
                    print(frame_end)
            except Exception:
                pass
            print(f"[episode {i+1}/{episodes}] return={ep_ret}")
        if ep_ret > 0:
            wins += 1
    return float(wins) / float(max(1, int(episodes)))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Guided RL learning demo (Q-learning) on FrozenLake-v1.")
    ap.add_argument("--map-name", choices=["4x4", "8x8"], default="4x4", help="FrozenLake built-in map size.")
    ap.add_argument("--random-map", action="store_true", help="Use a randomly generated map (requires --size).")
    ap.add_argument("--size", type=int, default=0, help="Random map size (e.g. 12). Only used with --random-map.")
    ap.add_argument("--episodes", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--is-slippery", action="store_true", help="Use slippery dynamics (harder). Default: deterministic.")
    ap.add_argument(
        "--render-mode",
        choices=["none", "human", "ansi"],
        default="none",
        help='Render mode. "human" requires pygame; "ansi" prints ASCII frames (no pygame).',
    )

    ap.add_argument("--lr", type=float, default=0.2)
    ap.add_argument("--gamma", type=float, default=0.99)
    ap.add_argument("--epsilon0", type=float, default=0.3)
    ap.add_argument("--epsilon-final", type=float, default=0.05)

    ap.add_argument("--guided", action="store_true", help="Enable guided learning with a shortest-path expert.")
    ap.add_argument("--guided-mode", choices=["none", "guide_only", "mix"], default="mix")
    ap.add_argument("--alpha-guided0", type=float, default=0.7, help="Initial probability of following the guide.")
    ap.add_argument("--alpha-guided-final", type=float, default=0.0, help="Final probability (anneal to 0 to let RL take over).")

    ap.add_argument("--eval-every", type=int, default=200)
    ap.add_argument("--eval-episodes", type=int, default=200)
    ap.add_argument("--out-csv", default="results/rl_guided/frozenlake_qlearning.csv")
    ap.add_argument("--save-q", default="", help="Optional: save learned Q-table to a .npy file.")
    ap.add_argument("--load-q", default="", help="If set, skip training and evaluate this .npy Q-table.")
    ap.add_argument("--test-episodes", type=int, default=1000, help="Episodes used when evaluating a loaded Q-table.")
    ap.add_argument("--test-seed", type=int, default=123, help="Seed used when evaluating a loaded Q-table.")
    ap.add_argument(
        "--render-steps",
        choices=["none", "episode", "step"],
        default="episode",
        help='When render_mode=ansi/human, how often to render during --load-q testing. "step" can be very verbose.',
    )
    args = ap.parse_args(argv)

    gym = _try_import_gym()
    rm = str(args.render_mode).strip().lower()
    render_mode = None if rm == "none" else rm
    try:
        make_kwargs: Dict[str, object] = {"is_slippery": bool(args.is_slippery), "render_mode": render_mode}
        if bool(args.random_map):
            n = int(args.size)
            if n <= 0:
                raise SystemExit("使用 --random-map 时必须提供 --size（例如 --size 12）。")
            # generate_random_map is part of gymnasium toy_text
            try:
                from gymnasium.envs.toy_text.frozen_lake import generate_random_map  # type: ignore
            except Exception as e:
                raise SystemExit(f"无法导入 generate_random_map：{e}")
            make_kwargs["desc"] = generate_random_map(size=n, seed=int(args.seed))
        else:
            make_kwargs["map_name"] = str(args.map_name)

        env = gym.make("FrozenLake-v1", **make_kwargs)
    except Exception as e:
        # Most common: pygame missing for render_mode="human"
        if rm == "human" and ("pygame" in str(e).lower() or "DependencyNotInstalled" in str(type(e))):
            raise SystemExit(
                "渲染模式 human 需要 pygame。\n"
                "请安装其一：\n"
                '  py -3 -m pip install "gymnasium[toy-text]"\n'
                "或：\n"
                "  py -3 -m pip install pygame\n"
                "或者改用无 GUI 的渲染：--render-mode ansi\n"
            )
        raise

    load_q = str(args.load_q or "").strip()
    if load_q:
        q_path = Path(load_q)
        if not q_path.exists():
            raise SystemExit(f"--load-q 文件不存在：{q_path}")
        Q = np.load(q_path)
        # Shape check
        nS = int(env.observation_space.n)
        nA = int(env.action_space.n)
        if tuple(Q.shape) != (nS, nA):
            raise SystemExit(f"Q 表 shape 不匹配环境：Q.shape={Q.shape} env=(nS={nS}, nA={nA})。请确认 map_name/size/is_slippery 一致。")
        win_rate = evaluate_q_table(
            env=env,
            Q=Q,
            episodes=int(args.test_episodes),
            seed=int(args.test_seed),
            render_steps=str(args.render_steps),
        )
        print("=== Q-table Test ===")
        print("q_path:", str(q_path.resolve()))
        print("env: FrozenLake-v1", "map_name:", str(args.map_name), "random_map:", bool(args.random_map), "size:", int(args.size))
        print("is_slippery:", bool(args.is_slippery))
        print("render_mode:", render_mode)
        print("test_episodes:", int(args.test_episodes), "test_seed:", int(args.test_seed))
        print(f"win_rate: {win_rate:.4f}")
        return 0

    guide = build_shortest_path_guide(env) if bool(args.guided) else None

    q_learning(
        env=env,
        episodes=int(args.episodes),
        seed=int(args.seed),
        alpha_guided0=float(args.alpha_guided0) if guide is not None else 0.0,
        alpha_guided_final=float(args.alpha_guided_final) if guide is not None else 0.0,
        epsilon0=float(args.epsilon0),
        epsilon_final=float(args.epsilon_final),
        lr=float(args.lr),
        gamma=float(args.gamma),
        guide=guide,
        guided_mode=str(args.guided_mode),
        eval_every=int(args.eval_every),
        eval_episodes=int(args.eval_episodes),
        out_csv=Path(str(args.out_csv)),
        save_q_path=(Path(str(args.save_q)) if str(args.save_q).strip() else None),
    )

    print("saved_csv:", str(Path(str(args.out_csv)).resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


