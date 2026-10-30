# -*- coding: utf-8 -*-
"""生成 50 份 ~50KB 混合格式随机测试文档，写入 out50/ 并产出 manifest.json。

格式组合（覆盖后端全部提取/切分路径）:
  md 13 | txt 11 | docx 10 | html 4 | csv 3 | json 3 | pdf 3 | xlsx 3

关键设计:
  - 内容随机（固定种子可复现），每份含 3 个唯一校验标记（内容归属抽查用）
  - 每份 45~58KB；文本类提取字符数 < 30000（后端 extract_char_budget）零截断，
    xlsx 压缩比高、字符数可能触顶截断——如实复刻后端截断逻辑记录期望值，
    且三个标记都放在文件头部行，确保落在预算内可被检索
  - manifest 记录每份文档的"后端期望描述文本 + 切块 metadata"（复刻 ai.py
    提取逻辑），校验脚本用真实后端 chunker（stub 掉 app.config 后直接加载）
    做逐字节比对
"""
import hashlib
import html as html_mod
import importlib.util
import io
import json
import random
import re
import sys
import types
import zipfile
from pathlib import Path

OUT_DIR = Path(__file__).parent / "out50"
OUT_DIR.mkdir(exist_ok=True)
BUDGET = 30000          # 后端 extract_char_budget
SIZE_MIN = 42 * 1024    # 文件大小下限（docx 压缩比高，内容受字符上限约束时放宽）
SIZE_TARGET = 49 * 1024 # 停止增长的目标线
SIZE_MAX = 58 * 1024    # 硬上限（xlsx 压缩粒度粗，放宽到 58KB）
CHARS_CAP = 28500       # 内容字符数硬停线：留余量确保不被后端 30000 预算截断
RNG = random.Random(20261029)

# ── 挂载真实后端 chunker（stub 掉 app.config，避免 pydantic 依赖）──
BACKEND = Path(__file__).parent.parent / "backend"


