#!/usr/bin/env python3
# W32a: 从 docs/mistery20260524.md 抽全部标题，三层归类生成 docs/theory_index.md
import json, re

data = json.load(open('/tmp/titles.json', encoding='utf-8'))
titles = data['titles']  # (chapter, line, title)

LAYER_COVER = '金标已覆盖'
LAYER_LABEL = '可标签'
LAYER_MIND = '仅心法'

def classify(ch, t):
    """返回 (层, 落点)。规则按标题关键词 + 章名兜底；顺序即优先级。"""
    # --- 金标已覆盖：AGENTS §2 / scorer 1.22.30 已进综合分的口径 ---
    if re.search(r'新手小白，给你们一个建议', t):
        return LAYER_COVER, 'basic_filter 年线滤网(MA5/10/20/60>MA250)+多头顺次'
    if '三振' in t or '三级共振' in t:
        return LAYER_COVER, 'three_resonance_analysis'
    if '破五反五' in t:
        return LAYER_COVER, 'check_po5_fan5'
    if re.search(r'主升|趋势行情5日线|强势板块短期见顶信号', t):
        return LAYER_COVER, 'main_bull_wave_signal/checklist8(沿MA5)'
    if re.search(r'平台|箱体结构', t):
        return LAYER_COVER, 'platform_breakthrough_analysis'
    if re.search(r'周线锚定|筑底|资金筑底', t):
        return LAYER_COVER, 'weekly_anchor_check'
    if re.search(r'零轴上死叉|零轴下死叉|MACD和RSI|MACD KDJ', t):
        return LAYER_COVER, 'checklist8「MACD零轴金叉」(RSI>50 亦在表内)'
    if re.search(r'缠论|背驰|三类买点|中枢里的博弈', t):
        return LAYER_COVER, 'czsc ChanStructure(混合分关闭；S_chan 仅 SCORE=1)'
    # --- 可标签：只读展示/打标签，不进分 ---
    if re.search(r'换手率|吸筹|筹码|放量|量比|量价|红三兵|高悬带量|小阳建仓', t):
        return LAYER_LABEL, 'turnover_tag(展示,不改chip_low)'
    if re.search(r'520|3天不创新高|三天陷阱', t):
        return LAYER_LABEL, 'turnover_tag/风格标签(展示,不进分)'
    if re.search(r'情绪', t):
        return LAYER_LABEL, 'style_tag(展示,不进分)'
    if re.search(r'趋势', t):
        return LAYER_LABEL, 'style_tag(展示,不进分)'
    if re.search(r'MACD|RSI|KDJ|乖离|BIAS|济安', t):
        return LAYER_LABEL, '指标展示(technical_detail_capture),不进分'
    if re.search(r'均线|20日线|5日、10日|斜率', t):
        return LAYER_LABEL, '均线展示(滤网已用MA250,余不进分)'
    if re.search(r'支撑|压力|回踩|破位|突破|空中加油|孕线|仙人指路|射击之星|缺口|顶底拐点|见底', t):
        return LAYER_LABEL, '形态/买卖点展示,不进分'
    if re.search(r'波浪|道氏|3浪|5浪|小5浪|第4浪', t):
        return LAYER_LABEL, '浪型展示(不与缠论混用),不进分'
    if re.search(r'集合竞价|看盘|挂单|委比|复盘|做T|正T|倒T|低成本', t):
        return LAYER_LABEL, '盘口技巧,不进分'
    if re.search(r'股票类型|机构票|游资票|庄股|杀猪盘|收割|股票特点|ETF|指数定投|宽基|微笑曲线|好股票|好股', t):
        return LAYER_LABEL, '标的分类展示,不进分'
    # --- 仅心法 ---
    if re.search(r'仓位|加仓|补仓|减仓|清仓|金字塔|半仓|做套|解套|逆势|重仓', t):
        return LAYER_MIND, '不进分'
    if re.search(r'龙头|打板|首板', t):
        return LAYER_MIND, '不进分'
    if ch.startswith('第五章') or re.search(r'宏观|经济|美元|日本|广场协议|安倍|中美|十五五|产业周期|双轨|流动性|数据面|军|大周期|改革|放[水电大]', t):
        return LAYER_MIND, '不进分'
    if ch.startswith('第六章'):
        return LAYER_MIND, '不进分'
    return LAYER_MIND, '不进分'

buckets = {LAYER_COVER: [], LAYER_LABEL: [], LAYER_MIND: []}
rows = []
for ch, ln, t in titles:
    layer, loc = classify(ch, t)
    buckets[layer].append(t)
    rows.append((ch, ln, t, layer, loc))

out = []
out.append('# 原论标题索引（theory_index）\n')
out.append('> 来源：`docs/mistery20260524.md`（W31 后由用户并入仓内，commit `259276d`）。')
out.append('> 提取：全部一级章名 + `## 【…】` 小节标题，共 %d 条（`##` 级全量，含 4 条 `[[…]]` Obsidian 页）。' % len(rows))
out.append('> 三层口径（W32 / docs/013.md）：**金标已覆盖** = 已进 Mystery 1.22.30 综合分或被其显式引用；**可标签** = 只做只读标签/展示、不进综合分；**仅心法** = 仓位/宏观/谜语等，明确不做成代码。\n')
out.append('> 分类由标题关键词 + 所在章粗分（脚本 `scripts/theory_index.py` 可重跑），**层为初判**，逐条精读修订走后续 PR；落点列引用 `AGENTS.md` §2.5 与 `mystery/core/` 现行函数名。\n')
out.append('## 汇总\n')
out.append('| 层 | 条数 |')
out.append('|----|-----:|')
out.append('| %s | %d |' % (LAYER_COVER, len(buckets[LAYER_COVER])))
out.append('| %s | %d |' % (LAYER_LABEL, len(buckets[LAYER_LABEL])))
out.append('| %s | %d |' % (LAYER_MIND, len(buckets[LAYER_MIND])))
out.append('| **合计** | **%d** |' % len(rows))
out.append('')
cur_ch = None
for ch, ln, t, layer, loc in rows:
    if ch != cur_ch:
        cur_ch = ch
        out.append('\n## %s\n' % ch)
        out.append('| 行 | 标题 | 层 | 落点 |')
        out.append('|----|------|----|------|')
    out.append('| %d | %s | %s | %s |' % (ln, t, layer, loc))

open('docs/theory_index.md', 'w', encoding='utf-8').write('\n'.join(out) + '\n')
print('titles:', len(rows), '| covered:', len(buckets[LAYER_COVER]),
      '| label:', len(buckets[LAYER_LABEL]), '| mind:', len(buckets[LAYER_MIND]))
