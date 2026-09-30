import json
import numpy as np
from sklearn.preprocessing import LabelEncoder
from sklearn.preprocessing import StandardScaler
class Trajectary:
    """
    轨迹类 (Trajectory Class) - 倾向学习算法的核心实现
    
    核心概念：
    1. 算法状态 (Algorithm State): 当前算法的所有可被观测的状态
       - 在决策树中，每个节点代表一个算法状态
    2. 算法轨迹 (Algorithm Trajectory): 顺序上按时序排列相邻的一系列状态
       - sequence: 按时间顺序的状态序列
    3. 倾向 (Tendency): 轨迹的变化方向
       - tendency: 每个状态的倾向值（反映算法在该状态下选择该路径的概率）
    4. 倾向学习算法 (Tendency Learning Algorithm): 
       通过学习机器学习算法倾向学习的量化模式，为相似结构/情况/经验的
       机器学习算法提供学习指导
    5. 算法指导 (Algorithm Guidance): 
       在算法训练过程中，通过纠正算法的学习轨迹来改变学习函数或超参数
    
    本类实现：
    - 存储算法轨迹（状态序列、倾向值、选择路径）
    - 量化轨迹特征（tendency, sequence, selection的组合）
    - 学习轨迹模式与决策效果的关联
    - 为算法指导提供支持
    
    trajectory_dict: 存储轨迹数据的字典，包含：
        - "tendency": 倾向值数组（轨迹变化方向）
        - "sequence": 状态序列（按时序排列的状态）
        - "selection": 选择路径（每个状态的选择）
        - "retrieval_time": 检索时间
        - "trajectory_length": 轨迹长度
        - "decision_effect": 决策效果（目标倾向）
    """

    def __init__(self, file_name: str, root_Node_array: list = None, trajectory_dict: dict = None):
        """
        Args:
            file_name (str): The file name for storing/loading trajectory data.
            root_Node_array (list, optional): The root node array for the trajectory tree.
            trajectory_dict (dict, optional): Dictionary to store the trajectory data. If not provided, will load from file_name.
                Expected structure:
                {
                    "tendency": [...],
                    "sequence": [...],
                    "selection": [...],
                    "retrieval_time": float,      # 检索时间（秒）
                    "trajectory_length": int,     # 轨迹长度（节点数）
                    "decision_effect": float      # 决策效果（如准确率、收益等）
                }
        """
        self.file_name = file_name

        # If no root_Node_array given, initialize as empty list
        if root_Node_array is not None:
            self.root_Node_array = root_Node_array
        else:
            self.root_Node_array = []

        # If trajectory_dict is not provided, try loading from file
        if trajectory_dict is not None:
            self.trajectory_dict = trajectory_dict
        else:
            try:
                with open(self.file_name, 'r', encoding='utf-8') as f:
                    self.trajectory_dict = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                self.trajectory_dict = {}
        
        # 验证轨迹标签是否完整
        self._validate_trajectory_labels()


    """
    using LabelEncoder to encode the selection choices of nodes in the trajectory
    """


    def _validate_trajectory_labels(self):
        """
        验证轨迹标签是否完整
        确保每条轨迹都有：检索时间、轨迹长度、决策效果
        """
        required_labels = ["retrieval_time", "trajectory_length", "decision_effect"]
        missing_labels = [label for label in required_labels if label not in self.trajectory_dict]
        
        if missing_labels:
            # 如果缺少标签，尝试自动计算或设置默认值
            if "trajectory_length" in missing_labels:
                # 自动计算轨迹长度
                if "tendency" in self.trajectory_dict:
                    self.trajectory_dict["trajectory_length"] = len(self.trajectory_dict["tendency"])
                elif "sequence" in self.trajectory_dict:
                    self.trajectory_dict["trajectory_length"] = len(self.trajectory_dict["sequence"])
                else:
                    self.trajectory_dict["trajectory_length"] = 0
            
            # 对于其他缺失的标签，设置默认值并发出警告
            if "retrieval_time" in missing_labels:
                self.trajectory_dict["retrieval_time"] = 0.0
                print(f"Warning: 'retrieval_time' not found, set to default 0.0")
            
            if "decision_effect" in missing_labels:
                self.trajectory_dict["decision_effect"] = 0.0
                print(f"Warning: 'decision_effect' not found, set to default 0.0")
        
        # 验证标签值的有效性
        if self.trajectory_dict.get("retrieval_time") is None:
            self.trajectory_dict["retrieval_time"] = 0.0
        if self.trajectory_dict.get("trajectory_length") is None:
            self.trajectory_dict["trajectory_length"] = 0
        if self.trajectory_dict.get("decision_effect") is None:
            self.trajectory_dict["decision_effect"] = 0.0
        
        # 确保轨迹长度与实际数据一致
        if "tendency" in self.trajectory_dict:
            actual_length = len(self.trajectory_dict["tendency"])
            if self.trajectory_dict["trajectory_length"] != actual_length:
                print(f"Warning: trajectory_length ({self.trajectory_dict['trajectory_length']}) "
                      f"does not match actual data length ({actual_length}), updating...")
                self.trajectory_dict["trajectory_length"] = actual_length

    def preprocess(self, np_selection_dict: np.array) -> np.array:
        label_encoder = LabelEncoder()
        np_selection_dict = label_encoder.fit_transform(np_selection_dict)
        return np_selection_dict

    def cal_mixed_tendency_sequence_selection(self, method: str = 'concatenate', normalize: bool = False) -> np.array:
        """
        特征相关性量化分析：量化倾向(tendency)、序列(sequence)、选择(selection)之间的相关性
        
        这是基本方法论的核心实现：使用特征相关性量化分析的算法，总结对于各特征
        相关性分析的算法的量化，选取最优的量化算法进行使用，并记录该模式的经验特征。
        
        本质：通过学习学习算法的学习轨迹（元学习），分析元学习和数据之间的特征相关性，
        来实现算法选择和算法指导。
        
        Args:
            method: 特征组合方法（不同的量化算法）
                - 'concatenate': 简单拼接（原始三维信息，基础量化）
                - 'with_interaction': 原始三维 + 交互特征（量化特征间的交互关系）
                - 'with_statistics': 原始三维 + 统计特征（量化特征的统计特性）
                - 'full': 原始三维 + 交互特征 + 统计特征（最完整的量化方法）
            normalize: 是否对特征进行归一化（可选，默认False保留原始值）
        
        Returns:
            np.array: 量化后的特征矩阵
                - 'concatenate': (n_samples, 3) - 基础量化
                - 'with_interaction': (n_samples, 6) - 包含交互关系
                - 'with_statistics': (n_samples, 9) - 包含统计特性
                - 'full': (n_samples, 12) - 完整量化（推荐）
        """
        # 确保三个数组长度一致
        assert len(self.np_tendency_dict) == len(self.np_sequence_dict) == len(self.np_selection_dict), \
            "三个数组长度必须一致"
        
        # 转换为 float64 确保精度
        tendency = self.np_tendency_dict.astype(np.float64)
        sequence = self.np_sequence_dict.astype(np.float64)
        selection = self.np_selection_dict.astype(np.float64)
        
        # 归一化（如果需要）
        if normalize:
            tendency_norm = (tendency - tendency.min()) / (tendency.max() - tendency.min() + 1e-8)
            sequence_norm = (sequence - sequence.min()) / (sequence.max() - sequence.min() + 1e-8)
            selection_norm = (selection - selection.min()) / (selection.max() - selection.min() + 1e-8)
            t, s, sel = tendency_norm, sequence_norm, selection_norm
        else:
            t, s, sel = tendency, sequence, selection
        
        # 方法1: 简单拼接（保留原始三维信息）
        if method == 'concatenate':
            self.mixed_feature_values = np.column_stack([t, s, sel])
        
        # 方法2: 原始三维 + 交互特征（保留原始信息）
        elif method == 'with_interaction':
            # 原始三维信息
            original_features = np.column_stack([t, s, sel])
            # 交互特征
            interaction_features = np.column_stack([
                t * s,      # tendency × sequence
                t * sel,    # tendency × selection
                s * sel,    # sequence × selection
            ])
            # 拼接：原始 + 交互（完全保留原始信息）
            self.mixed_feature_values = np.column_stack([original_features, interaction_features])
        
        # 方法3: 原始三维 + 统计特征（保留原始信息）
        elif method == 'with_statistics':
            # 原始三维信息
            original_features = np.column_stack([t, s, sel])
            # 计算每个样本的统计特征（相对于全局）
            t_mean, s_mean, sel_mean = t.mean(), s.mean(), sel.mean()
            t_std, s_std, sel_std = t.std(), s.std(), sel.std()
            
            # 中心化特征（保留相对位置信息）
            centered_features = np.column_stack([
                t - t_mean,    # tendency 中心化
                s - s_mean,    # sequence 中心化
                sel - sel_mean, # selection 中心化
            ])
            # 标准化特征（保留相对尺度信息）
            standardized_features = np.column_stack([
                (t - t_mean) / (t_std + 1e-8),    # tendency 标准化
                (s - s_mean) / (s_std + 1e-8),    # sequence 标准化
                (sel - sel_mean) / (sel_std + 1e-8), # selection 标准化
            ])
            # 拼接：原始 + 中心化 + 标准化（完全保留原始信息）
            self.mixed_feature_values = np.column_stack([original_features, centered_features, standardized_features])
        
        # 方法4: 完整特征集（原始三维 + 交互 + 统计）
        elif method == 'full':
            # 原始三维信息
            original_features = np.column_stack([t, s, sel])
            # 交互特征
            interaction_features = np.column_stack([
                t * s,
                t * sel,
                s * sel,
            ])
            # 统计特征（可用sklearn包函数 StandardScaler 进行中心化和标准化）

            # 注意: 这里依然要保留中间变量t_mean, t_std等以完全按原逻辑拼接（虽然StandardScaler.fit.transform也能输出标准化结果）
            t_mean, s_mean, sel_mean = t.mean(), s.mean(), sel.mean()
            t_std, s_std, sel_std = t.std(), s.std(), sel.std()
            centered_features = np.column_stack([
                t - t_mean,
                s - s_mean,
                sel - sel_mean,
            ])
            # sklearn StandardScaler 归一化后的结果与手工 (x-mean)/std 一致
            scaler = StandardScaler()
            standardized_features = scaler.fit_transform(np.column_stack([t, s, sel]))
            # 拼接：原始 + 交互 + 统计（完全保留原始信息）
            self.mixed_feature_values = np.column_stack([
                original_features,
                interaction_features,
                centered_features,
                standardized_features
            ])
        
        else:
            raise ValueError(f"Unknown method: {method}. Choose from 'concatenate', 'with_interaction', 'with_statistics', 'full'")
        
        return self.mixed_feature_values

    def get_trajectory_labels(self) -> dict:
        """
        获取轨迹标签：检索时间、轨迹长度、决策效果
        
        Returns:
            dict: 包含三个标签的字典
                {
                    "retrieval_time": float,      # 检索时间（秒）
                    "trajectory_length": int,     # 轨迹长度（节点数）
                    "decision_effect": float      # 决策效果
                }
        """
        return {
            "retrieval_time": self.trajectory_dict.get("retrieval_time", 0.0),
            "trajectory_length": self.trajectory_dict.get("trajectory_length", 0),
            "decision_effect": self.trajectory_dict.get("decision_effect", 0.0)
        }
    
    def set_trajectory_labels(self, retrieval_time: float = None, 
                              trajectory_length: int = None, 
                              decision_effect: float = None):
        """
        设置轨迹标签
        
        Args:
            retrieval_time: 检索时间（秒）
            trajectory_length: 轨迹长度（节点数）
            decision_effect: 决策效果
        """
        if retrieval_time is not None:
            self.trajectory_dict["retrieval_time"] = float(retrieval_time)
        if trajectory_length is not None:
            self.trajectory_dict["trajectory_length"] = int(trajectory_length)
        if decision_effect is not None:
            self.trajectory_dict["decision_effect"] = float(decision_effect)
        
        # 验证并更新
        self._validate_trajectory_labels()

    def trajectory_valulization(self, trajectory_dict: dict = None, method: str = 'concatenate', normalize: bool = False):
        """
        轨迹数值化：将轨迹字典转换为数值数组
        
        Args:
            trajectory_dict: 包含 tendency, sequence, selection 的字典
                如果为 None，使用 self.trajectory_dict
            method: 特征组合方法
                - 'concatenate': 简单拼接（原始三维信息）
                - 'with_interaction': 原始三维 + 交互特征
                - 'with_statistics': 原始三维 + 统计特征
                - 'full': 原始三维 + 交互特征 + 统计特征
            normalize: 是否归一化特征（默认False，保留原始值）
        
        Returns:
            np.array: 特征矩阵（维度取决于 method）
        """
        # 如果提供了新的 trajectory_dict，更新并验证
        if trajectory_dict is not None:
            self.trajectory_dict.update(trajectory_dict)
            self._validate_trajectory_labels()
        
        self.np_tendency_dict = np.array(self.trajectory_dict["tendency"])                               # the tendency(when the learning algorithm selecting, the probability of selecting the node) of the trajectory
        self.np_sequence_dict = np.array(self.trajectory_dict["sequence"])                               # the sequence nodes in the trajectory
        self.np_selection_dict = self.preprocess(np.array(self.trajectory_dict["selection"]))            # the selection choices of nodes in the trajectory
        return self.cal_mixed_tendency_sequence_selection(method=method, normalize=normalize)

    def trajectory_supervised_learning(self, trajectory_dict: dict = None):
        """
        倾向学习算法的监督学习实现
        
        通过量化后的特征矩阵（full方法得到12维特征），学习轨迹模式与决策效果的关联。
        这是倾向学习算法的核心：通过学习机器学习算法倾向学习的量化模式，为相似结构/
        情况/经验的机器学习算法提供学习指导。
        
        我们提前设计了很多十分复杂的决策树（包括好的、过拟合的、欠拟合的、分类不合格的），
        并为其每一条轨迹打上了检索时间、轨迹长度和决策效果的标签。通过这些标签，我们可以
        训练模型，学习哪些轨迹模式能产生更好的决策效果，从而实现算法指导。
        
        Args:
            trajectory_dict: 轨迹字典（可选，如果为None使用self.trajectory_dict）
        
        Returns:
            dict: 包含特征矩阵和标签的字典
                {
                    "features": np.array,  # 量化后的特征矩阵 (n_samples, 12)
                    "labels": {
                        "retrieval_time": float,      # 检索时间
                        "trajectory_length": int,     # 轨迹长度
                        "decision_effect": float      # 决策效果（目标倾向）
                    }
                }
        
        应用：
        - 识别高质量轨迹模式（decision_effect高）
        - 识别需要调整的轨迹模式（过拟合、欠拟合）
        - 为算法指导提供依据
        """
        # 使用提供的 trajectory_dict 或 self.trajectory_dict
        if trajectory_dict is not None:
            self.trajectory_dict.update(trajectory_dict)
            self._validate_trajectory_labels()
        
        # 确保标签存在
        labels = self.get_trajectory_labels()
        
        # 提取特征（使用 full 方法得到12维特征）
        features = self.trajectory_valulization(method='full', normalize=False)
        
        return {
            "features": features,
            "labels": labels
        }

    def trajectory_feature_extraction(self, feature_matrix: np.array = None):
        """
        轨迹特征提取：从特征矩阵中提取高级特征
        
        Args:
            feature_matrix: 特征矩阵（可选，如果为None则从self.trajectory_dict提取）
        
        Returns:
            np.array: 提取的特征
        """
        if feature_matrix is None:
            feature_matrix = self.trajectory_valulization(method='full')
        
        # 可以在这里添加更多的特征提取逻辑
        # 例如：统计特征、频域特征等
        return feature_matrix

    def processing(self) -> dict:
        """
        完整的处理流程：数值化 + 特征提取 + 标签提取
        
        Returns:
            dict: 包含特征和标签的字典
                {
                    "features": np.array,  # 特征矩阵
                    "labels": dict          # 轨迹标签（检索时间、轨迹长度、决策效果）
                }
        """
        mixed_feature_values = self.trajectory_valulization(method='full')
        trajectory_feature_values = self.trajectory_feature_extraction(mixed_feature_values)
        labels = self.get_trajectory_labels()
        
        return {
            "features": trajectory_feature_values,
            "labels": labels
        }
    
    def save_trajectory_with_labels(self, file_name: str = None):
        """
        保存轨迹数据（包含标签）到文件
        
        Args:
            file_name: 保存的文件名（如果为None，使用self.file_name）
        """
        if file_name is None:
            file_name = self.file_name
        
        # 确保标签存在
        self._validate_trajectory_labels()
        
        with open(file_name, 'w', encoding='utf-8') as f:
            json.dump(self.trajectory_dict, f, ensure_ascii=False, indent=2)
        
        print(f"Trajectory with labels saved to {file_name}")
        