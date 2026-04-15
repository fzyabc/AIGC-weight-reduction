/* ============================================================
   AIGC Reducer - 前端逻辑
   ============================================================ */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const state = {
    sessionId: null,
    level: 2,
    paragraphs: [],
    paragraphMap: new Map(),
    selectedIndices: new Set(),
    round: 1,
    aiEnabled: false,
    progress: {
        current: 0,
        total: 0,
        timer: null,
    },
    finalResults: {},
    hasUnsavedEdits: false,
    progressWarningShown: false,
    liveReduce: {
        current: 0,
        total: 0,
        done: false,
    },
};

// ============================================================
// 初始化
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    initUploadZones();
    initLevelSelector();
    initActionButtons();
    initReuploadButtons();
    initAISection();
    window.__AIGC_FRONTEND_BUILD__ = document.body.dataset.frontendVersion || 'unknown';
    console.log('[AIGC Reducer] frontend build =', window.__AIGC_FRONTEND_BUILD__);
    window.addEventListener('beforeunload', (e) => {
        if (!state.hasUnsavedEdits) return;
        e.preventDefault();
        e.returnValue = '你有未保存的修改，确认离开吗？';
    });
});

// ============================================================
// 文件上传
// ============================================================
function initUploadZones() {
    const docZone = $('#docDropZone');
    const docInput = $('#docInput');
    const reportZone = $('#reportDropZone');
    const reportInput = $('#reportInput');

    const setupDragDrop = (zone, input, handler) => {
        zone.addEventListener('click', () => input.click());
        input.addEventListener('change', () => {
            if (input.files[0]) handler(input.files[0]);
        });
        zone.addEventListener('dragover', (e) => {
            e.preventDefault();
            zone.classList.add('dragover');
        });
        zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
        zone.addEventListener('drop', (e) => {
            e.preventDefault();
            zone.classList.remove('dragover');
            if (e.dataTransfer.files[0]) handler(e.dataTransfer.files[0]);
        });
    };

    setupDragDrop(docZone, docInput, uploadDoc);
    setupDragDrop(reportZone, reportInput, uploadReport);
}

async function uploadDoc(file) {
    if (!file.name.endsWith('.docx')) {
        alert('请上传 .docx 格式的文件');
        return;
    }

    setStatus('working', '上传中...');
    showLoading('上传文档中...');

    const form = new FormData();
    form.append('file', file);

    try {
        const res = await fetch('/api/upload', { method: 'POST', body: form });
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        state.sessionId = data.session_id;

        $('#docDropZone').style.display = 'none';
        $('#docInfo').style.display = 'flex';
        $('#docName').textContent = data.filename;

        $('#statsBar').style.display = 'flex';
        $('#statParas').textContent = data.stats.content_paragraphs;
        $('#statWords').textContent = data.stats.total_words.toLocaleString();

        $('#analyzeBtn').disabled = false;
        setStatus('active', '文档已加载');
        $('#emptyState').style.display = 'none';
    } catch (err) {
        alert('上传失败: ' + err.message);
        setStatus('', '上传失败');
    } finally {
        hideLoading();
    }
}

async function uploadReport(file) {
    if (!state.sessionId) {
        alert('请先上传论文文档');
        return;
    }
    if (!file.name.endsWith('.pdf')) {
        alert('请上传 .pdf 格式的检测报告');
        return;
    }

    showLoading('解析检测报告...');
    const form = new FormData();
    form.append('file', file);
    form.append('session_id', state.sessionId);

    try {
        const res = await fetch('/api/upload-report', { method: 'POST', body: form });
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        $('#reportDropZone').style.display = 'none';
        $('#reportInfo').style.display = 'flex';
        $('#reportName').textContent = file.name;
        $('#reportStats').style.display = 'flex';
        $('#overallRate').textContent = data.overall_rate + '%';
        $('#highWords').textContent = data.high_risk_words + '字';
        $('#medWords').textContent = data.medium_risk_words + '字';
    } catch (err) {
        alert('报告解析失败: ' + err.message);
    } finally {
        hideLoading();
    }
}

// ============================================================
// 降重力度选择
// ============================================================
function initLevelSelector() {
    $$('.level-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            $$('.level-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            state.level = parseInt(btn.dataset.level);
        });
    });
}

// ============================================================
// 分析 & 降重
// ============================================================
function initActionButtons() {
    $('#analyzeBtn').addEventListener('click', runAnalyze);
    $('#reduceBtn').addEventListener('click', runReduce);
    $('#aiReduceBtn').addEventListener('click', runAIReduce);
    $('#selectAllBtn').addEventListener('click', selectAll);
    $('#selectHighBtn').addEventListener('click', selectHighOnly);
    $('#exportBtn').addEventListener('click', exportJson);
}

async function runAnalyze() {
    if (!state.sessionId) return;

    setStatus('working', '分析中...');
    showLoading('扫描AI写作特征...');

    try {
        const res = await fetch('/api/analyze', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: state.sessionId,
                min_length: parseInt($('#minLength').value) || 20,
                skip_english: $('#skipEnglish').checked,
            }),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        state.paragraphs = data.paragraphs;
        state.paragraphMap = new Map(data.paragraphs.map(p => [p.index, p]));

        $('#statHigh').textContent = data.high_count;
        $('#statMed').textContent = data.medium_count;
        $('#statLow').textContent = data.low_count;

        renderParagraphs(data.paragraphs);

        $('#resultsArea').style.display = 'block';
        $('#reduceResult').style.display = 'none';
        $('#reduceBtn').disabled = false;
        if (state.aiEnabled) $('#aiReduceBtn').disabled = false;
        $('#exportBtn').style.display = 'inline-flex';

        setStatus('active', `发现 ${data.total_flagged} 个风险段落`);
    } catch (err) {
        alert('分析失败: ' + err.message);
        setStatus('', '分析失败');
    } finally {
        hideLoading();
    }
}

