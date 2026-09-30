"""
生成复杂决策树并提取轨迹数据

核心功能：
1. 生成多个决策树（分布学习模型），包括好的、过拟合的、欠拟合的、分类不合格的
2. 从每个决策树中提取算法轨迹（算法状态序列）
3. 记录每个轨迹的经验特征（决策效果、检索时间、轨迹长度）
4. 为倾向学习算法提供数据支持

概念对应：
- 算法状态：决策树中的每个节点
- 算法轨迹：从根节点到叶子节点的状态序列（sequence）
- 倾向：每个节点的倾向值（tendency），反映轨迹变化方向
- 分布学习模型：各个独立的决策树
- 中心学习模型：Trajectary类整合所有经验
- 目标倾向：决策效果（decision_effect），用于算法指导

从sklearn决策树中提取轨迹信息，转换为trajectary.py可识别的格式
"""
import numpy as np
import time
from sklearn.datasets import make_classification
from sklearn.tree import DecisionTreeClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score


class DecisionTreeTrajectoryExtractor:
    """
    从sklearn决策树中提取算法轨迹数据
    
    核心功能：
    - 提取算法状态序列（sequence）：从根节点到叶子节点的完整路径
    - 计算倾向值（tendency）：每个状态的倾向值，反映轨迹变化方向
    - 记录选择路径（selection）：每个状态的选择（left/right/leaf）
    
    概念对应：
    - 算法状态：决策树中的每个节点
    - 算法轨迹：状态序列（sequence）
    - 倾向：倾向值（tendency）
    """
    
    def __init__(self, clf: DecisionTreeClassifier):
        """
        Args:
            clf: 训练好的决策树分类器
        """
        self.clf = clf
        self.tree = clf.tree_
    
    def extract_trajectory(self, sample: np.array) -> dict:
        """
        为单个样本提取算法轨迹
        
        提取算法在处理该样本时的完整状态序列，包括：
        - 算法状态序列（sequence）：按时序排列的状态
        - 倾向值（tendency）：每个状态的倾向值，反映轨迹变化方向
        - 选择路径（selection）：每个状态的选择
        
        Args:
            sample: 单个样本的特征向量 (n_features,)
        
        Returns:
            dict: 包含轨迹信息的字典
                {
                    "tendency": [...],      # 倾向值（轨迹变化方向）
                    "sequence": [...],      # 算法状态序列（按时序排列）
                    "selection": [...]      # 选择路径（"left"/"right"/"leaf"）
                }
        """
        tendency = []
        sequence = []
        selection = []
        
        # 从根节点开始遍历
        node_id = 0
        depth = 0
        
        while True:
            # 获取当前节点信息
            left_child = self.tree.children_left[node_id]
            right_child = self.tree.children_right[node_id]
            
            # 计算当前节点的倾向值（使用不纯度）
            impurity = self.tree.impurity[node_id]
            n_samples = self.tree.n_node_samples[node_id]
            
            # tendency: 使用加权不纯度作为倾向值
            tendency_value = impurity * (n_samples / self.tree.n_node_samples[0])
            tendency.append(tendency_value)
            
            # sequence: 使用节点深度
            sequence.append(depth)
            
            # 如果是叶子节点，结束
            if left_child == right_child:  # 叶子节点
                selection.append("leaf")
                break
            
            # 获取分裂特征和阈值
            feature = self.tree.feature[node_id]
            threshold = self.tree.threshold[node_id]
            
            # 根据特征值选择路径
            if sample[feature] <= threshold:
                selection.append("left")
                node_id = left_child
            else:
                selection.append("right")
                node_id = right_child
            
            depth += 1
        
        return {
            "tendency": tendency,
            "sequence": sequence,
            "selection": selection
        }
    
    def extract_trajectories_batch(self, X: np.array, y: np.array = None) -> list:
        """
        批量提取轨迹
        
        Args:
            X: 特征矩阵 (n_samples, n_features)
            y: 标签（可选，用于计算决策效果）
        
        Returns:
            list: 轨迹字典列表
        """
        trajectories = []
        
        for i, sample in enumerate(X):
            trajectory = self.extract_trajectory(sample)
            
            # 如果有标签，添加预测结果
            if y is not None:
                prediction = self.clf.predict([sample])[0]
                is_correct = (prediction == y[i])
                trajectory["prediction"] = prediction
                trajectory["is_correct"] = is_correct
            
            trajectories.append(trajectory)
        
        return trajectories


