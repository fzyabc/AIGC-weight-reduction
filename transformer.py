# -*- coding: utf-8 -*-
"""
AIGC降重规则引擎
基于实战经验总结的多层次降重策略，核心思路：打破AI文本的可预测性。

策略层级：
  Level 1 - 连接词替换：将AI偏好的序列词替换为因果链式表达
  Level 2 - 句式重构：打破对称结构，主动改被动，插入设问
  Level 3 - 人味注入：添加主观标记、具体语境、场景描述
  Level 4 - 段落重组：拆分过长段落，打乱总分总结构
"""
import random
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# ============================================================
# DYNAMIC_PROMPTS 互斥分组
# 每组内只能选一条，避免矛盾指令（如同时拆句和合句）
# ============================================================
DYNAMIC_PROMPT_GROUPS = [
    # 组A：句子结构（拆/合互斥）
    [
        '尝试把一个长句拆成两个短句，制造长短交替的节奏感。',
        '把两个相邻短句合成一个带从句的长句，让表达更紧凑。',
    ],
    # 组B：叙述手法
    [
        '挑选一处用反问或设问引出下文，但不要超过一处，避免刻意。',
        '把某个抽象概括改成具体的例子或场景描述，让读者能”看到画面”。',
    ],
    # 组C：结构调整
    [
        '调整段落内部的叙述顺序：比如先说结论再展开，或先抛问题再给答案。',
        '把”A具有B特点”改成”在B方面，A表现为……”这类句式倒装来打破模板感。',
    ],
]


def _pick_dynamic_prompts(k: int = 2, rng: Optional[random.Random] = None) -> list[str]:
    """从互斥分组中各取一条, 保证不会抽到矛盾指令"""
    _rng = rng or random
    groups = [g[:] for g in DYNAMIC_PROMPT_GROUPS]
    _rng.shuffle(groups)
    picked = []
    for group in groups:
        if len(picked) >= k:
            break
        picked.append(_rng.choice(group))
    return picked



# ============================================================
# 替换规则库（已清洗：移除过于口语化的替换词）
# ============================================================

SEQUENCE_CONNECTORS = {
    '首先': [
        '从源头来看', '先说一个前提',
        '问题的起点在于', '最根本的一点是',
    ],
    '其次': [
        '接下来', '在此基础上', '沿着这个思路往下',
        '第二点是',
    ],
    '此外': [
        '还有一点', '除了这些', '另外',
        '从另一面来看',
    ],
    '最后': [
        '回过头来看', '到这里可以说',
        '最后还要提到的是',
    ],
    '综上所述': [
        '总的来看', '回过头再看这些',
        '几点综合来看',
    ],
    '总而言之': [
        '归结起来', '总的来说', '概括地说',
    ],
    '其一': [
        '第一点', '一个方面是',
    ],
    '其二': [
        '第二点', '还有一方面',
    ],
    '其三': [
        '第三点', '另外还有',
    ],
    '一方面': [
        '从一面看', '就某个角度来说',
    ],
    '另一方面': [
        '换个角度', '从另一面看', '反过来看',
    ],
}

ACADEMIC_CONNECTORS = {
    '因此': ['所以', '这样一来', '正因如此', '这也就意味着', '结果就是'],
    '然而': ['不过', '但实际情况是', '话虽如此', '可问题在于', '但反过来看'],
    '同时': ['在这个过程中', '伴随这一变化', '也是在这一背景下'],
    '可以看出': ['能看出来', '从中可以发现', '这说明', '由此来看'],
    '具有重要意义': ['具有较强的现实意义', '在实践中较为突出', '产生了显著影响'],
    '值得注意的是': ['要注意的是', '有个细节值得留意', '比较特别的是'],
    '需要指出的是': ['要说明的是', '这里有个关键点', '必须提到的一点是'],
}

# ============================================================
# 人味标记三级词池
# 论文默认：TIER_ACADEMIC 全用 + TIER_SEMI 少量 + TIER_COLLOQUIAL 禁用
# ============================================================
MARKERS_ACADEMIC = [
    '换句话说', '严格来说', '从这个意义上说',
    '更具体地说', '进一步看', '换个角度看',
    '这也说明', '需要注意', '值得关注的是',
]

MARKERS_SEMI = [
    '仔细想想', '举个例子来说', '有意思的是',
    '反过来想', '细究起来',
]

MARKERS_COLLOQUIAL = [
    '说白了', '通俗地讲', '这里要多说一句',
    '稍微展开一下', '往深了说', '直白一点',
    '简单来讲', '坦率地说',
]

# 向后兼容：HUMAN_MARKERS 现在默认只包含学术层 + 半口语层
HUMAN_MARKERS = MARKERS_ACADEMIC + MARKERS_SEMI

SCENARIO_TEMPLATES = [
    '设想一个具体的场景：{context}',
    '以一个典型案例来说明：{context}',
    '换一种方式来理解——{context}',
    '举一个直观的例子：{context}',
    '把这个问题放到具体情境中看就很清楚了：{context}',
]

SYMMETRY_BREAKERS = [
    ('。{connector}', '——{connector}'),
    ('。{connector}', '；顺着这个思路，'),
    ('。{connector}', '。\n换个角度来说，'),
]


