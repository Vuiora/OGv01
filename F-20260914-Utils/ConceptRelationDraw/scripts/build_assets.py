"""Generate deterministic, explicitly hand-authored demo data and n8n workflow."""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from concept_relation.grounding import evidence_for, split_document
from concept_relation.models import RELATION_LABELS

ROOT = Path(__file__).resolve().parents[1]
DOC = """# 机器学习与任务类型
机器学习通过数据学习规律，并将这些规律用于预测。
监督学习属于机器学习，使用带有标签的数据训练模型。
分类属于监督学习，其预测目标是离散类别。
回归属于监督学习，其预测目标是连续数值。

# 模型训练与优化
损失函数度量模型预测与真实标签之间的差异。
梯度下降通过损失函数的梯度方向更新模型参数，以降低损失。
学习率控制梯度下降每次更新参数的步长。
训练集用于拟合模型参数。

# 评估与泛化
测试集用于评估模型在未参与训练的数据上的表现。
泛化能力指模型对未见数据保持良好预测表现的能力。
过拟合指模型过度适应训练集中的细节和噪声，导致在未见数据上表现变差。
过拟合会损害模型的泛化能力。
正则化在损失函数中加入约束项，以抑制过拟合。
交叉验证将数据分成多份并轮换训练与验证，用于评估泛化能力。
测试集与训练集应相互独立，避免评估结果受训练信息污染。
"""


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def demo():
    sections, _ = split_document(DOC)
    specs = [
        ("机器学习", "通过数据学习规律，用于预测。", "机器学习通过数据学习规律，并将这些规律用于预测。"),
        ("监督学习", "使用带有标签的数据训练模型。", "监督学习属于机器学习，使用带有标签的数据训练模型。"),
        ("分类", "预测离散类别的监督学习任务。", "分类属于监督学习，其预测目标是离散类别。"),
        ("回归", "预测连续数值的监督学习任务。", "回归属于监督学习，其预测目标是连续数值。"),
        ("损失函数", "度量预测与真实标签的差异。", "损失函数度量模型预测与真实标签之间的差异。"),
        ("梯度下降", "按照损失函数的梯度方向更新参数。", "梯度下降通过损失函数的梯度方向更新模型参数，以降低损失。"),
        ("学习率", "控制每次参数更新的步长。", "学习率控制梯度下降每次更新参数的步长。"),
        ("训练集", "用于拟合模型参数的数据。", "训练集用于拟合模型参数。"),
        ("测试集", "用于评估未参与训练的数据表现。", "测试集用于评估模型在未参与训练的数据上的表现。"),
        ("泛化能力", "对未见数据保持良好预测表现的能力。", "泛化能力指模型对未见数据保持良好预测表现的能力。"),
        ("过拟合", "过度适应训练集中的细节和噪声。", "过拟合指模型过度适应训练集中的细节和噪声，导致在未见数据上表现变差。"),
        ("正则化", "在损失函数中加入约束，抑制过拟合。", "正则化在损失函数中加入约束项，以抑制过拟合。"),
        ("交叉验证", "通过轮换训练与验证评估泛化能力。", "交叉验证将数据分成多份并轮换训练与验证，用于评估泛化能力。"),
    ]
    concepts = []
    for index, (label, definition, quote) in enumerate(specs):
        evidence = evidence_for([quote], DOC, sections)
        concepts.append(dict(id=f"c{index+1}", label=label, definition=definition, evidence=evidence,
                             section_ids=[evidence[0]["section_id"]]))
    edges = [
        (2, 1, "part_of", "监督学习属于机器学习。", specs[1][2]),
        (3, 2, "part_of", "分类是监督学习的一种任务。", specs[2][2]),
        (4, 2, "part_of", "回归是监督学习的一种任务。", specs[3][2]),
        (6, 5, "depends_on", "梯度下降使用损失函数的梯度更新参数。", specs[5][2]),
        (7, 6, "applies_to", "学习率控制梯度下降的更新步长。", specs[6][2]),
        (11, 8, "related", "过拟合描述了模型过度适应训练集的状态。", specs[10][2]),
        (11, 10, "related", "过拟合会损害泛化能力。", "过拟合会损害模型的泛化能力。"),
        (12, 11, "applies_to", "正则化用于抑制过拟合。", specs[11][2]),
        (12, 5, "applies_to", "正则化在损失函数中加入约束项。", specs[11][2]),
        (13, 10, "applies_to", "交叉验证用于评估泛化能力。", specs[12][2]),
        (9, 8, "contrasts", "测试集与训练集应保持独立。", "测试集与训练集应相互独立，避免评估结果受训练信息污染。"),
    ]
    relations = [dict(id=f"r{index+1}", source=f"c{source}", target=f"c{target}", type=kind,
                      explanation=explanation, evidence=evidence_for([quote], DOC, sections),
                      confidence=.94, review="unreviewed", evidence_verified=True)
                 for index, (source, target, kind, explanation, quote) in enumerate(edges)]
    graph = dict(schema_version="1.0", job_id="demo", title="机器学习基础.md", created_at="2026-09-16T00:00:00Z", demo=True,
                 document=dict(filename="机器学习基础.md", sha256=hashlib.sha256(DOC.encode()).hexdigest(), converter="hand-authored-demo"),
                 sections=sections, concepts=concepts, relations=relations, relation_labels=RELATION_LABELS,
                 validation=dict(rejected=[], isolated_concepts=[], global_context="hand_authored_demo", global_context_calls=0,
                                 note="人工整理的演示图谱，所有关系的 94% 数值仅用于演示，不代表实际模型调用。"))
    target = ROOT / "concept_relation/fixtures"
    write_json(target / "demo.json", graph)
    (target / "demo.md").write_text(DOC, encoding="utf-8")


