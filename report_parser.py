# -*- coding: utf-8 -*-
"""
AIGC检测报告解析器
支持解析主流AIGC检测平台的PDF报告，提取段落风险等级和概率。
"""
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


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
                     threshold: float = 0.5) -> dict[int, ParagraphRisk]:
    """
    将报告中的风险段落匹配到文档的段落索引。
    使用模糊文本匹配（基于公共子串比例）。
    返回 {段落索引: ParagraphRisk} 映射。
    """
    matched = {}
    for risk_para in report.paragraphs:
        if risk_para.risk_level == RiskLevel.SAFE:
            continue
        best_idx = -1
        best_score = 0
        rt = risk_para.text.replace(' ', '').replace('\n', '')
        for i, doc_text in enumerate(doc_paragraphs):
            dt = doc_text.replace(' ', '').replace('\n', '')
            if not dt:
                continue
            score = _similarity(rt, dt)
            if score > best_score:
                best_score = score
                best_idx = i
        if best_idx >= 0 and best_score >= threshold:
            matched[best_idx] = risk_para

    return matched


def _similarity(a: str, b: str) -> float:
    """基于最长公共子序列的相似度"""
    if not a or not b:
        return 0.0
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) > 200:
        short = short[:200]
        long = long[:200]
    match_count = 0
    search_start = 0
    for ch in short:
        idx = long.find(ch, search_start)
        if idx >= 0:
            match_count += 1
            search_start = idx + 1
    return match_count / len(short)
