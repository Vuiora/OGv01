"""Generate editable SDL architecture diagrams with native draw.io cells."""
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'SDL算法框架.drawio'
FONT = 'Microsoft YaHei'
C = dict(ink='#142D45', muted='#52687C', blue='#2667C9', blue_bg='#EDF4FF',
         teal='#187E77', teal_bg='#EAF6F2', orange='#AA641B', orange_bg='#FFF5E8',
         purple='#7253A0', purple_bg='#F3EEFA', red='#AA4D53', red_bg='#FFF0F0',
         line='#A9BBCB', bg='#F7FAFD', white='#FFFFFF')
mxfile = ET.Element('mxfile', host='Electron', agent='SDL Framework Builder',
                    version='31.4.5', type='device', compressed='false')


class Page:
    def __init__(self, name, width, height):
        d = ET.SubElement(mxfile, 'diagram', id=f'page-{len(mxfile)+1}', name=name)
        m = ET.SubElement(d, 'mxGraphModel', dx=str(width), dy=str(height), grid='1',
                          gridSize='10', guides='1', tooltips='1', connect='1',
                          arrows='1', fold='1', page='1', pageScale='1',
                          pageWidth=str(width), pageHeight=str(height), math='0', shadow='0')
        self.root = ET.SubElement(m, 'root')
        ET.SubElement(self.root, 'mxCell', id='0')
        ET.SubElement(self.root, 'mxCell', id='1', parent='0')
        self.ids = set()
        self.box('canvas', 0, 0, width, height, '', '#FFFFFF', '#FFFFFF', rounded=False)

    def cell(self, id, value, style, x, y, w, h):
        assert id not in self.ids, id
        self.ids.add(id)
        cell = ET.SubElement(self.root, 'mxCell', id=id, value=value, style=style,
                             vertex='1', parent='1')
        ET.SubElement(cell, 'mxGeometry', x=str(x), y=str(y), width=str(w), height=str(h), **{'as': 'geometry'})
        return id

    def box(self, id, x, y, w, h, value, fill=None, stroke=None, size=18, rounded=True, extra=''):
        style = (f'rounded={int(rounded)};arcSize=12;whiteSpace=wrap;html=1;'
                 f'fillColor={fill or C["white"]};strokeColor={stroke or C["line"]};'
                 f'fontColor={C["ink"]};fontFamily={FONT};fontSize={size};'
                 f'align=left;verticalAlign=middle;spacing=18;strokeWidth=1.5;{extra}')
        return self.cell(id, value, style, x, y, w, h)

    def text(self, id, x, y, w, h, value, size=18, color=None, bold=False, align='left'):
        style = (f'text;html=1;strokeColor=none;fillColor=none;whiteSpace=wrap;'
                 f'fontFamily={FONT};fontSize={size};fontColor={color or C["ink"]};'
                 f'fontStyle={int(bold)};align={align};verticalAlign=middle;spacing=0;')
        return self.cell(id, value, style, x, y, w, h)

    def edge(self, id, source, target, value='', color=None, dashed=False,
             exit=(1, .5), entry=(0, .5), points=None, extra=''):
        assert id not in self.ids
        self.ids.add(id)
        style = (f'edgeStyle=orthogonalEdgeStyle;rounded=1;orthogonalLoop=1;jettySize=auto;'
                 f'html=1;endArrow=block;endFill=1;strokeWidth=2;strokeColor={color or C["blue"]};'
                 f'fontColor={color or C["muted"]};fontFamily={FONT};fontSize=16;'
                 f'labelBackgroundColor=#FFFFFF;dashed={int(dashed)};'
                 f'exitX={exit[0]};exitY={exit[1]};exitDx=0;exitDy=0;'
                 f'entryX={entry[0]};entryY={entry[1]};entryDx=0;entryDy=0;{extra}')
        cell = ET.SubElement(self.root, 'mxCell', id=id, value=value, style=style,
                             edge='1', parent='1', source=source, target=target)
        g = ET.SubElement(cell, 'mxGeometry', relative='1', **{'as': 'geometry'})
        if points:
            a = ET.SubElement(g, 'Array', **{'as': 'points'})
            for x, y in points:
                ET.SubElement(a, 'mxPoint', x=str(x), y=str(y))
        return id

    def header(self, n, title, subtitle, width):
        self.text('eyebrow', 60, 30, width-120, 25,
                  f'SDL  /  STATISTICAL DISCOVERY LEARNING  /  FRAMEWORK v0.1  /  {n}',
                  14, C['blue'], True)
        self.text('title', 60, 65, width-120, 48, title, 33, bold=True)
        self.text('subtitle', 60, 118, width-120, 32, subtitle, 18, C['muted'])


