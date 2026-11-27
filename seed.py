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

# 示例数据涉及的所有表：任意一张有内容都不算空库。
# 只看 claims 是不够的——真实库可能剪藏很多、随手记很多，但还没用过命题功能，
# 那样一份库会被当成空库，灌进 7 条带 T0/高置信度的演示命题，
# 用户根本分不出哪些是自己的。
_SEED_TABLES = ("claims", "sources", "series", "observations", "calibers",
                "baserates", "predictions", "decisions", "clips", "clip_notes",
                "clip_annotations", "boards", "ai_outputs", "events")

# 示例条目的编号统一带 DEMO- 前缀：DEMO-C-0001 / DEMO-S-0001 / DEMO-P-0001 …
# 不加这个前缀时，示例编号与 next_code() 生成的真实编号同构（都是 DEMO-C-0001），
# 于是示例条目会：① 占用用户的编号序列，第一条真实命题从 0008 起编；
# ② 让 [DEMO-C-0005] 这类引用可能指向示例而不是用户自己的证据。
# 前缀把两套编号彻底分开，即使用户哪天删了库里的示例，编号空间也不会错乱。
DEMO = "DEMO-"


def _library_is_empty(conn: sqlite3.Connection) -> bool:
    """库是否完全空。表不存在（旧库）算空。"""
    for table in _SEED_TABLES:
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            continue        # 这张表在老库里没有，跳过
        if n:
            return False
    return True


