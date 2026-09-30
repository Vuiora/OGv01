"""
分析轨迹质量，找出哪种轨迹可能生成最好的树
"""

import json
import numpy as np
from collections import defaultdict
from trajectary import Trajectary
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import r2_score, mean_squared_error
import sys

def load_trajectories(file_path="all_trajectories.json"):
    """加载轨迹数据"""
    print(f"加载轨迹数据: {file_path}")
    with open(file_path, "r", encoding="utf-8") as f:
        trajectories = json.load(f)
    print(f"  加载了 {len(trajectories)} 条轨迹")
    return trajectories

def analyze_by_category(trajectories):
    """按树类型分析轨迹特征"""
    print("\n" + "=" * 60)
    print("按树类型分析轨迹特征")
    print("=" * 60)
    
    categories = defaultdict(list)
    for traj in trajectories:
        category = traj.get("tree_category", "unknown")
        categories[category].append(traj)
    
    for category, trajs in sorted(categories.items()):
        lengths = [t["trajectory_length"] for t in trajs]
        retrieval_times = [t["retrieval_time"] for t in trajs]
        decision_effects = [t["decision_effect"] for t in trajs]
        
        print(f"\n{category.upper()} 树 ({len(trajs)} 条轨迹):")
        print(f"  平均决策成功率: {np.mean(decision_effects):.4f}")
        print(f"  成功率范围: {np.min(decision_effects):.4f} - {np.max(decision_effects):.4f}")
        print(f"  平均轨迹长度: {np.mean(lengths):.2f} 节点")
        print(f"  轨迹长度范围: {np.min(lengths)} - {np.max(lengths)} 节点")
        print(f"  平均检索时间: {np.mean(retrieval_times):.4f} 秒")

def analyze_by_performance(trajectories):
    """按决策成功率分析轨迹特征"""
    print("\n" + "=" * 60)
    print("按决策成功率分析轨迹特征")
    print("=" * 60)
    
    # 定义质量等级
    high_quality = [t for t in trajectories if t["decision_effect"] >= 0.70]
    medium_quality = [t for t in trajectories if 0.50 <= t["decision_effect"] < 0.70]
    low_quality = [t for t in trajectories if t["decision_effect"] < 0.50]
    
    quality_groups = [
        ("高质量 (成功率≥0.70)", high_quality),
        ("中等质量 (0.50≤成功率<0.70)", medium_quality),
        ("低质量 (成功率<0.50)", low_quality),
    ]
    
    for label, trajs in quality_groups:
        if len(trajs) == 0:
            continue
        
        lengths = [t["trajectory_length"] for t in trajs]
        retrieval_times = [t["retrieval_time"] for t in trajs]
        decision_effects = [t["decision_effect"] for t in trajs]
        
        print(f"\n{label} ({len(trajs)} 条轨迹):")
        print(f"  平均决策成功率: {np.mean(decision_effects):.4f}")
        print(f"  平均轨迹长度: {np.mean(lengths):.2f} 节点")
        print(f"  平均检索时间: {np.mean(retrieval_times):.4f} 秒")
        
        # 分析树类型分布
        category_counts = defaultdict(int)
        for t in trajs:
            category = t.get("tree_category", "unknown")
            category_counts[category] += 1
        
        print(f"  树类型分布: {dict(category_counts)}")

