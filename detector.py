# -*- coding: utf-8 -*-
"""
AIGC 检测模块
封装真实 AIGC 检测 API 调用，支持 GPTZero、国内检测平台及自定义 API。
替代 analyze_ai_patterns() 作为评判标准。
"""
import hashlib
import json as _json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DetectPlatform(Enum):
    GPTZERO = 'gptzero'
    CUSTOM = 'custom'
    LOCAL = 'local'


@dataclass
class DetectionResult:
    """单段检测结果"""
    text: str
    ai_probability: float = 0.0      # 0-100
    risk_level: str = 'safe'          # high / medium / low / safe
    details: dict = field(default_factory=dict)
    cached: bool = False

    @staticmethod
    def classify(probability: float) -> str:
        if probability >= 75:
            return 'high'
        elif probability >= 50:
            return 'medium'
        elif probability >= 30:
            return 'low'
        return 'safe'


@dataclass
class DetectionReport:
    """全文检测汇总"""
    overall_rate: float = 0.0
    paragraph_results: list = field(default_factory=list)
    raw_response: dict = field(default_factory=dict)
    error: Optional[str] = None


# ============================================================
# 预设平台配置模板
# ============================================================

# ============================================================
# 本地模型检测器（Hello-SimpleAI/chatgpt-detector-roberta-chinese）
# ============================================================

class LocalModelDetector:
    """
    基于 HuggingFace transformers 的本地 AIGC 检测。
    默认使用 Hello-SimpleAI/chatgpt-detector-roberta-chinese（中文专用）。
    """

    _instance = None  # 单例，避免重复加载模型

    def __init__(self, model_name: str = 'Hello-SimpleAI/chatgpt-detector-roberta-chinese',
                 device: Optional[str] = None, max_length: int = 512):
        self.model_name = model_name
        self.max_length = max_length
        self._device = device
        self._pipeline = None

    def _ensure_loaded(self):
        """懒加载模型，首次调用时才下载/加载。"""
        if self._pipeline is not None:
            return

        try:
            from transformers import pipeline as hf_pipeline
            import torch
        except ImportError:
            raise RuntimeError(
                '本地检测模型需要 transformers 和 torch 库。\n'
                '请执行: pip install transformers torch'
            )

        device_arg = self._device
        if device_arg is None:
            import torch as _torch
            device_arg = 0 if _torch.cuda.is_available() else -1

        print(f'[LocalModelDetector] 加载模型: {self.model_name} (device={device_arg}) ...')
        self._pipeline = hf_pipeline(
            'text-classification',
            model=self.model_name,
            device=device_arg,
            truncation=True,
            max_length=self.max_length,
        )
        print(f'[LocalModelDetector] 模型加载完成')

    def predict(self, text: str) -> float:
        """
        预测单段文本的 AI 生成概率（0-100）。
        """
        self._ensure_loaded()
        text = text.strip()
        if not text:
            return 0.0

        result = self._pipeline(text)[0]
        label = result['label'].lower()
        score = result['score']

        # 模型输出: label='ChatGPT' score=0.95 → AI 概率 95%
        #           label='Human'   score=0.90 → AI 概率 10%
        if label in ('chatgpt', 'ai', 'generated', 'fake', 'machine'):
            return round(score * 100, 1)
        else:
            # label 是 human/real，score 是"人写"的置信度
            return round((1 - score) * 100, 1)

    def predict_batch(self, texts: list[str]) -> list[float]:
        """批量预测，利用 pipeline 的 batch 加速。"""
        self._ensure_loaded()
        cleaned = [t.strip() for t in texts]
        results = self._pipeline(cleaned)
        probs = []
        for r in results:
            label = r['label'].lower()
            score = r['score']
            if label in ('chatgpt', 'ai', 'generated', 'fake', 'machine'):
                probs.append(round(score * 100, 1))
            else:
                probs.append(round((1 - score) * 100, 1))
        return probs

    @classmethod
    def get_instance(cls, model_name: str = 'Hello-SimpleAI/chatgpt-detector-roberta-chinese',
                     **kwargs) -> 'LocalModelDetector':
        """获取单例实例（避免多次加载模型到显存）。"""
        if cls._instance is None or cls._instance.model_name != model_name:
            cls._instance = cls(model_name=model_name, **kwargs)
        return cls._instance