def run(conn: sqlite3.Connection) -> None:
    if not _library_is_empty(conn):
        return  # 已有数据，一行示例都不灌

    # ── 来源 ─────────────────────────────────────────
    sources = [
        ("DEMO-S-0001", "国家统计局年度数据公报", "国家统计局", "https://data.stats.gov.cn", "T0", "2026-02-28", "data"),
        ("DEMO-S-0002", "人社部城镇调查失业率说明", "人力资源和社会保障部", "", "T0", "2024-01-17", "report"),
        ("DEMO-S-0003", "第四次全国经济普查公报", "国务院新闻办", "", "T0", "2020-01-17", "report"),
        ("DEMO-S-0004", "货币政策执行报告（社融口径说明）", "中国人民银行", "", "T1", "2026-08-15", "report"),
        ("DEMO-S-0005", "某财经博主视频：青年失业率解读", "（示例）自媒体", "", "T3", "2026-10-01", "web"),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO sources (code,title,publisher,url,tier,published,kind) VALUES (?,?,?,?,?,?,?)",
        sources)

    # ── Claims：跨 tier 对照 + 互相矛盾样例 ──────────
    claims = [
        ("DEMO-C-0001", "claim",
         "2018年1月起国家统计局正式发布全国城镇调查失业率，此前官方主要口径为登记失业率，两序列不可直接比较",
         "economy", ["城镇调查失业率", "口径"], "DEMO-S-0002", "T1", 0.85, "2018-01-31",
         "若发现2018年前存在连续的官方调查失业率月度序列，本条错误",
         ["统计口径", "就业"], "两个口径分母不同：登记口径=户籍+主动登记；调查口径=城镇常住人口抽样。"),
        ("DEMO-C-0002", "claim",
         "第四次全国经济普查对2018年GDP初步核算数进行了向上修订，修订幅度约为2.1%",
         "economy", ["GDP", "经济普查"], "DEMO-S-0003", "T0", 0.92, "2020-01-17",
         "若普查公报未包含GDP修订条目，本条错误",
         ["GDP", "数据修订"], "这正是时序库必须存 vintage 的原因：基于旧值的叙事在修订后全部要重算。"),
        ("DEMO-C-0003", "claim",
         "社会融资规模存量统计在2018年经历口径调整，将地方专项债纳入统计范围",
         "economy", ["社融", "口径"], "DEMO-S-0004", "T1", 0.85, "2018-12-31",
         "若央行口径说明中无此调整记录，本条错误",
         ["社融", "统计口径"], "同年还纳入了资产支持证券；跨2018年的社融增速比较必须注明口径。"),
        ("DEMO-C-0004", "claim",
         "2025年中国GDP同比增速为5.0%左右（以最终核实数为准）",
         "economy", ["GDP"], "DEMO-S-0001", "T0", 0.90, "2025-12-31",
         "以统计局最终核实数为准，偏离±0.2pp以上本条降级",
         ["GDP", "增速"], "示例条目：初步核算数会被修订，请以 vintage 最新值为准。"),
        ("DEMO-C-0005", "claim",
         "2025年中国出生人口约为954万人",
         "population", ["出生人口"], "DEMO-S-0001", "T0", 0.88, "2025-12-31",
         "若统计局年报修订偏离±3%以上，本条降级",
         ["人口", "出生率"], "示例条目。"),
        ("DEMO-C-0006", "claim",
         "2025年中国出生人口约为820万人",
         "population", ["出生人口"], "DEMO-S-0005", "T2", 0.60, "2025-12-31",
         "与统计局公布数对照，偏离即证伪",
         ["人口"], "示例：与 DEMO-C-0005 构成同实体同年数值冲突，用于演示矛盾检测。"),
        ("DEMO-Q-0001", "question", "",
         "economy", ["青年失业率", "口径"], "DEMO-S-0005", "T3", 0.30, "2026-10-01",
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

    # DEMO-C-0005 与 DEMO-C-0006 同实体同年数值冲突：只在 0005 一侧登记 contradicts，
    # 用于演示「双向声明缺失」这个检测点。
    conn.execute(
        """UPDATE claims SET links=? WHERE code='DEMO-C-0005'""",
        (json.dumps({"contradicts": ["DEMO-C-0006"]}),))

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
    # calibers 表没有 UNIQUE 约束，INSERT OR IGNORE 永远不会忽略任何一行：
    # 重灌一次同一条口径就多一行。改用「先按 (series_code, changed_at) 删旧再插」。
    cal_rows = [
        ("CN.UNEMP.SURVEY.LEVEL", "2018-01",
         "调查失业率首次正式发布，替代登记失业率成为主要口径；两序列不可连接", 0),
        ("CN.UNEMP.SURVEY.LEVEL", "2024-01",
         "16-24岁青年分项改为『不含在校生』口径，与2023年6月前数据不可比", 0),
        ("CN.GDP.REAL.YOY", "2020-01",
         "第四次经济普查修订基础数据，2018及以前年份 GDP 上修约2.1%", 0),
        ("CN.GDP.REAL.YOY", "2023-12",
         "社会融资规模口径已在2018年调整（纳入专项债、ABS），涉及相关核算输入时注意", 1),
    ]
    conn.executemany(
        "DELETE FROM calibers WHERE series_code=? AND changed_at=?",
        [(row[0], row[1]) for row in cal_rows])
    conn.executemany(
        "INSERT INTO calibers (series_code,changed_at,description,comparable) VALUES (?,?,?,?)",
        cal_rows)

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
            ("DEMO-P-0001", "2027年底前，70城一线二手住宅价格指数较2026年中低点回升超过5%",
             0.35, "国家统计局70城二手住宅价格指数，一线四城简单平均，截至2027-12-31",
             "2027-12-31", "BR-MKT-002", json.dumps(["DEMO-C-0004"]),
             "错的话最可能错在：把政策底当成市场底，低估库存去化时间",
             "open", None, None, None, ""),
            ("DEMO-P-0002", "2026年内央行下调LPR至少一次",
             0.80, "1年期LPR报价低于2025-12-31水平即算", "2026-12-31",
             "", json.dumps([]), "高估了外部汇率约束的强度",
             "resolved", 1, 0.04, "2026-06-20", "政策转向的时点判断比我预想的更快——记录：对宽松方向的方向感可靠，时点偏保守。"),
        ])

    # ── 决策示例 ─────────────────────────────────────
    conn.execute(
        """INSERT OR IGNORE INTO decisions (code,decision,date,stakes,reversible,depends_on,chosen_because)
           VALUES (?,?,?,?,?,?,?)""",
        ("DEMO-D-0001", "示例：将时序库从『只存最新值』改为 vintage 追加制",
         "2026-10-31", "high", 0,
         json.dumps(["DEMO-C-0002"]),
         "四经普修订案例说明：不存旧值就无法复盘当时的判断。"))

    conn.commit()
    # 按实际行数报，别写死常量（写死的那份和真实行数对不上过）
    def _count(table: str) -> int:
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    print("种子数据已灌入："
          f"{_count('claims')} claims / {_count('series')} series / "
          f"{_count('observations')} observations / {_count('calibers')} calibers / "
          f"{_count('baserates')} baserates / {_count('predictions')} predictions / "
          f"{_count('decisions')} decision / {_count('sources')} sources")


# ══════════════════════════════════════════════════════════
# 存量库迁移：把早先用真实编号灌进去的示例条目改成 DEMO- 编号
# ══════════════════════════════════════════════════════════
#
# 改代码只对「新灌的库」有效。已经在库里躺着的老示例（C-2026-0001…0007、
# S-2026-0001…0005、P/D-2026-0001）不会自己改名，得单独跑一次。
#
# 迁移只认「编号 + 内容」双命中：编号对得上、内容也对得上才改，
# 免得把用户自己写的、恰好占着同一个编号的条目改掉。

