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
    '适当改变句式长短节奏，避免连续使用相同字数的短句。',
    '在逻辑转折处，尝试使用设问句引导，增加文章的启发性。',
    '将部分被动语态改为带有“笔者”或“本研究”等主观色彩的表达。',
    '在结尾适当使用强调性断句，增强表达起伏。',
]

random.seed()

# ============================================================
# 替换规则库
# ============================================================

SEQUENCE_CONNECTORS = {
    '首先': [
        '从根本上说', '追根溯源', '之所以这样说', '若要厘清这一问题',
        '问题的起点在于', '值得注意的是', '一个不容回避的事实是',
    ],
    '其次': [
        '进一步而言', '循此逻辑', '在此基础上', '沿着这条线索',
        '由此引申', '与此同时还需看到', '紧接着的问题是',
    ],
    '此外': [
        '另一个值得关注的维度是', '不仅如此', '除此之外还有一层考量',
        '从另一个侧面看', '值得补充的是', '还有一点不容忽视',
    ],
    '最后': [
        '归根结底', '回到问题的核心', '将上述分析汇总来看',
        '综合以上讨论', '行文至此', '如果做一个阶段性的小结',
    ],
    '综上所述': [
        '将上述几条线索归拢来看', '回顾前文的讨论脉络',
        '至此可以做一个初步判断', '经过上述分析不难发现',
        '如果将以上论点串联起来', '从整体视角审视',
    ],
    '总而言之': [
        '概括地讲', '行文至此可以说', '做一个简要的回顾',
        '站在全局的角度', '汇总前文要点',
    ],
    '其一': [
        '第一个层面', '从一个方面来看', '先看第一重关系',
    ],
    '其二': [
        '第二个层面涉及', '再看另一重关系', '从另一面来说',
    ],
    '其三': [
        '还有一个不可忽略的因素', '第三重考量在于',
    ],
    '一方面': [
        '站在一个角度看', '从某种意义上说', '就某一面向而言',
    ],
    '另一方面': [
        '换一个视角', '但从另一层意思理解', '反过来看这个问题',
    ],
}

ACADEMIC_CONNECTORS = {
    '因此': ['由此观之', '基于这一逻辑', '正因如此', '这也意味着', '循此推理'],
    '然而': ['但不可忽视的是', '话虽如此', '可事实并非如此简单', '不过需要看到'],
    '同时': ['与此并行的是', '在这一过程中', '伴随着这一趋势'],
    '可以看出': ['不难发现', '由此可见端倪', '这背后的逻辑是', '透过现象看本质'],
    '具有重要意义': ['其价值不言而喻', '这一点在实践中尤为突出', '其分量不可小觑'],
    '值得注意的是': ['需要特别指出的一点是', '有一个细节不容忽视', '耐人寻味的是'],
    '需要指出的是': ['有必要提及的是', '这里有一个关键点', '笔者认为应当强调'],
}

