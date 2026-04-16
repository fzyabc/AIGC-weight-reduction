# -*- coding: utf-8 -*-
"""
多轮闭环降重引擎
实现 rewrite → detect → analyze gap → targeted rewrite → repeat 循环。
"""
from dataclasses import dataclass, field
from typing import Optional, Callable

from detector import AIGCDetector, DetectionResult, DetectionReport
from transformer import AITransformer, RewriteStrategy, TransformResult


@dataclass
class LoopConfig:
    """闭环配置"""
    max_rounds: int = 5
    target_rate: float = 15.0       # 整体目标检测率(%)
    paragraph_target: float = 30.0  # 单段目标(%)
    early_stop_delta: float = 3.0   # 连续改善<3%则停止
    protected_words: list = field(default_factory=list)
    custom_prompt: str = ''


@dataclass
class RoundResult:
    """单轮结果"""
    round_num: int
    strategy: RewriteStrategy
    rate_before: float
    rate_after: float
    paragraphs_rewritten: int
    paragraph_details: list = field(default_factory=list)  # [{idx, before, after, prob_before, prob_after}]


# 每轮使用的策略递进序列
STRATEGY_SEQUENCE = [
    RewriteStrategy.STANDARD,
    RewriteStrategy.HIGH_VARIANCE,
    RewriteStrategy.STRUCTURAL,
    RewriteStrategy.MERGE_SPLIT,
    RewriteStrategy.HUMANIZE,
]


class LoopEngine:
    """
    多轮闭环降重引擎。
    每轮：检测 → 筛选高风险段 → 选策略改写 → 重新检测 → 记录结果。
    """

    def __init__(self, detector: AIGCDetector, ai_transformer: AITransformer,
                 config: Optional[LoopConfig] = None):
        self.detector = detector
        self.ai_transformer = ai_transformer
        self.config = config or LoopConfig()
        self._stop_requested = False

    def request_stop(self):
        """外部请求提前终止"""
        self._stop_requested = True

    def run(self, paragraphs: list[str],
            progress_callback: Optional[Callable] = None) -> list[RoundResult]:
        """
        运行完整闭环降重。

        Args:
            paragraphs: 文档段落列表（会被原地修改）
            progress_callback: fn(round_num, total_rounds, current_rate, status_msg)

        Returns:
            每轮的 RoundResult 列表
        """
        self._stop_requested = False
        history: list[RoundResult] = []

        # 初始检测
        report = self._detect_all(paragraphs)
        current_rate = report.overall_rate

        if progress_callback:
            progress_callback(0, self.config.max_rounds, current_rate, '初始检测完成')

        if current_rate <= self.config.target_rate:
            return history

        for round_num in range(1, self.config.max_rounds + 1):
            if self._stop_requested:
                break

            strategy = self._select_strategy(round_num, history)
            rate_before = current_rate

            # 筛选需要改写的段落
            tasks = self._analyze_gap(report, round_num)
            if not tasks:
                break

            if progress_callback:
                progress_callback(
                    round_num, self.config.max_rounds, current_rate,
                    f'第{round_num}轮: 改写{len(tasks)}个段落, 策略={strategy.value}',
                )

            # 执行改写
            details = []
            for task in tasks:
                if self._stop_requested:
                    break
                idx = task['index']
                old_text = paragraphs[idx]
                det_result = task.get('detection_result')

                result = self.ai_transformer.transform(
                    old_text,
                    risk_level=task['risk_level'],
                    protected_words=self.config.protected_words or None,
                    custom_prompt=self.config.custom_prompt,
                    strategy=strategy,
                    detection_result=det_result,
                    round_num=round_num,
                )

                new_text = result.transformed
                if new_text and new_text != old_text and result.change_ratio > 0.05:
                    paragraphs[idx] = new_text
                    # 使旧文本缓存失效
                    self.detector.invalidate_cache(old_text)

                details.append({
                    'idx': idx,
                    'prob_before': getattr(det_result, 'ai_probability', 0) if det_result else 0,
                    'changed': new_text != old_text,
                })

            # 重新检测
            report = self._detect_all(paragraphs)
            current_rate = report.overall_rate

            round_result = RoundResult(
                round_num=round_num,
                strategy=strategy,
                rate_before=rate_before,
                rate_after=current_rate,
                paragraphs_rewritten=sum(1 for d in details if d['changed']),
                paragraph_details=details,
            )
            history.append(round_result)

            if progress_callback:
                progress_callback(
                    round_num, self.config.max_rounds, current_rate,
                    f'第{round_num}轮完成: {rate_before:.1f}% → {current_rate:.1f}%',
                )

            # 检查停止条件
            if current_rate <= self.config.target_rate:
                break
            if self._check_convergence(history):
                break

        return history

    def _detect_all(self, paragraphs: list[str]) -> DetectionReport:
        """检测所有段落"""
        non_empty = [p for p in paragraphs if p.strip()]
        return self.detector.detect_document(non_empty)

    def _analyze_gap(self, report: DetectionReport,
                     round_num: int) -> list[dict]:
        """
        分析差距，生成改写任务列表。
        只返回需要改写的段落（高于段落目标阈值）。
        """
        tasks = []

        if not report.paragraph_results:
            return tasks

        # 按概率降序排列，优先处理高风险段
        indexed = list(enumerate(report.paragraph_results))
        indexed.sort(key=lambda x: x[1].ai_probability, reverse=True)

        for idx, det in indexed:
            if det.ai_probability <= self.config.paragraph_target:
                continue
            # 过短的段落跳过
            if len(det.text.strip()) < 15:
                continue

            risk = DetectionResult.classify(det.ai_probability)
            tasks.append({
                'index': idx,
                'risk_level': risk,
                'detection_result': det,
                'probability': det.ai_probability,
            })

        return tasks

    def _select_strategy(self, round_num: int,
                         history: list[RoundResult]) -> RewriteStrategy:
        """根据轮次和历史动态选策略"""
        idx = min(round_num - 1, len(STRATEGY_SEQUENCE) - 1)
        return STRATEGY_SEQUENCE[idx]

    def _check_convergence(self, history: list[RoundResult]) -> bool:
        """判断是否收敛（连续两轮改善小于阈值）"""
        if len(history) < 2:
            return False
        last_two = history[-2:]
        for rr in last_two:
            improvement = rr.rate_before - rr.rate_after
            if improvement >= self.config.early_stop_delta:
                return False
        return True
