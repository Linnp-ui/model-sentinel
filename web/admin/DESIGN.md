# 网关管理控制台 — 设计系统

> 适用：`web/admin`（React 18 + antd 5.21 + uPlot，Vite，zh_CN）。
> 读者：网关 SRE/管理员（2-3 人内网使用）。桌面优先，最低 1280px。

## 1. 设计概览 (Design Overview)

**产品**：公司内网大模型请求统一出口网关的管理控制台 —— API KEY 登记、
内容策略（L1 规则 / KEY 黑白名单）、路由与模型配置、审计日志与运行观测。

**风格定调**：扁平 + 数据密集仪表盘（Flat Design × Data-Dense Dashboard）。
antd 5 默认视觉即扁平企业风；不引入任何装饰性风格（玻璃拟态 / 新拟态 /
动效驱动均被排除于 data-heavy dashboard 之外）。基调 = 把 antd 默认收敛成
一套写死的 token，补齐状态与可达性短板，不改布局骨架。

**设计原则**：
1. 状态先于操作 —— 任何页面第一眼看到"现在系统什么状态"（StatusBar 全局常驻）。
2. 诊断闭环 —— 每个配置动作后给验证结果（健康检查 / 候选验证）。
3. 危险可见 —— 删除 / 熔断打开 / 密钥操作必须确认 + 明确后果文案。
4. 密度换屏效 —— 内网运维不追求留白美学，12-14px 数据字号、8-12px 间距。

## 2. 色彩 (Color Palette)

全部取 antd 5 内置语义色，不自造。

| Token | 值 | 用途 |
|---|---|---|
| colorPrimary | `#2f54eb` | 主按钮、链接、选中菜单（antd geekblue，视觉提色） |
| colorSuccess | `#52C41A` | 正常/允许/已配/CLOSED |
| colorWarning | `#FAAD14` | 警告/half_open/灰区 |
| colorError | `#FF4D4F` | 异常/block/OPEN/缺key |
| colorInfo | `#2f54eb` | 内网/提示类 Tag |
| 页面底 | `#F5F6F8` | Content 背景（colorBgLayout，视觉提色） |
| 卡片底 | `#FFFFFF` | Card 默认 |
| 表头底 | `#FAFAFA` | Table header |
| 正文/次要 | antd 默认 88%/45% 黑 | 次要文字 ≥12px 时对比度 4.5:1（AA 达标） |

**状态灯规范**（Tag，全局统一，颜色+文字双通道，禁纯颜色表意）：

| 语义 | Tag | 文案示例 |
|---|---|---|
| 正常 | `green` | 熔断正常 / key已配 / CLOSED |
| 警告 | `orange` | HALF_OPEN / 灰区 |
| 异常 | `red` | 缺key / OPEN / 审计异常 |
| 信息 | `blue` | 内网 oneapi |
| 未知/禁用 | 默认灰 | 停用 / - |

## 3. 字体 (Typography)

**字体栈**：系统字体栈，不引 web font（内网零下载）：
`-apple-system, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif`

**字号阶梯**（modular）：

| 用途 | px |
|---|---|
| 表头 hint / 次要说明 / StatusBar 标签 | 12 |
| 表格正文（数据密集） | 13 |
| 正文 / 表单 label | 14 |
| 卡片标题 | 16 |
| 页面标题（Header） | 18 |
| KPI 数字（总览页） | 24 |

**等宽**（KEY / base_url / 模型名 / 代码）：`"SFMono-Regular", Consolas, monospace`，12-13px。

## 4. 形状、间距与动效 (Shape, Spacing & Motion)

**圆角**：全局 `borderRadius: 8`（ConfigProvider 已落地），不覆盖组件级圆角。

**间距**：基数 4px：`4 / 8 / 12 / 16 / 24`。Content padding 24；卡片间距 16
（`Space size="middle"`）；表格单元格 8px（small 默认）。

**动效**：antd 默认 ~200ms，只用语义化场景（展开 / 弹窗 / toast）；
唯一新增强制动画 = 状态灯异常红 Tag `pulse 2s infinite`，且必须
`@media (prefers-reduced-motion: reduce)` 关闭。z-index 只走 antd 内置层
（dropdown/modal/message），禁止手写 z-index。

## 5. 布局与导航 (Layout & Navigation)

```
┌──────────┬────────────────────────────────────────────┐
│ Sider    │ Header（sticky，18px 页面标题）              │
│ 200px    ├────────────────────────────────────────────┤
│ 常驻     │ StatusBar（sticky 于 Header 下，60s 轮询）   │
│ sticky   ├────────────────────────────────────────────┤
│          │ Content（padding 24，单页纵向卡片流）        │
└──────────┴────────────────────────────────────────────┘
```