def content(title, body):
    return f'<b>{title}</b><br><span style="font-size:17px;color:{C["muted"]}">{body}</span>'


# PAGE 1 — Architecture, at a level suitable for implementation planning.
p = Page('01 总体架构', 1800, 1190)
p.header('01', '从统计模式，到可检验的候选发现',
         'SDL 将表示构造、假说搜索、独立确证与主动取证组织为一个可追溯的闭环。', 1800)
tops = [
    ('task', 60, 340, '研究任务 T', '目标现象 / 发现类型 / 适用范围'),
    ('knowledge', 440, 480, '已有知识 K（带版本）', '领域规律 / 量纲 / 已知概念 / 文献证据'),
    ('budget', 960, 360, '预算与约束 B, C', '算力 / 样本 / 检验预算 / 停止规则'),
    ('promise', 1360, 380, '交付标准', '明确预测、证据等级、边界与反例')]
for id, x, w, title, body in tops:
    p.box(id, x, 180, w, 90, content(title, body), C['bg'])

panels = [
    ('data_panel', 60, 340, C['blue_bg'], C['blue'], '01  数据与证据协议', '质量先行，确证数据封存'),
    ('search_panel', 440, 480, C['teal_bg'], C['teal'], '02  探索与候选生成', 'LLM 提案 + 数值 / 符号搜索'),
    ('rank_panel', 960, 360, C['purple_bg'], C['purple'], '03  计算验证与竞争', '可执行、可比较、可淘汰'),
    ('confirm_panel', 1360, 380, C['blue_bg'], C['blue'], '04  独立确证与交付', '冻结后访问未见证据')]
for id, x, w, fill, stroke, title, sub in panels:
    p.box(id, x, 330, w, 475, '', fill, stroke)
    p.text(id+'-title', x+20, 350, w-40, 36, title, 22, stroke, True)
    p.text(id+'-sub', x+20, 390, w-40, 25, sub, 16, C['muted'])

cards = [
    ('d1', 80, 300, '观测编码与质量检查', '单位、噪声、缺失、主体与环境'),
    ('d2', 80, 300, '建立数据分区', 'D_E 探索 / D_V 开发 / D_C,t 确证'),
    ('d3', 80, 300, '登记快照与访问权限', '版本、来源、采样方式与数据用途'),
    ('s1', 460, 440, '表示学习与概念构造', '原始变量 → 组合变量、关系与潜在表示'),
    ('s2', 460, 440, '模式搜索', '关系 / 方程 / 分群 / 不变量 / 变点'),
    ('s3', 460, 440, '构造竞争假说池 H', '陈述 + 预测 + 范围 + 基线 + 可执行形式'),
    ('r1', 980, 320, '编译与约束检查', '可执行性、量纲、逻辑、重复假说'),
    ('r2', 980, 320, '探索与开发评估', '预测增益、稳定性、复杂度、反例'),
    ('r3', 980, 320, 'Pareto 筛选与保留', '保留少量有效且不同的竞争解释'),
    ('c1', 1380, 340, '冻结假说与确证计划', '版本、变换、指标、阈值、停止规则'),
    ('c2', 1380, 340, '独立确证 D_C,t', '误差控制 + 效应量 + 不确定性'),
    ('c3', 1380, 340, '形成发现档案', '支持 / 反驳 / 证据不足；保留边界')]
for index, (id, x, w, title, body) in enumerate(cards):
    y = [445, 560, 675][index % 3]
    p.box(id, x, y, w, 90, content(title, body), size=18)
for prefix in ('d', 's', 'r', 'c'):
    for i in (1, 2):
        p.edge(prefix+f'-flow-{i}', prefix+str(i), prefix+str(i+1),
               exit=(.5, 1), entry=(.5, 0))
for a, b in [('data_panel', 'search_panel'), ('search_panel', 'rank_panel'), ('rank_panel', 'confirm_panel')]:
    p.edge(a+'-next', a, b)
for a, b in [('task', 'data_panel'), ('knowledge', 'search_panel'), ('budget', 'rank_panel'), ('promise', 'confirm_panel')]:
    p.edge(a+'-context', a, b, color=C['line'], dashed=True, exit=(.5, 1), entry=(.5, 0))

p.box('trace', 60, 920, 340, 130,
      content('贯穿全程：证据账本', '数据、代码、知识、假说版本<br>记录成功、失败与修订原因'), C['bg'])