def load_chunker():
    app_pkg = types.ModuleType("app")
    app_pkg.__path__ = []
    cfg = types.ModuleType("app.config")

    class _S:
        embedding_max_chars = 3000

    cfg.settings = _S()
    sys.modules["app"] = app_pkg
    sys.modules["app.config"] = cfg
    spec = importlib.util.spec_from_file_location(
        "app.chunker", BACKEND / "app" / "chunker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chunker = load_chunker()

# ═══════════════════════════════════════════════════════════
#  随机内容生成
# ═══════════════════════════════════════════════════════════

THEMES = [
    ("对象存储", ["桶与对象键", "纠删码", "生命周期规则", "预签名链接", "版本控制", "存储类别", "跨区域复制", "故障域", "条带化", "一致性哈希"]),
    ("向量检索", ["嵌入模型", "近似最近邻索引", "混合检索", "重排序器", "召回率", "标量过滤", "乘积量化", "可导航小世界图", "倒排文件索引", "查询改写"]),
    ("检索增强生成", ["切片策略", "双路召回", "上下文窗口", "忠实度评估", "引用溯源", "语料治理", "倒数排序融合", "交叉编码器", "评估集", "数据飞轮"]),
    ("分布式共识", ["领导者选举", "日志复制", "多数派", "快照机制", "成员变更", "租约", "观察者节点", "故障检测器", "补偿事务", "幂等屏障"]),
    ("容器编排", ["命名空间", "控制组", "联合文件系统", "健康检查", "污点与容忍度", "滚动更新", "服务发现", "资源请求与限度", "准入控制", "镜像签名"]),
    ("微服务治理", ["服务注册中心", "熔断器", "限流阈值", "舱壁隔离", "契约测试", "链路追踪", "灰度分流", "依赖降级", "编排器", "旁路缓存"]),
    ("消息队列", ["分区与副本", "消费位点", "重平衡", "死信队列", "事务消息", "幂等消费", "回压", "顺序性保证", "堆积治理", "冷热分离"]),
    ("可观测性", ["结构化日志", "黄金指标", "分布式追踪", "采样策略", "告警收敛", "故障演练", "基数控制", "服务等级目标", "错误预算", "慢查询分析"]),
    ("数据加密", ["信封加密", "密钥轮转", "前向保密", "消息认证码", "证书透明度", "后量子迁移", "加盐慢哈希", "硬件安全模块", "静态加密", "传输层加固"]),
    ("持续交付", ["测试金字塔", "主干开发", "特性开关", "制品晋级", "蓝绿部署", "流水线分片", "依赖锁定", "变异测试", "环境即代码", "发布列车"]),
    ("数据库内核", ["缓冲池", "写前日志", "乐观并发控制", "索引下推", "查询计划", "脏页刷盘", "主从延迟", "分库分表", "版本号锁", "统计信息收集"]),
    ("网络协议", ["拥塞控制", "队头阻塞", "零拷贝", "多路复用", "握手开销", "长连接调优", "负载均衡哈希", "连接池", "超时预算", "重试风暴"]),
    ("前端工程", ["虚拟列表", "代码分割", "首屏指标", "水合开销", "状态管理", "组件契约", "请求去重", "骨架屏", "离线缓存", "构建缓存"]),
    ("安全攻防", ["最小权限", "横向移动", "凭据轮换", "沙箱逃逸", "供应链投毒", "注入防护", "审计留痕", "威胁建模", "入侵检测", "应急响应"]),
    ("项目度量", ["交付周期", "在制品限制", "缺陷逃逸率", "变更失败率", "恢复时间", "流负载", "技术债利率", "回滚演练", "容量水位", "服务等级核算"]),
]

ASPECT_BANK = [
    "核心抽象与设计取舍", "容量模型与成本曲线", "故障模式与恢复路径", "安全边界与权限收敛",
    "性能调优与参数校准", "演进路线与迁移策略", "监控指标与告警口径", "团队协作与流程落地",
    "边界条件与反例推演", "工程纪律与长期主义", "选型对比与适配场景", "自动化验证与回归防护",
    "数据一致性与对账兜底", "灰度发布与爆炸半径", "多租户隔离与配额治理",
]

METRICS = ["吞吐", "延迟", "可靠性", "成本", "可观测性", "安全水位", "扩展性", "交付节奏", "故障恢复速度", "资源利用率"]
PREFIXES = [
    "值得注意的是，", "从运维视角看，", "在容量规划层面，", "结合历史故障案例，", "对照行业基线，",
    "若把时间轴拉长，", "从成本模型出发，", "在演练复盘中，", "抛开具体产品差异，", "落实到值班手册里，",
    "把指标拆开看，", "沿着调用链追踪，", "站在评审者的立场，", "参考近期社区实践，", "回到第一性原理，",
]

TEMPLATES = [
    "在{theme}的体系中，{n1}与{n2}的协同方式直接决定了{m}的基本盘。",
    "当{n1}的规模跨过量级临界点后，围绕{n2}的重新设计往往比局部调参更有效，这是{theme}工程里被反复验证的规律。",
    "评审一套{theme}方案时，应当先核对{n1}的容量假设与失效假设，再讨论{n2}的具体选型，顺序颠倒会把讨论带进细节泥潭。",
    "{n1}的引入并非没有代价：它以额外的复杂度换取{m}的改善，如果业务规模撑不起这份复杂度，{n2}反而会成为新的故障源。",
    "很多{theme}事故的根因并不在算法层面，而是{n1}的默认配置被原样带进了生产环境，{n2}的边界条件从未被真正推演过。",
    "把{n1}的变更纳入灰度发布流程，配合{n2}的回归校验，可以把变更失败率压到可接受区间，这是{theme}侧最划算的投入。",
    "在多租户场景下，{n1}的配额治理必须先于业务扩张完成，否则邻居干扰问题会最先在{m}指标上暴露出来。",
    "{n1}与{n2}之间的一致性契约要写成可执行的校验，而不是停留在设计文档里，口头约定在人员流动后必然失真。",
    "每一次{n1}的扩容操作都应该附带回滚预案与观察窗口，{theme}侧的链路抖动往往在操作完成半小时后才真正显现。",
    "从对账数据看，{n1}的绝大部分不一致都能追溯到{n2}的并发写路径，补齐幂等屏障之后，差异量会呈台阶式下降。",
    "容量规划里最容易低估的是{n1}的峰值放大系数，{n2}的日常水位线看着健康，大促场景下却可能直接击穿。",
    "{theme}的演进要遵循数据先行原则：先让{n1}的可观测性到位，再动{n2}的架构，没有度量的重构等于闭眼开车。",
    "关于{n1}的三个关键问题是：为什么需要、范围能否更小、何时回收；这三问同样适用于{n2}的每一次扩权申请。",
    "把{n1}的运维手册浓缩成一页纸的应急卡片，比一份三百页的详尽文档更能缩短平均恢复时间，{theme}团队值得花时间做这件事。",
    "{n2}的选型评估应当包含退出成本：迁移窗口、数据双写周期、回切预案，缺了任何一项，{n1}的替换都会演变成持久战。",
    "在压测环境复现{n1}的热点问题时，务必保留{n2}的真实规模参数，缩小数量级的模拟往往得出方向性错误的结论。",
    "{n1}的自动伸缩策略要给足预热时间，冷启动的毛刺会被{n2}的队列放大，最终以超时的形式在用户端可感知。",
    "一次完整的{theme}故障演练应覆盖发现、定位、决策、执行、复盘五个环节，缺环的演练会在真实故障面前露怯。",
    "{n1}的历史版本要能按时间点恢复，{n2}的误操作窗口期才能被完整兜住，这是写入运维底线的硬性要求。",
    "统计口径的统一比指标本身更重要：{n1}的成功率如果与{n2}的计数方式不一致，任何汇总报表都会失去公信力。",
    "{n1}的依赖要显式声明并周期性巡检，隐性依赖是{theme}架构里最难发现的债务，它们只在没有防备的时刻发作。",
    "围绕{n2}建立基准测试集，让每次{theme}变更都有可比对的量化依据，情绪化争论会自然让位于数据讨论。",
    "权限收敛的第一步是盘点{n1}的存量授权，把无人认领的条目清理掉，再谈{n2}的最小化改造才有干净的起点。",
    "{n1}的实现应当把配置外置，硬编码的阈值会让{n2}的环境差异变成排查噪音，问题定位时间随环境数量线性上升。",
    "在{theme}的技术雷达里，{n1}处于采用阶段而{n2}仍在评估阶段，两者的推进节奏应该解耦，避免绑定决策放大风险。",
    "预算的编制要从{n1}的历史曲线外推，再叠加{n2}的规划增量，拍脑袋式的额度分配会在年底演变成资源争抢。",
    "{n1}的降级路径要在平时演练，真到{n2}不可用的时刻才第一次执行降级，几乎必然伴随操作失误与超时雪崩。",
    "文档化是{theme}知识的最后防线：{n1}的每一次架构决策都应留下背景与取舍记录，{n2}的踩坑记录比成功案例更珍贵。",
    "{n2}的容量告警要分级：预警线触发工单，严重线自动执行预案，把决策前置到平静时刻，是处置{theme}突发流量的关键。",
    "新成员上手{theme}模块的最佳路径是先读{n1}的应急手册再做一次模拟排障，直接扎进{n2}的代码细节反而容易迷失。",
    "{n1}的采样率设置要在成本与还原度之间取平衡，全量采集在流量高峰的账单会让人重新思考{m}的监控粒度。",
    "任何针对{n1}的优化都要先建立可复现的度量基线，{n2}层面的直觉在数据面前经常站不住脚。",
    "{theme}的配置变更走版本化评审，回滚到上一版的时间应当以分钟计，这份敏捷是{n1}与{n2}协同稳定的前提。",
    "{n1}的扩展点设计要克制，预留过多的钩子会让{n2}的升级路径互相纠缠，最终形成谁也不敢动的脆弱核心。",
    "值班体系里要为{theme}相关的告警单独立类，{n1}的告警响应时效要求与{n2}不同，混在一起会稀释关键信号的注意力。",
    "把{n1}的健康度纳入服务等级目标的核算，{n2}的承诺才立得住，否则对外承诺的{m}只是纸面数字。",
    "{n1}的多环境漂移要用基础设施即代码收敛，手工改动的每一处差异都是{theme}侧未来某次故障的伏笔。",
    "面对{n2}的技术债，还债窗口要与业务节奏错峰，在迭代间隙批量清理{theme}侧的遗留问题，比零敲碎打更有效率。",
    "{n1}的成功迁移离不开双写期的数据比对，以{n2}的存量记录为基准逐条核对，差异收敛后再切换读路径。",
    "在{theme}的架构评审里给{n1}的方案留一个反方席位，专门论证{n2}路径的失效场景，正反碰撞后的结论更经得起考验。",
    "{n1}的依赖升级要锁定版本并单独验证，{n2}的联动变更混在同一次发布里，会让问题的归因分析成倍复杂化。",
    "从长期主义看，{theme}团队在{n1}上建立的度量体系，其价值远超某次{n2}优化的瞬时收益，前者是可复用的资产。",
    "{n1}的告警文案要包含上下文与处置建议，只有指标名片的告警会把决策成本转嫁给值班同学，{m}的恢复自然变慢。",
    "对{n2}做容量压测时，记得把{n1}的联动模块一起拉进来，孤立模块的漂亮曲线在链路视角下常常是幻觉。",
    "为{theme}设定季度级的架构走查机制，逐项核对{n1}的现状与设计假设的偏离度，{n2}的腐化从来是渐进而无声的。",
]

MARKER_WORDS = ["青梧", "丹枫", "墨澜", "流萤", "霜降", "云枕", "听松", "拾贝", "归鸿", "抱朴",
                "疏影", "枕河", "衔泥", "踏歌", "栖霞", "望舒", "扶摇", "怀瑾", "若木", "闻笛",
                "沉璧", "霁月", "汀兰", "画荻", "驰星", "映雪", "听澜", "沐野", "知秋", "望岫"]


def make_sentence(theme, terms, rng):
    tpl = rng.choice(TEMPLATES)
    n1, n2 = rng.sample(terms, 2)
    s = tpl.format(theme=theme, n1=n1, n2=n2, m=rng.choice(METRICS))
    if rng.random() < 0.45:
        s = rng.choice(PREFIXES) + s
    return s


def make_paragraph(theme, terms, rng):
    n = rng.randint(4, 7)
    return "".join(make_sentence(theme, terms, rng) for _ in range(n))


def unique_marker(rng, used):
    while True:
        m = f"{rng.choice(MARKER_WORDS)}-{rng.randrange(0x10000):04x}"
        if m not in used:
            used.add(m)
            return m


# ═══════════════════════════════════════════════════════════
#  后端提取逻辑复刻（与 app/routers/ai.py 保持一致）
#  返回 (期望描述文本, chunk metadata)
# ═══════════════════════════════════════════════════════════

def parse_docx_bytes(data: bytes) -> str | None:
    """复刻 ai.py::_parse_docx"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml_bytes = zf.read("word/document.xml")
    except Exception:
        return None
    try:
        xml_text = xml_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            xml_text = xml_bytes.decode("latin-1")
        except Exception:
            return None
    paras = []
    for p_match in re.finditer(r"<w:p[ >].*?</w:p>", xml_text, re.DOTALL):
        parts = re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", p_match.group())
        if parts:
            paras.append(html_mod.unescape("".join(parts)))
    text = "\n".join(paras).strip()
    return text if text else None


def clean_pdf_text(text: str) -> str:
    """复刻 ai.py::_clean_pdf_text"""
    text = text.replace("\t", " ")
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return text.strip()


def extract_pdf_desc(data: bytes, filename: str):
    """复刻 ai.py::_extract_pdf_text：返回 (描述, {"pages","font_sizes"})"""
    import fitz
    doc = fitz.open(stream=data, filetype="pdf")
    pages, font_sizes, text_parts = [], [], []
    for i in range(doc.page_count):
        page = doc.load_page(i)
        page_text = page.get_text("text").strip()
        pages.append(page_text)
        try:
            blocks = page.get_text("dict").get("blocks", [])
            fs = 0
            for b in blocks:
                if b.get("type") == 0:
                    for ln in b.get("lines", []):
                        for sp in ln.get("spans", []):
                            fs = round(sp.get("size", 0))
                            break
                        if fs:
                            break
                if fs:
                    break
            font_sizes.append(fs)
        except Exception:
            font_sizes.append(0)
        if page_text:
            text_parts.append(page_text)
            if sum(len(t) for t in text_parts) > BUDGET * 2:
                break
    doc.close()
    raw = clean_pdf_text("\n".join(text_parts))
    if len(raw) > BUDGET:
        raw = raw[:BUDGET]
        last_period = raw.rfind("。")
        if last_period > BUDGET // 2:
            raw = raw[:last_period + 1]
    return f"文件名：{filename}\nPDF内容：{raw}", {"pages": pages, "font_sizes": font_sizes}


def extract_xlsx_desc(data: bytes, filename: str):
    """复刻 ai.py::_extract_xlsx_text：返回 (描述, {"sheets"})"""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts, sheets_meta = [], []
    for name in wb.sheetnames:
        ws = wb[name]
        sheet_parts = [f"【{name}】"]
        sheet_rows = []
        for row in ws.iter_rows(values_only=True):
            values = [str(v) if v is not None else "" for v in row]
            if not any(v for v in values):
                continue
            sheet_rows.append(values)
            sheet_parts.append("\t".join(values))
            total = sum(len(p) for p in parts) + sum(len(p) for p in sheet_parts)
            if total > BUDGET * 2:
                break
        sheets_meta.append({"name": name, "rows": sheet_rows})
        if sheet_rows:
            parts.extend(sheet_parts)
    wb.close()
    raw = clean_pdf_text("\n".join(parts))
    if len(raw) > BUDGET:
        raw = raw[:BUDGET]
        last_line = raw.rfind("\n")
        if last_line > BUDGET // 2:
            raw = raw[:last_line]
    return f"文件名：{filename}\n表格内容：\n{raw}", {"sheets": sheets_meta}


def text_desc(content: str, filename: str):
    """复刻 ai.py::_format_text_description（文本类无 metadata）"""
    return f"文件名：{filename}\n文件内容：{content[:BUDGET]}", None


# ═══════════════════════════════════════════════════════════
#  各格式装配器：blueprint -> (bytes, desc, meta)
# ═══════════════════════════════════════════════════════════

def asm_txt(bp):
    blocks = [
        bp["title"],
        f"（主题域：{bp['theme']}｜校验标记：{bp['markers'][0]}）",
        f"引言 {bp['theme']}综述",
        bp["intro"],
    ]
    for i, (h, paras) in enumerate(bp["sections"], 1):
        blocks.append(f"第{i}节 {h}")
        blocks.extend(paras)
    blocks.append("结语")
    blocks.append(bp["concl"])
    content = "\n\n".join(blocks)
    return content.encode("utf-8"), text_desc(content, bp["filename"])


def asm_md(bp):
    blocks = [
        f"# {bp['title']}",
        f"> 摘要：本文围绕{bp['theme']}展开，校验标记 {bp['markers'][0]}。",
        bp["intro"],
    ]
    for h, paras in bp["sections"]:
        blocks.append(f"## {h}")
        blocks.extend(paras)
    blocks.append("## 结语")
    blocks.append(bp["concl"])
    content = "\n\n".join(blocks)
    return content.encode("utf-8"), text_desc(content, bp["filename"])


def asm_html(bp):
    blocks = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        '<head><meta charset="utf-8">',
        f"<title>{bp['title']}</title></head>",
        "<body>",
        f"<h1>{bp['title']}</h1>",
        f"<p>主题域：{bp['theme']}，校验标记：<b>{bp['markers'][0]}</b>。</p>",
        f"<p>{bp['intro']}</p>",
    ]
    for h, paras in bp["sections"]:
        blocks.append(f"<h2>{h}</h2>")
        blocks.extend(f"<p>{p}</p>" for p in paras)
    blocks.append("<h2>结语</h2>")
    blocks.append(f"<p>{bp['concl']}</p>")
    blocks.append("</body></html>")
    content = "\n\n".join(blocks)
    return content.encode("utf-8"), text_desc(content, bp["filename"])


def asm_csv(bp):
    rows = [",".join(["记录编号", "主题域", "核心对象", "关联机制", "指标维度", "摘要"])]
    rows.append(",".join(["0", bp["theme"], "文档说明", "全链路", "综合",
                          f"{bp['title']}——{bp['intro']}"]))
    rows.append(",".join(["1", bp["theme"], "纲要", "全链路", "综合",
                          f"中段标记：{bp['markers'][1]}；收尾标记：{bp['markers'][2]}。"]))
    for i, (h, paras) in enumerate(bp["sections"], 1):
        for j, p in enumerate(paras):
            terms = bp["terms"]
            rows.append(",".join([
                str(i * 100 + j), bp["theme"], terms[j % len(terms)],
                terms[(j + 3) % len(terms)], METRICS[j % len(METRICS)], p,
            ]))
    rows.append(",".join(["9999", bp["theme"], "总结", "全链路", "综合", bp["concl"]]))
    content = "\n".join(rows) + "\n"
    return content.encode("utf-8"), text_desc(content, bp["filename"])


def asm_json(bp):
    obj = {
        "文档标题": bp["title"],
        "主题域": bp["theme"],
        "校验标记": list(bp["markers"]),
        "引言": {"概述": bp["intro"], "标记": bp["markers"][0]},
        "章节": [
            {"编号": i, "标题": h, "要点": paras}
            for i, (h, paras) in enumerate(bp["sections"], 1)
        ],
        "结语": {"总结": bp["concl"], "标记": bp["markers"][2]},
    }
    content = json.dumps(obj, ensure_ascii=False, indent=1)
    return content.encode("utf-8"), text_desc(content, bp["filename"])


def asm_docx(bp):
    from docx import Document
    from docx.shared import Pt
    from docx.oxml.ns import qn

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)
    style._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")

    doc.add_heading(bp["title"], level=0)
    doc.add_paragraph(f"（主题域：{bp['theme']}｜校验标记：{bp['markers'][0]}）")
    doc.add_paragraph(bp["intro"])
    for h, paras in bp["sections"]:
        doc.add_heading(h, level=1)
        for p in paras:
            doc.add_paragraph(p)
    doc.add_heading("结语", level=1)
    doc.add_paragraph(bp["concl"])

    buf = io.BytesIO()
    doc.save(buf)
    data = buf.getvalue()
    extracted = parse_docx_bytes(data)
    return data, text_desc(extracted, bp["filename"])


def asm_pdf(bp):
    import fitz

    def in_token(text, j):
        # 切点 j 处前后字符同属一个 ASCII token（字母/数字/连字符）→ 不能切
        tok = lambda c: c.isascii() and (c.isalnum() or c == "-")
        return tok(text[j - 1]) and tok(text[j])

    def wrap(text, width=40):
        lines, i, n = [], 0, len(text)
        while i < n:
            j = min(i + width, n)
            while j < n and j - i < width + 10 and in_token(text, j):
                j += 1
            lines.append(text[i:j])
            i = j
        return lines or [""]

    lines = []
    lines += wrap(f"《{bp['title']}》 {bp['theme']}专题 校验标记：{bp['markers'][0]}")
    lines.append("")
    lines += wrap(bp["intro"])
    lines.append("")
    for h, paras in bp["sections"]:
        lines.append(f"【{h}】")
        for p in paras:
            lines += wrap(p)
            lines.append("")
    lines.append("【结语】")
    lines += wrap(bp["concl"])

    doc = fitz.open()
    page, y = None, 0.0
    for ln in lines:
        if page is None or y > 780:
            page = doc.new_page()
            y = 72
        page.insert_text((54, y), ln, fontname="china-s", fontsize=10.5)
        y += 19.5
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    data = buf.getvalue()
    desc, meta = extract_pdf_desc(data, bp["filename"])
    return data, (desc, meta)


def asm_xlsx(bp):
    """三个标记全部放进头部行：xlsx 字符数可能触顶 30000 截断，头部必然在预算内"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "明细数据"
    ws.append(["记录编号", "主题域", "核心对象", "关联机制", "指标维度", "摘要", "标记"])
    ws.append([0, bp["theme"], "文档说明", "全链路", "综合",
               f"{bp['title']}——{bp['intro']}", bp["markers"][0]])
    ws.append([1, bp["theme"], "纲要", "全链路", "综合",
               f"中段标记 {bp['markers'][1]}；收尾标记 {bp['markers'][2]}。", ""])
    seq = 2
    for i, (h, paras) in enumerate(bp["sections"], 1):
        for j, p in enumerate(paras):
            ws.append([seq, bp["theme"], bp["terms"][j % len(bp["terms"])],
                       bp["terms"][(j + 3) % len(bp["terms"])],
                       METRICS[j % len(METRICS)], p, ""])
            seq += 1
    ws.append([999999, bp["theme"], "总结", "全链路", "综合", bp["concl"], ""])
    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()
    desc, meta = extract_xlsx_desc(data, bp["filename"])
    return data, (desc, meta)


ASSEMBLERS = {
    "txt": asm_txt, "md": asm_md, "html": asm_html, "csv": asm_csv,
    "json": asm_json, "docx": asm_docx, "pdf": asm_pdf, "xlsx": asm_xlsx,
}

# 格式配额：md 13 | txt 11 | docx 10 | html 4 | csv 3 | json 3 | pdf 3 | xlsx 3 = 50
FORMAT_QUOTA = [("md", 13), ("txt", 11), ("docx", 10), ("html", 4),
                ("csv", 3), ("json", 3), ("pdf", 3), ("xlsx", 3)]

TITLE_SUFFIX = ["实践考辩", "工程纪要", "体系剖析", "复盘笔记", "设计与权衡", "应用指北"]
NAME_SUFFIX = ["实践", "纪要", "剖析", "笔记", "权衡", "指北"]


def build_blueprint(fmt, idx, rng, used_markers, used_names):
    theme_name, terms = rng.choice(THEMES)
    while True:
        tag = f"{rng.randrange(0x10000):04x}"
        title = f"{theme_name}{rng.choice(TITLE_SUFFIX)}（{tag}）"
        if title not in used_names:
            used_names.add(title)
            break
    markers = [unique_marker(rng, used_markers) for _ in range(3)]
    used_markers.update(markers)
    intro = make_paragraph(theme_name, terms, rng) + f"（基准标记：{markers[0]}）"
    mid = make_paragraph(theme_name, terms, rng) + f"（中段标记：{markers[1]}）"
    concl = make_paragraph(theme_name, terms, rng) + f"（收尾标记：{markers[2]}）"
    return {
        "title": title, "theme": theme_name, "terms": terms,
        "intro": intro, "mid": mid, "concl": concl, "markers": markers,
        "filename": f"{theme_name}{rng.choice(NAME_SUFFIX)}_{idx:02d}_{tag}.{fmt}",
    }


def gen_section(bp, rng):
    """生成一个新章节（增量式，不重复生成已有内容）"""
    heading = f"{bp['theme']}{rng.choice(ASPECT_BANK)}"
    paras = [make_paragraph(bp["theme"], bp["terms"], rng)
             for _ in range(rng.randint(3, 5))]
    return (heading, paras)


def with_mid(bp, sections):
    """把中段标记段落插入中间章节（不改原列表）"""
    if not sections:
        return []
    k = len(sections) // 2
    return [(h, paras + [bp["mid"]]) if i == k else (h, paras)
            for i, (h, paras) in enumerate(sections)]


def grow_to_size(bp, assembler, max_sections=40, chars_cap=CHARS_CAP):
    """增量加节直到达到大小目标；chars_cap 防止内容超过后端字符预算被截断。
    内容一旦生成不再重random；超大小硬上限则回退末节。"""
    sections = []
    while True:
        sections.append(gen_section(bp, RNG))
        cand = dict(bp)
        cand["sections"] = with_mid(bp, sections)
        data, (desc, meta) = assembler(cand)
        if (len(data) >= SIZE_TARGET or len(sections) >= max_sections
                or (chars_cap and len(desc) >= chars_cap)):
            break
    # 超过大小硬上限、或单节增量跨过字符预算线（28500+单节≈2000 可能越过 30000）则回退末节
    while (len(data) > SIZE_MAX or (chars_cap and len(desc) > 29500)) and len(sections) > 5:
        sections.pop()
        cand = dict(bp)
        cand["sections"] = with_mid(bp, sections)
        data, (desc, meta) = assembler(cand)
    return cand, data, desc, meta


def main():
    used_markers, used_names, used_md5 = set(), set(), set()
    manifest = []
    plan = [(fmt, i) for fmt, cnt in FORMAT_QUOTA for i in range(cnt)]
    RNG.shuffle(plan)

    for idx, (fmt, seq) in enumerate(plan, 1):
        bp = build_blueprint(fmt, idx, RNG, used_markers, used_names)
        # xlsx 压缩比高：不设字符上限，让它触顶后端 30000 预算（标记已前置到头部行）
        cand, data, desc, meta = grow_to_size(
            bp, ASSEMBLERS[fmt],
            max_sections=200 if fmt == "xlsx" else 40,
            chars_cap=None if fmt == "xlsx" else CHARS_CAP)
        path = OUT_DIR / cand["filename"]
        path.write_bytes(data)

        md5 = hashlib.md5(data).hexdigest()
        assert md5 not in used_md5, f"内容重复: {cand['filename']}"
        used_md5.add(md5)

        chunks = chunker.chunk_text(fmt, desc, metadata=meta)
        assert len(chunks) >= 2, f"切块数不足: {cand['filename']} -> {len(chunks)}"
        joined = "".join(c.text for c in chunks)
        for kw in cand["markers"]:
            if kw not in joined:
                in_desc = kw in desc
                print(f"!! 标记丢失诊断: {kw} in_desc={in_desc} in_joined=False "
                      f"chunks={len(chunks)} desc_len={len(desc)}")
                if in_desc:
                    p = desc.find(kw)
                    print("   desc 上下文:", repr(desc[max(0, p - 30):p + 30]))
                    for ci, c in enumerate(chunks):
                        if kw[:3] in c.text or kw[-3:] in c.text:
                            print(f"   部分命中 chunk[{ci}]: {c.text[:60]!r}")
                raise SystemExit(f"{cand['filename']} 标记 {kw} 未落入切块")

        manifest.append({
            "file": cand["filename"], "md5": md5, "type": fmt, "size": len(data),
            "desc": desc, "meta": meta, "keywords": list(cand["markers"]),
            "local_chunks": len(chunks),
        })
        print(f"[{idx:02d}/50] {cand['filename']} {len(data)/1024:.1f}KB "
              f"desc={len(desc)}字符 预计切块={len(chunks)}")

    assert len(manifest) == 50
    for m in manifest:
        assert SIZE_MIN <= m["size"] <= SIZE_MAX, \
            f"{m['file']} 大小越界: {m['size']}"

    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    from collections import Counter
    fmt_cnt = Counter(m["type"] for m in manifest)
    avg = sum(m["size"] for m in manifest) / 50 / 1024
    total_chunks = sum(m["local_chunks"] for m in manifest)
    max_desc = max(len(m["desc"]) for m in manifest)
    truncated = sum(1 for m in manifest if len(m["desc"]) >= BUDGET)
    print(f"\n50 份生成完毕: {dict(fmt_cnt)}")
    print(f"平均 {avg:.1f}KB | desc 最长 {max_desc} 字符（预算 {BUDGET}，触顶 {truncated} 份）"
          f" | 预计总切块 {total_chunks}")
    print("manifest.json 已写入 out50/")


if __name__ == "__main__":
    main()