_LEGACY_ROWS = (
    # (表, 旧编号, 新编号, 校验列, 该列应包含的片段)
    ("sources", "S-2026-0001", "DEMO-S-0001", "title", "国家统计局年度数据公报"),
    ("sources", "S-2026-0002", "DEMO-S-0002", "title", "人社部城镇调查失业率说明"),
    ("sources", "S-2026-0003", "DEMO-S-0003", "title", "第四次全国经济普查公报"),
    ("sources", "S-2026-0004", "DEMO-S-0004", "title", "货币政策执行报告"),
    ("sources", "S-2026-0005", "DEMO-S-0005", "title", "某财经博主视频"),
    ("claims", "C-2026-0001", "DEMO-C-0001", "statement", "2018年1月起国家统计局"),
    ("claims", "C-2026-0002", "DEMO-C-0002", "statement", "第四次全国经济普查对2018年GDP"),
    ("claims", "C-2026-0003", "DEMO-C-0003", "statement", "社会融资规模存量统计在2018年"),
    ("claims", "C-2026-0004", "DEMO-C-0004", "statement", "2025年中国GDP同比增速"),
    ("claims", "C-2026-0005", "DEMO-C-0005", "statement", "2025年中国出生人口约为954"),
    ("claims", "C-2026-0006", "DEMO-C-0006", "statement", "2025年中国出生人口约为820"),
    ("claims", "C-2026-0007", "DEMO-Q-0001", "question", "博主称新口径下16-24岁"),
    ("predictions", "P-2026-0001", "DEMO-P-0001", "proposition", "2027年底前"),
    ("predictions", "P-2026-0002", "DEMO-P-0002", "proposition", "2026年内央行下调LPR"),
    ("decisions", "D-2026-0001", "DEMO-D-0001", "decision", "示例：将时序库"),
)

# 引用示例编号的 JSON 列。改编号时这些地方要同步改，否则引用变死链。
_REF_COLUMNS = (
    ("claims", "links"),          # {"supports":[…],"contradicts":[…],"refines":[…]}
    ("predictions", "depends_on"),
    ("decisions", "depends_on"),
)


def legacy_demo_plan(conn: sqlite3.Connection) -> dict:
    """算一遍迁移方案，不动数据。

    返回 {"renames": [(表, 旧编号, 新编号, 行 id)], "refs": [(表, id, 旧 JSON, 新 JSON)],
          "problems": [说明]}
    已经迁移过（旧编号不存在）时 renames 为空，可以重复执行。
    """
    renames, refs, problems = [], [], []
    claim_map = {}

    for table, old, new, column, needle in _LEGACY_ROWS:
        row = conn.execute(f"SELECT id,{column} AS probe FROM {table} WHERE code=?",
                           (old,)).fetchone()
        if row is None:
            continue                      # 这条早就不在，或已经迁移过
        if needle not in (row["probe"] or ""):
            problems.append(f"{table}.{old} 编号对得上但内容不符（期望含「{needle}」），"
                            f"疑似用户自己的条目，跳过")
            continue
        taken = conn.execute(f"SELECT 1 FROM {table} WHERE code=?", (new,)).fetchone()
        if taken:
            problems.append(f"{table}.{new} 已被占用，无法迁移 {old}")
            continue
        renames.append((table, old, new, row["id"]))
        if table == "claims":
            claim_map[old] = new

    # 引用改写：只动真的含有旧编号的行
    for table, column in _REF_COLUMNS:
        if not claim_map:
            break
        rows = conn.execute(f"SELECT id,{column} AS raw FROM {table}").fetchall()
        for row in rows:
            raw = row["raw"] or "[]"
            if not any(old in raw for old in claim_map):
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                problems.append(f"{table}#{row['id']}.{column} 不是合法 JSON，未改")
                continue
            if isinstance(data, dict):
                data = {k: [claim_map.get(c, c) for c in v] if isinstance(v, list) else v
                        for k, v in data.items()}
            elif isinstance(data, list):
                data = [claim_map.get(c, c) if isinstance(c, str) else c for c in data]
            new_raw = json.dumps(data, ensure_ascii=False)
            if new_raw != raw:
                refs.append((table, row["id"], raw, new_raw))

    return {"renames": renames, "refs": refs, "problems": problems}


def apply_legacy_demo_codes(conn: sqlite3.Connection, plan: dict) -> tuple[int, int]:
    """按方案改库，一个事务内完成。返回 (改了几条编号, 改了几处引用)。"""
    n_code = 0
    for table, old, new, row_id in plan["renames"]:
        conn.execute(f"UPDATE {table} SET code=? WHERE id=?", (new, row_id))
        n_code += 1
    n_ref = 0
    for table, row_id, _old, new_raw in plan["refs"]:
        column = "links" if table == "claims" else "depends_on"
        conn.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (new_raw, row_id))
        n_ref += 1
    conn.commit()
    return n_code, n_ref