p.box('active', 440, 920, 880, 130,
      content('05  主动取证与假说修订',
              '优先区分竞争假说、寻找反例与适用边界；按预期信息价值与成本安排新观测。<br>'
              '新数据先登记用途；修订创建新版本；后续确证使用新的未见数据。'),
      C['orange_bg'], C['orange'], size=21)
p.box('archive', 1360, 920, 380, 130,
      content('可复现的候选发现', '假说 + 证据 + 预测 + 适用范围<br>知识库更新 / 外部复现任务'), C['teal_bg'], C['teal'])
p.edge('rank-to-active', 'rank_panel', 'active', '候选分歧 / 证据缺口',
       C['orange'], exit=(.5, 1), entry=(.8, 0))
p.edge('confirm-to-archive', 'confirm_panel', 'archive', exit=(.5, 1), entry=(.5, 0))
p.edge('confirm-to-active', 'confirm_panel', 'active', '', C['orange'], True,
       exit=(0, .94), entry=(1, .5), points=[(1340, 776), (1340, 985)])
p.edge('active-to-search', 'active', 'search_panel', '修订 / 扩展表示', C['orange'], True,
       exit=(.27, 0), entry=(.5, 1))
p.edge('active-to-data', 'active', 'data_panel', '补充数据', C['orange'],
       exit=(.13, 1), entry=(0, .82), points=[(554, 1090), (30, 1090), (30, 720)])
p.text('footer', 60, 1120, 1680, 38,
       '设计底线：探索排序 ≠ 确证结论  ·  新颖性不能抵消弱证据  ·  统计关联不能自动升级为因果机制',
       18, C['muted'])


# PAGE 2 — Executable orchestration flow and branches.
p = Page('02 算法主循环', 1800, 1550)
p.header('02', 'SDL 主循环：生成 → 竞争 → 确证 → 修订',
         '输入：观测 D、知识 K、任务 T、约束 C、预算 B；输出：带证据等级和版本的发现档案。', 1800)
p.box('start', 550, 180, 620, 65, '<b>初始化轮次 t = 1、假说池 H、证据账本 E</b>',
      C['ink'], C['ink'], 20, extra='fontColor=#FFFFFF;align=center;')
p.box('protocol', 550, 290, 620, 90,
      content('1  建立数据与任务协议', '划分 D_E / D_V / D_C,t；设定基线、预算和可检验的目标'), C['blue_bg'], C['blue'])
p.box('generate', 550, 425, 620, 105,
      content('2  构造表示、搜索模式、生成竞争假说', '仅用可见资料；加入零假说与替代解释；记录生成来源'), C['teal_bg'], C['teal'])
p.box('evaluate', 550, 575, 620, 105,
      content('3  执行检查与开发评估，保留 Pareto 候选', '评估预测增益、稳定性、复杂度和新颖性；不合格者淘汰'), C['purple_bg'], C['purple'])
p.box('freeze', 550, 725, 620, 105,
      content('4  冻结候选批次 H_t 与确证计划', '锁定特征变换、参数、预测、检验族、阈值及样本停止规则'), C['blue_bg'], C['blue'])
p.box('confirm', 550, 875, 620, 110,
      content('5  对 D_C,t 执行一次有效的独立确证', '输出效应量、区间及校正证据；记录 alpha_t 使用情况'), C['blue_bg'], C['blue'])
for a, b in [('start','protocol'), ('protocol','generate'), ('generate','evaluate'),
             ('evaluate','freeze'), ('freeze','confirm')]:
    p.edge(a+'-next', a, b, exit=(.5,1), entry=(.5,0))

p.box('support', 470, 1055, 300, 105,
      content('支持', '达到预设证据与效应要求<br>记录范围；安排必要复现'), C['teal_bg'], C['teal'])
p.box('inconclusive', 780, 1055, 320, 105,
      content('证据不足', '未显著 / 区间过宽 / 样本不足<br>不能解释为假说成立'), C['orange_bg'], C['orange'])
p.box('refute', 1140, 1055, 320, 105,
      content('反驳或不相容', '有效反例或预设反向证据<br>淘汰、限缩范围或修订'), C['red_bg'], C['red'])
for id, ex, px in [('support', .2, 620), ('inconclusive', .55, 940), ('refute', .85, 1300)]:
    p.edge('confirm-'+id, 'confirm', id, exit=(ex,1), entry=(.5,0), points=[(px,1020)])
