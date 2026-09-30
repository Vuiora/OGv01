"""
轨迹标签功能的单元测试（pytest）。

验证每条轨迹都有：检索时间、轨迹长度、决策效果。
"""

from mtbmt.trajectory_legacy import Trajectary


def test_labels_preserved_when_present():
    d = {
        "tendency": [0.8, 0.6, 0.9, 0.7, 0.85],
        "sequence": [1, 2, 3, 2, 4],
        "selection": ["left", "right", "left", "right", "left"],
        "retrieval_time": 0.125,
        "trajectory_length": 5,
        "decision_effect": 0.92,
    }
    traj = Trajectary(file_name="", trajectory_dict=d)
    labels = traj.get_trajectory_labels()
    assert labels["retrieval_time"] == 0.125
    assert labels["trajectory_length"] == 5
    assert labels["decision_effect"] == 0.92


def test_labels_auto_filled_when_missing():
    d = {
        "tendency": [0.8, 0.6, 0.9],
        "sequence": [1, 2, 3],
        "selection": ["left", "right", "left"],
    }
    traj = Trajectary(file_name="", trajectory_dict=d)
    labels = traj.get_trajectory_labels()
    assert labels["retrieval_time"] == 0.0
    assert labels["decision_effect"] == 0.0
    # trajectory_length should be derived from tendency length
    assert labels["trajectory_length"] == 3


def test_labels_can_be_set_manually():
    d = {
        "tendency": [0.8, 0.6, 0.9, 0.7],
        "sequence": [1, 2, 3, 2],
        "selection": ["left", "right", "left", "right"],
    }
    traj = Trajectary(file_name="", trajectory_dict=d)
    traj.set_trajectory_labels(retrieval_time=0.15, trajectory_length=4, decision_effect=0.88)
    labels = traj.get_trajectory_labels()
    assert labels["retrieval_time"] == 0.15
    assert labels["trajectory_length"] == 4
    assert labels["decision_effect"] == 0.88


def test_supervised_learning_output_has_expected_shape():
    d = {
        "tendency": [0.8, 0.6, 0.9, 0.7, 0.85],
        "sequence": [1, 2, 3, 2, 4],
        "selection": ["left", "right", "left", "right", "left"],
        "retrieval_time": 0.125,
        "trajectory_length": 5,
        "decision_effect": 0.92,
    }
    traj = Trajectary(file_name="", trajectory_dict=d)
    out = traj.trajectory_supervised_learning()
    assert out["features"].shape == (5, 12)
    assert out["labels"]["trajectory_length"] == 5
