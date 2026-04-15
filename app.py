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
    Transformer, AITransformer, TransformResult,
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


def _normalize_ai_config(raw: dict | None) -> dict:
    """归一化前端传入的 AI 配置。"""
    raw = raw or {}

    api_url = str(
        raw.get('api_url') or raw.get('api_base_url') or 'https://api.openai.com/v1'
    ).strip()
    api_key = str(raw.get('api_key') or '').strip()
    model = str(raw.get('model') or raw.get('model_name') or 'gpt-3.5-turbo').strip()
    custom_prompt = str(raw.get('custom_prompt') or '').strip()

    temperature = raw.get('temperature', 0.85)
    try:
        temperature = float(temperature)
    except (TypeError, ValueError):
        temperature = 0.85
    temperature = max(0.0, min(2.0, temperature))

    return {
        'api_url': api_url,
        'api_key': api_key,
        'model': model,
        'temperature': temperature,
        'custom_prompt': custom_prompt,
    }


def _normalize_protected_words(raw) -> list[str]:
    """归一化术语保护列表。"""
    if not raw:
        return []
    if isinstance(raw, str):
        parts = raw.replace('，', ',').replace('\n', ',').split(',')
    else:
        parts = raw

    result = []
    for item in parts:
        word = str(item or '').strip()
        if word and word not in result:
            result.append(word)
    return result


def _json_error(message: str, status: int = 400, error: dict | None = None):
    """返回统一的 JSON 错误响应。"""
    payload = {'error': message}
    if error:
        payload['error_detail'] = error
    return jsonify(payload), status


def _sanitize_final_text(value) -> str:
    """对最终导出的文本做基础清洗，防止异常控制字符注入。"""
    text = str(value or '')
    text = text.replace('\x00', '')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    cleaned = []
    for ch in text:
        code = ord(ch)
        if ch in ('\n', '\t'):
            cleaned.append(ch)
            continue
        if 32 <= code <= 0xD7FF or 0xE000 <= code <= 0xFFFD or 0x10000 <= code <= 0x10FFFF:
            cleaned.append(ch)
    return ''.join(cleaned).strip()


def _init_progress(sid: str, total: int, label: str):
    """初始化会话进度。"""
    sessions[sid]['progress'] = {
        'current': 0,
        'total': total,
        'label': label,
        'done': False,
    }


def _update_progress(sid: str, current: int, *, label: str | None = None, done: bool = False):
    """更新会话进度。"""
    progress = sessions.get(sid, {}).get('progress')
    if not progress:
        return
    progress['current'] = current
    if label is not None:
        progress['label'] = label
    progress['done'] = done


def _clear_progress(sid: str):
    """清理会话进度。"""
    if sid in sessions:
        sessions[sid].pop('progress', None)


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

    ext = Path(file.filename).suffix.lower().strip()
    if ext != '.docx' or not str(file.filename).lower().endswith('.docx'):
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
    ext = Path(file.filename).suffix.lower().strip()
    if ext != '.pdf' or not str(file.filename).lower().endswith('.pdf'):
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
                'is_low_confidence': getattr(risk_info, 'is_low_confidence', False),
            }
        for r in results:
            if r['index'] in matched_indices:
                r['report_probability'] = matched_indices[r['index']]['probability']
                r['report_risk'] = matched_indices[r['index']]['report_risk']
                r['report_low_confidence'] = matched_indices[r['index']]['is_low_confidence']
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
    result = transformer.transform(
        text,
        risk,
        protected_words=_normalize_protected_words(data.get('protected_words')),
    )

    return jsonify({
        'original': result.original,
        'transformed': result.transformed,
        'rules_applied': result.rules_applied,
        'change_ratio': round(result.change_ratio, 3),
    })