def workflow():
    credential = {"httpHeaderAuth": {"id": "CONFIGURE_HEADER_AUTH", "name": "ConceptRelationDraw API"}}
    nodes = [dict(id="webhook", name="接收文档任务", type="n8n-nodes-base.webhook", typeVersion=2.1,
                  position=[0, 240], webhookId="concept-relation-v1", credentials=credential,
                  parameters=dict(httpMethod="POST", path="concept-relation", authentication="headerAuth", responseMode="responseNode", options={})),
             dict(id="respond", name="立即确认任务", type="n8n-nodes-base.respondToWebhook", typeVersion=1.4,
                  position=[240, 240], parameters=dict(respondWith="json", responseBody="={{ { job_id: $('接收文档任务').first().json.body.job_id, accepted: true } }}", options={"responseCode": 202}))]
    stages = [("parse", "Docling 解析文档"), ("extract", "按知识部分提取概念"), ("relate", "结合全文判断关系"), ("render", "校验与生成关系图")]
    for index, (stage, name) in enumerate(stages):
        nodes.append(dict(id=stage, name=name, type="n8n-nodes-base.httpRequest", typeVersion=4.2,
                          position=[480+index*280, 240], credentials=credential,
                          parameters=dict(method="POST", url="={{ 'http://concept-api:8091/api/jobs/' + $('接收文档任务').first().json.body.job_id + '/stages/"+stage+"' }}",
                                          authentication="genericCredentialType", genericAuthType="httpHeaderAuth", options={"timeout": 7200000})))
    names = [node["name"] for node in nodes]
    connections = {name: {"main": [[{"node": names[index+1], "type": "main", "index": 0}]]} for index, name in enumerate(names[:-1])}
    nodes.append(dict(id="readme", name="首次部署说明", type="n8n-nodes-base.stickyNote", typeVersion=1,
                      position=[0, -140], parameters=dict(width=1450, height=280, content="## ConceptRelationDraw · 文档知识图谱服务\n1. 创建 Header Auth 凭据：Name = X-API-Key，Value = .env 中的 APP_API_KEY。\n2. 将同一凭据绑定到 Webhook 和四个 HTTP Request 节点，然后发布工作流。\n3. Web UI 上传文档后调用此 Webhook，仅传 job_id；文档和模型密钥留在 API 服务。\n4. 每阶段持久化状态与证据；失败停止流程，并可在工作台查看错误。\n5. 全文用于每批关系分析；超出预算则失败，不截断文档。\n6. 幂等接口跳过已成功完成的阶段。中断任务请重新上传。\n7. Docker 内 API 地址为 concept-api:8091。")))
    write_json(ROOT / "n8n/workflow.json", dict(id="ConceptRelationDrawV1", name="ConceptRelationDraw · n8n + Docling · 文档概念关系图",
               active=False, nodes=nodes, connections=connections, settings={"executionOrder": "v1", "executionTimeout": 7200}, pinData={}))


if __name__ == "__main__":
    demo()
    workflow()