def predict_trajectory_quality(trajectories, method='full'):
    """使用机器学习预测轨迹质量"""
    print("\n" + "=" * 60)
    print("使用机器学习预测轨迹质量")
    print("=" * 60)
    
    try:
        # 准备轨迹数据 - 提取统计特征以统一维度
        all_features = []
        
        for traj in trajectories:
            # 提取统计特征
            tendency = np.array(traj["tendency"], dtype=float)
            sequence = np.array(traj["sequence"], dtype=float)
            selection = np.array(traj["selection"])
            
            # 对 selection 进行编码（使用简单的哈希编码）
            selection_encoded = np.array([hash(str(s)) % 1000 for s in selection], dtype=float)
            
            # 提取统计特征（均值、标准差、最小值、最大值、中位数）
            features = []
            
            # tendency 统计特征
            features.extend([
                np.mean(tendency), np.std(tendency), np.min(tendency), 
                np.max(tendency), np.median(tendency)
            ])
            
            # sequence 统计特征
            features.extend([
                np.mean(sequence), np.std(sequence), np.min(sequence), 
                np.max(sequence), np.median(sequence)
            ])
            
            # selection 统计特征
            features.extend([
                np.mean(selection_encoded), np.std(selection_encoded), 
                np.min(selection_encoded), np.max(selection_encoded), 
                np.median(selection_encoded)
            ])
            
            # 轨迹长度和检索时间
            features.append(traj.get("trajectory_length", len(tendency)))
            features.append(traj.get("retrieval_time", 0.0))
            
            all_features.append(features)
        
        # 转换为特征矩阵
        X = np.array(all_features)
        
        # 准备标签
        y = np.array([t["decision_effect"] for t in trajectories])
        
        print(f"特征维度: {X.shape}")
        print(f"标签范围: {y.min():.4f} - {y.max():.4f}")
        
        # 训练预测模型
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42
        )
        
        model = RandomForestRegressor(n_estimators=100, random_state=42, n_jobs=-1)
        model.fit(X_train, y_train)
        
        # 评估模型
        y_pred_train = model.predict(X_train)
        y_pred_test = model.predict(X_test)
        
        r2_train = r2_score(y_train, y_pred_train)
        r2_test = r2_score(y_test, y_pred_test)
        mse_test = mean_squared_error(y_test, y_pred_test)
        
        print(f"\n模型性能:")
        print(f"  训练集 R2: {r2_train:.4f}")
        print(f"  测试集 R2: {r2_test:.4f}")
        print(f"  测试集 MSE: {mse_test:.4f}")
        
        # 找出可能生成最好树的轨迹
        predictions = model.predict(X)
        best_indices = np.argsort(predictions)[-100:]  # 前100个
        best_trajectories = [trajectories[i] for i in best_indices]
        
        print(f"\n预测可能生成最好树的轨迹（前100个）:")
        print(f"  平均预测成功率: {np.mean(predictions[best_indices]):.4f}")
        print(f"  实际平均成功率: {np.mean([t['decision_effect'] for t in best_trajectories]):.4f}")
        print(f"  预测成功率范围: {np.min(predictions[best_indices]):.4f} - {np.max(predictions[best_indices]):.4f}")
        
        # 分析这些轨迹的特征
        best_lengths = [t["trajectory_length"] for t in best_trajectories]
        best_retrieval_times = [t["retrieval_time"] for t in best_trajectories]
        
        print(f"\n这些轨迹的特征:")
        print(f"  平均轨迹长度: {np.mean(best_lengths):.2f} 节点")
        print(f"  轨迹长度范围: {np.min(best_lengths)} - {np.max(best_lengths)} 节点")
        print(f"  平均检索时间: {np.mean(best_retrieval_times):.4f} 秒")
        
        # 分析树类型分布
        category_counts = defaultdict(int)
        for t in best_trajectories:
            category = t.get("tree_category", "unknown")
            category_counts[category] += 1
        
        print(f"  树类型分布: {dict(category_counts)}")
        
        # 特征重要性
        feature_importance = model.feature_importances_
        top_features = np.argsort(feature_importance)[-10:][::-1]
        
        print(f"\n最重要的10个特征:")
        for i, idx in enumerate(top_features, 1):
            print(f"  特征 {idx}: 重要性 = {feature_importance[idx]:.4f}")
        
        return model, predictions, best_indices
        
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()
        return None, None, None

def analyze_trajectory_patterns(trajectories):
    """分析轨迹模式"""
    print("\n" + "=" * 60)
    print("分析轨迹模式")
    print("=" * 60)
    
    # 按轨迹长度分组
    length_groups = defaultdict(list)
    for traj in trajectories:
        length = traj["trajectory_length"]
        # 分组：短(2-5), 中(6-15), 长(16-25), 很长(26+)
        if length <= 5:
            group = "短(2-5)"
        elif length <= 15:
            group = "中(6-15)"
        elif length <= 25:
            group = "长(16-25)"
        else:
            group = "很长(26+)"
        length_groups[group].append(traj)
    
    print("\n按轨迹长度分组:")
    for group, trajs in sorted(length_groups.items()):
        decision_effects = [t["decision_effect"] for t in trajs]
        print(f"  {group} ({len(trajs)} 条轨迹):")
        print(f"    平均决策成功率: {np.mean(decision_effects):.4f}")
        print(f"    成功率范围: {np.min(decision_effects):.4f} - {np.max(decision_effects):.4f}")

def main():
    """主函数"""
    print("=" * 60)
    print("轨迹质量分析")
    print("=" * 60)
    
    # 加载轨迹数据（优先使用完整轨迹文件）
    import os
    if os.path.exists("all_trajectories.json"):
        trajectories = load_trajectories("all_trajectories.json")
    else:
        print("警告: 未找到 all_trajectories.json，使用 sample_trajectories.json")
        trajectories = load_trajectories("sample_trajectories.json")
    
    if len(trajectories) == 0:
        print("错误: 没有加载到轨迹数据")
        return
    
    # 1. 按树类型分析
    analyze_by_category(trajectories)
    
    # 2. 按决策成功率分析
    analyze_by_performance(trajectories)
    
    # 3. 分析轨迹模式
    analyze_trajectory_patterns(trajectories)
    
    # 4. 使用机器学习预测
    model, predictions, best_indices = predict_trajectory_quality(trajectories)
    
    # 5. 总结
    print("\n" + "=" * 60)
    print("总结")
    print("=" * 60)
    
    if best_indices is not None:
        best_trajectories = [trajectories[i] for i in best_indices[:10]]
        print("\n预测可能生成最好树的轨迹（前10个）:")
        for i, traj in enumerate(best_trajectories, 1):
            print(f"  {i}. 树: {traj.get('tree_name', '未知')}, "
                  f"成功率: {traj['decision_effect']:.4f}, "
                  f"长度: {traj['trajectory_length']}, "
                  f"类型: {traj.get('tree_category', 'unknown')}")
    
    print("\n分析完成！")
    print("可以使用这些信息来筛选高质量轨迹，用于生成更好的决策树。")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n用户中断")
        sys.exit(0)
    except Exception as e:
        print(f"\n错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