- **Sider（200px，常驻 sticky）**：5 组（接入 / 安全策略 / 路由与模型 / 观测 / 系统）。
  - `rules`「规则管理」与 `keyrules`「KEY 黑白名单」同归「安全策略」组
    （内容策略 + 身份策略，均与路由/模型无关）。
  - 文案统一：菜单「路由检查」= Header「路由检查（诊断）」→ 统一为「路由检查」。
- **Header + StatusBar 均 `position: sticky; top: 0`**：滚动长表格（审计）时
  全局状态灯与当前页名始终可见（实时监控模式核心要求）。
- **StatusBar**（6 灯：外网key / 内网 / L2 / 熔断 / 策略 / 审计DB）：
  1. 刷新按钮旁显示「更新于 HH:MM:SS」（轮询成功才更新）。
  2. 任一红灯时对应 Tag 加 pulse 动画（§4）。
  3. 全部正常时不占视觉权重（灰/绿小 Tag）。
- **URL 状态**：当前页写入 `location.hash`（`#/audit`），初始化时读取并校验；
  收益：刷新不丢页、可书签、浏览器前进后退可用。筛选状态入 URL 只给审计页，
  其他页筛选即点即走。
- 不做面包屑（单层导航，无深嵌套）。

## 6. 组件规则 (Components)

### 6.1 表格（全应用主要信息载体）

| 规则 | 说明 |
|---|---|
| 加载态 | `Table loading`（有 async 数据的表必须有；健康检查/候选表等） |
| 空态 | 必须带下一步动作：如「暂无登记 KEY，点右上角『新增 KEY』」「未检查，点『全部检查』」「该时间范围无审计记录，可扩大范围或清空筛选」 |
| 长字段 | `ellipsis: true` + Tooltip 全文（KEY/base_url/text_preview） |
| 列宽 | 短枚举列（状态/Rank/启用）固定 60-90px；长文本列不设宽；操作列固定 |
| 行操作 | 文本按钮（link）；危险操作 `danger` + Popconfirm，确认文案含对象名 |
| 行内即时开关 | Switch 小尺寸（外网候选/启用列已用），onChange 失败要 message.error 回滚 |
| >50 行 | 分页或虚拟滚动（审计页走服务端分页） |

### 6.2 表单 / 弹窗

- label 常显（antd Form 默认），禁用 placeholder 当唯一说明。
- 错误 inline 到字段下（antd 默认）；提交按钮 `confirmLoading`（spinner 保持
  可点语义，禁 disabled 灰死）。
- 密码/密钥输入：`Input.Password`（可切换可见）；**密钥值永不回显**（产品铁律）
  ——placeholder 写清「留空保存 = 清除」。
- 弹窗：width 480-560，`destroyOnClose`。
- 保存后闭环：provider key 保存→自动健康检查、候选保存→route-inspect 干跑
  （凡是改了路由相关配置，都给一次验证反馈）。

### 6.3 反馈

- 成功/失败：`message.success/error`（antd 自动消失 ≈3s，符合 3-5s 规范）。
- 耗时操作（L2 干跑 ~230ms、健康检查、全量检查）：按钮/区域 loading 态，
  禁止静默等待。
- 诊断类结果（路由检查）：结论用醒目组件而非纯文字 ——
  `final_action`：allow=绿 / route_local=橙 / block=红 `Alert` 横幅置顶，
  其下才是 L1/L2/routing 明细；「去向」一行用等宽字体突出。

### 6.4 可达性基线（WCAG AA，最小集）

- 对比度：正文 4.5:1（antd 默认 88% 黑达标；12px 次要 45% 黑 ≈4.6:1 达标）。
- 颜色不作唯一信息通道：所有 Tag 必带文字。
- `:focus-visible`：保留 antd 默认焦点环，**禁止** `outline: none` 无替代。
- 纯图标按钮补 `aria-label`（StatusBar 刷新、表格行内图标按钮）。
- 键盘：Tab 序 = DOM 序（antd 组件默认合规），弹窗焦点圈定（antd 默认）。
- 新增强制动画（pulse）必须响应 `prefers-reduced-motion`。

## 7. 页面清单 (Screens)