# ============================================================
# 多轮递进改写策略
# ============================================================

class RewriteStrategy(Enum):
    """改写策略枚举，每轮闭环使用不同策略"""
    STANDARD = 'standard'           # 标准改写
    HIGH_VARIANCE = 'high_variance' # 大幅改变句子长度分布
    STRUCTURAL = 'structural'       # 打乱信息顺序，改变因果链方向
    MERGE_SPLIT = 'merge_split'     # 拆段或与相邻段合并
    HUMANIZE = 'humanize'           # 注入人类独有内容引导


# 每个策略对应的 system prompt 追加片段
STRATEGY_PROMPT_FRAGMENTS = {
    RewriteStrategy.STANDARD: '',  # 使用基础 SYSTEM_PROMPT

    RewriteStrategy.HIGH_VARIANCE: (
        '\n\n## 本轮重点：句长分布差异化\n'
        '这段文字上一轮改写后仍然被检测为 AI 生成，原因是句子长度过于均匀。\n'
        '你必须刻意制造句长差异：\n'
        '- 必须有至少一个极短句（10字以内），如"这很关键。""问题在于此。"\n'
        '- 必须有至少一个长复合句（40字以上），用"因为……所以……而且……"等嵌套结构\n'
        '- 相邻两句的长度差距至少要达到 2 倍\n'
        '- 不要让任何连续三句话的长度接近（差距<5字）\n'
        '这比保持语句优美更重要——检测器靠句长方差来判断。'
    ),

    RewriteStrategy.STRUCTURAL: (
        '\n\n## 本轮重点：信息顺序重组\n'
        '这段文字已经被改写过但仍被检测为 AI，因为信息的排列顺序仍然遵循 AI 的"总分总"模式。\n'
        '你必须：\n'
        '- 把原文最后的结论/总结提到最前面说\n'
        '- 把原文开头的背景铺垫移到中间作为补充说明\n'
        '- 把"A 导致 B"改为"B 的出现，追溯其原因，是 A"\n'
        '- 把并列的几个点按重要性倒序排列（把最不重要的放前面，最重要的放最后）\n'
        '- 可以用插入语打断线性叙事，如"——这里需要说明的是——"\n'
        '信息完整性不变，但叙述路径必须与原文完全不同。'
    ),

    RewriteStrategy.MERGE_SPLIT: (
        '\n\n## 本轮重点：段落结构重组\n'
        '这段文字经过多轮改写仍被检测，需要从段落结构层面打破模式。\n'
        '你必须：\n'
        '- 如果原文是一个长段，把它拆成 2-3 个短段落（用换行分隔）\n'
        '- 如果原文包含多个并列论点，把前两点合成一句紧凑的话，第三点展开详细说\n'
        '- 改变论证的"粒度"：把概括性的话展开成具体描述，把啰嗦的细节压缩成一句话\n'
        '- 可以在段落中间插入一句转折性的短评\n'
        '保持语义完整，但段落的节奏和密度必须变化。'
    ),

    RewriteStrategy.HUMANIZE: (
        '\n\n## 本轮重点：人类写作痕迹\n'
        '这段文字需要注入真实人类写作的痕迹来降低 AI 检测率。\n'
        '你必须（至少做到 2 点）：\n'
        '- 在论述中加入一个稍微犹豫/不确定的表达，如"大致可以认为""或许更准确的说法是"\n'
        '- 用一个不太常见但准确的词替换一个常见词（不是故意用生僻词，而是选择更精确的同义词）\n'
        '- 在某处加入一个带具体语境的例子引导，如"以XX领域的实践来看"\n'
        '- 故意让某一句的表达不那么"完美"——比如稍微冗余、或者用口语化的连接词"而且""也就是"\n'
        '- 句子之间可以有轻微的逻辑跳跃（人类写作中常见），不必每句都平滑过渡\n'
        '核心：完美的文字=AI的文字。人类文字有"毛边"。'
    ),
}


@dataclass
class TransformRule:
    """单条变换规则"""
    name: str
    pattern: str
    replacements: list[str]
    priority: int = 0  # 越高越优先
    context_required: bool = False


@dataclass
class TransformResult:
    """单次变换结果"""
    paragraph_index: int
    original: str
    transformed: str
    rules_applied: list[str]
    change_ratio: float  # 文本变化比例
    error: Optional[dict] = None