def generate_multiple_decision_trees(use_validation_set: bool = True, 
                                     n_trees: int = 10):
    """
    生成多个分布学习模型（决策树），用于倾向学习算法
    
    生成多种类型的决策树，包括：
    - 好的树（good）：合理的参数配置，作为对比基准
    - 过拟合的树（overfit）：训练准确率高但测试准确率低
    - 欠拟合的树（underfit）：训练和测试准确率都低
    - 分类不合格的树（poor）：测试准确率很低
    
    每个分布学习模型都会：
    1. 在训练集上训练
    2. 在测试集上评估决策效果（目标倾向）
    3. 记录经验特征（过拟合差距、树类型等）
    
    这些经验将被传递给中心学习模型（Trajectary），用于学习轨迹模式。
    
    Args:
        use_validation_set: 是否使用三部分数据划分（训练/验证/测试）
        n_trees: 生成的决策树数量（分布学习模型数量）
    
    Returns:
        list: 决策树列表，每个元素为 (clf, test_accuracy, tree_info)
            - clf: 训练好的决策树分类器
            - test_accuracy: 测试准确率（目标倾向/决策效果）
            - tree_info: 包含树类型、参数、评估指标等信息
    """
    print("=" * 60)
    print(f"生成 {n_trees} 个不同参数的决策树")
    print("=" * 60)
    
    # 生成数据集
    print("\n生成数据集...")
    X, y = make_classification(
        n_samples=100000,
        n_features=30,
        n_informative=15,
        n_redundant=5,
        n_classes=5,
        random_state=42
    )
    
    print(f"数据集形状: X={X.shape}, y={y.shape}")
    
    # 数据划分
    if use_validation_set:
        X_train, X_temp, y_train, y_temp = train_test_split(
            X, y, test_size=0.3, random_state=42
        )
        X_val, X_test, y_val, y_test = train_test_split(
            X_temp, y_temp, test_size=0.5, random_state=42
        )
        print(f"\n数据划分: 训练集={X_train.shape[0]}, 验证集={X_val.shape[0]}, 测试集={X_test.shape[0]}")
    else:
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42
        )
        X_val, y_val = X_test, y_test  # 向后兼容
        print(f"\n数据划分: 训练集={X_train.shape[0]}, 测试集={X_test.shape[0]}")
    
    # 定义不同的决策树参数组合
    # 重点：生成所有决策效率低下的树（过拟合、欠拟合、分类不合格）
    tree_configs = [
        # ========== 必要的决策树（合理的参数，作为对比基准）==========
        {"max_depth": 20, "min_samples_split": 5, "min_samples_leaf": 2, "criterion": "gini", "name": "必要-基准", "category": "good"},
        {"max_depth": 20, "min_samples_split": 10, "min_samples_leaf": 5, "criterion": "gini", "name": "必要-粗分裂", "category": "good"},
        
        # ========== 欠拟合的树（分类不合格，测试准确率低）==========
        {"max_depth": 1, "min_samples_split": 5, "min_samples_leaf": 2, "criterion": "gini", "name": "欠拟合-单层", "category": "underfit"},
        {"max_depth": 2, "min_samples_split": 5, "min_samples_leaf": 2, "criterion": "gini", "name": "欠拟合-2层", "category": "underfit"},
        {"max_depth": 3, "min_samples_split": 5, "min_samples_leaf": 2, "criterion": "gini", "name": "欠拟合-3层", "category": "underfit"},
        {"max_depth": 4, "min_samples_split": 5, "min_samples_leaf": 2, "criterion": "gini", "name": "欠拟合-4层", "category": "underfit"},
        {"max_depth": 5, "min_samples_split": 5, "min_samples_leaf": 2, "criterion": "gini", "name": "欠拟合-5层", "category": "underfit"},
        
        # 欠拟合：过于保守的分裂策略
        {"max_depth": 20, "min_samples_split": 50, "min_samples_leaf": 25, "criterion": "gini", "name": "欠拟合-保守分裂1", "category": "underfit"},
        {"max_depth": 20, "min_samples_split": 100, "min_samples_leaf": 50, "criterion": "gini", "name": "欠拟合-保守分裂2", "category": "underfit"},
        {"max_depth": 20, "min_samples_split": 200, "min_samples_leaf": 100, "criterion": "gini", "name": "欠拟合-保守分裂3", "category": "underfit"},
        {"max_depth": 20, "min_samples_split": 500, "min_samples_leaf": 250, "criterion": "gini", "name": "欠拟合-保守分裂4", "category": "underfit"},
        {"max_depth": 20, "min_samples_split": 1000, "min_samples_leaf": 500, "criterion": "gini", "name": "欠拟合-保守分裂5", "category": "underfit"},
        
        # ========== 过拟合的树（训练准确率高但测试准确率低）==========
        # 过拟合：过深的树
        {"max_depth": 100, "min_samples_split": 2, "min_samples_leaf": 1, "criterion": "gini", "name": "过拟合-过深1", "category": "overfit"},
        {"max_depth": 200, "min_samples_split": 2, "min_samples_leaf": 1, "criterion": "gini", "name": "过拟合-过深2", "category": "overfit"},
        {"max_depth": None, "min_samples_split": 2, "min_samples_leaf": 1, "criterion": "gini", "name": "过拟合-无限制深度", "category": "overfit"},
        
        # 过拟合：过于精细的分裂
        {"max_depth": 50, "min_samples_split": 2, "min_samples_leaf": 1, "criterion": "gini", "name": "过拟合-精细分裂1", "category": "overfit"},
        {"max_depth": 50, "min_samples_split": 2, "min_samples_leaf": 1, "criterion": "gini", "name": "过拟合-精细分裂2", "category": "overfit"},
        {"max_depth": 30, "min_samples_split": 2, "min_samples_leaf": 1, "criterion": "gini", "name": "过拟合-精细分裂3", "category": "overfit"},
        
        # ========== 分类不合格的树（测试准确率很低）==========
        # 极端欠拟合导致分类不合格
        {"max_depth": 1, "min_samples_split": 1000, "min_samples_leaf": 500, "criterion": "gini", "name": "不合格-极端欠拟合1", "category": "poor"},
        {"max_depth": 1, "min_samples_split": 2000, "min_samples_leaf": 1000, "criterion": "gini", "name": "不合格-极端欠拟合2", "category": "poor"},
        
        # 随机性导致的分类不合格（使用不同的随机种子但参数不合理）
        {"max_depth": 2, "min_samples_split": 100, "min_samples_leaf": 50, "criterion": "gini", "name": "不合格-浅且保守", "category": "poor"},
        {"max_depth": 3, "min_samples_split": 200, "min_samples_leaf": 100, "criterion": "gini", "name": "不合格-浅且保守2", "category": "poor"},
    ]
    
    # 如果需要的树数量超过配置数量，重复使用配置
    if n_trees > len(tree_configs):
        # 扩展配置列表
        base_configs = tree_configs.copy()
        for i in range(n_trees - len(tree_configs)):
            config = base_configs[i % len(base_configs)].copy()
            config["name"] = f"{config['name']}-变体{i+1}"
            config["max_depth"] = config["max_depth"] + (i % 5) * 2  # 变化深度
            tree_configs.append(config)
    else:
        tree_configs = tree_configs[:n_trees]
    
    print(f"\n生成 {len(tree_configs)} 个决策树...")
    
    trees = []
    for i, config in enumerate(tree_configs):
        print(f"\n[{i+1}/{len(tree_configs)}] 训练决策树: {config['name']}")
        print(f"  参数: max_depth={config['max_depth']}, "
              f"min_samples_split={config['min_samples_split']}, "
              f"min_samples_leaf={config['min_samples_leaf']}, "
              f"criterion={config['criterion']}")
        
        # 创建决策树
        clf = DecisionTreeClassifier(
            max_depth=config["max_depth"],
            min_samples_split=config["min_samples_split"],
            min_samples_leaf=config["min_samples_leaf"],
            criterion=config["criterion"],
            random_state=42 + i  # 不同的随机种子
        )
        
        # 训练
        start_time = time.time()
        clf.fit(X_train, y_train)
        training_time = time.time() - start_time
        
        # 评估（在测试集上评估决策成功率）
        train_score = clf.score(X_train, y_train)
        test_score = clf.score(X_test, y_test)
        
        # 计算过拟合/欠拟合指标
        overfitting_gap = train_score - test_score  # 训练和测试的差距
        underfitting_score = test_score  # 测试准确率低表示欠拟合
        
        # 判断树的类型
        category = config.get("category", "unknown")
        if category == "unknown":
            # 自动判断
            if overfitting_gap > 0.15:  # 训练和测试差距大
                category = "overfit"
            elif test_score < 0.5:  # 测试准确率很低
                category = "poor"  # 分类不合格
            elif test_score < 0.6:  # 测试准确率较低
                category = "underfit"
            else:
                category = "good"
        
        # 判断是否分类不合格
        is_poor_performer = test_score < 0.5  # 测试准确率低于50%视为不合格
        
        # 判断是否过拟合
        is_overfitting = overfitting_gap > 0.15 and train_score > 0.9
        
        # 判断是否欠拟合
        is_underfitting = test_score < 0.6 and train_score < 0.7
        
        tree_info = {
            "name": config["name"],
            "category": category,  # good, overfit, underfit, poor
            "config": config,
            "training_time": training_time,
            "train_accuracy": train_score,
            "test_accuracy": test_score,
            "decision_success_rate": test_score,  # 决策成功率 = 测试准确率
            "overfitting_gap": overfitting_gap,  # 过拟合差距
            "is_overfitting": is_overfitting,
            "is_underfitting": is_underfitting,
            "is_poor_performer": is_poor_performer,
            "tree_depth": clf.tree_.max_depth,
            "tree_nodes": clf.tree_.node_count,
            "tree_leaves": clf.tree_.n_leaves
        }
        
        # 打印详细信息
        print(f"  训练时间: {training_time:.4f}秒")
        print(f"  训练准确率: {train_score:.4f}")
        print(f"  测试准确率(决策成功率): {test_score:.4f} ← 作为决策效果")
        print(f"  过拟合差距: {overfitting_gap:.4f}")
        print(f"  树类型: {category}")
        print(f"  分类不合格: {'是' if is_poor_performer else '否'}")
        print(f"  过拟合: {'是' if is_overfitting else '否'}")
        print(f"  欠拟合: {'是' if is_underfitting else '否'}")
        print(f"  树深度: {clf.tree_.max_depth}, 节点数: {clf.tree_.node_count}")
        
        trees.append((clf, test_score, tree_info))
    
    # 返回所有数据
    if use_validation_set:
        return trees, X_train, X_val, X_test, y_train, y_val, y_test
    else:
        return trees, X_train, X_test, y_train, y_test


