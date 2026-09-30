"""
轨迹特征提取的单元测试（pytest）。

这些测试只验证：
- 输出维度是否符合预期
- 原始三维信息（tendency/sequence/selection）是否被保留在前 3 列
"""

import numpy as np

from mtbmt.trajectory_legacy import Trajectary


def test_trajectory_valulization_shapes_and_raw_columns():
    d = {
        "tendency": [0.8, 0.6, 0.9, 0.7, 0.85],
        "sequence": [1, 2, 3, 2, 4],
        "selection": ["left", "right", "left", "right", "left"],
    }
    traj = Trajectary(file_name="", trajectory_dict=d)

    f1 = traj.trajectory_valulization(d, method="concatenate")
    assert f1.shape == (5, 3)
    assert np.allclose(f1[:, 0], np.asarray(d["tendency"], dtype=float))
    assert np.allclose(f1[:, 1], np.asarray(d["sequence"], dtype=float))
    # LabelEncoder: left=0, right=1
    assert np.array_equal(f1[:, 2].astype(int), np.asarray([0, 1, 0, 1, 0], dtype=int))

    f2 = traj.trajectory_valulization(d, method="with_interaction")
    assert f2.shape == (5, 6)
    assert np.allclose(f2[:, :3], f1)

    f3 = traj.trajectory_valulization(d, method="with_statistics")
    assert f3.shape == (5, 9)
    assert np.allclose(f3[:, :3], f1)

    f4 = traj.trajectory_valulization(d, method="full")
    assert f4.shape == (5, 12)
    assert np.allclose(f4[:, :3], f1)
