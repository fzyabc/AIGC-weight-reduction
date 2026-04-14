# -*- coding: utf-8 -*-
"""
AIGC降重工具 - 主程序
基于规则引擎的学术论文AIGC检测率降低工具。

用法：
  python reducer.py --doc 论文.docx                              # 全文扫描模式
  python reducer.py --doc 论文.docx --report 检测报告.pdf          # 精准模式
  python reducer.py --doc 论文.docx --report 报告.pdf --level 3   # 激进模式
  python reducer.py --doc 论文.docx --interactive                 # 交互模式

作者：AIGC-Reducer
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

from report_parser import (
    ReportData, RiskLevel, parse_pdf_report,
    match_paragraphs, ParagraphRisk,
)
from doc_handler import (
    read_docx, replace_paragraph_text, save_docx,
    get_content_paragraphs, analyze_document, ParagraphInfo,
)
from transformer import (
    Transformer, TransformResult,
    get_strategy_description, analyze_ai_patterns,
)


BANNER = r"""
    _    ___ ____  ____    ____          _
   / \  |_ _/ ___|/ ___|  |  _ \ ___  __| |_   _  ___ ___ _ __
  / _ \  | | |  _| |      | |_) / _ \/ _` | | | |/ __/ _ \ '__|
 / ___ \ | | |_| | |___   |  _ <  __/ (_| | |_| | (_|  __/ |
/_/   \_\___\____|\____|  |_| \_\___|\__,_|\__,_|\___\___|_|

    学术论文AIGC降重工具 v1.0
"""


def main():
    parser = argparse.ArgumentParser(
        description='AIGC降重工具 - 降低学术论文的AI检测率',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  %(prog)s --doc 论文.docx                        全文扫描，自动修改
  %(prog)s --doc 论文.docx --report 报告.pdf       根据检测报告精准修改
  %(prog)s --doc 论文.docx --level 3               激进降重模式
  %(prog)s --doc 论文.docx --analyze-only          仅分析，不修改
  %(prog)s --doc 论文.docx --interactive           交互式逐段确认
  %(prog)s --doc 论文.docx --export-json map.json  导出替换映射
        """,
    )
    parser.add_argument('--doc', required=True, help='输入的docx论文文件路径')
    parser.add_argument('--report', help='AIGC检测报告PDF路径（可选）')
    parser.add_argument('--output', '-o', help='输出文件路径（默认在原文件名后加_降重版）')
    parser.add_argument('--level', type=int, default=2, choices=[1, 2, 3],
                        help='降重力度：1=轻微 2=中等 3=激进（默认2）')
    parser.add_argument('--analyze-only', action='store_true',
                        help='仅分析AI特征，不做修改')
    parser.add_argument('--interactive', action='store_true',
                        help='交互模式：逐段确认是否应用修改')
    parser.add_argument('--export-json', help='导出替换映射到JSON文件')
    parser.add_argument('--import-json', help='从JSON文件导入替换映射并直接应用')
    parser.add_argument('--skip-headings', action='store_true', default=True,
                        help='跳过标题段落（默认开启）')
    parser.add_argument('--min-length', type=int, default=20,
                        help='忽略字数少于此值的段落（默认20）')
    parser.add_argument('--skip-english', action='store_true', default=True,
                        help='跳过英文段落（默认开启）')

    args = parser.parse_args()

    print(BANNER)

    if not os.path.exists(args.doc):
        print(f'❌ 文件不存在: {args.doc}')
        sys.exit(1)

    if args.import_json:
        run_import_mode(args)
        return

    if args.analyze_only:
        run_analyze_mode(args)
        return

    if args.report:
        run_report_mode(args)
    else:
        run_scan_mode(args)


def run_analyze_mode(args):
    """仅分析模式：扫描文档中的AI写作特征"""
    print('📊 分析模式：扫描AI写作特征...\n')
    doc, paragraphs = read_docx(args.doc)
    stats = analyze_document(paragraphs)

    print(f'文档统计:')
    print(f'  总段落数: {stats["total_paragraphs"]}')
    print(f'  正文段落: {stats["content_paragraphs"]}')
    print(f'  标题段落: {stats["headings"]}')
    print(f'  总字数:   {stats["total_words"]}')
    print()

    content = get_content_paragraphs(paragraphs)
    high_risk = []
    for para in content:
        if para.word_count < args.min_length:
            continue
        if args.skip_english and _is_english(para.text):
            continue
        indicators = analyze_ai_patterns(para.text)
        if indicators['risk_score'] > 30:
            high_risk.append((para, indicators))

    high_risk.sort(key=lambda x: x[1]['risk_score'], reverse=True)

    if not high_risk:
        print('✅ 未检测到明显的AI写作特征。')
        return

    print(f'⚠️  发现 {len(high_risk)} 个段落存在AI写作特征:\n')
    for para, indicators in high_risk[:20]:
        score = indicators['risk_score']
        level = '🔴高' if score >= 60 else ('🟡中' if score >= 40 else '🟢低')
        print(f'  P{para.index} [{level}风险 {score}分] {para.text[:60]}...')
        details = []
        if indicators['sequence_words']:
            details.append(f'序列词×{indicators["sequence_words"]}')
        if indicators['symmetric_structures']:
            details.append(f'对称结构×{indicators["symmetric_structures"]}')
        if indicators['long_sentences']:
            details.append(f'长句×{indicators["long_sentences"]}')
        if details:
            print(f'    原因: {", ".join(details)}')
        print()


def run_report_mode(args):
    """报告精准模式：根据检测报告定向修改高风险段落"""
    print('🎯 精准模式：根据检测报告定向降重...\n')

    if not os.path.exists(args.report):
        print(f'❌ 报告文件不存在: {args.report}')
        sys.exit(1)

    doc, paragraphs = read_docx(args.doc)
    report = parse_pdf_report(args.report)

    print(f'检测报告摘要:')
    print(f'  AIGC总体率: {report.overall_rate}%')
    print(f'  高风险字数: {report.high_risk_words}')
    print(f'  中风险字数: {report.medium_risk_words}')
    print(f'  低风险字数: {report.low_risk_words}')
    print()

    doc_texts = [p.text for p in paragraphs]
    risk_matched = match_paragraphs(report, doc_texts)

    if not risk_matched:
        print('⚠️  无法自动匹配报告段落与文档段落，切换为全文扫描模式...\n')
        run_scan_mode(args)
        return

    print(f'匹配到 {len(risk_matched)} 个风险段落:')
    risk_map = {}
    targets = {}
    for idx, risk_info in sorted(risk_matched.items()):
        level_name = risk_info.risk_level.value
        risk_map[idx] = level_name
        targets[idx] = paragraphs[idx].text
        emoji = {'high': '🔴', 'medium': '🟡', 'low': '🟢'}.get(level_name, '⚪')
        print(f'  {emoji} P{idx} [{risk_info.probability:.1f}%] {paragraphs[idx].text[:50]}...')
    print()

    _apply_transforms(args, doc, paragraphs, targets, risk_map)


def run_scan_mode(args):
    """全文扫描模式：扫描全文，对疑似AI段落进行修改"""
    print('🔍 扫描模式：全文扫描AI特征并修改...\n')

    doc, paragraphs = read_docx(args.doc)
    stats = analyze_document(paragraphs)
    print(f'文档: {stats["content_paragraphs"]} 个正文段落, {stats["total_words"]} 字\n')

    content = get_content_paragraphs(paragraphs)
    targets = {}
    risk_map = {}

    for para in content:
        if para.word_count < args.min_length:
            continue
        if args.skip_english and _is_english(para.text):
            continue
        if args.skip_headings and para.is_heading:
            continue

        indicators = analyze_ai_patterns(para.text)
        if indicators['risk_score'] > 25:
            targets[para.index] = para.text
            if indicators['risk_score'] >= 60:
                risk_map[para.index] = 'high'
            elif indicators['risk_score'] >= 40:
                risk_map[para.index] = 'medium'
            else:
                risk_map[para.index] = 'low'

    if not targets:
        print('✅ 全文扫描完成，未发现需要修改的AI特征段落。')
        return

    print(f'发现 {len(targets)} 个段落需要降重处理\n')
    _apply_transforms(args, doc, paragraphs, targets, risk_map)


def _apply_transforms(args, doc, paragraphs, targets, risk_map):
    """执行变换并保存结果"""
    strategy = get_strategy_description(args.level)
    print(f'降重策略: {strategy}\n')

    transformer = Transformer(aggressiveness=args.level)
    results = transformer.batch_transform(targets, risk_map)

    applied_count = 0
    replacements = {}

    for result in results:
        if not result.rules_applied:
            continue

        idx = result.paragraph_index
        if args.interactive:
            print(f'\n{"="*60}')
            print(f'P{idx} (风险: {risk_map.get(idx, "unknown")})')
            print(f'{"="*60}')
            print(f'[原文] {result.original[:120]}...' if len(result.original) > 120
                  else f'[原文] {result.original}')
            print(f'\n[修改] {result.transformed[:120]}...' if len(result.transformed) > 120
                  else f'\n[修改] {result.transformed}')
            print(f'\n应用规则: {", ".join(result.rules_applied)}')
            choice = input('\n是否应用此修改？(y/n/q退出) ').strip().lower()
            if choice == 'q':
                break
            if choice != 'y':
                continue

        replace_paragraph_text(doc, idx, result.transformed)
        replacements[str(idx)] = result.transformed
        applied_count += 1

    if args.export_json:
        with open(args.export_json, 'w', encoding='utf-8') as f:
            json.dump(replacements, f, ensure_ascii=False, indent=2)
        print(f'\n📄 替换映射已导出: {args.export_json}')

    output_path = args.output
    if not output_path:
        stem = Path(args.doc).stem
        suffix = Path(args.doc).suffix
        output_path = str(Path(args.doc).parent / f'{stem}_降重版{suffix}')

    save_docx(doc, output_path)

    print(f'\n{"="*60}')
    print(f'✅ 降重完成!')
    print(f'   修改段落数: {applied_count}')
    print(f'   输出文件:   {output_path}')
    if args.export_json:
        print(f'   替换映射:   {args.export_json}')
    print(f'{"="*60}')
    print()
    _print_tips()


def run_import_mode(args):
    """导入模式：从JSON文件导入替换映射并应用"""
    print('📥 导入模式：从JSON文件应用替换...\n')

    if not os.path.exists(args.import_json):
        print(f'❌ JSON文件不存在: {args.import_json}')
        sys.exit(1)

    with open(args.import_json, 'r', encoding='utf-8') as f:
        replacements = json.load(f)

    doc, paragraphs = read_docx(args.doc)

    count = 0
    for key, new_text in replacements.items():
        idx = int(key)
        if replace_paragraph_text(doc, idx, new_text):
            count += 1

    output_path = args.output
    if not output_path:
        stem = Path(args.doc).stem
        suffix = Path(args.doc).suffix
        output_path = str(Path(args.doc).parent / f'{stem}_降重版{suffix}')

    save_docx(doc, output_path)

    print(f'✅ 导入完成! 替换 {count} 个段落')
    print(f'   输出文件: {output_path}')


def _is_english(text: str) -> bool:
    """判断是否为英文段落"""
    if not text:
        return False
    ascii_count = sum(1 for c in text if ord(c) < 128)
    return ascii_count / len(text) > 0.7


def _print_tips():
    """打印后续建议"""
    tips = """
💡 降重后建议:
   1. 提交检测前，手动审阅修改后的段落，确保语义未变
   2. 如果检测率仍然偏高，可以：
      - 提高降重力度: --level 3
      - 对高风险段落进行手动重写（效果最好）
      - 将红色段落拆分后与相邻安全段落合并
   3. 手动优化的优先技巧：
      - 加入具体法条编号、判例名称、政策文件日期
      - 用"笔者认为""不可否认的是"等主观标记
      - 把"首先…其次…此外…"改为因果链叙述
      - 将总-分-总结构改为问题-分析-回应结构
   4. 导出替换映射后可手动编辑JSON，再用 --import-json 导入
"""
    print(tips)


if __name__ == '__main__':
    main()
