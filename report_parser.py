# -*- coding: utf-8 -*-
"""
AIGC检测报告解析器
支持解析主流AIGC检测平台的PDF报告，提取段落风险等级和概率。
"""
import re
from dataclasses import dataclass, field, replace
from enum import Enum


class RiskLevel(Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    SAFE = "safe"


@dataclass
class ParagraphRisk:
    """单段落的AIGC风险信息"""
    text: str
    probability: float = 0.0
    risk_level: RiskLevel = RiskLevel.SAFE
    page: int = 0
    is_low_confidence: bool = False


@dataclass
class ReportData:
    """完整报告数据"""
    overall_rate: float = 0.0
    total_words: int = 0
    high_risk_words: int = 0
    medium_risk_words: int = 0
    low_risk_words: int = 0
    paragraphs: list = field(default_factory=list)
    raw_text: str = ""


STOPWORDS = {
    '进行', '开展', '实现', '可以', '能够', '以及', '对于', '通过', '具有',
    '相关', '研究', '分析', '提出', '为了', '其中', '这种', '这个', '我们',
    '本文', '本研究', '笔者', '结果', '问题', '方面', '情况', '进一步',
    '因此', '然而', '同时', '如果', '那么', '并且', '或者', '由于', '基于',
}

ACADEMIC_HINTS = {
    '分析', '验证', '构建', '评估', '优化', '证明', '识别', '提取', '计算',
    '比较', '研究', '设计', '训练', '建模', '推导', '实验', '匹配', '生成',
}


def _classify_risk(prob: float) -> RiskLevel:
    if prob >= 75:
        return RiskLevel.HIGH
    elif prob >= 50:
        return RiskLevel.MEDIUM
    elif prob >= 30:
        return RiskLevel.LOW
    return RiskLevel.SAFE


def parse_pdf_report(pdf_path: str) -> ReportData:
    """解析AIGC检测PDF报告"""
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        raise ImportError("需要安装 PyPDF2: pip install PyPDF2")

    reader = PdfReader(pdf_path)
    all_text = []
    for page in reader.pages:
        t = page.extract_text()
        if t:
            all_text.append(t)
    raw = "\n".join(all_text)

    report = ReportData(raw_text=raw)

    rate_match = re.search(r'(?:AIGC|疑似AI|AI生成|总体).*?(\d+\.?\d*)%', raw)
    if rate_match:
        report.overall_rate = float(rate_match.group(1))

    total_match = re.search(r'(?:总|全文|检测).*?字数.*?(\d+)', raw)
    if total_match:
        report.total_words = int(total_match.group(1))

    high_match = re.search(r'高风险.*?(\d+)\s*字', raw)
    if high_match:
        report.high_risk_words = int(high_match.group(1))

    medium_match = re.search(r'中风险.*?(\d+)\s*字', raw)
    if medium_match:
        report.medium_risk_words = int(medium_match.group(1))

    low_match = re.search(r'低风险.*?(\d+)\s*字', raw)
    if low_match:
        report.low_risk_words = int(low_match.group(1))

    _parse_paragraph_risks(raw, report)

    return report


def _parse_paragraph_risks(raw: str, report: ReportData):
    """从报告文本中提取各段落及其风险概率"""
    prob_pattern = re.compile(
        r'(\d+\.?\d*)%\s*\n([\s\S]*?)(?=\d+\.?\d*%\s*\n|$)'
    )
    for m in prob_pattern.finditer(raw):
        prob = float(m.group(1))
        text = m.group(2).strip()
        if len(text) < 10:
            continue
        risk = _classify_risk(prob)
        report.paragraphs.append(ParagraphRisk(
            text=text,
            probability=prob,
            risk_level=risk,
        ))

    if not report.paragraphs:
        _fallback_parse(raw, report)


def _fallback_parse(raw: str, report: ReportData):
    """备用解析：按颜色标记或关键词提取"""
    lines = raw.split('\n')
    current_text = []
    for line in lines:
        line = line.strip()
        if not line:
            if current_text:
                joined = ''.join(current_text)
                if len(joined) >= 20:
                    report.paragraphs.append(ParagraphRisk(
                        text=joined,
                        probability=0,
                        risk_level=RiskLevel.SAFE,
                    ))
                current_text = []
            continue
        current_text.append(line)


def parse_text_report(text: str) -> ReportData:
    """解析纯文本格式的报告（用户手动粘贴）"""
    report = ReportData(raw_text=text)

    rate_match = re.search(r'(\d+\.?\d*)%', text)
    if rate_match:
        report.overall_rate = float(rate_match.group(1))

    lines = text.strip().split('\n')
    for line in lines:
        line = line.strip()
        prob_match = re.match(r'^(\d+\.?\d*)%\s*[:：]?\s*(.*)', line)
        if prob_match:
            prob = float(prob_match.group(1))
            para_text = prob_match.group(2).strip()
            if para_text:
                report.paragraphs.append(ParagraphRisk(
                    text=para_text,
                    probability=prob,
                    risk_level=_classify_risk(prob),
                ))

    return report


def match_paragraphs(report: ReportData, doc_paragraphs: list[str],
                     threshold: float = 0.6) -> dict[int, ParagraphRisk]:
    """
    将报告中的风险段落匹配到文档的段落索引。
    依次尝试：1:1 模糊匹配 → 滑动窗口匹配 → 关键词指纹匹配。
    返回 {段落索引: ParagraphRisk} 映射。
    """
    matched = {}
    matched_scores = {}

    for risk_para in report.paragraphs:
        if risk_para.risk_level == RiskLevel.SAFE:
            continue

        best_idx, score, is_low_confidence = match_paragraphs_enhanced(
            risk_para.text,
            doc_paragraphs,
            threshold=threshold,
        )
        if best_idx < 0:
            continue

        if best_idx in matched_scores and matched_scores[best_idx] >= score:
            continue

        matched_scores[best_idx] = score
        matched[best_idx] = replace(risk_para, is_low_confidence=is_low_confidence)

    return matched


def match_paragraphs_enhanced(report_para_text: str, doc_paragraphs: list[str],
                              threshold: float = 0.6) -> tuple[int, float, bool]:
    """增强版段落匹配：单段 → 滑动窗口 → 关键词指纹。"""
    rt = _normalize_text(report_para_text)
    if not rt:
        return -1, 0.0, True

    best_idx = -1
    best_score = 0.0

    # 1) 1:1 精准/模糊匹配
    for i, doc_text in enumerate(doc_paragraphs):
        dt = _normalize_text(doc_text)
        if not dt:
            continue
        score = _similarity(rt, dt)
        if score > best_score:
            best_score = score
            best_idx = i
    if best_idx >= 0 and best_score >= threshold:
        return best_idx, best_score, False

    # 2) 滑动窗口 2-3 段合并
    window_best_idx = -1
    window_best_score = best_score
    for window_size in (2, 3):
        for start in range(0, max(len(doc_paragraphs) - window_size + 1, 0)):
            merged = _normalize_text(''.join(doc_paragraphs[start:start + window_size]))
            if not merged:
                continue
            score = _similarity(rt, merged)
            if score > window_best_score:
                window_best_score = score
                window_best_idx = _pick_best_index_in_window(
                    rt,
                    doc_paragraphs,
                    start,
                    window_size,
                )
    if window_best_idx >= 0 and window_best_score >= max(0.45, threshold - 0.1):
        return window_best_idx, window_best_score, True

    # 3) 关键词指纹匹配
    keyword_idx, keyword_score = _keyword_fingerprint_match(rt, doc_paragraphs)
    if keyword_idx >= 0:
        return keyword_idx, keyword_score, True

    return best_idx, best_score, True


def _normalize_text(text: str) -> str:
    """归一化文本，减少空白和标点噪音。"""
    text = (text or '').replace(' ', '').replace('\n', '')
    return re.sub(r'[“”"\'\'、,，；;：:（）()【】\[\]]', '', text)


def _pick_best_index_in_window(report_text: str, doc_paragraphs: list[str],
                               start: int, window_size: int) -> int:
    """在滑动窗口内选择最像原报告段的实际段落索引。"""
    best_idx = start
    best_score = -1.0
    keywords = _extract_keywords(report_text)

    for idx in range(start, min(start + window_size, len(doc_paragraphs))):
        dt = _normalize_text(doc_paragraphs[idx])
        sim = _similarity(report_text, dt)
        overlap = _keyword_overlap_ratio(keywords, _extract_keywords(dt))
        score = sim * 0.7 + overlap * 0.3
        if score > best_score:
            best_score = score
            best_idx = idx
    return best_idx


def _extract_keywords(text: str) -> list[str]:
    """提取名词/学术动词风格的关键词指纹。"""
    normalized = _normalize_text(text)
    if not normalized:
        return []

    parts = re.findall(r'[A-Za-z]{2,}|\d+(?:\.\d+)?|[\u4e00-\u9fff]{2,6}', normalized)
    keywords = []
    for part in parts:
        token = part.strip().lower()
        if not token or token in STOPWORDS:
            continue
        if token in ACADEMIC_HINTS or len(token) >= 2:
            keywords.append(token)
    return keywords[:24]


def _keyword_overlap_ratio(a: list[str], b: list[str]) -> float:
    """关键词交集比例。"""
    if not a or not b:
        return 0.0
    sa = set(a)
    sb = set(b)
    if not sa:
        return 0.0
    return len(sa & sb) / len(sa)


def _keyword_fingerprint_match(report_text: str, doc_paragraphs: list[str]) -> tuple[int, float]:
    """基于关键词指纹做兜底匹配。"""
    report_keywords = _extract_keywords(report_text)
    if not report_keywords:
        return -1, 0.0

    best_idx = -1
    best_score = 0.0
    for idx, doc_text in enumerate(doc_paragraphs):
        doc_keywords = _extract_keywords(doc_text)
        overlap = _keyword_overlap_ratio(report_keywords, doc_keywords)
        if overlap <= 0:
            continue
        sim = _similarity(_normalize_text(report_text), _normalize_text(doc_text))
        score = overlap * 0.75 + sim * 0.25
        if score > best_score:
            best_score = score
            best_idx = idx

    if best_score >= 0.2:
        return best_idx, best_score
    return -1, 0.0


def _similarity(a: str, b: str) -> float:
    """基于最长公共子序列的相似度。"""
    if not a or not b:
        return 0.0
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) > 240:
        short = short[:240]
        long = long[:240]
    match_count = 0
    search_start = 0
    for ch in short:
        idx = long.find(ch, search_start)
        if idx >= 0:
            match_count += 1
            search_start = idx + 1
    return match_count / len(short)