@app.route('/api/reduce', methods=['POST'])
def reduce():
    """执行降重并生成文档，支持混合模式与术语保护。"""
    data = request.get_json() or {}
    sid = data.get('session_id')
    if not sid or sid not in sessions:
        return _json_error('无效的会话ID')

    level = data.get('level', 2)
    selected_indices = data.get('selected_indices', None)
    custom_replacements = data.get('custom_replacements', {})
    ai_config = _normalize_ai_config(data.get('ai_config'))
    protected_words = _normalize_protected_words(data.get('protected_words'))
    hybrid_mode = bool(data.get('hybrid_mode'))

    doc, paragraphs = read_docx(sessions[sid]['doc_path'])

    analysis = sessions[sid].get('analysis', [])
    if not analysis:
        return _json_error('请先执行分析')

    analysis_map = {item['index']: item for item in analysis}
    targets = {}
    risk_map = {}
    for item in analysis:
        idx = item['index']
        if selected_indices is not None and idx not in selected_indices:
            continue
        targets[idx] = item['full_text']
        risk_map[idx] = item.get('report_risk') or item['risk_level']

    if not targets:
        return _json_error('没有可处理的段落')

    use_hybrid = hybrid_mode and bool(sessions[sid].get('report_data'))
    if use_hybrid and not ai_config['api_key']:
        return _json_error('混合模式需要提供 API Key')

    _init_progress(sid, len(targets), '混合降重进行中' if use_hybrid else '规则降重进行中')

    rule_transformer = Transformer(aggressiveness=2 if use_hybrid else level)
    ai_transformer = AITransformer(api_config=ai_config) if use_hybrid else None

    results = []
    errors = []
    processed = 0

    for idx, text in targets.items():
        risk_level = risk_map.get(idx, 'low')
        processed += 1

        if use_hybrid:
            if risk_level in ('high', 'medium'):
                result = ai_transformer.transform(
                    text,
                    risk_level,
                    protected_words=protected_words,
                    custom_prompt=ai_config.get('custom_prompt', ''),
                )
                mode = 'ai'
                if result.error:
                    fallback = rule_transformer.transform(
                        text,
                        'high' if risk_level == 'high' else 'medium',
                        protected_words=protected_words,
                    )
                    fallback.rules_applied.append('AI失败后自动切换规则降重')
                    result = fallback
                    mode = 'rule'
            elif risk_level == 'low':
                result = rule_transformer.transform(
                    text,
                    'medium',
                    protected_words=protected_words,
                )
                mode = 'rule'
            else:
                result = TransformResult(
                    paragraph_index=idx,
                    original=text,
                    transformed=text,
                    rules_applied=[],
                    change_ratio=0,
                )
                mode = 'skip'
        else:
            result = rule_transformer.transform(
                text,
                risk_level,
                protected_words=protected_words,
            )
            mode = 'rule'

        result.paragraph_index = idx
        results.append((mode, result))
        _update_progress(sid, processed)

    applied = []
    for mode, result in results:
        idx = result.paragraph_index
        if result.error:
            errors.append({'index': idx, 'error': result.error})
            continue

        new_text = custom_replacements.get(str(idx), result.transformed)
        if mode == 'skip' and str(idx) not in custom_replacements:
            continue
        if result.rules_applied or str(idx) in custom_replacements:
            replace_paragraph_text(doc, idx, new_text)
            applied.append({
                'index': idx,
                'original': result.original,
                'transformed': new_text,
                'rules': result.rules_applied + ([f'模式: {mode}'] if mode != 'skip' else ['模式: 跳过']),
                'method': 'none' if mode == 'skip' else mode,
                'risk_level': risk_map.get(idx, 'unknown'),
                'is_low_confidence': bool(analysis_map.get(idx, {}).get('report_low_confidence', False)),
                'fallback': mode == 'rule' and any('AI失败后自动切换规则降重' in str(rule) for rule in result.rules_applied),
            })

    orig_name = sessions[sid].get('original_name', 'document.docx')
    stem = Path(orig_name).stem
    out_filename = f'{sid}_{stem}_降重版.docx'
    out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_filename)
    save_docx(doc, out_path)

    sessions[sid]['output_path'] = out_path
    sessions[sid]['output_filename'] = f'{stem}_降重版.docx'
    sessions[sid]['last_ai_config'] = {
        'api_url': ai_config['api_url'],
        'model': ai_config['model'],
        'temperature': ai_config['temperature'],
        'custom_prompt': ai_config['custom_prompt'],
        'has_api_key': bool(ai_config['api_key']),
    }
    sessions[sid]['last_reduce_options'] = {
        'protected_words': protected_words,
        'hybrid_mode': use_hybrid,
    }
    _update_progress(sid, len(targets), label='降重已完成', done=True)

    status = 200
    if errors and not applied:
        status = errors[0].get('error', {}).get('status', 502) or 502

    analysis_result = []
    for mode, result in results:
        analysis_result.append({
            'index': result.paragraph_index,
            'method': 'none' if mode == 'skip' else mode,
            'risk_level': risk_map.get(result.paragraph_index, 'unknown'),
            'has_error': bool(result.error),
        })

    return jsonify({
        'success': True,
        'modified_count': len(applied),
        'error_count': len(errors),
        'details': applied,
        'analysis': analysis_result,
        'errors': errors,
        'download_url': f'/api/download/{sid}',
        'hybrid_mode': use_hybrid,
        'protected_words_count': len(protected_words),
        'fallback': any(item.get('fallback') for item in applied),
        'ai_config_received': {
            'api_url': ai_config['api_url'],
            'model': ai_config['model'],
            'temperature': ai_config['temperature'],
            'custom_prompt': ai_config['custom_prompt'],
            'has_api_key': bool(ai_config['api_key']),
        },
    }), status


