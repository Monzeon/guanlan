#!/usr/bin/env python
"""
观澜 · 种子数据。
把认知平台方法论里的真实案例直接灌成开箱即用的库：
口径断裂、vintage 修订、T0/T2/T3 对照、待验证命题、预测与基准率示例。
仅示例用公开常识级数据，精确数值请以官方发布为准（这本身就是平台要教的事）。
"""

from __future__ import annotations

import json
import sqlite3


def run(conn: sqlite3.Connection) -> None:
    n = conn.execute("SELECT COUNT(*) n FROM claims").fetchone()["n"]
    if n > 0:
        return  # 已有数据，不重复灌

    # ── 来源 ─────────────────────────────────────────
    sources = [
        ("S-2026-0001", "国家统计局年度数据公报", "国家统计局", "https://data.stats.gov.cn", "T0", "2026-02-28", "data"),
        ("S-2026-0002", "人社部城镇调查失业率说明", "人力资源和社会保障部", "", "T0", "2024-01-17", "report"),
        ("S-2026-0003", "第四次全国经济普查公报", "国务院新闻办", "", "T0", "2020-01-17", "report"),
        ("S-2026-0004", "货币政策执行报告（社融口径说明）", "中国人民银行", "", "T1", "2026-08-15", "report"),
        ("S-2026-0005", "某财经博主视频：青年失业率解读", "（示例）自媒体", "", "T3", "2026-10-01", "web"),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO sources (code,title,publisher,url,tier,published,kind) VALUES (?,?,?,?,?,?,?)",
        sources)

    # ── Claims：跨 tier 对照 + 互相矛盾样例 ──────────
    claims = [
        ("C-2026-0001", "claim",
         "2018年1月起国家统计局正式发布全国城镇调查失业率，此前官方主要口径为登记失业率，两序列不可直接比较",
         "economy", ["城镇调查失业率", "口径"], "S-2026-0002", "T1", 0.85, "2018-01-31",
         "若发现2018年前存在连续的官方调查失业率月度序列，本条错误",
         ["统计口径", "就业"], "两个口径分母不同：登记口径=户籍+主动登记；调查口径=城镇常住人口抽样。"),
        ("C-2026-0002", "claim",
         "第四次全国经济普查对2018年GDP初步核算数进行了向上修订，修订幅度约为2.1%",
         "economy", ["GDP", "经济普查"], "S-2026-0003", "T0", 0.92, "2020-01-17",
         "若普查公报未包含GDP修订条目，本条错误",
         ["GDP", "数据修订"], "这正是时序库必须存 vintage 的原因：基于旧值的叙事在修订后全部要重算。"),
        ("C-2026-0003", "claim",
         "社会融资规模存量统计在2018年经历口径调整，将地方专项债纳入统计范围",
         "economy", ["社融", "口径"], "S-2026-0004", "T1", 0.85, "2018-12-31",
         "若央行口径说明中无此调整记录，本条错误",
         ["社融", "统计口径"], "同年还纳入了资产支持证券；跨2018年的社融增速比较必须注明口径。"),
        ("C-2026-0004", "claim",
         "2025年中国GDP同比增速为5.0%左右（以最终核实数为准）",
         "economy", ["GDP"], "S-2026-0001", "T0", 0.90, "2025-12-31",
         "以统计局最终核实数为准，偏离±0.2pp以上本条降级",
         ["GDP", "增速"], "示例条目：初步核算数会被修订，请以 vintage 最新值为准。"),
        ("C-2026-0005", "claim",
         "2025年中国出生人口约为954万人",
         "population", ["出生人口"], "S-2026-0001", "T0", 0.88, "2025-12-31",
         "若统计局年报修订偏离±3%以上，本条降级",
         ["人口", "出生率"], "示例条目。"),
        ("C-2026-0006", "claim",
         "2025年中国出生人口约为820万人",
         "population", ["出生人口"], "S-2026-0005", "T2", 0.60, "2025-12-31",
         "与统计局公布数对照，偏离即证伪",
         ["人口"], "示例：与 C-2026-0005 构成同实体同年数值冲突，用于演示矛盾检测。"),
        ("C-2026-0007", "question", "",
         "economy", ["青年失业率", "口径"], "S-2026-0005", "T3", 0.30, "2026-10-01",
         "以新口径完整序列与PMI就业分项交叉验证，若方向一致则问题关闭",
         ["口径", "就业"], "T3 内容的标准处理：吸收视角，不吸收结论。",
         "博主称新口径下16-24岁青年失业率系统性低于旧口径的真实水平，这是否成立？",
         "拉取新口径月度序列，与不受口径影响的PMI就业分项、农民工监测调查三方对照"),
    ]
    for c in claims:
        code, kind, st, domain, ents, src, tier, conf, as_of, falsifier, tags, notes = c[:12]
        question = c[12] if len(c) > 12 else ""
        resolution = c[13] if len(c) > 13 else ""
        src_id = conn.execute("SELECT id FROM sources WHERE code=?", (src,)).fetchone()
        conn.execute(
            """INSERT OR IGNORE INTO claims (code,kind,statement,question,resolution,domain,entities,
               source_id,tier,confidence,as_of,observed_at,falsifier,status,tags,notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (code, kind, st, question, resolution, domain,
             json.dumps(ents, ensure_ascii=False),
             src_id["id"] if src_id else None, tier, conf, as_of, "2026-10-31",
             falsifier, "draft" if kind == "question" else "active",
             json.dumps(tags, ensure_ascii=False), notes))

    # C-2026-0004 vs 假想保守值：演示 contradicts 双向声明缺失
    conn.execute(
        """UPDATE claims SET links=? WHERE code='C-2026-0005'""",
        (json.dumps({"contradicts": ["C-2026-0006"]}),))

    # ── 时序：两条真实口径示例序列（示意数据 + vintage）──
    series = [
        ("CN.GDP.REAL.YOY", "中国GDP不变价同比", "%", "Q", "国家统计局", "累计同比；2019年起基础数据经四经普修订"),
        ("CN.UNEMP.SURVEY.LEVEL", "城镇调查失业率", "%", "M", "国家统计局", "2018-01 起发布，与登记失业率不可比"),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO series (code,name,unit,freq,source,definition) VALUES (?,?,?,?,?,?)",
        series)

    obs = []
    # 调查失业率（真实形态示意：5% 上下波动 + 2020-02 跳升 + 季节性）
    for period, v in [("2023-01","5.3"),("2023-02","5.6"),("2023-03","5.3"),("2023-04","5.2"),
                      ("2023-05","5.2"),("2023-06","5.2"),("2023-07","5.3"),("2023-08","5.2"),
                      ("2023-09","5.0"),("2023-10","5.0"),("2023-11","5.1"),("2023-12","5.1"),
                      ("2024-01","5.2"),("2024-02","5.3"),("2024-03","5.2"),("2024-04","5.0"),
                      ("2024-05","5.0"),("2024-06","5.0"),("2024-07","5.2"),("2024-08","5.3"),
                      ("2024-09","5.1"),("2024-10","5.0"),("2024-11","5.0"),("2024-12","5.1")]:
        obs.append(("CN.UNEMP.SURVEY.LEVEL", period, v, "%", "2026-10-31"))
    # GDP 累计同比 + 一次修订演示（2023-Q2 在新 vintage 中被修订）
    for period, v, vt in [
        ("2023-Q1","4.5","2024-01-20"),("2023-Q2","5.5","2024-01-20"),
        ("2023-Q3","5.2","2024-01-20"),("2023-Q4","5.2","2024-01-20"),
        ("2024-Q1","5.3","2025-01-18"),("2024-Q2","5.0","2025-01-18"),
        ("2024-Q3","4.6","2025-01-18"),("2024-Q4","5.0","2025-01-18"),
        ("2023-Q2","5.6","2025-01-18"),  # 修订：5.5 → 5.6，留痕
    ]:
        obs.append(("CN.GDP.REAL.YOY", period, v, "%", vt))
    conn.executemany(
        "INSERT OR IGNORE INTO observations (code,period,value,unit,vintage) VALUES (?,?,?,?,?)", obs)

    # ── 口径登记簿 ───────────────────────────────────
    conn.executemany(
        "INSERT OR IGNORE INTO calibers (series_code,changed_at,description,comparable) VALUES (?,?,?,?)",
        [
            ("CN.UNEMP.SURVEY.LEVEL", "2018-01",
             "调查失业率首次正式发布，替代登记失业率成为主要口径；两序列不可连接", 0),
            ("CN.UNEMP.SURVEY.LEVEL", "2024-01",
             "16-24岁青年分项改为『不含在校生』口径，与2023年6月前数据不可比", 0),
            ("CN.GDP.REAL.YOY", "2020-01",
             "第四次经济普查修订基础数据，2018及以前年份 GDP 上修约2.1%", 0),
            ("CN.GDP.REAL.YOY", "2023-12",
             "社会融资规模口径已在2018年调整（纳入专项债、ABS），涉及相关核算输入时注意", 1),
        ])

    # ── 基准率库（方案原文的参考类 + 数值刻意留空）──
    conn.executemany(
        """INSERT OR IGNORE INTO baserates (code,name,question,reference_class,window,
           sample_size,base_rate,low_confidence,note) VALUES (?,?,?,?,?,?,?,?,?)""",
        [
            ("BR-MACRO-001", "GDP增速落入指定区间频率", "给定年初，当年GDP增速落在[x,x+1)的概率",
             "1992-2025年GDP不变价同比分布", "1992-2025", 34, None, 0,
             "回测方法：取年度序列按1pp分箱。多数人对增速的判断过于依赖当年叙事。"),
            ("BR-MKT-002", "房价下行周期时长", "本轮房地产调整还可能持续多久",
             "1970-2020全球20个主要经济体名义房价调整周期", "1970-2020", None, None, 0,
             "外部视角：内部视角总说『这次不一样』，外部视角给分布。"),
            ("BR-DEMO-001", "人口负增长可逆性", "人口负增长开始后10年内恢复正增长的频率",
             "1950-2020所有进入负增长的经济体（排除战争饥荒）", "1950-2020", None, None, 0,
             "分布里几乎没有回头案例——这是判断『中国人口能不能回来』最硬的外部视角。"),
            ("BR-HIST-001", "大一统王朝存续时长", "一个中央集权王朝通常维持多久",
             "秦至清主要统一王朝", "-221至1912", 9, None, 1,
             "样本9个，只作校准直觉的参照系，不作推断依据。"),
        ])

    # ── 预测示例（一条 open 一条 resolved，演示 Brier）──
    conn.executemany(
        """INSERT OR IGNORE INTO predictions (code,proposition,probability,criteria,resolve_by,
           base_rate,depends_on,pre_mortem,status,outcome,brier,resolved_at,lesson)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            ("P-2026-0001", "2027年底前，70城一线二手住宅价格指数较2026年中低点回升超过5%",
             0.35, "国家统计局70城二手住宅价格指数，一线四城简单平均，截至2027-12-31",
             "2027-12-31", "BR-MKT-002", json.dumps(["C-2026-0004"]),
             "错的话最可能错在：把政策底当成市场底，低估库存去化时间",
             "open", None, None, None, ""),
            ("P-2026-0002", "2026年内央行下调LPR至少一次",
             0.80, "1年期LPR报价低于2025-12-31水平即算", "2026-12-31",
             "", json.dumps([]), "高估了外部汇率约束的强度",
             "resolved", 1, 0.04, "2026-06-20", "政策转向的时点判断比我预想的更快——记录：对宽松方向的方向感可靠，时点偏保守。"),
        ])

    # ── 决策示例 ─────────────────────────────────────
    conn.execute(
        """INSERT OR IGNORE INTO decisions (code,decision,date,stakes,reversible,depends_on,chosen_because)
           VALUES (?,?,?,?,?,?,?)""",
        ("D-2026-0001", "示例：将时序库从『只存最新值』改为 vintage 追加制",
         "2026-10-31", "high", 0,
         json.dumps(["C-2026-0002"]),
         "四经普修订案例说明：不存旧值就无法复盘当时的判断。"))

    conn.commit()
    print("种子数据已灌入：7 claims / 2 series / 33 observations / 4 calibers / 4 baserates / 2 predictions / 1 decision")