def generate_complex_decision_tree(use_validation_set: bool = True):
    """
    生成复杂的决策树并返回训练好的模型和数据
    
    Args:
        use_validation_set: 是否使用三部分划分（训练/验证/测试）
            - True: 训练集(70%) + 验证集(15%) + 测试集(15%)
            - False: 训练集(80%) + 测试集(20%)
    
    Returns:
        如果 use_validation_set=True:
            (clf, X_train, X_val, X_test, y_train, y_val, y_test, training_time, test_score)
        如果 use_validation_set=False:
            (clf, X_test, y_test, training_time, test_score)
    """
    print("=" * 60)
    print("生成复杂决策树")
    print("=" * 60)
    
    # 生成复杂的合成数据集
    print("\n生成数据集...")
    X, y = make_classification(
        n_samples=100000,
        n_features=30,
        n_informative=15,
        n_redundant=5,
        n_classes=5,
        random_state=42
    )
    
    print(f"数据集形状: X={X.shape}, y={y.shape}")
    print(f"类别数: {len(np.unique(y))}")
    
    if use_validation_set:
        # 三部分划分：训练/验证/测试
        # 第一次划分：训练集(70%) + 临时集(30%)
        X_train, X_temp, y_train, y_temp = train_test_split(
            X, y, test_size=0.3, random_state=42
        )
        
        # 第二次划分：验证集(15%) + 测试集(15%)
        X_val, X_test, y_val, y_test = train_test_split(
            X_temp, y_temp, test_size=0.5, random_state=42
        )
        
        print(f"\n数据划分（三部分）:")
        print(f"  训练集: {X_train.shape[0]} 样本 (70%) - 用于训练决策树")
        print(f"  验证集: {X_val.shape[0]} 样本 (15%) - 用于提取轨迹和开发")
        print(f"  测试集: {X_test.shape[0]} 样本 (15%) - 用于最终评估")
    else:
        # 两部分划分：训练/测试（向后兼容）
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42
        )
        X_val, y_val = None, None
        
        print(f"\n数据划分（两部分）:")
        print(f"  训练集: {X_train.shape[0]} 样本 (80%)")
        print(f"  测试集: {X_test.shape[0]} 样本 (20%)")
    
    # 创建深度决策树
    print("\n训练决策树...")
    start_time = time.time()
    
    clf = DecisionTreeClassifier(
        max_depth=50,
        min_samples_split=5,
        min_samples_leaf=2,
        random_state=42
    )
    clf.fit(X_train, y_train)
    
    training_time = time.time() - start_time
    print(f"训练时间: {training_time:.4f} 秒")
    
    # 评估模型
    train_score = clf.score(X_train, y_train)
    test_score = clf.score(X_test, y_test)
    
    print(f"\n模型性能:")
    print(f"  训练集准确率: {train_score:.4f}")
    print(f"  测试集准确率: {test_score:.4f}")
    if use_validation_set:
        val_score = clf.score(X_val, y_val)
        print(f"  验证集准确率: {val_score:.4f}")
    print(f"  树深度: {clf.tree_.max_depth}")
    print(f"  节点数: {clf.tree_.node_count}")
    print(f"  叶子节点数: {clf.tree_.n_leaves}")
    
    if use_validation_set:
        return clf, X_train, X_val, X_test, y_train, y_val, y_test, training_time, test_score
    else:
        return clf, X_test, y_test, training_time, test_score