async function runReduce() {
    if (!state.sessionId || state.paragraphs.length === 0) return;

    const selected = getSelectedIndices();
    if (selected.length === 0) {
        alert('请至少选择一个段落');
        return;
    }

    const aiConfig = getAIConfig();
    const protectedWords = getProtectedWords();
    const hybridMode = $('#hybridMode').checked;
    saveAIConfig();

    setStatus('working', hybridMode ? '混合降重中...' : '降重中...');
    startProgress(selected.length, hybridMode ? '混合降重进行中' : '规则降重进行中');
    startLiveProgress(selected.length, hybridMode ? '实时混合降重监控' : '实时规则降重监控');
    prepareLiveReduceView();
    pollProgress(state.sessionId);

    try {
        const tasks = selected.map(async (index, order) => {
            const para = state.paragraphMap.get(index);
            if (!para) {
                updateLiveProgress(order + 1, selected.length);
                return null;
            }

            const useAiPreview = hybridMode && Boolean(aiConfig.api_key);
            const previewUrl = useAiPreview ? '/api/ai-preview' : '/api/preview';
            const previewPayload = useAiPreview
                ? {
                    text: para.full_text || para.text,
                    risk_level: para.risk_level,
                    protected_words: protectedWords,
                    ...aiConfig,
                }
                : {
                    session_id: state.sessionId,
                    text: para.full_text || para.text,
                    level: state.level,
                    risk_level: para.risk_level,
                    protected_words: protectedWords,
                };

            try {
                const previewRes = await fetch(previewUrl, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(previewPayload),
                });
                const previewData = await previewRes.json();
                appendParagraphToView({
                    index,
                    original: para.full_text || para.text,
                    transformed: previewData.transformed || para.full_text || para.text,
                    rules: previewData.rules_applied || [],
                    method: previewData.fallback === 'rule' ? 'rule' : (useAiPreview ? 'ai' : 'rule'),
                    risk_level: para.risk_level,
                    is_low_confidence: Boolean(para.report_low_confidence),
                    fallback: previewData.fallback === 'rule',
                    ai_error: previewData.ai_error || null,
                });
            } catch {
                appendParagraphToView({
                    index,
                    original: para.full_text || para.text,
                    transformed: para.full_text || para.text,
                    rules: ['实时预览失败，等待最终结果'],
                    method: useAiPreview ? 'ai' : 'rule',
                    risk_level: para.risk_level,
                    is_low_confidence: Boolean(para.report_low_confidence),
                    fallback: false,
                    ai_error: {
                        code: 'preview_failed',
                        message: `实时渲染阶段调用 ${previewUrl} 失败`,
                    },
                });
            } finally {
                updateLiveProgress(order + 1, selected.length);
            }
            return null;
        });

        const finalPromise = fetch('/api/reduce', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: state.sessionId,
                level: state.level,
                selected_indices: selected,
                ai_config: aiConfig,
                protected_words: protectedWords,
                hybrid_mode: hybridMode,
            }),
        }).then(res => res.json());

        await Promise.all(tasks);
        const data = await finalPromise;
        if (data.error) throw new Error(data.error);

        finishProgress(selected.length, selected.length, '规则降重已完成');
        renderReduceResult(data);

        $('#resultsArea').style.display = 'none';
        $('#reduceResult').style.display = 'block';

        const modeText = data.hybrid_mode ? '混合降重' : '规则降重';
        setStatus('active', `${modeText}完成! 修改了 ${data.modified_count} 个段落`);
    } catch (err) {
        alert('降重失败: ' + err.message);
        setStatus('', '降重失败');
    } finally {
        stopProgress();
        stopLiveProgress();
        hideLoading();
    }
}

// ============================================================
// 渲染段落列表
// ============================================================
function renderParagraphs(paragraphs) {
    const list = $('#paragraphList');
    list.innerHTML = '';

    state.selectedIndices.clear();

    paragraphs.forEach(p => {
        state.selectedIndices.add(p.index);

        const item = document.createElement('div');
        item.className = 'para-item selected';
        item.dataset.index = p.index;

        const tags = [];
        if (p.sequence_words) tags.push(`序列词×${p.sequence_words}`);
        if (p.symmetric_structures) tags.push(`对称结构×${p.symmetric_structures}`);
        if (p.long_sentences) tags.push(`长句×${p.long_sentences}`);

        const reportBadge = p.report_probability
            ? `<span class="para-badge ${p.report_risk}">${p.report_probability}%</span>` : '';

        item.innerHTML = `
            <div class="para-top">
                <input type="checkbox" class="para-checkbox" checked data-idx="${p.index}">
                <span class="para-badge ${p.risk_level}">${
                    p.risk_level === 'high' ? '高风险' :
                    p.risk_level === 'medium' ? '中风险' : '低风险'
                }</span>
                ${reportBadge}
                <span class="para-index">P${p.index}</span>
                <span class="para-score">风险分 ${p.risk_score}</span>
            </div>
            <div class="para-text">${escapeHtml(p.text)}</div>
            ${tags.length ? `<div class="para-tags">${tags.map(t => `<span class="para-tag">${t}</span>`).join('')}</div>` : ''}
            <div class="para-expanded">
                <div class="preview-section">
                    <div class="preview-label">降重预览</div>
                    <div class="preview-text" id="preview-${p.index}">点击加载预览...</div>
                    <div class="preview-rules" id="rules-${p.index}"></div>
                </div>
            </div>
        `;

        const checkbox = item.querySelector('.para-checkbox');
        checkbox.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleSelect(p.index, checkbox.checked, item);
        });

        item.addEventListener('click', (e) => {
            if (e.target.type === 'checkbox') return;
            toggleExpand(item, p);
        });

        list.appendChild(item);
    });
}

