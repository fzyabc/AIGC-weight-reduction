# AIGC Reducer - 技术架构文档

## 系统总览

```
┌─────────────────────────────────────────────────────────────────┐
│                        用户浏览器                                │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              前端 (index.html + app.js + style.css)       │   │
│  │                                                          │   │
│  │  ┌─────────┐  ┌─────────┐  ┌──────────┐  ┌──────────┐  │   │
│  │  │文件上传  │  │风险分析  │  │降重预览   │  │结果下载   │  │   │
│  │  │(拖拽/选择)│  │(段落列表) │  │(原文→改写)│  │(docx导出) │  │   │
│  │  └────┬────┘  └────┬────┘  └────┬─────┘  └────┬─────┘  │   │
│  │       │            │            │              │         │   │
│  │  ┌────┴────────────┴────────────┴──────────────┴────┐   │   │
│  │  │            AI 配置面板 (可选)                       │   │   │
│  │  │   API URL / API Key / 模型选择 / 测试连接          │   │   │
│  │  └────────────────────┬───────────────────────────┘   │   │
│  └───────────────────────┼──────────────────────────────┘   │
│                          │ HTTP API (JSON)                    │
└──────────────────────────┼──────────────────────────────────┘
                           │
┌──────────────────────────┼──────────────────────────────────┐
│                     Flask 后端 (app.py)                       │
│                          │                                    │
│  ┌───────────────────────┴───────────────────────────────┐   │
│  │                    API 路由层                           │   │
│  │                                                        │   │
│  │  POST /api/upload          上传论文 docx               │   │
│  │  POST /api/upload-report   上传检测报告 PDF            │   │
│  │  POST /api/analyze         分析 AI 写作特征            │   │
│  │  POST /api/preview         预览单段降重效果            │   │
│  │  POST /api/reduce          规则引擎批量降重            │   │
│  │  POST /api/ai-preview      AI 预览单段                │   │
│  │  POST /api/ai-reduce       AI 批量降重                │   │
│  │  POST /api/test-llm        测试 LLM 连接              │   │
│  │  POST /api/continue-reduce 多轮迭代降重               │   │
│  │  POST /api/reset           重置会话                    │   │
│  │  GET  /api/download/:sid   下载降重文档                │   │
│  │  GET  /api/export-json/:sid 导出替换映射               │   │
│  └──────────┬──────────────────────────┬─────────────────┘   │
│             │                          │                      │
│  ┌──────────┴──────────┐   ┌──────────┴──────────────────┐   │
│  │  会话管理 (sessions) │   │  文件管理 (uploads/outputs)  │   │
│  │  dict[sid] → state   │   │  上传暂存 / 降重结果输出     │   │
│  └─────────────────────┘   └─────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
          │                    │                    │
          ▼                    ▼                    ▼
┌─────────────────┐ ┌─────────────────┐ ┌──────────────────────┐
│  doc_handler.py │ │ report_parser.py│ │   transformer.py     │
│  文档处理器      │ │  报告解析器     │ │    降重引擎（核心）    │
│                 │ │                 │ │                      │
│ · read_docx()   │ │ · parse_pdf()   │ │ ┌──────────────────┐ │
│ · replace_para()│ │ · match_paras() │ │ │  Transformer     │ │
│ · save_docx()   │ │ · parse_text()  │ │ │  (规则引擎)       │ │
│ · analyze_doc() │ │ · _similarity() │ │ │                  │ │
│                 │ │                 │ │ │ · 序列词替换      │ │
│ 依赖:           │ │ 依赖:           │ │ │ · 连接词替换      │ │
│  python-docx    │ │  PyPDF2         │ │ │ · 打破对称        │ │
└─────────────────┘ └─────────────────┘ │ │ · 注入人味        │ │
                                        │ │ · 句式重构        │ │
                                        │ └──────────────────┘ │
                                        │                      │
                                        │ ┌──────────────────┐ │
                                        │ │  AITransformer   │ │
                                        │ │  (LLM 策略层)    │ │
                                        │ │                  │ │
                                        │ │ · system prompt  │ │
                                        │ │ · _call_api()    │ │
                                        │ │ · test_connect() │ │
                                        │ └────────┬─────────┘ │
                                        │          │           │
                                        └──────────┼───────────┘
                                                   │
                                                   ▼
                                        ┌─────────────────────┐
                                        │  外部 LLM API       │
                                        │                     │
                                        │  · OpenAI           │
                                        │  · DeepSeek         │
                                        │  · 通义千问          │
                                        │  · 任何兼容接口      │
                                        └─────────────────────┘
```

