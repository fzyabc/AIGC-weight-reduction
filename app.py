# -*- coding: utf-8 -*-
"""
AIGC降重工具 - Web后端
Flask API 提供文档分析、降重、下载等接口。
"""
import json
import os
import sys
import uuid
import time
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

from flask import Flask, request, jsonify, send_file, render_template
from werkzeug.utils import secure_filename

from report_parser import parse_pdf_report, match_paragraphs, RiskLevel
from doc_handler import (
    read_docx, replace_paragraph_text, save_docx,
    get_content_paragraphs, analyze_document,
)
from transformer import (
    Transformer, AITransformer,
    analyze_ai_patterns, get_strategy_description,
)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
app.config['OUTPUT_FOLDER'] = os.path.join(os.path.dirname(__file__), 'outputs')

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['OUTPUT_FOLDER'], exist_ok=True)

sessions = {}


def _session_id():
    return str(uuid.uuid4())[:8]


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/upload', methods=['POST'])
def upload_doc():
    """上传论文docx文件"""
    if 'file' not in request.files:
        return jsonify({'error': '未选择文件'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': '文件名为空'}), 400

    ext = Path(file.filename).suffix.lower()
    if ext != '.docx':
        return jsonify({'error': '仅支持 .docx 格式'}), 400

    sid = _session_id()
    filename = f'{sid}{ext}'
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        doc, paragraphs = read_docx(filepath)
        stats = analyze_document(paragraphs)
    except Exception as e:
        return jsonify({'error': f'文档解析失败: {str(e)}'}), 400

    sessions[sid] = {
        'doc_path': filepath,
        'original_name': file.filename,
        'stats': stats,
        'created': time.time(),
    }

    return jsonify({
        'session_id': sid,
        'filename': file.filename,
        'stats': stats,
    })


@app.route('/api/upload-report', methods=['POST'])
def upload_report():
    """上传AIGC检测报告PDF"""
    sid = request.form.get('session_id')
    if not sid or sid not in sessions:
        return jsonify({'error': '无效的会话ID'}), 400

    if 'file' not in request.files:
        return jsonify({'error': '未选择文件'}), 400

    file = request.files['file']
    ext = Path(file.filename).suffix.lower()
    if ext != '.pdf':
        return jsonify({'error': '仅支持 .pdf 格式'}), 400

    filename = f'{sid}_report{ext}'
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    try:
        report = parse_pdf_report(filepath)
    except Exception as e:
        return jsonify({'error': f'报告解析失败: {str(e)}'}), 400

    sessions[sid]['report_path'] = filepath
    sessions[sid]['report_data'] = {
        'overall_rate': report.overall_rate,
        'high_risk_words': report.high_risk_words,
        'medium_risk_words': report.medium_risk_words,
        'low_risk_words': report.low_risk_words,
        'paragraph_count': len(report.paragraphs),
    }

    return jsonify({
        'overall_rate': report.overall_rate,
        'high_risk_words': report.high_risk_words,
        'medium_risk_words': report.medium_risk_words,
        'low_risk_words': report.low_risk_words,
        'flagged_paragraphs': len(report.paragraphs),
    })


@app.route('/api/analyze', methods=['POST'])
def analyze():
    """分析文档中的AI写作特征"""
    data = request.get_json() or {}
    sid = data.get('session_id')
    if not sid or sid not in sessions:
        return jsonify({'error': '无效的会话ID'}), 400

    min_length = data.get('min_length', 20)
    skip_english = data.get('skip_english', True)

    doc, paragraphs = read_docx(sessions[sid]['doc_path'])
    content = get_content_paragraphs(paragraphs)

    results = []
    for para in content:
        if para.word_count < min_length:
            continue
        if skip_english and _is_english(para.text):
            continue

        indicators = analyze_ai_patterns(para.text)
        if indicators['risk_score'] > 20:
            level = 'high' if indicators['risk_score'] >= 60 else (
                'medium' if indicators['risk_score'] >= 40 else 'low')
            results.append({
                'index': para.index,
                'text': para.text[:200] + ('...' if len(para.text) > 200 else ''),
                'full_text': para.text,
                'risk_score': indicators['risk_score'],
                'risk_level': level,
                'sequence_words': indicators['sequence_words'],
                'symmetric_structures': indicators['symmetric_structures'],
                'long_sentences': indicators['long_sentences'],
                'word_count': para.word_count,
            })

    results.sort(key=lambda x: x['risk_score'], reverse=True)

    if 'report_path' in sessions[sid]:
        report = parse_pdf_report(sessions[sid]['report_path'])
        doc_texts = [p.text for p in paragraphs]
        matched = match_paragraphs(report, doc_texts)
        matched_indices = {}
        for idx, risk_info in matched.items():
            matched_indices[idx] = {
                'probability': risk_info.probability,
                'report_risk': risk_info.risk_level.value,
            }
        for r in results:
            if r['index'] in matched_indices:
                r['report_probability'] = matched_indices[r['index']]['probability']
                r['report_risk'] = matched_indices[r['index']]['report_risk']
    else:
        matched_indices = {}

    sessions[sid]['analysis'] = results

    return jsonify({
        'total_flagged': len(results),
        'high_count': sum(1 for r in results if r['risk_level'] == 'high'),
        'medium_count': sum(1 for r in results if r['risk_level'] == 'medium'),
        'low_count': sum(1 for r in results if r['risk_level'] == 'low'),
        'paragraphs': results,
    })


@app.route('/api/preview', methods=['POST'])
def preview():
    """预览单段降重效果"""
    data = request.get_json() or {}
    sid = data.get('session_id')
    text = data.get('text', '')
    level = data.get('level', 2)
    risk = data.get('risk_level', 'medium')

    if not text:
        return jsonify({'error': '文本为空'}), 400

    transformer = Transformer(aggressiveness=level)
    result = transformer.transform(text, risk)

    return jsonify({
        'original': result.original,
        'transformed': result.transformed,
        'rules_applied': result.rules_applied,
        'change_ratio': round(result.change_ratio, 3),
    })


@app.route('/api/reduce', methods=['POST'])
def reduce():
    """执行降重并生成文档"""
    data = request.get_json() or {}
    sid = data.get('session_id')
    if not sid or sid not in sessions:
        return jsonify({'error': '无效的会话ID'}), 400

    level = data.get('level', 2)
    selected_indices = data.get('selected_indices', None)
    custom_replacements = data.get('custom_replacements', {})

    doc, paragraphs = read_docx(sessions[sid]['doc_path'])

    analysis = sessions[sid].get('analysis', [])
    if not analysis:
        return jsonify({'error': '请先执行分析'}), 400

    targets = {}
    risk_map = {}
    for item in analysis:
        idx = item['index']
        if selected_indices is not None and idx not in selected_indices:
            continue
        targets[idx] = item['full_text']
        risk_map[idx] = item['risk_level']

    transformer = Transformer(aggressiveness=level)
    results = transformer.batch_transform(targets, risk_map)

    applied = []
    for result in results:
        idx = result.paragraph_index
        new_text = custom_replacements.get(str(idx), result.transformed)
        if result.rules_applied or str(idx) in custom_replacements:
            replace_paragraph_text(doc, idx, new_text)
            applied.append({
                'index': idx,
                'original': result.original[:100],
                'transformed': new_text[:100],
                'rules': result.rules_applied,
            })

    orig_name = sessions[sid].get('original_name', 'document.docx')
    stem = Path(orig_name).stem
    out_filename = f'{sid}_{stem}_降重版.docx'
    out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_filename)
    save_docx(doc, out_path)

    sessions[sid]['output_path'] = out_path
    sessions[sid]['output_filename'] = f'{stem}_降重版.docx'

    return jsonify({
        'success': True,
        'modified_count': len(applied),
        'details': applied,
        'download_url': f'/api/download/{sid}',
    })


@app.route('/api/download/<sid>')
def download(sid):
    """下载降重后的文档"""
    if sid not in sessions or 'output_path' not in sessions[sid]:
        return jsonify({'error': '文件不存在'}), 404

    path = sessions[sid]['output_path']
    name = sessions[sid].get('output_filename', 'output.docx')

    return send_file(path, as_attachment=True, download_name=name)


@app.route('/api/test-llm', methods=['POST'])
def test_llm():
    """测试LLM API连接"""
    data = request.get_json() or {}
    api_key = data.get('api_key', '')
    api_url = data.get('api_url', 'https://api.openai.com/v1')
    if not api_key:
        return jsonify({'ok': False, 'message': 'API Key 不能为空', 'models': []})
    ai = AITransformer(api_key=api_key, api_url=api_url)
    result = ai.test_connection()
    return jsonify(result)


@app.route('/api/ai-preview', methods=['POST'])
def ai_preview():
    """AI预览单段降重效果"""
    data = request.get_json() or {}
    api_key = data.get('api_key', '')
    api_url = data.get('api_url', 'https://api.openai.com/v1')
    model = data.get('model', 'gpt-4o-mini')
    text = data.get('text', '')
    risk = data.get('risk_level', 'medium')

    if not api_key:
        return jsonify({'error': 'API Key 不能为空'}), 400
    if not text:
        return jsonify({'error': '文本为空'}), 400

    ai = AITransformer(api_key=api_key, api_url=api_url, model=model)
    result = ai.transform(text, risk)

    return jsonify({
        'original': result.original,
        'transformed': result.transformed,
        'rules_applied': result.rules_applied,
        'change_ratio': round(result.change_ratio, 3),
    })


@app.route('/api/ai-reduce', methods=['POST'])
def ai_reduce():
    """AI模式执行降重并生成文档"""
    data = request.get_json() or {}
    sid = data.get('session_id')
    if not sid or sid not in sessions:
        return jsonify({'error': '无效的会话ID'}), 400

    api_key = data.get('api_key', '')
    api_url = data.get('api_url', 'https://api.openai.com/v1')
    model = data.get('model', 'gpt-4o-mini')
    selected_indices = data.get('selected_indices', None)

    if not api_key:
        return jsonify({'error': 'API Key 不能为空'}), 400

    doc, paragraphs = read_docx(sessions[sid]['doc_path'])
    analysis = sessions[sid].get('analysis', [])
    if not analysis:
        return jsonify({'error': '请先执行分析'}), 400

    targets = {}
    risk_map = {}
    for item in analysis:
        idx = item['index']
        if selected_indices is not None and idx not in selected_indices:
            continue
        targets[idx] = item['full_text']
        risk_map[idx] = item['risk_level']

    ai = AITransformer(api_key=api_key, api_url=api_url, model=model)
    results = ai.batch_transform(targets, risk_map)

    applied = []
    errors = []
    for result in results:
        idx = result.paragraph_index
        if any('失败' in r for r in result.rules_applied):
            errors.append({
                'index': idx,
                'error': result.rules_applied[0] if result.rules_applied else 'unknown',
            })
            continue
        if result.transformed != result.original:
            replace_paragraph_text(doc, idx, result.transformed)
            applied.append({
                'index': idx,
                'original': result.original[:100],
                'transformed': result.transformed[:100],
                'rules': result.rules_applied,
            })

    orig_name = sessions[sid].get('original_name', 'document.docx')
    stem = Path(orig_name).stem
    round_num = sessions[sid].get('round', 1)
    out_filename = f'{sid}_{stem}_AI降重v{round_num}.docx'
    out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_filename)
    save_docx(doc, out_path)

    sessions[sid]['output_path'] = out_path
    sessions[sid]['output_filename'] = f'{stem}_AI降重版.docx'

    return jsonify({
        'success': True,
        'modified_count': len(applied),
        'error_count': len(errors),
        'details': applied,
        'errors': errors,
        'download_url': f'/api/download/{sid}',
    })