p.box('update', 710, 1230, 500, 110,
      content('6  更新知识、假说状态与证据账本', '已见确证数据转为后续可见资料<br>修订产生 H_new；不得复用旧确证证据'), C['bg'])
for id, entry in [('support',.12), ('inconclusive',.46), ('refute',.85)]:
    p.edge(id+'-update', id, 'update', exit=(.5,1), entry=(entry,0), points=[(960,1195)] if id=='inconclusive' else None)
p.box('continue', 180, 1225, 230, 130, '<b>预算允许且<br>存在有价值的<br>下一步？</b>',
      C['orange_bg'], C['orange'], 19, False, 'shape=rhombus;align=center;spacing=4;')
p.edge('update-continue','update','continue',exit=(0,.5),entry=(1,.5),color=C['orange'])
p.box('acquire', 60, 425, 345, 150,
      content('7  选择下一条高价值证据', '候选分歧 / 反例 / 边界检验<br>采集前登记探索或确证用途<br>更新可见数据；t ← t + 1'), C['orange_bg'], C['orange'])
p.edge('continue-acquire','continue','acquire','是',C['orange'],
       exit=(0,.5),entry=(0,.5),points=[(30,1290),(30,500)])
p.edge('acquire-generate','acquire','generate','新一轮',C['orange'])
p.box('finish', 80, 1435, 430, 65, '<b>停止并交付：发现档案 + 未决问题</b>',
      C['ink'], C['ink'], 19, extra='fontColor=#FFFFFF;align=center;')
p.edge('continue-finish','continue','finish','否',C['orange'],exit=(.5,1),entry=(.5,0))
p.edge('empty-candidates','evaluate','continue','无候选：检查预算',C['muted'],True,
       exit=(0,.5),entry=(.5,0),points=[(435,627),(435,1190),(295,1190)])

p.box('budget-note', 60, 185, 345, 175,
      content('停止条件提前设定', '计算 / 采样 / 轮次预算耗尽<br>无有价值的候选或下一步<br>达到预定复现目标<br>禁止看普通 p 值后随意停采'), C['bg'])
p.box('revision-note', 60, 685, 345, 190,
      content('假说修订必须可追踪', '修改变量、参数或适用范围<br>均创建新版本并保留父版本<br><br>修订假说需要新的确证数据'), C['bg'])
p.box('role-note', 1310, 230, 420, 160,
      content('LLM 与执行器分工', 'LLM：提案、概念、代码草案、修订<br>执行器：运行、计算、检验、审计<br>LLM 不能替代数值证据裁决'), C['bg'])
p.box('eval-note', 1310, 440, 420, 165,
      content('开发评分不是最终显著性', 'D_E 内部交叉验证 + D_V 开发反馈<br>反复查看 D_V 会影响选择过程<br>最终结论仍需未见的 D_C,t'), C['purple_bg'], C['purple'])
p.box('alpha-note', 1310, 665, 420, 160,
      content('每轮显式管理检验预算', '示例：alpha_t = alpha / 2^t，t ≥ 1<br>批次内 Holm 校正（有效 p 值前提）<br>跨轮预算总和不超过 alpha'), C['blue_bg'], C['blue'])
p.box('causal-note', 1310, 875, 420, 120,
      content('机制主张需要额外证据', '仅有关联证据时标记为关联<br>因果主张需识别条件或干预证据'), C['bg'])
p.text('footer', 570, 1432, 1170, 62,
       '所有结果均保留：支持、失败与证据不足都进入账本。<br>本图为可实现的研究原型设计；统计结论依赖采样方案、检验假设与数据质量。',
       17,C['muted'])


# PAGE 3 — Data contracts and the crucial independent-evidence barrier.
p = Page('03 数据与证据隔离', 1800, 1270)
p.header('03', '数据可以反馈，独立性不能重复使用',
         '以主体、时间或环境为分组单位建立快照；同一对象的近重复记录不能跨分区泄漏。', 1800)