## 文件结构

```
d:\aigc-reducer\
│
├── app.py                 Flask 后端主程序，所有 API 路由
├── reducer.py             CLI 命令行入口（独立可用，不依赖 Flask）
├── transformer.py         核心降重引擎
│                           ├── Transformer       规则引擎（免费/离线）
│                           ├── AITransformer     LLM 策略层（需 API）
│                           └── analyze_ai_patterns()  AI 特征分析
├── doc_handler.py         docx 文档读写，保留原格式
├── report_parser.py       AIGC 检测报告 PDF 解析
├── requirements.txt       Python 依赖
├── README.md              使用说明
├── ARCHITECTURE.md        本文件
│
├── templates/
│   └── index.html         前端页面（Jinja2 模板）
│
├── static/
│   ├── style.css          深色主题 UI 样式
│   └── app.js             前端交互逻辑
│
├── uploads/               用户上传的文件（运行时生成）
└── outputs/               降重后的文档（运行时生成）
```

## 数据流

### 规则降重流程

```
用户上传 .docx
       │
       ▼
  doc_handler.read_docx()        → 解析段落列表
       │
       ▼
  transformer.analyze_ai_patterns()  → 逐段计算风险分
       │                               (序列词×15 + 对称结构×20
       │                                + 通用连接词×10 + 长句×10
       │                                - 人味标记×15)
       ▼
  用户选择段落 + 降重力度
       │
       ▼
  transformer.Transformer.batch_transform()
       │
       │  Level 1: 序列词替换 + 学术连接词替换
       │  Level 2: + 打破句式对称 + 注入主观标记
       │  Level 3: + 拆长句 + 主被动转换
       │
       ▼
  doc_handler.replace_paragraph_text()  → 保留格式写回
       │
       ▼
  doc_handler.save_docx()  → 输出降重版 .docx
```

### AI 降重流程

```
用户配置 API URL + Key + 模型
       │
       ▼
  AITransformer.test_connection()     → 验证连接
       │
       ▼
  用户选择段落 → AI 降重
       │
       ▼
  AITransformer.batch_transform()
       │
       │  对每段调用 LLM API:
       │    system: 降重专家 prompt (7条改写策略)
       │    user:   风险等级提示 + 原文
       │
       │  调用方式: urllib → POST /chat/completions
       │  超时: 60s / 段
       │  失败: fallback 跳过该段，不影响整体
       │
       ▼
  写回文档 → 输出
```

### 多轮迭代

```
  第 1 轮: 原文 → 分析 → 降重 → v1.docx
                                    │
  第 2 轮: v1.docx → 分析 → 降重 → v2.docx   (continue-reduce)
                                    │
  第 N 轮: ...直到满意
```

## 核心模块说明

### transformer.py — 降重引擎

| 类/函数 | 职责 |
|---------|------|
| `Transformer` | 规则引擎，基于预设替换表做结构性调整 |
| `AITransformer` | LLM 策略层，调用外部 API 做语义级改写 |
| `analyze_ai_patterns()` | 分析单段文本的 AI 特征，返回风险分 |
| `SEQUENCE_CONNECTORS` | 序列词替换表（首先→从根本上说 等） |
| `ACADEMIC_CONNECTORS` | 学术连接词替换表（因此→由此观之 等） |
| `HUMAN_MARKERS` | 人味标记库（笔者认为、坦白讲 等） |