function toggleSelect(index, checked, item) {
    if (checked) {
        state.selectedIndices.add(index);
        item.classList.add('selected');
    } else {
        state.selectedIndices.delete(index);
        item.classList.remove('selected');
    }
}

async function toggleExpand(item, para) {
    const wasExpanded = item.classList.contains('expanded');
    item.classList.toggle('expanded');

    if (!wasExpanded) {
        const previewEl = $(`#preview-${para.index}`);
        const rulesEl = $(`#rules-${para.index}`);
        if (previewEl.textContent === '点击加载预览...') {
            previewEl.textContent = '加载中...';
            try {
                const res = await fetch('/api/preview', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        session_id: state.sessionId,
                        text: para.full_text,
                        level: state.level,
                        risk_level: para.risk_level,
                        protected_words: getProtectedWords(),
                    }),
                });
                const data = await res.json();
                previewEl.textContent = data.transformed;
                rulesEl.innerHTML = data.rules_applied
                    .map(r => `<span class="rule-tag">${r}</span>`).join('');
            } catch {
                previewEl.textContent = '预览加载失败';
                setStatus('', '预览加载失败');
            }
        }
    }
}

function selectAll() {
    state.selectedIndices.clear();
    $$('.para-item').forEach(item => {
        const idx = parseInt(item.dataset.index);
        state.selectedIndices.add(idx);
        item.classList.add('selected');
        item.querySelector('.para-checkbox').checked = true;
    });
}

function selectHighOnly() {
    state.selectedIndices.clear();
    $$('.para-item').forEach(item => {
        const idx = parseInt(item.dataset.index);
        const para = state.paragraphs.find(p => p.index === idx);
        const isHigh = para && para.risk_level === 'high';
        item.querySelector('.para-checkbox').checked = isHigh;
        if (isHigh) {
            state.selectedIndices.add(idx);
            item.classList.add('selected');
        } else {
            item.classList.remove('selected');
        }
    });
}

function getSelectedIndices() {
    return Array.from(state.selectedIndices);
}

// ============================================================
// 降重结果
// ============================================================
function prepareLiveReduceView() {
    $('#resultsArea').style.display = 'none';
    $('#reduceResult').style.display = 'block';
    $('#changeList').innerHTML = '';
    $('#reduceInfo').textContent = '正在实时渲染降重结果...';
    const fallbackNotice = $('#fallbackNotice');
    if (fallbackNotice) fallbackNotice.style.display = 'none';
    state.finalResults = {};
    state.hasUnsavedEdits = false;
    updateDownloadButtonState();
}

function appendParagraphToView(detail) {
    const list = $('#changeList');
    const existing = list.querySelector(`.change-item[data-index="${detail.index}"]`);
    const badge = getMethodBadge(detail.method);
    const lowConfidenceBadge = detail.is_low_confidence
        ? '<span class="badge-warning">⚠ 匹配置信度较低</span>'
        : '';
    const aiErrorTags = detail.ai_error ? buildAiErrorTags(detail.ai_error) : '';
    const strategyTags = (Array.isArray(detail.rules) ? detail.rules : [])
        .map(rule => `<span class="strategy-tag">${escapeHtml(rule)}</span>`)
        .join('') + aiErrorTags;
    const html = `
        <div class="change-header">
            <span class="para-index">P${detail.index}</span>
            <span class="method-badge ${badge.className}">${badge.text}</span>
            ${lowConfidenceBadge}
            <span class="change-arrow">→</span>
            <button class="btn btn-small undo-btn" data-index="${detail.index}" title="撤销">↶</button>
            <button class="btn btn-small retry-btn" data-index="${detail.index}">单段重试</button>
        </div>
        <div class="change-before">${escapeHtml(detail.original || '')}</div>
        <div class="change-after editable" contenteditable="true" data-index="${detail.index}">${renderDiffHtml(detail.original || '', detail.transformed || '')}</div>
        <div class="change-meta">${strategyTags}</div>
    `;

    let item = existing;
    if (!item) {
        item = document.createElement('div');
        item.className = 'change-item';
        item.dataset.index = detail.index;
        list.appendChild(item);
    }

    item.dataset.method = detail.method || 'none';
    item.dataset.riskLevel = detail.risk_level || 'medium';
    item.dataset.original = detail.original || '';
    item.innerHTML = html;

    state.finalResults[String(detail.index)] = [detail.transformed || ''];

    const editable = item.querySelector('.change-after');
    editable.addEventListener('focus', () => {
        editable.innerText = getCurrentFinalText(String(detail.index));
    });
    editable.addEventListener('blur', () => {
        const idx = String(detail.index);
        const newText = editable.innerText;
        pushHistoryVersion(idx, newText);
        editable.innerHTML = renderDiffHtml(item.dataset.original || detail.original || '', newText);
        state.hasUnsavedEdits = true;
        updateDownloadButtonState();
    });

    item.querySelector('.retry-btn').addEventListener('click', () => retrySingleSegment(item, detail));
    item.querySelector('.undo-btn').addEventListener('click', () => undoSingleSegment(item, detail.index));
}