@app.route('/api/continue-reduce', methods=['POST'])
def continue_reduce():
    """继续降重：把上一轮输出作为新的输入文档重新分析"""
    data = request.get_json() or {}
    sid = data.get('session_id')
    if not sid or sid not in sessions:
        return jsonify({'error': '无效的会话ID'}), 400

    if 'output_path' not in sessions[sid]:
        return jsonify({'error': '没有上一轮的降重结果'}), 400

    sessions[sid]['doc_path'] = sessions[sid]['output_path']
    sessions[sid]['round'] = sessions[sid].get('round', 1) + 1
    sessions[sid].pop('analysis', None)
    sessions[sid].pop('report_path', None)
    sessions[sid].pop('report_data', None)

    try:
        doc, paragraphs = read_docx(sessions[sid]['doc_path'])
        stats = analyze_document(paragraphs)
        sessions[sid]['stats'] = stats
    except Exception as e:
        return jsonify({'error': f'文档解析失败: {str(e)}'}), 400

    return jsonify({
        'session_id': sid,
        'round': sessions[sid]['round'],
        'stats': stats,
    })


@app.route('/api/reset', methods=['POST'])
def reset_session():
    """重置会话，允许重新上传"""
    data = request.get_json() or {}
    sid = data.get('session_id')
    if sid and sid in sessions:
        del sessions[sid]
    return jsonify({'success': True})


@app.route('/api/export-json/<sid>')
def export_json(sid):
    """导出替换映射JSON"""
    if sid not in sessions or 'analysis' not in sessions[sid]:
        return jsonify({'error': '无分析数据'}), 404

    analysis = sessions[sid]['analysis']
    mapping = {str(item['index']): item['full_text'] for item in analysis}

    return jsonify(mapping)


def _is_english(text: str) -> bool:
    if not text:
        return False
    ascii_count = sum(1 for c in text if ord(c) < 128)
    return ascii_count / len(text) > 0.7


if __name__ == '__main__':
    print('\n  AIGC降重工具 Web版')
    print('  访问 http://localhost:5000\n')
    app.run(host='0.0.0.0', port=5000, debug=True)