def convert_to_trajectary_format(trajectories: list, 
                                 retrieval_time: float,
                                 decision_effect: float) -> list:
    """
    将提取的算法轨迹转换为trajectary.py可识别的格式
    
    为每条轨迹添加经验特征标签：
    - retrieval_time: 检索时间（算法效率指标）
    - trajectory_length: 轨迹长度（算法复杂度指标）
    - decision_effect: 决策效果（目标倾向，用于算法指导）
    
    这些标签是倾向学习算法的关键：通过记录经验特征，中心学习模型可以
    学习哪些轨迹模式能产生更好的决策效果，从而实现算法指导。
    
    Args:
        trajectories: 算法轨迹列表（包含tendency, sequence, selection）
        retrieval_time: 检索时间（秒）
        decision_effect: 决策效果（目标倾向，如测试准确率）
    
    Returns:
        list: 格式化的轨迹字典列表，每条轨迹包含完整的经验特征
    """
    formatted_trajectories = []
    
    for traj in trajectories:
        # 确保所有numpy类型转换为Python原生类型
        formatted = {
            "tendency": [float(x) for x in traj["tendency"]],
            "sequence": [int(x) for x in traj["sequence"]],
            "selection": [str(x) for x in traj["selection"]],
            "retrieval_time": float(retrieval_time),
            "trajectory_length": int(len(traj["tendency"])),
            "decision_effect": float(decision_effect)
        }
        
        # 如果有预测信息，也保存
        if "prediction" in traj:
            formatted["prediction"] = int(traj["prediction"])
            formatted["is_correct"] = bool(traj["is_correct"])
        
        formatted_trajectories.append(formatted)
    
    return formatted_trajectories