function renderReduceResult(data) {
    let infoText = `已修改 ${data.modified_count} 个段落`;
    if (data.summary) {
        const aiSuccess = data.summary.ai_success_count || 0;
        const fallbackCount = data.summary.fallback_count || 0;
        const failedCount = data.summary.failed_count || 0;
        const parts = [`AI成功 ${aiSuccess} 段`];
        if (fallbackCount > 0) parts.push(`规则兜底 ${fallbackCount} 段`);
        if (failedCount > 0) parts.push(`彻底失败 ${failedCount} 段`);
        infoText = `${infoText}（${parts.join('，')}）`;
    }
    $('#reduceInfo').textContent = infoText;

    state.finalResults = {};
    data.details.forEach(d => {
        state.finalResults[String(d.index)] = [d.transformed];
    });
    state.hasUnsavedEdits = false;

    const downloadBtn = $('#downloadBtn');
    downloadBtn.onclick = finalizeAndDownload;
    downloadBtn.textContent = '保存所有修改并下载';
    downloadBtn.classList.remove('btn-warning');
    $('#continueBtn').onclick = continueReduce;
    $('#restartBtn').onclick = resetDoc;

    const fallbackNotice = $('#fallbackNotice');
    const fallbackNoticeText = $('#fallbackNoticeText');
    const hasFallback = Boolean(data.fallback)
        || data.details.some(d => d.fallback)
        || data.details.some(d => Array.isArray(d.rules) && d.rules.some(rule => String(rule).includes('AI失败后自动切换规则降重')));
    fallbackNotice.style.display = hasFallback ? 'flex' : 'none';
    if (hasFallback && fallbackNoticeText) {
        fallbackNoticeText.textContent = '已自动切换为规则降重兜底';
    }

    const list = $('#changeList');
    list.innerHTML = '';

    data.details.forEach(d => {
        const badge = getMethodBadge(d.method);
        const lowConfidenceBadge = d.is_low_confidence
            ? '<span class="badge-warning">⚠ 匹配置信度较低</span>'
            : '';
        const aiErrorTags = d.ai_error ? buildAiErrorTags(d.ai_error) : '';
        const strategyTags = (Array.isArray(d.rules) ? d.rules : [])
            .map(rule => `<span class="strategy-tag">${escapeHtml(rule)}</span>`)
            .join('') + aiErrorTags;
        const item = document.createElement('div');
        item.className = 'change-item';
        item.dataset.index = d.index;
        item.dataset.method = d.method || 'none';
        item.dataset.riskLevel = d.risk_level || 'medium';
        item.dataset.original = d.original;

        item.innerHTML = `
            <div class="change-header">
                <span class="para-index">P${d.index}</span>
                <span class="method-badge ${badge.className}">${badge.text}</span>
                ${lowConfidenceBadge}
                <span class="change-arrow">→</span>
                <button class="btn btn-small undo-btn" data-index="${d.index}" title="撤销">↶</button>
                <button class="btn btn-small retry-btn" data-index="${d.index}">单段重试</button>
            </div>
            <div class="change-before">${escapeHtml(d.original)}</div>
            <div class="change-after editable" contenteditable="true" data-index="${d.index}">${renderDiffHtml(d.original, d.transformed)}</div>
            <div class="change-meta">${strategyTags}</div>
        `;

        const editable = item.querySelector('.change-after');
        editable.addEventListener('focus', () => {
            editable.innerText = getCurrentFinalText(String(d.index));
        });
        editable.addEventListener('blur', () => {
            const idx = String(d.index);
            const newText = editable.innerText;
            pushHistoryVersion(idx, newText);
            editable.innerHTML = renderDiffHtml(item.dataset.original || d.original, newText);
            state.hasUnsavedEdits = true;
            updateDownloadButtonState();
            showSaveFeedback('已记录修改，待保存');
        });

        item.querySelector('.retry-btn').addEventListener('click', () => retrySingleSegment(item, d));
        item.querySelector('.undo-btn').addEventListener('click', () => undoSingleSegment(item, d.index));
        list.appendChild(item);
    });
}

function getMethodBadge(method) {
    if (method === 'ai') {
        return { text: '[AI Mode]', className: 'ai' };
    }
    if (method === 'rule') {
        return { text: '[Rule Mode]', className: 'rule' };
    }
    return { text: '[Original/Skipped]', className: 'none' };
}

function pushHistoryVersion(idx, text) {
    if (!state.finalResults[idx]) {
        state.finalResults[idx] = [];
    }
    const history = state.finalResults[idx];
    if (history.length === 0 || history[history.length - 1] !== text) {
        history.push(text);
    }
}

function getCurrentFinalText(idx) {
    const history = state.finalResults[idx] || [];
    return history.length ? history[history.length - 1] : '';
}

function undoSingleSegment(item, index) {
    const idx = String(index);
    const history = state.finalResults[idx] || [];
    if (history.length <= 1) {
        return;
    }

    history.pop();
    const previous = history[history.length - 1] || '';
    const editable = item.querySelector('.change-after');
    editable.innerHTML = renderDiffHtml(item.dataset.original || '', previous);
    state.hasUnsavedEdits = true;
    updateDownloadButtonState();
    showSaveFeedback('已撤销到上一版本，待保存');
}