class Transformer:
    """AIGC降重变换器"""

    def __init__(self, aggressiveness: int = 2, seed: Optional[int] = None):
        """
        aggressiveness: 降重力度 (1=轻微, 2=中等, 3=激进)
        seed: 随机种子，传入固定值可复现结果，用于回归测试和 A/B 对比
        """
        self.aggressiveness = aggressiveness
        self._used_markers = set()
        self._rng = random.Random(seed)

    def protect_keywords(self, text: str,
                         protected_words: Optional[list[str]] = None) -> tuple[str, dict[str, str]]:
        """将指定术语替换为占位符，避免改写时被误伤。"""
        if not text or not protected_words:
            return text, {}

        normalized = []
        for word in protected_words:
            w = str(word or '').strip()
            if w and w not in normalized:
                normalized.append(w)

        normalized.sort(key=len, reverse=True)
        protected_text = text
        placeholder_map: dict[str, str] = {}

        for word in normalized:
            if word not in protected_text:
                continue
            token = f'[KEYWORD_{len(placeholder_map)}]'
            protected_text = re.sub(re.escape(word), token, protected_text)
            placeholder_map[token] = word

        return protected_text, placeholder_map

    def restore_keywords(self, text: str, placeholder_map: Optional[dict[str, str]] = None) -> str:
        """将占位符还原为原始术语。"""
        if not text or not placeholder_map:
            return text

        restored = text
        for token, word in placeholder_map.items():
            restored = restored.replace(token, word)
        return restored

    def transform(self, text: str, risk_level: str = 'medium',
                  protected_words: Optional[list[str]] = None) -> TransformResult:
        """
        对单段文本进行降重变换。

        Args:
            text: 原始段落文本
            risk_level: 风险等级 ('high', 'medium', 'low')
            protected_words: 需要保护的术语列表
        """
        if len(text.strip()) < 15:
            return TransformResult(
                paragraph_index=-1,
                original=text,
                transformed=text,
                rules_applied=[],
                change_ratio=0,
            )

        rules_applied = []
        protected_text, placeholder_map = self.protect_keywords(text, protected_words)
        result = protected_text

        if placeholder_map:
            rules_applied.append(f'术语保护 × {len(placeholder_map)}')

        result, applied = self._replace_sequence_connectors(result)
        rules_applied.extend(applied)

        result, applied = self._replace_academic_connectors(result)
        rules_applied.extend(applied)

        if risk_level in ('high', 'medium') or self.aggressiveness >= 2:
            result, applied = self._break_symmetry(result)
            rules_applied.extend(applied)

        if risk_level == 'high' or self.aggressiveness >= 2:
            result, applied = self._inject_human_markers(result)
            rules_applied.extend(applied)

        if risk_level == 'high' and self.aggressiveness >= 3:
            result, applied = self._restructure_sentences(result)
            rules_applied.extend(applied)

        result = self.restore_keywords(result, placeholder_map)
        change_ratio = 1 - _text_overlap(text, result)

        return TransformResult(
            paragraph_index=-1,
            original=text,
            transformed=result,
            rules_applied=rules_applied,
            change_ratio=change_ratio,
        )

    def _replace_sequence_connectors(self, text: str) -> tuple[str, list[str]]:
        """替换AI偏好的序列连接词"""
        applied = []
        for original, alternatives in SEQUENCE_CONNECTORS.items():
            if original in text:
                positions = [m.start() for m in re.finditer(re.escape(original), text)]
                for pos in reversed(positions):
                    replacement = self._rng.choice(alternatives)
                    text = text[:pos] + replacement + text[pos + len(original):]
                applied.append(f'序列词替换: {original} → {replacement}')
        return text, applied

    def _replace_academic_connectors(self, text: str) -> tuple[str, list[str]]:
        """替换常见学术连接词为更具变化的表达"""
        applied = []
        for original, alternatives in ACADEMIC_CONNECTORS.items():
            if original in text:
                count = text.count(original)
                if count == 1:
                    replacement = self._rng.choice(alternatives)
                    text = text.replace(original, replacement, 1)
                    applied.append(f'连接词替换: {original} → {replacement}')
                else:
                    for _ in range(count):
                        replacement = self._rng.choice(alternatives)
                        text = text.replace(original, replacement, 1)
                    applied.append(f'连接词替换: {original} × {count}')
        return text, applied

    def _break_symmetry(self, text: str) -> tuple[str, list[str]]:
        """打破句式对称性"""
        applied = []

        parallel_markers = ['；', '，又', '，也', '，还']
        semicolons = [m.start() for m in re.finditer('；', text)]
        if len(semicolons) >= 3:
            mid = semicolons[len(semicolons) // 2]
            text = text[:mid] + '。' + text[mid + 1:]
            applied.append('打破并列：将分号拆为句号')

        if self.aggressiveness >= 2:
            if re.search(r'是.*?是.*?是', text) and len(text) > 80:
                idx = text.rfind('是')
                if idx > 20:
                    before = text[:idx]
                    after = text[idx:]
                    text = before + '可以说' + after[1:]
                    applied.append('打破重复"是"字句式')

        return text, applied

    def _inject_human_markers(self, text: str) -> tuple[str, list[str]]:
        """注入人类写作痕迹——三种注入形态轮换，避免固定模板"""
        applied = []
        available = [m for m in HUMAN_MARKERS if m not in self._used_markers]
        if not available:
            self._used_markers.clear()
            available = HUMAN_MARKERS.copy()

        sentences = re.split(r'(?<=[。！？])', text)
        sentences = [s for s in sentences if s.strip()]
        if len(sentences) < 3:
            return text, applied

        marker = self._rng.choice(available)
        self._used_markers.add(marker)

        insert_pos = self._rng.randint(1, min(4, len(sentences) - 1))
        s = sentences[insert_pos].strip()
        if not s or any(s.startswith(m) for m in HUMAN_MARKERS):
            return text, applied

        # 三种注入形态随机轮换；短段落禁用 bridge 避免突兀
        modes = ['prefix', 'mid', 'bridge'] if len(sentences) >= 5 else ['prefix', 'mid']
        mode = self._rng.choice(modes)
        if mode == 'prefix':
            # 句首插入：marker，...
            sentences[insert_pos] = marker + '，' + s
        elif mode == 'mid':
            # 句中插入：找第一个逗号后插入
            comma_pos = s.find('，')
            if comma_pos > 0 and comma_pos < len(s) - 2:
                sentences[insert_pos] = s[:comma_pos + 1] + marker + '，' + s[comma_pos + 1:]
            else:
                sentences[insert_pos] = marker + '，' + s
        else:
            # 独立短句桥接：marker。原句
            sentences[insert_pos] = marker + '。' + s
        applied.append(f'插入衔接({mode}): {marker}')

        text = ''.join(sentences)
        return text, applied

    def _restructure_sentences(self, text: str) -> tuple[str, list[str]]:
        """重构句子结构（激进模式）——只做拆分长句，不做机械的主被动转换"""
        applied = []

        long_sentences = re.findall(r'[^。！？]{60,}[。！？]', text)
        if long_sentences:
            target = long_sentences[0]
            commas = [m.start() for m in re.finditer('，', target)]
            if len(commas) >= 3:
                mid_comma = commas[len(commas) // 2]
                new_sentence = target[:mid_comma] + '。' + target[mid_comma + 1:]
                text = text.replace(target, new_sentence, 1)
                applied.append('拆分长句')

        return text, applied

    def batch_transform(self, paragraphs: dict[int, str],
                        risk_map: Optional[dict] = None) -> list[TransformResult]:
        """
        批量变换多个段落。

        Args:
            paragraphs: {段落索引: 原始文本}
            risk_map: {段落索引: 风险等级字符串}，不传则全部按 medium 处理
        """
        results = []
        for idx, text in paragraphs.items():
            level = 'medium'
            if risk_map and idx in risk_map:
                level = risk_map[idx]
            result = self.transform(text, level)
            result.paragraph_index = idx
            results.append(result)
        return results


def _text_overlap(a: str, b: str) -> float:
    """计算两段文本的重叠比例（基于 bigram，对顺序变化和结构调整敏感）"""
    if not a or not b:
        return 0.0
    a_clean = re.sub(r'\s+', '', a)
    b_clean = re.sub(r'\s+', '', b)
    if len(a_clean) < 2:
        return 0.0
    a_bigrams = [a_clean[i:i+2] for i in range(len(a_clean) - 1)]
    b_bigrams = set(b_clean[i:i+2] for i in range(len(b_clean) - 1))
    if not a_bigrams:
        return 0.0
    matches = sum(1 for bg in a_bigrams if bg in b_bigrams)
    return matches / len(a_bigrams)


# ============================================================
# 预置策略组合
# ============================================================

def get_strategy_description(aggressiveness: int) -> str:
    """返回当前策略等级的描述"""
    descriptions = {
        1: "轻微降重 — 仅替换序列连接词和常见学术套话，改动幅度最小",
        2: "中等降重 — 在替换连接词的基础上，打破句式对称，注入主观标记",
        3: "激进降重 — 全面重构：拆长句、主被动转换、注入场景描述和人味表达",
    }
    return descriptions.get(aggressiveness, descriptions[2])


class AITransformer:
    """
    基于LLM API的深度降重变换器。
    每次实例化时动态传入 API 配置，不依赖环境变量。
    """

    SYSTEM_PROMPT = (
        '你的任务：把用户给的学术段落用”人类研究生真正写论文”的方式重新写一遍，'
        '目标是让 AIGC 检测工具判定为人类写作。\n\n'
        '## 核心原则\n'
        '你要做的不是”在原文上换词”，而是：理解原文要表达的意思，然后用自己的话重新组织语言。'
        '想象你是一个读完这段内容后、合上书凭记忆重新写的学生。\n\n'
        '## 必须做到\n'
        '- 语义完全忠实于原文，不丢信息、不加信息\n'
        '- 专业术语、数据、法条编号、人名、案例名原封不动保留\n'
        '- 句子之间的逻辑关系（因果、并列、转折、递进）保持不变\n\n'
        '## 改写手法（自然运用，不要全部硬套）\n'
        '- 重新组织句子结构：拆句、合句、倒装、变换主语\n'
        '- 用同义但不同词根的表达替换（不是近义词硬换，要语义通顺）\n'
        '- 改变叙述切入角度：比如原文从”A的特点是B”可以改为”在B方面，A……”\n'
        '- 可以合并原文中相邻的两三个点到一句话里，也可以把一个长句拆成两句，打乱原文的”一句一个点”节奏\n'
        '- 句子长短要有明显变化：一段话里应该有短句（10字以内）也有长句（30字以上），不要每句都差不多长\n'
        '- 不要按照原文顺序逐句改写，可以调整信息的先后顺序，比如把结论提前、把细节后移\n\n'
        '## 严禁\n'
        '- 禁止使用”笔者认为””不可否认的是””坦白讲”等套话——这些已被标记为 AI 降重痕迹\n'
        '- 禁止使用”首先/其次/最后””总而言之””综上所述”等序列连接词\n'
        '- 禁止使用”与此同时””除此之外””此外””从而””进而””不仅如此””值得一提的是””说到底””实际上””具体来看””也就是说”等AI高频连接词，用”而且””也””所以””这样”等日常用词代替\n'
        '- 禁止逐词逐句按原文顺序对照替换——这种模式本身就会被检测到\n'
        '- 设问句（”……怎么……？””……能做什么？”）整段最多用一次，不用也完全可以\n'
        '- 禁止改变原文的专业含义\n\n'
        '直接输出改写后的段落，不要输出解释。'
    )

    def __init__(self, api_config: Optional[dict] = None, *,
                 api_key: str = '',
                 api_url: str = "https://api.openai.com/v1",
                 model: str = "gpt-3.5-turbo",
                 temperature: float = 0.85,
                 seed: Optional[int] = None):
        """初始化 AI 变换器。seed 控制 prompt 选择的随机性，便于回归测试。"""
        cfg = api_config or {}
        self._rng = random.Random(seed)
        self.api_key = str(cfg.get('api_key') or api_key or '').strip()

        raw_url = str(cfg.get('api_url') or api_url or '').strip() or 'https://api.openai.com/v1'
        self.api_url = raw_url.rstrip('/')
        if not self.api_url.endswith('/v1') and '/v1' not in self.api_url:
            self.api_url += '/v1'

        self.model = str(cfg.get('model') or model or 'gpt-3.5-turbo').strip()

        raw_temp = cfg.get('temperature', temperature)
        try:
            self.temperature = float(raw_temp)
        except (TypeError, ValueError):
            self.temperature = float(temperature)
        self.temperature = max(0.0, min(2.0, self.temperature))

        raw_max_tokens = cfg.get('max_tokens', 1536)
        try:
            self.max_tokens = int(raw_max_tokens)
        except (TypeError, ValueError):
            self.max_tokens = 1536
        self.max_tokens = max(256, min(4096, self.max_tokens))

    def protect_keywords(self, text: str,
                         protected_words: Optional[list[str]] = None) -> tuple[str, dict[str, str]]:
        """将指定术语替换为占位符，避免改写时被误伤。"""
        if not text or not protected_words:
            return text, {}
        normalized = []
        for word in protected_words:
            w = str(word or '').strip()
            if w and w not in normalized:
                normalized.append(w)
        normalized.sort(key=len, reverse=True)
        protected_text = text
        placeholder_map: dict[str, str] = {}
        for word in normalized:
            if word not in protected_text:
                continue
            token = f'[KEYWORD_{len(placeholder_map)}]'
            protected_text = re.sub(re.escape(word), token, protected_text)
            placeholder_map[token] = word
        return protected_text, placeholder_map

    def restore_keywords(self, text: str, placeholder_map: Optional[dict[str, str]] = None) -> str:
        """将占位符还原为原始术语。"""
        if not text or not placeholder_map:
            return text
        restored = text
        for token, word in placeholder_map.items():
            restored = restored.replace(token, word)
        return restored

    # 策略对应的温度递增
    STRATEGY_TEMP = {
        RewriteStrategy.STANDARD: 0.85,
        RewriteStrategy.HIGH_VARIANCE: 0.92,
        RewriteStrategy.STRUCTURAL: 0.95,
        RewriteStrategy.MERGE_SPLIT: 0.98,
        RewriteStrategy.HUMANIZE: 0.90,
    }

    def _build_statistical_prompt(self, paragraph: str,
                                  detection_result=None,
                                  round_num: int = 1,
                                  strategy: 'RewriteStrategy' = None) -> str:
        """
        构建针对统计特征的增强 system prompt。
        组合：基础 SYSTEM_PROMPT + 策略片段 + 检测反馈。
        """
        strategy = strategy or RewriteStrategy.STANDARD
        base = self.SYSTEM_PROMPT

        # 追加策略片段
        fragment = STRATEGY_PROMPT_FRAGMENTS.get(strategy, '')
        if fragment:
            base += fragment

        # 如果有检测结果，追加具体反馈
        if detection_result is not None:
            details = getattr(detection_result, 'details', {}) or {}
            prob = getattr(detection_result, 'ai_probability', 0)
            feedback_lines = [
                f'\n\n## 检测反馈（第 {round_num} 轮）',
                f'上一轮该段 AI 检测概率: {prob:.1f}%',
            ]
            # 如果检测 API 返回了具体指标，加入针对性提示
            raw = details.get('raw', {})
            if isinstance(raw, dict):
                perplexity = raw.get('perplexity') or raw.get('average_perplexity')
                burstiness = raw.get('burstiness')
                if perplexity is not None:
                    feedback_lines.append(f'困惑度(perplexity): {perplexity} — 需要提高文本的不可预测性')
                if burstiness is not None:
                    feedback_lines.append(f'突发性(burstiness): {burstiness} — 需要加大句子复杂度的变化幅度')
            base += '\n'.join(feedback_lines)

        return base

    def transform(self, text: str, risk_level: str = 'medium',
                  protected_words: Optional[list[str]] = None,
                  custom_prompt: str = '',
                  strategy: Optional['RewriteStrategy'] = None,
                  detection_result=None,
                  round_num: int = 1) -> TransformResult:
        """
        改写单段文本，支持术语保护、额外 AI 指令和多轮策略。

        Args:
            strategy: 改写策略（闭环模式用），None 则使用 STANDARD
            detection_result: 上一轮检测结果（闭环模式用）
            round_num: 当前闭环轮次
        """
        if len(text.strip()) < 15:
            return TransformResult(
                paragraph_index=-1, original=text, transformed=text,
                rules_applied=[], change_ratio=0,
            )

        strategy = strategy or RewriteStrategy.STANDARD

        intensity_hint = {
            'high': '【高风险段落】这段被判定AI概率>75%，需要大幅度重写——重新组织句子结构、更换表达方式、调整叙述顺序，但语义必须完全一致。',
            'medium': '【中风险段落】AI概率50-75%，需要适度改写——调整部分句式和用词，打破原文的模板感。',
            'low': '【低风险段落】AI概率30-50%，轻度调整即可——改几处关键表达，不需要大动。',
        }

        # 温度：策略优先 > 风险等级
        temp_override = self.STRATEGY_TEMP.get(strategy, 0.85)
        risk_temp = {'high': 0.92, 'medium': 0.80, 'low': 0.65}
        temp_override = max(temp_override, risk_temp.get(risk_level, 0.80))

        protected_text, placeholder_map = self.protect_keywords(text, protected_words)
        user_msg = f"{intensity_hint.get(risk_level, '')}\n\n原文：\n{protected_text}"

        # 构建增强 prompt（含策略片段和检测反馈）
        stat_prompt = self._build_statistical_prompt(
            paragraph=text,
            detection_result=detection_result,
            round_num=round_num,
            strategy=strategy,
        )

        call_result = self._call_api(user_msg, custom_prompt=custom_prompt,
                                     temperature_override=temp_override,
                                     system_prompt_override=stat_prompt)
        if not call_result.get('ok'):
            err = call_result.get('error', {})
            msg = err.get('message', 'unknown error')
            return TransformResult(
                paragraph_index=-1,
                original=text,
                transformed=text,
                rules_applied=[f'AI调用失败: {msg[:80]}'],
                change_ratio=0,
                error=err,
            )

        result_text = self.restore_keywords(call_result.get('content', ''), placeholder_map)
        rules = [f'AI改写 ({self.model})']
        if placeholder_map:
            rules.insert(0, f'术语保护 × {len(placeholder_map)}')
        if strategy != RewriteStrategy.STANDARD:
            rules.append(f'策略: {strategy.value}')
        if custom_prompt.strip():
            rules.append('附加提示词')

        change_ratio = 1 - _text_overlap(text, result_text)
        return TransformResult(
            paragraph_index=-1,
            original=text,
            transformed=result_text,
            rules_applied=rules,
            change_ratio=change_ratio,
        )

    def batch_transform(self, paragraphs: dict[int, str],
                        risk_map: Optional[dict] = None,
                        protected_words: Optional[list[str]] = None,
                        custom_prompt: str = '',
                        strategy: Optional['RewriteStrategy'] = None,
                        detection_results: Optional[dict] = None,
                        round_num: int = 1) -> list[TransformResult]:
        """
        批量改写段落。采用串行调用，避免不必要的并发冲突。

        Args:
            detection_results: {段落索引: DetectionResult} 用于闭环模式的检测反馈
        """
        results = []
        for idx, text in paragraphs.items():
            level = risk_map.get(idx, 'medium') if risk_map else 'medium'
            det_result = detection_results.get(idx) if detection_results else None
            result = self.transform(
                text,
                level,
                protected_words=protected_words,
                custom_prompt=custom_prompt,
                strategy=strategy,
                detection_result=det_result,
                round_num=round_num,
            )
            result.paragraph_index = idx
            results.append(result)
        return results

    def test_connection(self) -> dict:
        """测试 API 连接并返回结构化结果。"""
        if not self.api_key:
            return {
                'ok': False,
                'message': 'API Key 不能为空',
                'models': [],
                'error': {
                    'code': 'missing_api_key',
                    'status': 400,
                    'message': 'API Key 不能为空',
                },
            }

        result = self._request_json('/models', timeout=10)
        if not result.get('ok'):
            err = result.get('error', {})
            return {
                'ok': False,
                'message': err.get('message', '连接失败'),
                'models': [],
                'error': err,
            }

        data = result.get('data', {})
        model_ids = [m.get('id', '') for m in data.get('data', []) if isinstance(m, dict)][:20]
        return {
            'ok': True,
            'message': '连接成功',
            'models': model_ids,
            'error': None,
        }

    def _build_http_error(self, status: int, message: str,
                          *, retry_after: Optional[str] = None,
                          provider_code: str = '') -> dict:
        """构建结构化 HTTP 错误。"""
        if status == 401:
            code = 'unauthorized'
        elif status == 429:
            code = 'rate_limited'
        elif status >= 500:
            code = 'upstream_server_error'
        else:
            code = 'http_error'

        err = {
            'code': code,
            'status': status,
            'message': message,
        }
        if retry_after:
            err['retry_after'] = retry_after
        if provider_code:
            err['provider_code'] = provider_code
        return err

    def _request_json(self, endpoint: str,
                      payload: Optional[dict] = None,
                      timeout: int = 60) -> dict:
        """发送 JSON 请求并返回结构化结果。"""
        import json as _json
        import urllib.error
        import urllib.request

        url = self.api_url + endpoint
        data = None
        method = 'GET'
        headers = {
            'Authorization': f'Bearer {self.api_key}',
        }

        if payload is not None:
            method = 'POST'
            data = _json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json'

        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode('utf-8', errors='ignore')
                parsed = self._parse_response(raw)
                return {
                    'ok': True,
                    'status': getattr(resp, 'status', 200),
                    'data': parsed,
                }
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='ignore') if hasattr(e, 'read') else ''
            provider_msg = body.strip() or str(e)
            provider_code = ''
            try:
                parsed = _json.loads(body) if body else {}
                provider_msg = (
                    parsed.get('error', {}).get('message')
                    or parsed.get('message')
                    or provider_msg
                )
                provider_code = (
                    parsed.get('error', {}).get('code')
                    or parsed.get('code')
                    or ''
                )
            except Exception:
                pass

            retry_after = e.headers.get('Retry-After') if e.headers else None
            return {
                'ok': False,
                'error': self._build_http_error(
                    e.code,
                    provider_msg[:220],
                    retry_after=retry_after,
                    provider_code=provider_code,
                ),
            }
        except urllib.error.URLError as e:
            return {
                'ok': False,
                'error': {
                    'code': 'network_error',
                    'status': 0,
                    'message': str(e.reason)[:220],
                },
            }
        except Exception as e:
            return {
                'ok': False,
                'error': {
                    'code': 'unexpected_error',
                    'status': 0,
                    'message': str(e)[:220],
                },
            }

    def _parse_retry_after(self, value) -> Optional[float]:
        """把 Retry-After 头尽量转成秒数。"""
        if value in (None, ''):
            return None
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(seconds, 30.0))

    @staticmethod
    def _extract_content(data: dict) -> str:
        """从 API 响应中提取文本内容，兼容多种格式。"""
        if not isinstance(data, dict):
            return ''
        # 标准 OpenAI 格式: choices[0].message.content
        choices = data.get('choices')
        if isinstance(choices, list) and choices:
            c = choices[0]
            if isinstance(c, dict):
                msg = c.get('message') or c.get('delta') or {}
                if isinstance(msg, dict):
                    txt = msg.get('content')
                    if isinstance(txt, str) and txt.strip():
                        return txt.strip()
                # 兼容 text 字段
                txt = c.get('text')
                if isinstance(txt, str) and txt.strip():
                    return txt.strip()
        # 兼容 result / output / content 顶层字段
        for key in ('result', 'output', 'content', 'response'):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ''

    @staticmethod
    def _parse_response(raw: str) -> dict:
        """解析 API 响应，支持标准 JSON 和 SSE 流式格式。"""
        import json as _json
        if not raw or not raw.strip():
            return {}
        # 先尝试标准 JSON
        try:
            return _json.loads(raw)
        except Exception:
            pass
        # SSE 流式格式: "data: {...}\ndata: {...}\n..."
        if 'data:' not in raw:
            return {'raw': raw}

        collected_content = []
        model_name = ''
        for line in raw.replace('\r\n', '\n').split('\n'):
            line = line.strip()
            if not line.startswith('data:'):
                continue
            payload = line[5:].strip()
            if payload == '[DONE]':
                continue
            try:
                chunk = _json.loads(payload)
            except Exception:
                continue
            if not model_name:
                model_name = chunk.get('model', '')
            choices = chunk.get('choices', [])
            if not choices:
                continue
            delta = choices[0].get('delta', {})
            text = delta.get('content', '')
            if text:
                collected_content.append(text)
        if collected_content:
            full_text = ''.join(collected_content)
            return {
                'choices': [{
                    'message': {'content': full_text},
                }],
                'model': model_name,
            }
        return {'raw': raw}

    def _call_api(self, user_message: str, custom_prompt: str = '',
                  temperature_override: Optional[float] = None,
                  system_prompt_override: Optional[str] = None) -> dict:
        """调用 /chat/completions 接口并返回结构化结果。"""
        system_prompt = system_prompt_override or self.SYSTEM_PROMPT
        selected = _pick_dynamic_prompts(k=2, rng=self._rng)
        if selected:
            system_prompt = f"{system_prompt}\n\n动态写作指令：\n- " + "\n- ".join(selected)
        if custom_prompt.strip():
            system_prompt = f"{system_prompt}\n\n附加要求：\n{custom_prompt.strip()}"

        temp = temperature_override if temperature_override is not None else self.temperature
        payload = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_message},
            ],
            'temperature': temp,
            'max_tokens': self.max_tokens,
            'stream': False,
        }

        max_attempts = 3
        retriable_codes = {'rate_limited', 'upstream_server_error', 'network_error'}
        last_error = None

        for attempt in range(1, max_attempts + 1):
            result = self._request_json('/chat/completions', payload=payload, timeout=120)
            if result.get('ok'):
                data = result.get('data', {})
                content = self._extract_content(data)
                if content:
                    return {
                        'ok': True,
                        'content': content,
                        'error': None,
                        'attempts': attempt,
                    }
                last_error = {
                    'code': 'invalid_response',
                    'status': result.get('status', 200),
                    'message': '模型返回格式异常，缺少 choices.message.content',
                    'attempts': attempt,
                }
                break

            error = dict(result.get('error') or {})
            error['attempts'] = attempt
            last_error = error
            code = error.get('code')
            if attempt >= max_attempts or code not in retriable_codes:
                break

            retry_after = self._parse_retry_after(error.get('retry_after'))
            sleep_seconds = retry_after if retry_after is not None else min(1.2 * attempt, 4.0)
            time.sleep(sleep_seconds)

        return {
            'ok': False,
            'error': last_error or {
                'code': 'unexpected_error',
                'status': 0,
                'message': '模型调用失败',
                'attempts': 1,
            },
        }


