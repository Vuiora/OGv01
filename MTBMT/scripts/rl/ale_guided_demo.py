from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from time import perf_counter
from typing import List, Optional, Tuple


def _ensure_utf8_console() -> None:
    """
    Best-effort fix for Windows console mojibake.
    PowerShell/cmd may not be UTF-8 by default, so Chinese output can get garbled.
    """
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        # If we can't reconfigure, just continue.
        pass


def _try_import_gym() -> "object":
    try:
        import gymnasium as gym  # type: ignore

        return gym
    except Exception as e:  # pragma: no cover
        raise SystemExit(
            "未安装 ALE/Gymnasium 相关依赖，无法运行该示例。\n"
            "请先执行（建议用虚拟环境）：\n"
            '  py -3 -m pip install "gymnasium[atari]" ale-py\n'
            f"原始错误：{e}"
        )


def _action_meanings(env) -> List[str]:
    # Gymnasium ALE envs typically expose this.
    try:
        return list(env.unwrapped.get_action_meanings())
    except Exception:
        try:
            return list(env.get_action_meanings())
        except Exception:
            return []


def _find_action(meanings: List[str], target: str) -> Optional[int]:
    target = target.strip().upper()
    for i, m in enumerate(meanings):
        if str(m).strip().upper() == target:
            return int(i)
    return None


def _minimal_action_set(env) -> Optional[List[int]]:
    """
    Try to fetch ALE minimal action set. This reduces the discrete action space and is a common
    "guided exploration" trick on Atari.
    """
    # Many envs expose env.unwrapped.ale.getMinimalActionSet()
    try:
        aset = env.unwrapped.ale.getMinimalActionSet()
        return [int(a) for a in list(aset)]
    except Exception:
        return None


def _parse_action_list(s: str) -> List[str]:
    return [x.strip().lower() for x in (s or "").split(",") if x.strip()]


def _resolve_actions(env, action_names: List[str]) -> List[int]:
    meanings = _action_meanings(env)
    if not meanings:
        raise SystemExit("该环境未暴露 action meanings，无法用动作名称解析（请换环境或改用 random）。")
    out: List[int] = []
    for name in action_names:
        a = _find_action(meanings, str(name).upper())
        if a is None:
            raise SystemExit(f"找不到动作 {name!r}，可用 action_meanings={meanings}")
        out.append(int(a))
    uniq: List[int] = []
    seen = set()
    for a in out:
        if a not in seen:
            uniq.append(int(a))
            seen.add(int(a))
    return uniq


def _get_ram(env) -> Optional[object]:
    try:
        return env.unwrapped.ale.getRAM()
    except Exception:
        return None


@dataclass
class EpisodeStat:
    ep_return: float
    ep_len: int


def _run_one_episode(
    env,
    *,
    policy: str,
    alpha: float,
    guide: str,
    guide_mode: str,
    guide_actions: List[int],
    minimal_set: Optional[List[int]],
    ram_ball_x: Optional[int],
    ram_paddle_x: Optional[int],
    ram_deadzone: float,
) -> EpisodeStat:
    obs, info = env.reset()
    done = False
    ep_ret = 0.0
    ep_len = 0

    rng = getattr(env, "np_random", None)
    if rng is None:
        import numpy as np

        rng = np.random.default_rng(0)

    while not done:
        # base action
        if policy == "random":
            a_base = int(env.action_space.sample())
        elif policy == "minimal_random":
            if minimal_set:
                a_base = int(minimal_set[int(rng.integers(0, len(minimal_set)))])
            else:
                a_base = int(env.action_space.sample())
        else:
            a_base = int(env.action_space.sample())

        # guided mixture: with prob alpha override by guided action(s)
        a = a_base
        if guide_actions and float(alpha) > 0.0:
            if float(rng.random()) < float(alpha):
                gm = (guide_mode or "fixed").strip().lower()
                if gm == "set":
                    a = int(guide_actions[int(rng.integers(0, len(guide_actions)))])
                elif gm == "ram_follow_x":
                    # Choose LEFT/RIGHT based on RAM ball_x vs paddle_x
                    if ram_ball_x is None or ram_paddle_x is None:
                        a = int(guide_actions[0])
                    else:
                        ram = _get_ram(env)
                        if ram is None:
                            a = int(guide_actions[0])
                        else:
                            bx = float(ram[int(ram_ball_x)])
                            px = float(ram[int(ram_paddle_x)])
                            dz = float(max(0.0, ram_deadzone))
                            meanings = _action_meanings(env)
                            left = _find_action(meanings, "LEFT") if meanings else None
                            right = _find_action(meanings, "RIGHT") if meanings else None
                            if left is None or right is None:
                                a = int(guide_actions[0])
                            else:
                                if bx < px - dz:
                                    a = int(left)
                                elif bx > px + dz:
                                    a = int(right)
                                else:
                                    a = int(guide_actions[0])
                else:
                    # fixed
                    a = int(guide_actions[0])

        obs, r, terminated, truncated, info = env.step(a)
        done = bool(terminated) or bool(truncated)
        ep_ret += float(r)
        ep_len += 1

    return EpisodeStat(ep_return=float(ep_ret), ep_len=int(ep_len))