async function retrySingleSegment(item, detail) {
    const idx = String(detail.index);
    const btn = item.querySelector('.retry-btn');
    const editable = item.querySelector('.change-after');
    const cfg = getAIConfig();

    if (!cfg.api_key) {
        alert('单段重试需要先填写 API Key');
        return;
    }

    btn.disabled = true;
    btn.textContent = '重试中...';

    try {
        const res = await fetch('/api/ai-preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                text: editable.innerText || detail.original,
                risk_level: item.dataset.riskLevel || detail.risk_level || 'medium',
                protected_words: getProtectedWords(),
                ...cfg,
            }),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        editable.innerHTML = renderDiffHtml(item.dataset.original || detail.original, data.transformed);
        pushHistoryVersion(idx, data.transformed);
        state.hasUnsavedEdits = true;
        updateDownloadButtonState();

        const usedFallback = data.fallback === 'rule' || (Array.isArray(data.rules_applied) && data.rules_applied.some(r => r.includes('AI失败后自动切换规则降重')));
        if (usedFallback) {
            const fallbackNotice = $('#fallbackNotice');
            const fallbackNoticeText = $('#fallbackNoticeText');
            if (fallbackNotice && fallbackNoticeText) {
                fallbackNotice.style.display = 'flex';
                fallbackNoticeText.textContent = '已自动切换为规则降重兜底';
            }
            showSaveFeedback('AI失败，已自动转为规则降重');
        }

        item.dataset.method = usedFallback ? 'rule' : 'ai';
        const badge = item.querySelector('.method-badge');
        if (badge) {
            badge.className = `method-badge ${usedFallback ? 'rule' : 'ai'}`;
            badge.textContent = usedFallback ? '[Rule Mode]' : '[AI Mode]';
        }
        const meta = item.querySelector('.change-meta');
        if (meta) {
            const tags = (Array.isArray(data.rules_applied) ? data.rules_applied : [])
                .map(rule => `<span class="strategy-tag">${escapeHtml(rule)}</span>`)
                .join('');
            meta.innerHTML = tags + (data.ai_error ? buildAiErrorTags(data.ai_error) : '');
        }
        showSaveFeedback('单段重试成功，记得保存');
    } catch (err) {
        alert('单段重试失败: ' + err.message);
    } finally {
        btn.disabled = false;
        btn.textContent = '单段重试';
    }
}

function updateDownloadButtonState() {
    const btn = $('#downloadBtn');
    if (!btn) return;
    if (state.hasUnsavedEdits) {
        btn.classList.add('btn-warning');
    } else {
        btn.classList.remove('btn-warning');
    }
}

function buildAiErrorTags(error) {
    if (!error) return '';
    const tags = [];
    if (error.code) {
        tags.push(`<span class="strategy-tag strategy-tag-error">AI错误: ${escapeHtml(String(error.code))}</span>`);
    }
    if (error.status !== undefined && error.status !== null) {
        tags.push(`<span class="strategy-tag strategy-tag-error">状态码: ${escapeHtml(String(error.status))}</span>`);
    }
    if (error.attempts) {
        tags.push(`<span class="strategy-tag strategy-tag-error">重试次数: ${escapeHtml(String(error.attempts))}</span>`);
    }
    if (error.retry_after) {
        tags.push(`<span class="strategy-tag strategy-tag-error">Retry-After: ${escapeHtml(String(error.retry_after))}</span>`);
    }
    if (error.message) {
        tags.push(`<span class="strategy-tag strategy-tag-error">${escapeHtml(String(error.message).slice(0, 120))}</span>`);
    }
    return tags.join('');
}

function tokenizeForDiff(text) {
    return (text || '')
        .split(/([，。；：、“”‘’！？,.!?()（）\s]+)/)
        .filter(token => token !== '');
}

function buildTokenIndexMap(tokens) {
    const indexMap = new Map();
    tokens.forEach((token, idx) => {
        if (!token.trim()) return;
        if (!indexMap.has(token)) {
            indexMap.set(token, []);
        }
        indexMap.get(token).push(idx);
    });
    return indexMap;
}

function findNearbyTokenIndex(indexMap, token, center, usedMap, windowSize = 3) {
    const candidates = indexMap.get(token) || [];
    let bestIdx = -1;
    let bestDistance = Infinity;

    candidates.forEach((candidateIdx) => {
        if (usedMap.has(candidateIdx)) return;
        const distance = Math.abs(candidateIdx - center);
        if (distance <= windowSize && distance < bestDistance) {
            bestIdx = candidateIdx;
            bestDistance = distance;
        }
    });

    return bestIdx;
}

function renderDiffHtml(original, transformed) {
    const origTokens = tokenizeForDiff(original);
    const newTokens = tokenizeForDiff(transformed);
    const usedOrigIndices = new Set();
    const origIndexMap = buildTokenIndexMap(origTokens);

    return newTokens.map((token, idx) => {
        const safe = escapeHtml(token);
        if (!token.trim()) return safe;

        if (origTokens[idx] === token && !usedOrigIndices.has(idx)) {
            usedOrigIndices.add(idx);
            return safe;
        }

        const nearbyIdx = findNearbyTokenIndex(origIndexMap, token, idx, usedOrigIndices, 3);
        if (nearbyIdx >= 0) {
            usedOrigIndices.add(nearbyIdx);
            return safe;
        }

        return `<span class="diff-added">${safe}</span>`;
    }).join('');
}