HUMAN_MARKERS = [
    '笔者认为', '不可否认的是', '诚如前文所述',
    '需要坦率地说', '从实践经验来看',
    '一个值得追问的问题是', '站在实务的角度',
    '坦白讲', '说得更具体一点',
    '这里有一个不容回避的现实', '如果追问背后的原因',
    '从笔者调研的情况来看', '在这个问题上',
    '事实上', '客观地说', '回到问题本身',
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
    ('。{connector}', '；从这个角度出发，'),
    ('。{connector}', '。\n换一个维度来看，'),
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
        """注入人类写作痕迹"""
        applied = []
        available = [m for m in HUMAN_MARKERS if m not in self._used_markers]
        if not available:
            self._used_markers.clear()
            available = HUMAN_MARKERS.copy()

        sentences = re.split(r'(?<=[。！？])', text)
        if len(sentences) < 2:
            return text, applied

        marker = random.choice(available)
        self._used_markers.add(marker)

        insert_pos = random.randint(1, min(3, len(sentences) - 1))
        s = sentences[insert_pos].strip()
        if s and not any(s.startswith(m) for m in HUMAN_MARKERS):
            sentences[insert_pos] = marker + '，' + s[0].lower() + s[1:] if s else s
            applied.append(f'注入主观标记: {marker}')

        text = ''.join(sentences)
        return text, applied

    def _restructure_sentences(self, text: str) -> tuple[str, list[str]]:
        """重构句子结构（激进模式）"""
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

        if self.aggressiveness >= 3:
            passive_candidates = re.findall(r'(\S{2,4})能够(\S+)', text)
            for subj, verb_rest in passive_candidates[:1]:
                old = f'{subj}能够{verb_rest}'
                new = f'{verb_rest}得以通过{subj}实现'
                if len(new) < len(old) * 2:
                    text = text.replace(old, new, 1)
                    applied.append(f'主动→被动: {old[:15]}...')

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
        '你是一名学术论文降重专家。你的任务是改写用户提供的段落，使其通过AIGC检测（降低AI生成概率），'
        '同时必须严格保留原文的学术语义、逻辑结构、数据和法条引用。\n\n'
        '改写策略（按优先级）：\n'
        '1. 打破「总-分-总」的八股结构，用因果链代替序数词排列\n'
        '2. 打破句式对称性，故意让句子长短不一\n'
        '3. 加入主观标记（如「笔者认为」「不可否认的是」「坦白讲」）\n'
        '4. 用具体场景或设问切入，代替抽象概述\n'
        '5. 偶尔使用口语化过渡（如「说白了」「换个角度看」）增加人味\n'
        '6. 保留所有专业术语、法条编号、数据、人名、案例名，不得篡改\n'
        '7. 不要添加原文没有的信息\n'
        '8. 严禁使用“总而言之”“综上所述”“首先/其次/最后”等典型 AI 序列连接词。\n\n'
        '直接输出改写后的段落，不要输出任何解释。'
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
            'high': '这段AIGC检测概率很高（>75%），请大幅改写句式结构和表达方式，但保留全部语义。',
            'medium': '这段AIGC检测概率中等（50-75%），请适度调整句式和用词。',
            'low': '这段AIGC检测概率偏低（30-50%），只需轻微调整表达即可。',
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

        # 保存原始响应用于诊断（仅保留最近一次）
        try:
            import os, tempfile
            dbg_path = os.path.join(tempfile.gettempdir(), 'ai_last_sse_raw.txt')
            with open(dbg_path, 'w', encoding='utf-8') as f:
                f.write(raw)
            print(f'[AI DEBUG] raw SSE saved to {dbg_path}, length={len(raw)}, lines={raw.count(chr(10))}')
        except Exception:
            pass

        collected_content = []
        model_name = ''
        # 用 \n 和 \r\n 都分割，并处理可能的多行 data
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
        print(f'[AI DEBUG] SSE parsed: {len(collected_content)} content chunks, model={model_name}')
        if collected_content:
            full_text = ''.join(collected_content)
            return {
                'choices': [{
                    'message': {'content': full_text},
                }],
                'model': model_name,
                '_parsed_from': 'sse_stream',
            }
        # 没有解析到内容，打印前3个chunk用于诊断
        chunk_count = 0
        for line in raw.replace('\r\n', '\n').split('\n'):
            line = line.strip()
            if line.startswith('data:') and line[5:].strip() != '[DONE]':
                chunk_count += 1
                if chunk_count <= 3:
                    print(f'[AI DEBUG] chunk#{chunk_count}: {line[:300]}')
        print(f'[AI DEBUG] total SSE chunks: {chunk_count}, none had content')
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
                import json as _dbg_json
                raw_preview = _dbg_json.dumps(data, ensure_ascii=False, default=str)[:500] if data else '(empty)'
                print(f'[AI DEBUG] invalid_response, raw data: {raw_preview}')
                last_error = {
                    'code': 'invalid_response',
                    'status': result.get('status', 200),
                    'message': f'模型返回格式异常，缺少 choices.message.content。响应预览: {raw_preview[:200]}',
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