**风险评分公式：**
```
risk_score = 序列词数×15 + 对称结构数×20 + 通用连接词×10 + 长句数×10 - 人味标记数×15
范围: 0 ~ 100
≥60: 高风险    ≥40: 中风险    ≥25: 低风险
```

### app.py — Flask 后端

- 会话管理：`sessions` 字典，key 为 8 位 UUID 前缀
- 文件管理：上传到 `uploads/`，输出到 `outputs/`
- 无数据库，纯内存状态（重启后会话丢失，文件保留）

### doc_handler.py — 文档处理

- 使用 `python-docx` 读写
- `replace_paragraph_text()`: 清空所有 run，在第一个 run 写入新文本 → 保留原格式
- 识别标题（Heading 样式）自动跳过

### report_parser.py — 报告解析

- 使用 `PyPDF2` 提取 PDF 文本
- 正则匹配 AIGC 概率、风险字数
- `match_paragraphs()`: 基于最长公共子序列将报告段落匹配到文档段落

## 前端架构

```
index.html
    │
    ├── 左侧面板 (sidebar)
    │   ├── Step 1: 上传论文 (拖拽区 + 重新上传)
    │   ├── Step 2: 检测报告 (可选, 拖拽区)
    │   ├── Step 3: 降重设置 (力度 L1/L2/L3)
    │   ├── Step AI: AI 配置 (URL/Key/模型/测试)
    │   └── 操作按钮 (分析 / 规则降重 / AI降重)
    │
    └── 右侧内容区 (content)
        ├── 统计栏 (段落数/字数/高中低风险)
        ├── 空状态提示
        ├── 分析结果列表 (可展开预览/勾选)
        ├── 降重结果 (对比 + 下载/继续/重新上传)
        └── 加载遮罩

app.js
    │
    ├── state          全局状态 (sessionId, level, paragraphs, aiEnabled...)
    ├── 上传逻辑        uploadDoc / uploadReport (FormData + fetch)
    ├── 分析/降重       runAnalyze / runReduce / runAIReduce
    ├── AI 配置         initAISection / testLLMConnection / getAIConfig
    ├── 段落渲染        renderParagraphs / toggleExpand / toggleSelect
    ├── 结果渲染        renderReduceResult (对比列表 + 下载按钮)
    ├── 重新上传        resetDoc / resetReport / continueReduce
    └── 工具函数        setStatus / showLoading / escapeHtml

style.css
    │
    ├── CSS 变量       --bg, --primary, --green, --red 等
    ├── 布局           header + sidebar + content (flex)
    ├── 组件           card, upload-zone, btn, para-item, stat-card...
    ├── AI 面板        ai-config, preset-btn, test-result...
    └── 响应式         @media (max-width: 900px)
```

## 技术栈

| 层 | 技术 | 说明 |
|---|------|------|
| 前端 | 原生 HTML/CSS/JS | 无框架依赖，深色主题 |
| 后端 | Flask 3.x | 轻量 Python Web 框架 |
| 文档 | python-docx | 读写 Word .docx |
| PDF | PyPDF2 | 解析检测报告 |
| AI | OpenAI 兼容 API | urllib 直接调用，无 SDK 依赖 |
| CLI | argparse | 命令行独立可用 |

## 扩展指南

### 新增降重规则
在 `transformer.py` 的 `SEQUENCE_CONNECTORS` 或 `ACADEMIC_CONNECTORS` 字典中添加新的替换映射。

### 接入新的 LLM 平台
只要平台提供 OpenAI 兼容的 `/v1/chat/completions` 接口，填入 URL 和 Key 即可。无需改代码。

### 优化 AI Prompt
修改 `AITransformer.SYSTEM_PROMPT`，调整改写策略。

### 添加持久化
当前用内存 dict 存会话，如需持久化可替换为 SQLite 或 Redis。

### 前端增加新功能
`app.js` 中 `state` 对象管理全局状态，新增 API 后在 JS 中加对应的 fetch 调用即可。