async function finalizeAndDownload() {
    if (!state.sessionId) return;
    if (!Object.keys(state.finalResults).length) {
        alert('没有可导出的修改结果');
        return;
    }

    const payload = {};
    Object.keys(state.finalResults).forEach((idx) => {
        payload[idx] = getCurrentFinalText(idx);
    });

    showLoading('正在保存所有修改并生成最终文档...');
    try {
        const res = await fetch('/api/finalize-export', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: state.sessionId,
                final_results: payload,
            }),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        state.hasUnsavedEdits = false;
        updateDownloadButtonState();
        showSaveFeedback(`已保存 ${data.applied_count} 段修改`, true);
        window.location.href = data.download_url;
    } catch (err) {
        alert('保存并导出失败: ' + err.message);
    } finally {
        hideLoading();
    }
}

function showSaveFeedback(message, success = false) {
    setStatus('active', message);
    if (success) {
        const btn = $('#downloadBtn');
        if (!btn) return;
        const originalText = '保存所有修改并下载';
        btn.textContent = '✅ 导出成功';
        setTimeout(() => {
            btn.textContent = originalText;
        }, 2000);
    }
}

// ============================================================
// 导出JSON
// ============================================================
async function exportJson() {
    if (!state.sessionId) return;
    try {
        const res = await fetch(`/api/export-json/${state.sessionId}`);
        const data = await res.json();
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'replacements.json';
        a.click();
        URL.revokeObjectURL(url);
    } catch (err) {
        alert('导出失败: ' + err.message);
    }
}

// ============================================================
// AI 深度降重
// ============================================================
function initAISection() {
    const toggle = $('#aiEnabled');
    const config = $('#aiConfig');
    const aiBtn = $('#aiReduceBtn');
    const testBtn = $('#testLlmBtn');
    const toggleKeyBtn = $('#toggleKeyBtn');

    toggle.addEventListener('change', () => {
        state.aiEnabled = toggle.checked;
        config.style.display = toggle.checked ? 'flex' : 'none';
        aiBtn.style.display = toggle.checked ? '' : 'none';
        if (toggle.checked && state.paragraphs.length > 0) {
            aiBtn.disabled = false;
        }
        if (!toggle.checked) {
            aiBtn.disabled = true;
        }
    });

    toggleKeyBtn.addEventListener('click', () => {
        const input = $('#apiKey');
        input.type = input.type === 'password' ? 'text' : 'password';
    });

    testBtn.addEventListener('click', testLLMConnection);

    $$('.preset-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            $('#aiModel').value = btn.dataset.model;
            const urlMap = {
                'deepseek-chat': 'https://api.deepseek.com/v1',
                'qwen-plus': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
            };
            if (urlMap[btn.dataset.model]) {
                $('#apiUrl').value = urlMap[btn.dataset.model];
            }
        });
    });

    loadAIConfig();
}

function getAIConfig() {
    return {
        api_key: $('#apiKey').value.trim(),
        api_url: $('#apiUrl').value.trim() || 'https://api.openai.com/v1',
        model: $('#aiModel').value.trim() || 'gpt-3.5-turbo',
        temperature: parseFloat($('#aiTemperature').value || '0.85') || 0.85,
        custom_prompt: $('#customPrompt').value.trim(),
    };
}

function getProtectedWords() {
    return ($('#protectedWords').value || '')
        .replace(/，/g, ',')
        .split(/[\n,]/)
        .map(item => item.trim())
        .filter(Boolean);
}

function saveAIConfig() {
    const cfg = getAIConfig();
    try {
        localStorage.setItem('aigc_ai_url', cfg.api_url);
        localStorage.setItem('aigc_ai_key', cfg.api_key);
        localStorage.setItem('aigc_ai_model', cfg.model);
        localStorage.setItem('aigc_ai_temperature', String(cfg.temperature));
        localStorage.setItem('aigc_ai_custom_prompt', cfg.custom_prompt || '');
        localStorage.setItem('aigc_protected_words', $('#protectedWords').value || '');
        localStorage.setItem('aigc_hybrid_mode', $('#hybridMode').checked ? '1' : '0');
    } catch {
        setStatus('', '本地配置保存失败');
    }
}

function loadAIConfig() {
    try {
        const url = localStorage.getItem('aigc_ai_url');
        const key = localStorage.getItem('aigc_ai_key');
        const model = localStorage.getItem('aigc_ai_model');
        const temperature = localStorage.getItem('aigc_ai_temperature');
        const customPrompt = localStorage.getItem('aigc_ai_custom_prompt');
        const protectedWords = localStorage.getItem('aigc_protected_words');
        const hybridMode = localStorage.getItem('aigc_hybrid_mode');
        if (url) $('#apiUrl').value = url;
        if (key) $('#apiKey').value = key;
        if (model) $('#aiModel').value = model;
        if (temperature) $('#aiTemperature').value = temperature;
        if (customPrompt) $('#customPrompt').value = customPrompt;
        if (protectedWords) $('#protectedWords').value = protectedWords;
        $('#hybridMode').checked = hybridMode === '1';
    } catch {
        setStatus('', '本地配置加载失败');
    }
}