@app.route('/api/download/<sid>')
def download(sid):
    """下载最终导出的文档。"""
    if sid not in sessions:
        return jsonify({'error': '文件不存在'}), 404

    path = sessions[sid].get('final_output_path') or sessions[sid].get('output_path')
    name = sessions[sid].get('final_output_filename') or sessions[sid].get('output_filename', 'output.docx')
    if not path or not os.path.exists(path):
        return jsonify({'error': '文件不存在'}), 404

    return send_file(path, as_attachment=True, download_name=name)


@app.route('/api/test_llm', methods=['POST'])
@app.route('/api/test-llm', methods=['POST'])
def test_llm():
    """测试 LLM API 连接。"""
    data = request.get_json() or {}
    ai_config = _normalize_ai_config(data)
    ai = AITransformer(api_config=ai_config)
    result = ai.test_connection()
    status = 200 if result.get('ok') else result.get('error', {}).get('status', 400) or 400
    return jsonify(result), status


@app.route('/api/ai-preview', methods=['POST'])
def ai_preview():
    """AI 预览单段降重效果。"""
    data = request.get_json() or {}
    text = data.get('text', '')
    risk = data.get('risk_level', 'medium')
    ai_config = _normalize_ai_config(data)

    if not ai_config['api_key']:
        return _json_error('API Key 不能为空')
    if not text:
        return _json_error('文本为空')

    ai = AITransformer(api_config=ai_config)
    protected_words = _normalize_protected_words(data.get('protected_words'))
    result = ai.transform(
        text,
        risk,
        protected_words=protected_words,
        custom_prompt=ai_config.get('custom_prompt', ''),
    )

    if result.error:
        original_ai_error = result.error
        fallback = Transformer(aggressiveness=3)
        result = fallback.transform(text, risk, protected_words=protected_words)
        result.rules_applied.append('AI失败后自动切换规则降重')
        if original_ai_error:
            code = original_ai_error.get('code', 'unknown')
            msg = str(original_ai_error.get('message', ''))[:80]
            result.rules_applied.append(f'AI错误: {code}')
            if msg:
                result.rules_applied.append(f'失败原因: {msg}')
        return jsonify({
            'original': result.original,
            'transformed': result.transformed,
            'rules_applied': result.rules_applied,
            'change_ratio': round(result.change_ratio, 3),
            'fallback': 'rule',
            'fallback_message': '已自动转为规则降重',
            'ai_error': original_ai_error,
        })

    return jsonify({
        'original': result.original,
        'transformed': result.transformed,
        'rules_applied': result.rules_applied,
        'change_ratio': round(result.change_ratio, 3),
    })