p.box('confirm-zone',1215,175,550,755,'',C['blue_bg'],C['blue'],extra='dashed=1;dashPattern=6 4;')
p.text('zone-label',1235,186,510,30,'独立确证区  /  冻结后按轮次授权访问',17,C['blue'],True)
data = [
    ('explore',60,230,500,390,'D_E  探索集',C['teal_bg'],C['teal'],
     '允许：表示学习、模式挖掘、参数拟合<br>允许：内部交叉验证、生成竞争假说<br><br>'
     '产物：固定变换、模型参数、候选结构<br>状态：探索器可见，可随新证据扩充'),
    ('develop',650,230,500,390,'D_V  开发评估集',C['orange_bg'],C['orange'],
     '允许：比较候选、检查稳定性与泛化<br>反馈可参与后续算法与假说选择<br><br>'
     '产物：开发指标、错误分析、候选排序<br>状态：多轮反馈后不再是独立确证证据'),
    ('holdout',1240,230,500,390,'D_C,t  第 t 轮确证集',C['white'],C['blue'],
     '来自封存未见数据，或预定方案的新采样<br>探索器与 LLM 不得读取未解封内容<br><br>'
     '只用于已冻结假说及其固定评估方案<br>产物：效应量、区间、校正证据与结论')]
for id,x,y,w,h,title,fill,stroke,body in data:
    p.box(id,x,y,w,h,'',fill,stroke)
    p.text(id+'-title',x+25,y+25,w-50,43,title,26,stroke,True)
    p.text(id+'-body',x+25,y+90,w-50,245,body,19,C['ink'])

p.box('engine',60,790,780,120,
      content('探索器：表示 → 模式 → 假说 → 开发评估',
              '保留候选版本与数据使用记录；训练拟合的变换在确认前固定。'),C['teal_bg'],C['teal'],21)
p.box('freeze',895,790,285,120,
      '<b>冻结闸门</b><br><span style="font-size:17px">假说 / 参数 / 预测<br>检验 / 阈值 / 停止规则</span>',
      C['ink'],C['ink'],22,extra='fontColor=#FFFFFF;align=center;')
p.box('validator',1240,790,500,120,
      content('确证执行器：一次有效的独立检验',
              '读取已冻结候选；输出支持 / 反驳 / 证据不足'),C['white'],C['blue'],21)
p.edge('explore-engine','explore','engine','训练 / 搜索',C['teal'],exit=(.5,1),entry=(.3,0))
p.edge('develop-engine','develop','engine','开发反馈',C['orange'],exit=(.5,1),entry=(.85,0),points=[(900,710),(723,710)])
p.edge('holdout-validator','holdout','validator','冻结后解封',C['blue'],exit=(.5,1),entry=(.5,0))
p.edge('engine-freeze','engine','freeze',color=C['teal'])
p.edge('freeze-validator','freeze','validator',color=C['blue'])

p.box('feedback',60,1045,1120,125,
      content('已见证据进入下一轮探索；修订后的假说重新冻结',
              'D_C,t 一旦被查看或用于修订，就标记为“已见”。它可以帮助学习，但不能再次独立确证 H_new。<br>'
              '确证 H_new 需要新的 D_C,t+1；主动采集的数据也必须在采集前登记用途和抽样方案。'),
      C['orange_bg'],C['orange'],21)
p.edge('validator-feedback','validator','feedback','结果与反例',C['orange'],
       exit=(0,.85),entry=(1,.4),points=[(1200,892),(1200,1095)])
p.edge('feedback-engine','feedback','engine','',C['orange'],True,
       exit=(0,.5),entry=(0,.6),points=[(30,1107),(30,862)])
p.text('feedback-label',60,965,360,32,'新版本 / 新探索',17,C['orange'])
p.box('error-budget',1240,1010,500,160,
      content('误差预算与证据边界',
              'alpha_t = alpha / 2^t；批内 Holm 校正<br>前提：对当前冻结假说，p 值有效<br>探索分数、新颖性、LLM 自信均不是确证证据'),
      C['bg'],C['line'])
p.text('footer',60,1210,1680,30,
       '输出必须注明知识库版本、数据快照、假说版本、适用范围及证据等级；“支持”仍是可修正的结论。',18,C['muted'])

ET.indent(mxfile, space='  ')
ET.ElementTree(mxfile).write(OUT, encoding='utf-8', xml_declaration=True)

# Structural validation: native editable objects, unique IDs and all edge endpoints.
summary = []
for d in mxfile.findall('diagram'):
    cells = d.findall('./mxGraphModel/root/mxCell')
    ids = {c.attrib['id'] for c in cells}
    assert len(ids) == len(cells)
    for c in cells:
        if c.get('edge') == '1':
            assert c.get('source') in ids and c.get('target') in ids
    summary.append((d.get('name'), sum(c.get('vertex') == '1' for c in cells),
                    sum(c.get('edge') == '1' for c in cells)))
print(f'Created {OUT.name}')
for row in summary:
    print(f'{row[0]}: {row[1]} editable nodes, {row[2]} connectors')