async function testLLMConnection() {
    const cfg = getAIConfig();
    if (!cfg.api_key) {
        alert('请填写 API Key');
        return;
    }

    const testResult = $('#testResult');
    const statusEl = $('#aiStatus');
    testResult.style.display = 'block';
    testResult.className = 'test-result';
    testResult.textContent = '测试中...';

    try {
        const res = await fetch('/api/test_llm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(cfg),
        });
        const data = await res.json();

        if (data.ok) {
            testResult.className = 'test-result ok';
            const modelList = data.models.length > 0
                ? `\n可用模型: ${data.models.slice(0, 8).join(', ')}${data.models.length > 8 ? '...' : ''}`
                : '';
            testResult.textContent = `连接成功!${modelList}`;
            statusEl.textContent = '已连接';
            statusEl.className = 'ai-status ok';
            saveAIConfig();
        } else {
            const detail = data.error ? ` [${data.error.code || 'unknown'}]` : '';
            testResult.className = 'test-result fail';
            testResult.textContent = `连接失败${detail}: ${data.message}`;
            statusEl.textContent = '未连接';
            statusEl.className = 'ai-status fail';
        }
    } catch (err) {
        testResult.className = 'test-result fail';
        testResult.textContent = `请求失败: ${err.message}`;
        statusEl.textContent = '未连接';
        statusEl.className = 'ai-status fail';
    }
}

async function runAIReduce() {
    if (!state.sessionId || state.paragraphs.length === 0) return;

    const cfg = getAIConfig();
    if (!cfg.api_key) {
        alert('请先填写 API Key');
        return;
    }

    const selected = getSelectedIndices();
    if (selected.length === 0) {
        alert('请至少选择一个段落');
        return;
    }

    const protectedWords = getProtectedWords();
    saveAIConfig();
    setStatus('working', 'AI降重中...');
    console.log('[AIGC Reducer] runAIReduce live mode start', { selected: selected.length, build: window.__AIGC_FRONTEND_BUILD__ });
    startProgress(selected.length, 'AI 降重进行中');
    startLiveProgress(selected.length, '实时 AI 降重监控');
    prepareLiveReduceView();
    pollProgress(state.sessionId);

    try {
        const tasks = selected.map(async (index, order) => {
            const para = state.paragraphMap.get(index);
            if (!para) {
                updateLiveProgress(order + 1, selected.length);
                return null;
            }

            try {
                const previewRes = await fetch('/api/ai-preview', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        text: para.full_text || para.text,
                        risk_level: para.risk_level,
                        protected_words: protectedWords,
                        ...cfg,
                    }),
                });
                const previewData = await previewRes.json();
                appendParagraphToView({
                    index,
                    original: para.full_text || para.text,
                    transformed: previewData.transformed || para.full_text || para.text,
                    rules: previewData.rules_applied || [],
                    method: previewData.fallback === 'rule' ? 'rule' : 'ai',
                    risk_level: para.risk_level,
                    is_low_confidence: Boolean(para.report_low_confidence),
                    fallback: previewData.fallback === 'rule',
                    ai_error: previewData.ai_error || null,
                });
            } catch {
                appendParagraphToView({
                    index,
                    original: para.full_text || para.text,
                    transformed: para.full_text || para.text,
                    rules: ['实时 AI 预览失败，等待最终结果'],
                    method: 'ai',
                    risk_level: para.risk_level,
                    is_low_confidence: Boolean(para.report_low_confidence),
                    fallback: false,
                    ai_error: {
                        code: 'preview_failed',
                        message: '实时渲染阶段调用 /api/ai-preview 失败',
                    },
                });
            } finally {
                updateLiveProgress(order + 1, selected.length);
            }
            return null;
        });

        const finalPromise = fetch('/api/ai-reduce', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: state.sessionId,
                selected_indices: selected,
                protected_words: protectedWords,
                ...cfg,
            }),
        }).then(res => res.json());

        await Promise.all(tasks);
        const data = await finalPromise;
        if (data.error) throw new Error(data.error);

        finishProgress(selected.length, selected.length, 'AI 降重已完成');

        let info = `AI改写 ${data.modified_count} 个段落`;
        if (data.summary) {
            info += `，AI成功 ${data.summary.ai_success_count || 0} 段`;
            if ((data.summary.fallback_count || 0) > 0) {
                info += `，规则兜底 ${data.summary.fallback_count} 段`;
            }
            if ((data.summary.failed_count || 0) > 0) {
                info += `，彻底失败 ${data.summary.failed_count} 段`;
            }
        } else if (data.error_count > 0) {
            info += `，${data.error_count} 个失败`;
        }
        renderReduceResult(data);

        $('#resultsArea').style.display = 'none';
        $('#reduceResult').style.display = 'block';

        setStatus('active', info);
    } catch (err) {
        alert('AI降重失败: ' + err.message);
        setStatus('', 'AI降重失败');
    } finally {
        stopProgress();
        stopLiveProgress();
        hideLoading();
    }
}

// ============================================================
// 重新上传 & 继续降重
// ============================================================
function initReuploadButtons() {
    $('#reuploadDocBtn').addEventListener('click', (e) => {
        e.stopPropagation();
        resetDoc();
    });
    $('#reuploadReportBtn').addEventListener('click', (e) => {
        e.stopPropagation();
        resetReport();
    });
}