# AI 高频禁用词（SYSTEM_PROMPT 中明确禁止的词汇）
_BANNED_WORDS = [
    '首先', '其次', '最后', '总而言之', '综上所述',
    '与此同时', '除此之外', '此外', '从而', '进而',
    '不仅如此', '值得一提的是', '说到底', '实际上',
    '具体来看', '也就是说', '笔者认为', '不可否认的是', '坦白讲',
]


def analyze_ai_patterns(text: str) -> dict:
    """
    分析一段文本中的AI写作特征，返回风险指标。

    维度：
      - sequence_words: 序列连接词命中数
      - symmetric_structures: 对称结构数
      - generic_connectors: 学术套话命中数
      - long_sentences: 超长句数量
      - human_markers: 人味标记数（减分项）
      - banned_words: 禁用词命中数（新）
      - rhetorical_questions: 设问句数量（新）
      - marker_repeats: marker 重复使用次数（新）
      - sentence_length_variance: 句长标准差（新，越小越像 AI）
      - risk_score: 综合风险分 0-100
    """
    indicators: dict[str, float] = {
        'sequence_words': 0,
        'symmetric_structures': 0,
        'generic_connectors': 0,
        'long_sentences': 0,
        'human_markers': 0,
        'banned_words': 0,
        'rhetorical_questions': 0,
        'marker_repeats': 0,
        'sentence_length_variance': 0.0,
        'risk_score': 0,
    }

    # 1. 序列连接词
    for word in SEQUENCE_CONNECTORS:
        indicators['sequence_words'] += text.count(word)

    # 2. 对称结构
    semicolons = text.count('；')
    if semicolons >= 3:
        indicators['symmetric_structures'] += 1
    parallel_patterns = len(re.findall(r'(，\S{1,3})(.*?\1)', text))
    indicators['symmetric_structures'] += parallel_patterns

    # 3. 学术套话
    for word in ACADEMIC_CONNECTORS:
        indicators['generic_connectors'] += text.count(word)

    # 4. 超长句
    long_sents = re.findall(r'[^。！？]{80,}[。！？]', text)
    indicators['long_sentences'] = len(long_sents)

    # 5. 人味标记（减分项）
    for marker in HUMAN_MARKERS:
        if marker in text:
            indicators['human_markers'] += 1

    # 6. 禁用词命中
    for word in _BANNED_WORDS:
        indicators['banned_words'] += text.count(word)

    # 7. 设问句频率
    indicators['rhetorical_questions'] = len(re.findall(r'[^。！？]*？', text))

    # 8. marker 重复率：同一 marker 出现 ≥2 次即计入
    for marker in HUMAN_MARKERS:
        cnt = text.count(marker)
        if cnt >= 2:
            indicators['marker_repeats'] += cnt - 1

    # 9. 句长方差：方差越小越像 AI 的均匀输出
    sentences = [s.strip() for s in re.split(r'[。！？]', text) if s.strip()]
    if len(sentences) >= 3:
        lengths = [len(s) for s in sentences]
        mean_len = sum(lengths) / len(lengths)
        variance = sum((l - mean_len) ** 2 for l in lengths) / len(lengths)
        indicators['sentence_length_variance'] = round(variance ** 0.5, 1)  # 标准差

    # 综合评分
    std_dev = indicators['sentence_length_variance']
    low_variance_penalty = max(0, 15 - std_dev) if len(sentences) >= 3 else 0

    risk = (
        indicators['sequence_words'] * 15
        + indicators['symmetric_structures'] * 20
        + indicators['generic_connectors'] * 10
        + indicators['long_sentences'] * 10
        + indicators['banned_words'] * 20
        + max(0, indicators['rhetorical_questions'] - 1) * 12
        + indicators['marker_repeats'] * 8
        + low_variance_penalty
        - indicators['human_markers'] * 15
    )
    indicators['risk_score'] = max(0, min(100, risk))

    return indicators
