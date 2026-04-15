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
from typing import Optional

DYNAMIC_PROMPTS = [
    '尝试把一个长句拆成两个短句，或把两个短句合成一个带从句的长句，制造长短交替的节奏感。',
    '挑选一处用反问或设问引出下文，但不要超过一处，避免刻意。',
    '把某个抽象概括改成具体的例子或场景描述，让读者能”看到画面”。',
    '调整段落内部的叙述顺序：比如先说结论再展开，或先抛问题再给答案。',
    '把”A具有B特点”改成”在B方面，A表现为……”这类句式倒装来打破模板感。',
    '找到一处可以用简短口语衔接的地方，比如”说到底””实际上””换句话说”，但全段最多用一次。',
]

random.seed()

# ============================================================
# 替换规则库
# ============================================================

SEQUENCE_CONNECTORS = {
    '首先': [
        '从源头来看', '要说清楚这件事', '先说一个前提',
        '问题的起点在于', '最根本的一点是',
    ],
    '其次': [
        '接下来', '在此基础上', '沿着这个思路往下',
        '再往下说', '第二点是',
    ],
    '此外': [
        '还有一点', '除了这些', '另外',
        '从另一面来看', '同时也要注意',
    ],
    '最后': [
        '说到最后', '回过头来看', '做个收尾',
        '到这里可以说', '末了还要提一句',
    ],
    '综上所述': [
        '总的来看', '回过头再看这些',
        '把前面说的串起来', '几点综合来看',
    ],
    '总而言之': [
        '简单来讲', '归结起来', '总的来说',
        '一句话概括', '概括地说',
    ],
    '其一': [
        '第一点', '先看一面', '一个方面是',
    ],
    '其二': [
        '第二点', '再看另一面', '还有一方面',
    ],
    '其三': [
        '第三点', '另外还有',
    ],
    '一方面': [
        '从一面看', '就某个角度来说', '一边是',
    ],
    '另一方面': [
        '换个角度', '从另一面看', '反过来看',
    ],
}

ACADEMIC_CONNECTORS = {
    '因此': ['所以', '这样一来', '正因如此', '这也就意味着', '也就是说'],
    '然而': ['不过', '但实际情况是', '话虽如此', '可问题在于', '但反过来看'],
    '同时': ['与此同时', '在这个过程中', '伴随这一变化'],
    '可以看出': ['能看出来', '从中可以发现', '这说明', '由此来看'],
    '具有重要意义': ['很关键', '在实践中很突出', '影响很大'],
    '值得注意的是': ['要注意的是', '有个细节值得留意', '比较特别的是'],
    '需要指出的是': ['要说明的是', '这里有个关键点', '必须提到的一点是'],
}