@app.route('/api/ai-reduce', methods=['POST'])
def ai_reduce():
    """AI 模式执行降重并生成文档。"""
    data = request.get_json() or {}
    sid = data.get('session_id')
    if not sid or sid not in sessions:
        return _json_error('无效的会话ID')

    selected_indices = data.get('selected_indices', None)
    ai_config = _normalize_ai_config(data)
    protected_words = _normalize_protected_words(data.get('protected_words'))

    if not ai_config['api_key']:
        return _json_error('API Key 不能为空')

    doc, paragraphs = read_docx(sessions[sid]['doc_path'])
    analysis = sessions[sid].get('analysis', [])
    if not analysis:
        return _json_error('请先执行分析')

    targets = {}
    risk_map = {}
    for item in analysis:
        idx = item['index']
        if selected_indices is not None and idx not in selected_indices:
            continue
        targets[idx] = item['full_text']
        risk_map[idx] = item['risk_level']

    _init_progress(sid, len(targets), 'AI 降重进行中')

    ai = AITransformer(api_config=ai_config)
    results = []
    diagnostics = []
    for current, (idx, text) in enumerate(targets.items(), start=1):
        level_name = risk_map.get(idx, 'medium')
        ai_result = ai.transform(
            text,
            level_name,
            protected_words=protected_words,
            custom_prompt=ai_config.get('custom_prompt', ''),
        )

        original_ai_error = ai_result.error
        used_fallback = False
        result = ai_result
        if ai_result.error:
            used_fallback = True
            fallback = Transformer(aggressiveness=3)
            result = fallback.transform(text, level_name, protected_words=protected_words)
            result.rules_applied.append('AI失败后自动切换规则降重')
            if original_ai_error:
                code = original_ai_error.get('code', 'unknown')
                msg = str(original_ai_error.get('message', ''))[:80]
                result.rules_applied.append(f'AI错误: {code}')
                if msg:
                    result.rules_applied.append(f'失败原因: {msg}')

        result.paragraph_index = idx
        results.append((result, original_ai_error, used_fallback))
        diagnostics.append({
            'index': idx,
            'method': 'rule' if used_fallback else 'ai',
            'fallback': used_fallback,
            'ai_error': original_ai_error,
        })
        _update_progress(sid, current, label=f'AI 降重进行中（{current}/{len(targets)}）')

    applied = []
    errors = []
    for result, original_ai_error, used_fallback in results:
        idx = result.paragraph_index
        if result.error:
            errors.append({
                'index': idx,
                'error': result.error,
            })
            continue
        if result.transformed != result.original:
            replace_paragraph_text(doc, idx, result.transformed)
            applied.append({
                'index': idx,
                'original': result.original,
                'transformed': result.transformed,
                'rules': result.rules_applied,
                'method': 'rule' if used_fallback else 'ai',
                'risk_level': risk_map.get(idx, 'unknown'),
                'is_low_confidence': bool(next((item.get('report_low_confidence', False) for item in analysis if item['index'] == idx), False)),
                'fallback': used_fallback,
                'ai_error': original_ai_error,
            })

    orig_name = sessions[sid].get('original_name', 'document.docx')
    stem = Path(orig_name).stem
    round_num = sessions[sid].get('round', 1)
    out_filename = f'{sid}_{stem}_AI降重v{round_num}.docx'
    out_path = os.path.join(app.config['OUTPUT_FOLDER'], out_filename)
    save_docx(doc, out_path)

    sessions[sid]['output_path'] = out_path
    sessions[sid]['output_filename'] = f'{stem}_AI降重版.docx'
    _update_progress(sid, len(targets), label='AI 降重已完成', done=True)

    status = 200
    if errors and not applied:
        status = errors[0].get('error', {}).get('status', 502) or 502

    return jsonify({
        'success': True,
        'modified_count': len(applied),
        'error_count': len(errors),
        'details': applied,
        'errors': errors,
        'diagnostics': diagnostics,
        'download_url': f'/api/download/{sid}',
        'fallback': any(item.get('fallback') for item in applied),
        'summary': {
            'ai_success_count': sum(1 for item in diagnostics if item['method'] == 'ai' and not item['fallback']),
            'fallback_count': sum(1 for item in diagnostics if item['fallback']),
            'failed_count': len(errors),
        },
    }), status


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


@app.route('/api/finalize-export', methods=['POST'])
def finalize_export():
    """根据前端传回的最终文本映射生成最终导出版文档。"""
    data = request.get_json() or {}
    sid = data.get('session_id')
    final_results = data.get('final_results') or {}

    if not sid or sid not in sessions:
        return _json_error('无效的会话ID')
    if not isinstance(final_results, dict) or not final_results:
        return _json_error('final_results 不能为空')

    source_path = sessions[sid].get('doc_path')
    if not source_path or not os.path.exists(source_path):
        return _json_error('原始文档不存在', 404)

    doc, paragraphs = read_docx(source_path)

    applied_count = 0
    for key, final_text in final_results.items():
        try:
            idx = int(key)
        except (TypeError, ValueError):
            continue
        if final_text is None:
            continue

        safe_text = _sanitize_final_text(final_text)
        if replace_paragraph_text(doc, idx, safe_text):
            applied_count += 1

    orig_name = sessions[sid].get('original_name', 'document.docx')
    stem = Path(orig_name).stem
    final_filename = f'{sid}_{stem}_最终版.docx'
    final_path = os.path.join(app.config['OUTPUT_FOLDER'], final_filename)
    save_docx(doc, final_path)

    sessions[sid]['final_output_path'] = final_path
    sessions[sid]['final_output_filename'] = f'{stem}_最终版.docx'
    sessions[sid]['final_results'] = final_results

    return jsonify({
        'success': True,
        'applied_count': applied_count,
        'download_url': f'/api/download/{sid}',
        'filename': sessions[sid]['final_output_filename'],
    })


@app.route('/api/progress/<sid>')
def progress(sid):
    """获取当前会话处理进度。"""
    if sid not in sessions:
        return _json_error('无效的会话ID')

    progress_data = sessions[sid].get('progress') or {
        'current': 0,
        'total': 0,
        'label': '暂无任务',
        'done': True,
    }
    return jsonify(progress_data)


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