| 页面 | 设计要点 |
|---|---|
| 总览 Overview | KPI 去重：每页只挑一个"一眼看状态"的信号进总览，同一指标不出现两次；熔断/外网 key 由全局 StatusBar 灯覆盖，不重复做卡 |
| API KEY 管理 | 空态引导「新增 KEY」；表格 loading |
| KEY 黑白名单 | 空态引导；删除确认含 key 名 |
| 模型配置 | 外部候选表 loading/空态；fallback 开关区保留在候选卡顶部 |
| Provider 管理 | 健康检查结果空态「未检查」；逐行检查 loading 互斥提示 |
| 路由检查 | 结论 Alert 置顶（allow/route_local/block 三色）；检查中 loading；去向等宽 |
| 规则管理 | 「安全策略」组菜单入口；其余按表格规则 |
| 统计可视化 | 无数据态「该范围无请求」；时间范围控件明确当前值 |
| 审计日志 | 空态带搜索建议；筛选态入 URL；长文本 ellipsis+Tooltip |
| 运行指标 | 图表 loading 骨架（占位固定高度防布局跳）；无数据态 |
| 管理员密码 | 内联错误；保存成功 toast |

总览 KPI 映射（详情跳转）：

| 总览项 | 取自页 | 数据源 | 详情跳转 |
|---|---|---|---|
| 当前 QPS + sparkline | 运行指标 | metrics/timeseries qps_total | #/metrics |
| 错误率 5xx+429 + sparkline | 运行指标 | 同上 err 率 | #/metrics |
| 拦截率 24h（%/次数/总量） | 审计日志 | stats/overview.blocked/total | #/audit?action=block |
| 本地路由率 24h（%/次数/总量） | 审计日志 | stats/overview.local_routed/total | #/audit?action=route_local |
| Token 用量 24h | 统计可视化 | stats/overview.tokens_total | #/stats |
| 配置速览条：自动切换/已登记 KEY/外部候选启用 | 模型配置+KEY 管理 | model-policy + meta | #/modelpolicy、#/keys |

## 8. Do's and Don'ts（明确不做）

**Don't**：
- 装饰性风格：玻璃拟态、渐变 hero、滚动动画（与数据密集仪表盘互斥）。
- 引 web font / 动效库 / 图表库（uPlot 已够）。
- 移动端优先适配（桌面内网工具；保底 1280px 不出横向滚动即可）。
- 多语言（zh_CN 单语）。
- 自造组件库/设计系统站 —— token 就写在 `main.jsx` + `theme.css` 两处。

## 9. 工程备注：实施计划

> 本节为实现排期与落点，不属于设计系统规格。

现状基线：`ConfigProvider` 已有全局 theme token（见下方落点，主色/底色/圆角为视觉提色后现行值）；
`rules` 页原无菜单入口；图标按钮无 aria-label、无 reduced-motion 处理。

### Phase 1 —— 地基（约 0.5 天，全部小 diff）

| # | 文件 | 改动 | 验收 |
|---|---|---|---|
| 1 | `App.jsx` | 新建「安全策略」组收 `rules`+`keyrules`（原无菜单入口）；标题文案统一 | 菜单可进规则管理页 |
| 2 | `App.jsx` | 页面 → `location.hash`（读/写/校验/popstate） | 刷新留页；`#/audit` 直达 |
| 3 | `main.jsx` | ConfigProvider theme token（§9 落点） | 表格 13px、表头 #FAFAFA，无样式回归 |
| 4 | `App.jsx` + `theme.css`（card-padding.css 改名并并入） | Header/StatusBar sticky；StatusBar 加「更新于」时间；红灯 pulse + reduced-motion | 滚动审计表时状态灯可见；红灯脉冲；系统减动效时不脉冲 |
| 5 | 全局 | 纯图标按钮补 aria-label（约 5-8 处） | 键盘 Tab 过每个图标按钮有可访问名 |

### Phase 2 —— 状态与密度（约 1 天）

1. §6.1 表格规则逐页落地（重点：审计、外部候选、健康检查、KEY 四页的
   loading + 空态）。
2. 审计页筛选（时间范围/动作/全文）→ URL query（`#/audit?since=...&action=...&q=...`），
   支持分享/刷新保留。
3. 路由检查结果 Alert 置顶 + loading。
4. 统计/指标页无数据态与图表占位高度（防布局跳）。

### Phase 3 —— 可选（按需立项）

- ~~总览页做默认落地页~~ **已做**（`OverviewPage.jsx`，菜单首项 + 默认路由）。
- 审计表 >50 行虚拟滚动（若分页已够快则不做）。
- 暗色模式（OLED 风，token 已分层时成本低）——运维夜班场景再说。

### Token 落点：`main.jsx` ConfigProvider

```jsx
<ConfigProvider locale={zhCN} theme={{
  token: {
    colorPrimary: '#2f54eb',
    colorBgLayout: '#f5f6f8',
    fontSize: 14,
    fontSizeSM: 13,            // 数据密集表格
    borderRadius: 8,
    motionDurationMid: '0.2s',
  },
  components: {
    Table: { headerBg: '#FAFAFA' },
    Card:  { paddingLG: 16 },
  },
}}>
```

其余 token 一律用 antd 默认 —— 不改就没有 bug。