HUMAN_MARKERS = [
    '实际上', '说白了', '换句话说',
    '说到底', '具体来看', '仔细想想',
    '严格来说', '通俗地讲', '举个例子来说',
    '这里要多说一句', '有意思的是',
    '稍微展开一下', '往深了说',
    '直白一点', '简单来讲',
]

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

    def __init__(self, aggressiveness: int = 2):
        """
        aggressiveness: 降重力度 (1=轻微, 2=中等, 3=激进)
        """
        self.aggressiveness = aggressiveness
        self._used_markers = set()

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
                    replacement = random.choice(alternatives)
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
                    replacement = random.choice(alternatives)
                    text = text.replace(original, replacement, 1)
                    applied.append(f'连接词替换: {original} → {replacement}')
                else:
                    for _ in range(count):
                        replacement = random.choice(alternatives)
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
        """注入人类写作痕迹——在句间自然插入口语化衔接词"""
        applied = []
        available = [m for m in HUMAN_MARKERS if m not in self._used_markers]
        if not available:
            self._used_markers.clear()
            available = HUMAN_MARKERS.copy()

        sentences = re.split(r'(?<=[。！？])', text)
        sentences = [s for s in sentences if s.strip()]
        if len(sentences) < 3:
            return text, applied

        marker = random.choice(available)
        self._used_markers.add(marker)

        # 选一个中间位置的句子，在句首自然地加上衔接词
        insert_pos = random.randint(1, min(4, len(sentences) - 1))
        s = sentences[insert_pos].strip()
        if s and not any(s.startswith(m) for m in HUMAN_MARKERS):
            sentences[insert_pos] = marker + '，' + s
            applied.append(f'插入衔接: {marker}')

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
    """计算两段文本的重叠比例"""
    if not a or not b:
        return 0.0
    a_chars = set(a)
    b_chars = set(b)
    if not a_chars:
        return 0.0
    return len(a_chars & b_chars) / len(a_chars)


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
        '- 禁止使用”与此同时””除此之外””此外””从而””进而””不仅如此””值得一提的是”等AI高频连接词，用”而且””也””所以””这样”等日常用词代替\n'
        '- 禁止逐词逐句按原文顺序对照替换——这种模式本身就会被检测到\n'
        '- 设问句（”……怎么……？””……能做什么？”）整段最多用一次，不用也完全可以\n'
        '- 禁止改变原文的专业含义\n\n'
        '直接输出改写后的段落，不要输出解释。'
    )

    def __init__(self, api_config: Optional[dict] = None, *,
                 api_key: str = '',
                 api_url: str = "https://api.openai.com/v1",
                 model: str = "gpt-3.5-turbo",
                 temperature: float = 0.85):
        """初始化 AI 变换器。"""
        cfg = api_config or {}
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

    def transform(self, text: str, risk_level: str = 'medium',
                  protected_words: Optional[list[str]] = None,
                  custom_prompt: str = '') -> TransformResult:
        """改写单段文本，支持术语保护和额外 AI 指令。"""
        if len(text.strip()) < 15:
            return TransformResult(
                paragraph_index=-1, original=text, transformed=text,
                rules_applied=[], change_ratio=0,
            )

        intensity_hint = {
            'high': '【高风险段落】这段被判定AI概率>75%，需要大幅度重写——重新组织句子结构、更换表达方式、调整叙述顺序，但语义必须完全一致。',
            'medium': '【中风险段落】AI概率50-75%，需要适度改写——调整部分句式和用词，打破原文的模板感。',
            'low': '【低风险段落】AI概率30-50%，轻度调整即可——改几处关键表达，不需要大动。',
        }

        protected_text, placeholder_map = self.protect_keywords(text, protected_words)
        user_msg = f"{intensity_hint.get(risk_level, '')}\n\n原文：\n{protected_text}"

        call_result = self._call_api(user_msg, custom_prompt=custom_prompt)
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
                        custom_prompt: str = '') -> list[TransformResult]:
        """批量改写段落。采用串行调用，避免不必要的并发冲突。"""
        results = []
        for idx, text in paragraphs.items():
            level = risk_map.get(idx, 'medium') if risk_map else 'medium'
            result = self.transform(
                text,
                level,
                protected_words=protected_words,
                custom_prompt=custom_prompt,
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

    def _call_api(self, user_message: str, custom_prompt: str = '') -> dict:
        """调用 /chat/completions 接口并返回结构化结果。"""
        system_prompt = self.SYSTEM_PROMPT
        selected = random.sample(DYNAMIC_PROMPTS, k=min(2, len(DYNAMIC_PROMPTS)))
        if selected:
            system_prompt = f"{system_prompt}\n\n动态写作指令：\n- " + "\n- ".join(selected)
        if custom_prompt.strip():
            system_prompt = f"{system_prompt}\n\n附加要求：\n{custom_prompt.strip()}"

        payload = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_message},
            ],
            'temperature': self.temperature,
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


def analyze_ai_patterns(text: str) -> dict:
    """
    分析一段文本中的AI写作特征，返回风险指标。
    用于诊断哪些方面需要重点修改。
    """
    indicators = {
        'sequence_words': 0,
        'symmetric_structures': 0,
        'generic_connectors': 0,
        'long_sentences': 0,
        'human_markers': 0,
        'risk_score': 0,
    }

    for word in SEQUENCE_CONNECTORS:
        indicators['sequence_words'] += text.count(word)

    semicolons = text.count('；')
    if semicolons >= 3:
        indicators['symmetric_structures'] += 1
    parallel_patterns = len(re.findall(r'(，\S{1,3})(.*?\1)', text))
    indicators['symmetric_structures'] += parallel_patterns

    for word in ACADEMIC_CONNECTORS:
        indicators['generic_connectors'] += text.count(word)

    long_sents = re.findall(r'[^。！？]{80,}[。！？]', text)
    indicators['long_sentences'] = len(long_sents)

    for marker in HUMAN_MARKERS:
        if marker in text:
            indicators['human_markers'] += 1

    risk = (
        indicators['sequence_words'] * 15
        + indicators['symmetric_structures'] * 20
        + indicators['generic_connectors'] * 10
        + indicators['long_sentences'] * 10
        - indicators['human_markers'] * 15
    )
    indicators['risk_score'] = max(0, min(100, risk))

    return indicators