function resetDoc() {
    if (state.sessionId) {
        fetch('/api/reset', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: state.sessionId }),
        });
    }
    state.sessionId = null;
    state.paragraphs = [];
    state.selectedIndices.clear();
    state.round = 1;
    state.finalResults = {};
    state.hasUnsavedEdits = false;

    $('#docDropZone').style.display = '';
    $('#docInfo').style.display = 'none';
    $('#docInput').value = '';
    $('#roundBadge').style.display = 'none';

    resetReport();

    $('#statsBar').style.display = 'none';
    $('#emptyState').style.display = '';
    $('#resultsArea').style.display = 'none';
    $('#reduceResult').style.display = 'none';
    $('#analyzeBtn').disabled = true;
    $('#reduceBtn').disabled = true;
    $('#aiReduceBtn').disabled = true;
    setStatus('', '等待上传');
}

function resetReport() {
    $('#reportDropZone').style.display = '';
    $('#reportInfo').style.display = 'none';
    $('#reportStats').style.display = 'none';
    $('#reportInput').value = '';
}

async function continueReduce() {
    if (!state.sessionId) return;

    setStatus('working', '准备下一轮...');
    showLoading('加载降重后文档...');

    try {
        const res = await fetch('/api/continue-reduce', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: state.sessionId }),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        state.round = data.round;
        state.paragraphs = [];
        state.selectedIndices.clear();

        $('#roundBadge').style.display = 'flex';
        $('#roundText').textContent = `第 ${state.round} 轮`;
        $('#docName').textContent = `降重版 (第${state.round - 1}轮输出)`;

        $('#statParas').textContent = data.stats.content_paragraphs;
        $('#statWords').textContent = data.stats.total_words.toLocaleString();
        $('#statHigh').textContent = '0';
        $('#statMed').textContent = '0';
        $('#statLow').textContent = '0';
        $('#statsBar').style.display = 'flex';

        resetReport();

        $('#reduceResult').style.display = 'none';
        $('#resultsArea').style.display = 'none';
        $('#emptyState').style.display = 'none';
        $('#analyzeBtn').disabled = false;
        $('#reduceBtn').disabled = true;
        $('#aiReduceBtn').disabled = true;

        setStatus('active', `第 ${state.round} 轮 - 请重新分析`);
    } catch (err) {
        alert('继续降重失败: ' + err.message);
        setStatus('', '操作失败');
    } finally {
        hideLoading();
    }
}

// ============================================================
// 工具函数
// ============================================================
function setStatus(type, text) {
    const dot = $('#statusDot');
    dot.className = 'status-dot';
    if (type) dot.classList.add(type);
    $('#statusText').textContent = text;
}

function showLoading(text) {
    $('#loadingText').textContent = text || '处理中...';
    $('#loading').style.display = 'flex';
}

function hideLoading() {
    $('#loading').style.display = 'none';
}

function startProgress(total, label = '处理中...') {
    state.progress.current = 0;
    state.progress.total = total;
    if (state.progress.timer) {
        clearInterval(state.progress.timer);
        state.progress.timer = null;
    }
    $('#progressPanel').style.display = 'block';
    $('#progressLabel').textContent = label;
    updateProgress(0, total);
}

function updateProgress(current, total = state.progress.total) {
    state.progress.current = current;
    state.progress.total = total;
    const safeTotal = Math.max(total, 1);
    const percent = Math.max(0, Math.min(100, (current / safeTotal) * 100));
    $('#progressText').textContent = `${current} / ${total}`;
    $('#progressFill').style.width = `${percent}%`;
}

function finishProgress(current, total, label = '已完成') {
    $('#progressLabel').textContent = label;
    updateProgress(current, total);
}

function stopProgress() {
    if (state.progress.timer) {
        clearInterval(state.progress.timer);
        state.progress.timer = null;
    }
    setTimeout(() => {
        $('#progressPanel').style.display = 'none';
        $('#progressFill').style.width = '0%';
        $('#progressText').textContent = '0 / 0';
    }, 300);
}

function startLiveProgress(total, title = '实时降重监控') {
    state.liveReduce.current = 0;
    state.liveReduce.total = total;
    state.liveReduce.done = false;
    $('#liveProgressFloat').style.display = 'block';
    $('#liveProgressTitle').textContent = title;
    updateLiveProgress(0, total);
}

function updateLiveProgress(current, total = state.liveReduce.total) {
    state.liveReduce.current = current;
    state.liveReduce.total = total;
    const safeTotal = Math.max(total, 1);
    const percent = Math.max(0, Math.min(100, (current / safeTotal) * 100));
    $('#liveProgressValue').textContent = `${current} / ${total}`;
    $('#liveProgressFill').style.width = `${percent}%`;
}

function stopLiveProgress() {
    state.liveReduce.done = true;
    setTimeout(() => {
        $('#liveProgressFloat').style.display = 'none';
        $('#liveProgressFill').style.width = '0%';
        $('#liveProgressValue').textContent = '0 / 0';
    }, 400);
}

function pollProgress(sessionId) {
    if (!sessionId) return;
    if (state.progress.timer) {
        clearInterval(state.progress.timer);
    }

    state.progressWarningShown = false;
    state.progress.timer = setInterval(async () => {
        try {
            const res = await fetch(`/api/progress/${sessionId}`);
            const data = await res.json();
            if (data.error) return;
            $('#progressLabel').textContent = data.label || '处理中...';
            updateProgress(data.current || 0, data.total || 0);
            if (data.done) {
                clearInterval(state.progress.timer);
                state.progress.timer = null;
            }
        } catch {
            if (!state.progressWarningShown) {
                setStatus('', '进度轮询暂时异常');
                state.progressWarningShown = true;
            }
        }
    }, 350);
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
