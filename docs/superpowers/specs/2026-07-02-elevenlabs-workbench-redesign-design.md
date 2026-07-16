# ElevenLabs 风格智能会议工作台重构设计

## 背景

当前前端是无构建步骤的静态原型，核心文件为 `frontend/index.html`、`frontend/styles.css`、`frontend/app.js` 和 `frontend/prototype_spec_test.mjs`。业务入口已经覆盖会议列表、导入转写、声纹库、识别优化、禁忌词、纪要模板和系统对接。本次重构采用已确认的 B 方案：视觉重构 + 轻量布局重构，不引入新框架，不大拆业务 JS。

参考设计来自 ElevenLabs DESIGN.md：米白画布、近黑文字和 pill CTA、轻量 serif 展示标题、Inter/系统无衬线正文、白色卡片、细发丝线、柔和 pastel 光晕、音频波形组件感。本项目需要转译为高频办公工作台，而不是营销落地页。

## 目标

1. 让前端整体从蓝色政企后台感转为 ElevenLabs 式浅色编辑工作台。
2. 保留现有业务入口、DOM 标记、关键 `id`、`data-route` 和 JS 行为，避免破坏原型功能。
3. 强化三个核心区域：会议列表工作台、导入转写台账、转写详情三栏编辑区。
4. 保持桌面和窄屏下可读、可操作，不出现文字溢出或控件重叠。

## 非目标

1. 不引入 React、Vue、打包器或图标库。
2. 不重写 `app.js` 的数据流和 API 逻辑。
3. 不把页面改成营销首页，不隐藏会议系统的高频操作入口。
4. 不修改后端接口。

## 视觉系统

### 色彩

- 页面画布：`#f5f5f3`，替换当前冷灰蓝背景。
- 主文字和主按钮：`#0c0a09` / `#292524`，替换当前高饱和蓝色主色。
- 正文：`#57534e`，弱化说明文字。
- 边框：`#e7e2dc` 和 `#d8d0c8`，作为发丝线层级。
- 卡片：`#ffffff` 和 `#faf9f7`，形成轻微纸张层次。
- 氛围光晕：mint、peach、lavender、sky、rose，仅用于 hero、空状态和背景装饰，不用于按钮和文字。
- 语义色保留：错误红、成功绿，用在状态徽章和危险操作上。

### 字体

- 大标题使用系统 serif 兜底：`"Times New Roman", "Noto Serif SC", serif`，权重 300-400，模拟 ElevenLabs 的轻量编辑感。
- 正文、按钮和表格使用系统无衬线：`Inter`, `"PingFang SC"`, `"Microsoft YaHei"`, Arial, sans-serif。
- 不使用负字距，避免中文显示不稳。

### 组件

- 主按钮统一为近黑 pill，40-44px 高。
- 次按钮为透明或白底 pill，发丝线描边。
- 表格放入白色圆角面板，行分隔用发丝线，减少蓝色块面。
- 状态徽章使用浅暖灰 pill，语义状态只轻微着色。
- 详情页底部音频条使用 waveform 视觉，保留现有播放按钮和时间显示。

## 页面设计

### 会议列表

在 `page-records` 内保留 `meetingListView`、筛选栏、`recordsOverview` 和 `recordsTableBody`。在页面头部加入更强的导视感：标题文案更精炼，主按钮“快速会议”为近黑 pill，批量导出为 outline pill。`recordsOverview` 从硬指标条改为 5 个柔和指标卡。表格面板采用白色卡片和细分隔线。

### 导入转写

保留 `importLedgerView` 和 `importDetailView` 双视图。导入台账顶部改为浅色工具区：上传入口、搜索、语言、模板、声纹区分、热词选择在视觉上合并成一个工作区。`importResultPanel` 使用结果卡片，强调处理完成后留在导入页展示。

### 转写详情

保留 `speakerPanel`、`transcriptEditor`、`right-tool-dock` 和 `bottomAudioPlayer`。三栏比例调整为发言人较窄、中间编辑器最大、AI 工具适中。工具 tabs 改为竖向/紧凑 pill，结果面板使用浅色卡片。底部音频条固定在编辑器底部视觉区域，波形线条更轻。

### 词库、声纹、模板、对接

这些页面不做业务结构重排，只统一视觉系统：背景、卡片、表格、tabs、输入框、弹窗和按钮。这样整体一致，同时降低破坏风险。

## 实现边界

1. `frontend/styles.css` 是主要修改文件：引入 CSS 变量，重写全局、导航、标题、按钮、面板、表格、详情三栏、响应式样式。
2. `frontend/index.html` 做少量结构和文案调整：增加 `workspace-hero`、`surface-panel` 等样式钩子，保留测试依赖的标记。
3. `frontend/prototype_spec_test.mjs` 增加设计系统标记检查：确保 ElevenLabs 方向的 tokens、hero、三栏详情和注释约束存在。
4. `frontend/app.js` 原则上不动；如果必须改，只限文案或 class 输出，并添加详细注释。

## 测试与验证

1. 先扩展 `prototype_spec_test.mjs`，让它检查新设计钩子；运行后应失败。
2. 修改 HTML/CSS 后再次运行 `node frontend\prototype_spec_test.mjs`，应输出 `prototype spec ok`。
3. 使用 `scripts\start_frontend.ps1` 或 Python 静态服务打开 `http://127.0.0.1:5173`。
4. 检查桌面宽屏与窄屏：左侧导航、会议列表、导入台账、详情三栏、工具区和音频条不重叠。

## 风险

- 当前部分文件在 PowerShell 输出中显示乱码，修改时必须保持 UTF-8 并避免无意义全文件重编码。
- 现有 CSS 较大且类名复用多，重写按钮和面板时要避免影响隐藏弹窗、菜单和详情工具。
- Git 元数据当前无法被 `git status` 正常识别，可能无法按流程提交设计文档。