def evaluate(
    *,
    env_id: str,
    episodes: int,
    seed: int,
    policy: str,
    alpha: float,
    guide: str,
    guide_mode: str,
    obs_type: str,
    ram_ball_x: Optional[int],
    ram_paddle_x: Optional[int],
    ram_deadzone: float,
    render: bool,
) -> Tuple[float, float]:
    gym = _try_import_gym()
    render_mode = "human" if bool(render) else None
    # `ALE/*` envs are registered by `ale-py` at import time (not by gymnasium itself).
    # Without this import, `gym.make("ALE/...")` will raise "Namespace ALE not found".
    if str(env_id).startswith("ALE/"):
        try:
            import ale_py  # noqa: F401
        except Exception as e:
            raise SystemExit(
                "你选择了 `ALE/*` 环境，但当前环境无法导入 `ale-py`。\n"
                "请先安装 ale-py（Windows/Python 3.14 可能需要源码编译并准备 ZLIB）：\n"
                "  py -3 -m pip install ale-py\n"
                f"原始错误：{e}"
            )
    make_kwargs = {"render_mode": render_mode}
    ot = (obs_type or "").strip().lower()
    if ot in {"ram", "rgb"}:
        make_kwargs["obs_type"] = ot
    try:
        env = gym.make(str(env_id), **make_kwargs)
    except Exception as e:
        msg = str(e)
        # Common after `ale-py` is installed: ROMs are not installed yet.
        if "ale_py" in msg and ("roms" in msg or msg.lower().endswith(".bin'")) and ("No such file" in msg or "Errno 2" in msg):
            raise SystemExit(
                "已安装 `ale-py`，但未找到对应 Atari ROM 文件（需要下载并接受 ROM 许可）。\n"
                "\n"
                "请执行：\n"
                "  py -3 -m pip install \"autorom[accept-rom-license]\"\n"
                "  py -3 -m AutoROM --accept-license\n"
                "\n"
                "执行完成后再重试本脚本。\n"
                f"原始错误：{e}"
            )

        # Most common in fresh installs: ALE namespace missing because `ale-py` isn't imported/installed.
        if "NamespaceNotFound" in msg or "Namespace ALE not found" in msg:
            pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
            raise SystemExit(
                "未找到 Gymnasium 的 ALE/Atari 环境注册信息（例如 ALE/Freeway-v5）。\n"
                "这通常意味着你还没装 Atari/ALE 依赖（`gymnasium[atari]` + `ale-py`）。\n"
                "\n"
                "可选解决方案：\n"
                f"1) 继续使用当前 Python {pyver}：需要从源码编译 `ale-py`，并安装系统依赖（Windows 下常见缺 ZLIB）。\n"
                "2) 推荐：换 Python 3.11/3.12（更容易拿到 `ale-py` 预编译 wheel），然后安装：\n"
                '   python -m pip install "gymnasium[atari]" ale-py\n'
                "\n"
                "如果你只是想跑通“guided mixture”的逻辑而不依赖 Atari，可以改用非 ALE 环境（例如 CartPole-v1）。\n"
                f"原始错误：{e}"
            )
        raise
    env.reset(seed=int(seed))

    meanings = _action_meanings(env)
    minimal_set = _minimal_action_set(env)

    guide_names = _parse_action_list(guide)
    if not guide_names or (len(guide_names) == 1 and guide_names[0] == "none"):
        guide_actions: List[int] = []
    else:
        guide_actions = _resolve_actions(env, guide_names)

    stats: List[EpisodeStat] = []
    t0 = perf_counter()
    for i in range(int(episodes)):
        st = _run_one_episode(
            env,
            policy=str(policy),
            alpha=float(alpha),
            guide=str(guide),
            guide_mode=str(guide_mode),
            guide_actions=guide_actions,
            minimal_set=minimal_set,
            ram_ball_x=ram_ball_x,
            ram_paddle_x=ram_paddle_x,
            ram_deadzone=float(ram_deadzone),
        )
        stats.append(st)
        if (i + 1) % max(1, int(episodes // 5)) == 0:
            sys.stderr.write(f"\r[progress] {i+1}/{episodes} episodes")
            sys.stderr.flush()
    sys.stderr.write("\n")
    sys.stderr.flush()

    dt = perf_counter() - t0
    rets = [s.ep_return for s in stats]
    mean_ret = sum(rets) / max(1, len(rets))
    mean_len = sum(s.ep_len for s in stats) / max(1, len(stats))

    print("=== ALE Guided Demo ===")
    print("env_id:", env_id)
    print("policy:", policy)
    print("guide :", guide, "guide_mode:", guide_mode, "alpha:", alpha, "obs_type:", obs_type)
    print("episodes:", episodes, "seed:", seed)
    print("action_meanings:", meanings)
    print("minimal_action_set:", minimal_set if minimal_set is not None else "N/A")
    ram = _get_ram(env)
    if ram is not None:
        try:
            import numpy as np

            ram_preview = list(np.asarray(ram, dtype=int)[:16])
        except Exception:
            ram_preview = []
        print("ram_available: True  ram_preview(first16):", ram_preview)
    else:
        print("ram_available: False")
    print(f"mean_return: {mean_ret:.4f}")
    print(f"mean_ep_len: {mean_len:.2f}")
    print(f"time_sec   : {dt:.2f}")

    env.close()
    return float(mean_ret), float(mean_len)


def main(argv: Optional[List[str]] = None) -> int:
    _ensure_utf8_console()
    ap = argparse.ArgumentParser(
        description="Minimal ALE demo: show how a 'guided' action prior (alpha-mixture) can influence decisions."
    )
    ap.add_argument("--env-id", default="ALE/Freeway-v5", help="Gymnasium ALE env id, e.g. ALE/Freeway-v5, ALE/Breakout-v5")
    ap.add_argument("--episodes", type=int, default=10)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--policy", choices=["random", "minimal_random"], default="random")
    ap.add_argument(
        "--guide",
        type=str,
        default="up",
        help='Comma-separated guided actions by name, e.g. "fire", "left,right", "fire,left,right", or "none".',
    )
    ap.add_argument(
        "--guide-mode",
        choices=["fixed", "set", "ram_follow_x"],
        default="fixed",
        help="fixed=always use first guided action; set=random pick from set; ram_follow_x=use RAM ball_x vs paddle_x to choose left/right.",
    )
    ap.add_argument("--obs-type", choices=["rgb", "ram"], default="rgb", help="Observation type requested from Gymnasium ALE.")
    ap.add_argument("--ram-ball-x", type=int, default=None, help="RAM index for ball x (needed for --guide-mode ram_follow_x).")
    ap.add_argument("--ram-paddle-x", type=int, default=None, help="RAM index for paddle x (needed for --guide-mode ram_follow_x).")
    ap.add_argument("--ram-deadzone", type=float, default=2.0, help="Deadzone in RAM pixels for follow_x (avoid jitter).")
    ap.add_argument("--alpha", type=float, default=0.7, help="probability of overriding base action by guide action")
    ap.add_argument("--render", action="store_true")
    args = ap.parse_args(argv)

    if not (0.0 <= float(args.alpha) <= 1.0):
        raise SystemExit("--alpha 必须在 [0,1] 内")

    evaluate(
        env_id=str(args.env_id),
        episodes=int(args.episodes),
        seed=int(args.seed),
        policy=str(args.policy),
        alpha=float(args.alpha),
        guide=str(args.guide),
        guide_mode=str(args.guide_mode),
        obs_type=str(args.obs_type),
        ram_ball_x=args.ram_ball_x,
        ram_paddle_x=args.ram_paddle_x,
        ram_deadzone=float(args.ram_deadzone),
        render=bool(args.render),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