PLATFORM_PRESETS = {
    'local': {
        'name': '本地模型 (中文 RoBERTa)',
        'base_url': '',
        'auth_header': '',
        'request_format': {},
        'response_mapping': {},
    },
    'gptzero': {
        'name': 'GPTZero',
        'base_url': 'https://api.gptzero.me/v2/predict',
        'auth_header': 'x-api-key',
        'request_format': {
            'method': 'POST',
            'body_template': {
                'document': '{text}',
            },
        },
        'response_mapping': {
            'overall_probability': 'documents[0].completely_generated_prob',
            'paragraph_probabilities': 'documents[0].paragraphs[*].completely_generated_prob',
            'paragraph_texts': 'documents[0].paragraphs[*].generated_prob',
        },
    },
    'custom': {
        'name': '自定义 API',
        'base_url': '',
        'auth_header': 'Authorization',
        'request_format': {
            'method': 'POST',
            'body_template': {
                'text': '{text}',
            },
        },
        'response_mapping': {
            'overall_probability': 'ai_probability',
            'paragraph_probabilities': 'paragraphs[*].probability',
            'paragraph_texts': 'paragraphs[*].text',
        },
    },
}


class AIGCDetector:
    """
    AIGC 检测器主类。
    封装检测 API 调用，支持多平台、缓存、批量检测。
    """

    def __init__(self, api_config: Optional[dict] = None):
        """
        api_config 结构:
        {
            'platform': 'gptzero' | 'custom',
            'api_url': str,
            'api_key': str,
            'auth_header': str,           # 可选，默认按平台预设
            'request_body_template': dict, # 可选，自定义请求体模板
            'response_mapping': dict,      # 可选，自定义响应字段映射
            'timeout': int,                # 可选，默认 30s
        }
        """
        cfg = api_config or {}
        self.platform = cfg.get('platform', 'custom')
        preset = PLATFORM_PRESETS.get(self.platform, PLATFORM_PRESETS['custom'])

        self.api_url = str(cfg.get('api_url') or preset['base_url']).strip().rstrip('/')
        self.api_key = str(cfg.get('api_key') or '').strip()
        self.auth_header = str(cfg.get('auth_header') or preset['auth_header']).strip()
        self.timeout = int(cfg.get('timeout', 30))

        self.request_body_template = cfg.get('request_body_template') or preset['request_format'].get('body_template', {})
        self.response_mapping = cfg.get('response_mapping') or preset['response_mapping']

        # 本地模型相关配置
        self._local_detector: Optional[LocalModelDetector] = None
        if self.platform == 'local':
            model_name = cfg.get('model_name', 'Hello-SimpleAI/chatgpt-detector-roberta-chinese')
            self._local_detector = LocalModelDetector.get_instance(model_name=model_name)

        # 检测结果缓存：{text_hash: DetectionResult}
        self._cache: dict[str, DetectionResult] = {}

    @staticmethod
    def _text_hash(text: str) -> str:
        return hashlib.md5(text.strip().encode('utf-8')).hexdigest()

    def is_configured(self) -> bool:
        """检查是否已配置可用的检测 API"""
        if self.platform == 'local':
            return True
        return bool(self.api_url and self.api_key)

    def test_connection(self) -> dict:
        """测试检测 API 连接"""
        if not self.is_configured():
            return {
                'ok': False,
                'message': '检测 API 未配置（缺少 api_url 或 api_key）',
            }

        test_text = '人工智能技术在近年来取得了显著的发展，深度学习模型的不断演进推动了自然语言处理领域的重大突破。'
        try:
            result = self.detect_paragraph(test_text)
            source = '本地模型' if self.platform == 'local' else f'{self.platform} API'
            return {
                'ok': True,
                'message': f'{source}就绪，测试段落 AI 概率: {result.ai_probability:.1f}%',
                'test_result': {
                    'probability': result.ai_probability,
                    'risk_level': result.risk_level,
                    'details': result.details,
                },
            }
        except Exception as e:
            return {
                'ok': False,
                'message': f'连接失败: {str(e)[:200]}',
            }

    def detect_paragraph(self, text: str) -> DetectionResult:
        """
        检测单段文本的 AIGC 概率。
        优先使用缓存，未命中时调用 API。
        """
        text = text.strip()
        if not text:
            return DetectionResult(text=text, ai_probability=0, risk_level='safe')

        cache_key = self._text_hash(text)
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            cached.cached = True
            return cached

        # 本地模型检测
        if self.platform == 'local' and self._local_detector:
            prob = self._local_detector.predict(text)
            result = DetectionResult(
                text=text,
                ai_probability=prob,
                risk_level=DetectionResult.classify(prob),
                details={'source': 'local_model', 'model': self._local_detector.model_name},
            )
            self._cache[cache_key] = result
            return result

        if not self.is_configured():
            # fallback：本地评估
            from transformer import analyze_ai_patterns
            indicators = analyze_ai_patterns(text)
            prob = min(indicators['risk_score'], 100)
            result = DetectionResult(
                text=text,
                ai_probability=prob,
                risk_level=DetectionResult.classify(prob),
                details={'source': 'local_fallback', 'indicators': indicators},
            )
            self._cache[cache_key] = result
            return result

        # 调用真实检测 API
        result = self._call_detect_api(text)
        self._cache[cache_key] = result
        return result

    def detect_batch(self, texts: list[str],
                     progress_callback=None) -> list[DetectionResult]:
        """
        批量检测多段文本。
        progress_callback(current, total) 用于报告进度。
        本地模型模式下使用 batch 推理加速。
        """
        total = len(texts)

        # 本地模型：先检查缓存，未命中的走 batch 推理
        if self.platform == 'local' and self._local_detector:
            results: list[Optional[DetectionResult]] = [None] * total
            uncached_indices = []
            uncached_texts = []

            for i, text in enumerate(texts):
                text = text.strip()
                cache_key = self._text_hash(text)
                if cache_key in self._cache:
                    cached = self._cache[cache_key]
                    cached.cached = True
                    results[i] = cached
                else:
                    uncached_indices.append(i)
                    uncached_texts.append(text)

            if uncached_texts:
                probs = self._local_detector.predict_batch(uncached_texts)
                for j, idx in enumerate(uncached_indices):
                    text = uncached_texts[j]
                    prob = probs[j]
                    det = DetectionResult(
                        text=text,
                        ai_probability=prob,
                        risk_level=DetectionResult.classify(prob),
                        details={'source': 'local_model', 'model': self._local_detector.model_name},
                    )
                    self._cache[self._text_hash(text)] = det
                    results[idx] = det

            if progress_callback:
                progress_callback(total, total)
            return results

        # API 模式：逐条检测
        results = []
        for i, text in enumerate(texts):
            result = self.detect_paragraph(text)
            results.append(result)
            if progress_callback:
                progress_callback(i + 1, total)
        return results

    def detect_document(self, paragraphs: list[str]) -> DetectionReport:
        """
        全文检测：对所有段落执行检测，汇总报告。
        """
        if not paragraphs:
            return DetectionReport()

        # 先尝试全文提交（某些 API 支持整篇文档检测，本地模型走逐段）
        if self.is_configured() and self.platform == 'gptzero' and self.platform != 'local':
            full_text = '\n\n'.join(p for p in paragraphs if p.strip())
            try:
                full_result = self._call_detect_api_full(full_text)
                if full_result and not full_result.error:
                    return full_result
            except Exception:
                pass

        # 逐段检测
        results = self.detect_batch([p for p in paragraphs if p.strip()])

        if not results:
            return DetectionReport()

        # 计算加权整体率（按字数加权）
        total_chars = sum(len(r.text) for r in results)
        if total_chars > 0:
            weighted_rate = sum(r.ai_probability * len(r.text) for r in results) / total_chars
        else:
            weighted_rate = 0.0

        return DetectionReport(
            overall_rate=round(weighted_rate, 1),
            paragraph_results=results,
        )

    def clear_cache(self):
        """清空检测结果缓存"""
        self._cache.clear()

    def invalidate_cache(self, text: str):
        """使特定文本的缓存失效（改写后需要重新检测）"""
        cache_key = self._text_hash(text)
        self._cache.pop(cache_key, None)

    # ----------------------------------------------------------
    # 内部方法
    # ----------------------------------------------------------

    def _build_request_body(self, text: str) -> dict:
        """根据模板构建请求体"""
        body = {}
        for key, value in self.request_body_template.items():
            if isinstance(value, str) and '{text}' in value:
                body[key] = value.replace('{text}', text)
            else:
                body[key] = value
        return body

    def _build_headers(self) -> dict:
        """构建请求头"""
        headers = {'Content-Type': 'application/json'}

        if self.auth_header.lower() == 'authorization':
            headers['Authorization'] = f'Bearer {self.api_key}'
        else:
            headers[self.auth_header] = self.api_key

        return headers

    def _extract_probability(self, data: dict) -> float:
        """从 API 响应中提取 AI 概率"""
        mapping = self.response_mapping.get('overall_probability', '')
        if not mapping:
            return 0.0

        value = _resolve_json_path(data, mapping)
        if value is None:
            # 尝试常见的字段名
            for key in ('ai_probability', 'ai_score', 'probability',
                        'ai_generated_probability', 'score', 'result'):
                if key in data:
                    val = data[key]
                    if isinstance(val, (int, float)):
                        return float(val) * (100 if val <= 1.0 else 1)
            return 0.0

        prob = float(value)
        # 如果值在 0-1 之间，转换为百分比
        if 0 <= prob <= 1.0:
            prob *= 100
        return round(prob, 1)

    def _call_detect_api(self, text: str) -> DetectionResult:
        """调用检测 API 检测单段文本"""
        body = self._build_request_body(text)
        headers = self._build_headers()

        data = _json.dumps(body).encode('utf-8')
        req = urllib.request.Request(
            self.api_url,
            data=data,
            headers=headers,
            method='POST',
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode('utf-8', errors='ignore')
                response_data = _json.loads(raw)

                prob = self._extract_probability(response_data)
                return DetectionResult(
                    text=text,
                    ai_probability=prob,
                    risk_level=DetectionResult.classify(prob),
                    details={'source': self.platform, 'raw': response_data},
                )

        except urllib.error.HTTPError as e:
            body_text = ''
            try:
                body_text = e.read().decode('utf-8', errors='ignore')
            except Exception:
                pass
            raise RuntimeError(
                f'检测 API 返回错误 HTTP {e.code}: {body_text[:200]}'
            )
        except urllib.error.URLError as e:
            raise RuntimeError(f'检测 API 网络错误: {e.reason}')
        except _json.JSONDecodeError as e:
            raise RuntimeError(f'检测 API 返回非 JSON 格式: {e}')

    def _call_detect_api_full(self, full_text: str) -> Optional[DetectionReport]:
        """尝试全文提交检测（GPTZero 等平台支持）"""
        body = self._build_request_body(full_text)
        headers = self._build_headers()

        data = _json.dumps(body).encode('utf-8')
        req = urllib.request.Request(
            self.api_url,
            data=data,
            headers=headers,
            method='POST',
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout * 2) as resp:
                raw = resp.read().decode('utf-8', errors='ignore')
                response_data = _json.loads(raw)

                overall_prob = self._extract_probability(response_data)

                # 尝试提取段落级别的结果
                para_results = self._extract_paragraph_results(response_data, full_text)

                return DetectionReport(
                    overall_rate=overall_prob,
                    paragraph_results=para_results,
                    raw_response=response_data,
                )
        except Exception:
            return None

    def _extract_paragraph_results(self, data: dict, full_text: str) -> list[DetectionResult]:
        """从全文检测响应中提取段落级别的结果"""
        results = []

        # 尝试从 response_mapping 中解析段落数据
        prob_path = self.response_mapping.get('paragraph_probabilities', '')
        text_path = self.response_mapping.get('paragraph_texts', '')

        if prob_path and '[*]' in prob_path:
            probs = _resolve_json_array_path(data, prob_path)
            texts = _resolve_json_array_path(data, text_path) if text_path else []

            for i, prob in enumerate(probs):
                prob_val = float(prob) * (100 if float(prob) <= 1.0 else 1)
                text = texts[i] if i < len(texts) else ''
                results.append(DetectionResult(
                    text=str(text),
                    ai_probability=round(prob_val, 1),
                    risk_level=DetectionResult.classify(prob_val),
                    details={'source': self.platform, 'index': i},
                ))

        return results


# ============================================================
# JSON 路径解析辅助函数
# ============================================================

def _resolve_json_path(data: dict, path: str):
    """
    简易 JSON 路径解析。
    支持 'a.b.c' 和 'a[0].b' 格式。
    """
    if not path or not data:
        return None

    current = data
    parts = re.split(r'\.(?![^\[]*\])', path)

    for part in parts:
        if '[*]' in part:
            # 数组通配符在 _resolve_json_array_path 中处理
            return None

        array_match = re.match(r'(\w+)\[(\d+)\]', part)
        if array_match:
            key = array_match.group(1)
            idx = int(array_match.group(2))
            if isinstance(current, dict) and key in current:
                current = current[key]
                if isinstance(current, list) and idx < len(current):
                    current = current[idx]
                else:
                    return None
            else:
                return None
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None

    return current


def _resolve_json_array_path(data: dict, path: str) -> list:
    """
    解析含 [*] 通配符的 JSON 路径，返回所有匹配元素。
    例如 'documents[0].paragraphs[*].prob' -> [prob1, prob2, ...]
    """
    if not path or not data:
        return []

    parts = path.split('[*]')
    if len(parts) != 2:
        return []

    prefix = parts[0].rstrip('.')
    suffix = parts[1].lstrip('.')

    # 先解析到数组位置
    array_data = _resolve_json_path(data, prefix) if prefix else data
    if not isinstance(array_data, list):
        return []

    # 对每个数组元素取 suffix 字段
    results = []
    for item in array_data:
        if suffix:
            val = _resolve_json_path(item, suffix) if isinstance(item, dict) else item
        else:
            val = item
        if val is not None:
            results.append(val)

    return results
