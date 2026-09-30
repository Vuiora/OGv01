# 整体业务闭环实施计划

范围：n8n-Docling、CLC、CRD、MTBMT、SDL、PLDA、Mentor、Self Renew、TPWF。

1. 建立统一配置、任务契约、CLI/API、运行目录与事件记录。
2. 用 M1 分组划分 E/V/C；仅用 E 训练 MTBMT 推荐器和生成候选。
3. 把推荐的特征、模型候选接入 SDL M2–M8；明确冻结方法、参数、效应阈值。
4. 以独立观测组为统计单位执行单侧符号检验，使用 SDL 的族内校正、结果分类和归档。
5. 接入文档知识输入、Mentor 应用规格、Self Renew 反馈和 PLDA 确定性计算。
6. TPWF 读取实现代码，分析、设计、渲染、校验并交付原生 draw.io。
7. 验证隔离、冻结、历史经验去重、失败状态、模型配置及跨项目端到端运行。

完成状态和实际验证将在 BUSINESS_CLOSED_LOOP.md 中记录。密钥只保存在被 Git 忽略的本机环境文件。