def main(use_validation_set: bool = True, use_multiple_trees: bool = True, n_trees: int = 10):
    """
    主函数：生成多个分布学习模型（决策树）并提取算法轨迹
    
    核心功能：
    1. 生成多个分布学习模型（不同参数的决策树）
       - 好的树（good）：作为对比基准
       - 过拟合的树（overfit）：训练准确率高但测试准确率低
       - 欠拟合的树（underfit）：训练和测试准确率都低
       - 分类不合格的树（poor）：测试准确率很低
    
    2. 从每个分布学习模型提取算法轨迹
       - 算法状态序列（sequence）
       - 倾向值（tendency）
       - 选择路径（selection）
    
    3. 记录经验特征（目标倾向）
       - decision_effect: 决策效果（测试准确率）
       - retrieval_time: 检索时间
       - trajectory_length: 轨迹长度
    
    4. 为倾向学习算法提供数据
       - 中心学习模型（Trajectary）可以整合所有经验
       - 学习轨迹模式与决策效果的关联
       - 实现算法指导
    
    Args:
        use_validation_set: 是否使用三部分数据划分（训练/验证/测试）
        use_multiple_trees: 是否生成多个决策树（分布学习模型）
        n_trees: 生成的决策树数量
    
    Returns:
        tuple: (所有轨迹列表, 决策树列表或单个决策树)
    """
    """
    主函数：生成多个决策树并提取轨迹
    
    Args:
        use_validation_set: 是否使用验证集提取轨迹（推荐True，避免数据泄露）
        use_multiple_trees: 是否生成多个决策树（推荐True，评估不同决策成功率）
        n_trees: 生成的决策树数量
    """
    
    all_formatted_trajectories = []
    
    if use_multiple_trees:
        # 1. 生成多个决策树（必要的和不必要的）
        if use_validation_set:
            trees, X_train, X_val, X_test, y_train, y_val, y_test = generate_multiple_decision_trees(
                use_validation_set=True, n_trees=n_trees
            )
            X_trajectory, y_trajectory = X_val, y_val
            print("\n[数据安全] 使用验证集提取轨迹，测试集仅用于最终评估")
        else:
            trees, X_train, X_test, y_train, y_test = generate_multiple_decision_trees(
                use_validation_set=False, n_trees=n_trees
            )
            X_val, y_val = X_test, y_test
            X_trajectory, y_trajectory = X_test, y_test
            print("\n[警告] 使用测试集提取轨迹，如果后续用于训练新模型会造成数据泄露")
        
        # 2. 为每个决策树提取轨迹
        print("\n" + "=" * 60)
        print("为每个决策树提取轨迹数据")
        print("=" * 60)
        
        # 提取样本的轨迹（避免数据过大）
        n_samples_per_tree = min(1000, len(X_trajectory))
        print(f"\n每个决策树提取 {n_samples_per_tree} 个样本的轨迹...")
        
        for tree_idx, (clf, decision_success_rate, tree_info) in enumerate(trees):
            print(f"\n--- 决策树 {tree_idx+1}: {tree_info['name']} ---")
            print(f"决策成功率: {decision_success_rate:.4f}")
            
            extractor = DecisionTreeTrajectoryExtractor(clf)
            
            start_time = time.time()
            trajectories = extractor.extract_trajectories_batch(
                X_trajectory[:n_samples_per_tree],
                y_trajectory[:n_samples_per_tree]
            )
            extraction_time = time.time() - start_time
            
            avg_retrieval_time = extraction_time / n_samples_per_tree
            
            # 转换为trajectary格式，使用该决策树的成功率作为决策效果
            formatted_trajectories = convert_to_trajectary_format(
                trajectories,
                retrieval_time=avg_retrieval_time,
                decision_effect=decision_success_rate  # 使用该决策树的测试准确率作为决策效果
            )
            
            # 为每条轨迹添加决策树信息
            for traj in formatted_trajectories:
                traj["tree_name"] = tree_info["name"]
                traj["tree_idx"] = tree_idx
                traj["tree_category"] = tree_info["category"]  # good, overfit, underfit, poor
                traj["is_overfitting"] = tree_info["is_overfitting"]
                traj["is_underfitting"] = tree_info["is_underfitting"]
                traj["is_poor_performer"] = tree_info["is_poor_performer"]
                traj["overfitting_gap"] = float(tree_info["overfitting_gap"])
                traj["train_accuracy"] = float(tree_info["train_accuracy"])
            
            all_formatted_trajectories.extend(formatted_trajectories)
            
            print(f"  提取时间: {extraction_time:.4f}秒")
            print(f"  生成轨迹数: {len(formatted_trajectories)}")
            print(f"  决策效果(成功率): {decision_success_rate:.4f}")
        
        print(f"\n总共生成了 {len(all_formatted_trajectories)} 条轨迹（来自 {len(trees)} 个决策树）")
        
    else:
        # 向后兼容：生成单个决策树
        if use_validation_set:
            clf, X_train, X_val, X_test, y_train, y_val, y_test, training_time, test_accuracy = generate_complex_decision_tree(use_validation_set=True)
            X_trajectory, y_trajectory = X_val, y_val
            print("\n[数据安全] 使用验证集提取轨迹，测试集仅用于最终评估")
        else:
            clf, X_test, y_test, training_time, test_accuracy = generate_complex_decision_tree(use_validation_set=False)
            X_trajectory, y_trajectory = X_test, y_test
            print("\n[警告] 使用测试集提取轨迹，如果后续用于训练新模型会造成数据泄露")
        
        # 提取轨迹
        print("\n" + "=" * 60)
        print("提取轨迹数据")
        print("=" * 60)
        
        extractor = DecisionTreeTrajectoryExtractor(clf)
        
        n_samples = min(10000, len(X_trajectory))
        print(f"\n提取 {n_samples} 个样本的轨迹...")
        
        start_time = time.time()
        trajectories = extractor.extract_trajectories_batch(
            X_trajectory[:n_samples], 
            y_trajectory[:n_samples]
        )
        extraction_time = time.time() - start_time
        
        print(f"提取时间: {extraction_time:.4f} 秒")
        print(f"平均每个样本: {extraction_time/n_samples*1000:.2f} 毫秒")
        
        # 转换为trajectary格式
        print("\n转换为trajectary格式...")
        all_formatted_trajectories = convert_to_trajectary_format(
            trajectories,
            retrieval_time=extraction_time / n_samples,
            decision_effect=test_accuracy
        )
    
    # 3. 统计信息
    print("\n" + "=" * 60)
    print("轨迹统计信息")
    print("=" * 60)
    
    lengths = [t["trajectory_length"] for t in all_formatted_trajectories]
    decision_effects = [t["decision_effect"] for t in all_formatted_trajectories]
    
    print(f"\n轨迹长度统计:")
    print(f"  平均轨迹长度: {np.mean(lengths):.2f} 节点")
    print(f"  最短轨迹: {np.min(lengths)} 节点")
    print(f"  最长轨迹: {np.max(lengths)} 节点")
    print(f"  轨迹长度标准差: {np.std(lengths):.2f}")
    
    print(f"\n决策效果(成功率)统计:")
    print(f"  平均决策成功率: {np.mean(decision_effects):.4f}")
    print(f"  最低决策成功率: {np.min(decision_effects):.4f}")
    print(f"  最高决策成功率: {np.max(decision_effects):.4f}")
    print(f"  决策成功率标准差: {np.std(decision_effects):.4f}")
    
    # 按决策树分组统计
    if use_multiple_trees:
        print(f"\n按决策树分组的统计:")
        tree_groups = {}
        for traj in all_formatted_trajectories:
            tree_name = traj.get("tree_name", "未知")
            if tree_name not in tree_groups:
                tree_groups[tree_name] = []
            tree_groups[tree_name].append(traj["decision_effect"])
        
        for tree_name, effects in tree_groups.items():
            category = all_formatted_trajectories[0].get("tree_category", "unknown") if all_formatted_trajectories else "unknown"
            for traj in all_formatted_trajectories:
                if traj.get("tree_name") == tree_name:
                    category = traj.get("tree_category", "unknown")
                    break
            print(f"  {tree_name} [{category}]: 平均成功率={np.mean(effects):.4f}, 轨迹数={len(effects)}")
        
        # 按树类型分组统计
        print(f"\n按树类型分组的统计:")
        category_groups = {}
        for traj in all_formatted_trajectories:
            category = traj.get("tree_category", "unknown")
            if category not in category_groups:
                category_groups[category] = []
            category_groups[category].append(traj["decision_effect"])
        
        for category, effects in sorted(category_groups.items()):
            print(f"  {category}: 平均成功率={np.mean(effects):.4f}, "
                  f"最低={np.min(effects):.4f}, 最高={np.max(effects):.4f}, "
                  f"轨迹数={len(effects)}")
        
        # 统计不好的树
        poor_trees = [t for t in all_formatted_trajectories if t.get("is_poor_performer", False)]
        overfitting_trees = [t for t in all_formatted_trajectories if t.get("is_overfitting", False)]
        underfitting_trees = [t for t in all_formatted_trajectories if t.get("is_underfitting", False)]
        
        print(f"\n不好的树统计:")
        print(f"  分类不合格的树: {len(set(t.get('tree_name') for t in poor_trees))} 个, "
              f"轨迹数: {len(poor_trees)}")
        print(f"  过拟合的树: {len(set(t.get('tree_name') for t in overfitting_trees))} 个, "
              f"轨迹数: {len(overfitting_trees)}")
        print(f"  欠拟合的树: {len(set(t.get('tree_name') for t in underfitting_trees))} 个, "
              f"轨迹数: {len(underfitting_trees)}")
    
    # 4. 保存轨迹
    print("\n保存轨迹...")
    import json
    
    # 保存所有轨迹（用于分析）
    with open("all_trajectories.json", "w", encoding="utf-8") as f:
        json.dump(all_formatted_trajectories, f, ensure_ascii=False, indent=2)
    print(f"已保存 {len(all_formatted_trajectories)} 条完整轨迹到 all_trajectories.json")
    
    # 保存示例轨迹（用于快速查看）
    sample_trajectories = all_formatted_trajectories[:1000]
    with open("sample_trajectories.json", "w", encoding="utf-8") as f:
        json.dump(sample_trajectories, f, ensure_ascii=False, indent=2)
    print(f"已保存 {len(sample_trajectories)} 个示例轨迹到 sample_trajectories.json")
    
    # 5. 返回所有轨迹供测试使用
    return all_formatted_trajectories, trees if use_multiple_trees else clf


if __name__ == "__main__":
    # 使用多个决策树生成轨迹（推荐）
    # 生成所有不好的树（过拟合、欠拟合、分类不合格）
    trajectories, trees = main(use_validation_set=True, use_multiple_trees=True, n_trees=25)
    print(f"\n总共生成了 {len(trajectories)} 条轨迹")
    print(f"来自 {len(trees) if isinstance(trees, list) else 1} 个决策树")
    print("每条轨迹的决策效果(decision_effect)是该决策树在测试集上的成功率")
    print("包含所有决策效率低下的树（过拟合、欠拟合、分类不合格）")
    print("可以使用这些轨迹分析哪种轨迹能生成最好的树")
    print("可以使用这些轨迹测试 trajectary.py")

