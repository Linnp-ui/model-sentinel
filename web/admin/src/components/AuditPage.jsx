import { lazy, Suspense, useEffect, useState, useCallback, useMemo, useRef } from 'react';
import { Table, Button, Input, Select, Tag, Space, message, Typography, Card, Statistic, Tooltip, Tabs, Spin } from 'antd';
import { SearchOutlined, DownloadOutlined, DownOutlined } from '@ant-design/icons';
import { api, errText, fetchJson, usePoll, RANGES } from '../api.js';
import { wideSticky, patchHashQuery } from '../utils.js';
import { Tip } from '../terms.jsx';

// 请求明细页签（2026-11 由独立 #/logs 菜单并入本页：单一入口）：懒加载，切到才下 chunk
const LogsTab = lazy(() => import('./LogsPage.jsx'));

// 页签态入 URL（#/audit?tab=logs，默认 audit）；兼容老 #/logs 链接
// （App 路由已把它落到本页，首屏 hash 段仍是 logs）
const readTab = () => {
  const seg = window.location.hash.replace(/^#\/?/, '').split('?')[0];
  const q = new URLSearchParams(window.location.hash.split('?')[1] || '');
  return seg === 'logs' || q.get('tab') === 'logs' ? 'logs' : 'audit';
};

const { Text } = Typography;
import { ruleZh, RULE_FILTER_NAMES, ACTION_INFO, RuleZh, SystemBuiltinLegend } from '../ruleNames.jsx';

// L2 展开行：主判定 + laya 影子（shadow_*）。无 L2 的行不可展开。
// l2 可能是对象（新接口）或字符串（容错），统一归一化再渲染。
const normL2 = (l2) => {
  if (!l2) return null;
  if (typeof l2 === 'string') { try { return JSON.parse(l2); } catch { return { raw: l2 }; } }
  return l2;
};
const L2Detail = ({ rec }) => {
  const l2 = normL2(rec.l2);
  if (!l2) return <Text type="secondary">无 L2 判定（L1 直判，未进语义审查）</Text>;
  // 主影一致 = 正常态（无信息量，占可比行 80%+）：不展示主影判定结果。
  // 不一致 / 无影子 / 影子失败 / 主判定降级仍展示——那才是需要看的。
  // 口径与后端 _audit_shadow_agree 相同（degraded 主判的兜底 label 不可比）。
  if (l2.label && l2.shadow_label && !l2.degraded && l2.label === l2.shadow_label) return null;
  const hasShadow = l2.shadow_label || l2.shadow_conf !== undefined || l2.shadow_degraded;
  return (
    /* 展开行是单 td 横跨整表且被 theme 强制居中：限宽 + 块居中，
       文字左对齐由 theme.css 的 .audit-l2detail 翻回（同 ellipsis 例外口径），
       不然宽屏下详情像悬浮在超宽空带中间 */
    <Space className="audit-l2detail" direction="vertical" size={2} style={{ fontSize: 'var(--gw-fs-12)', maxWidth: 880, margin: '0 auto' }}>
      <Space wrap>
        <Text strong>主判定：</Text>
        <Tag color={l2.label === 'CONFIDENTIAL' ? 'red' : 'green'}>{l2.label || '?'}</Tag>
        <Text type="secondary">置信度：{l2.confidence ?? '?'}</Text>
        <Text type="secondary">{l2.reason || ''}</Text>
        {l2.latency_ms !== undefined && <Text type="secondary">{l2.latency_ms}ms</Text>}
        {l2.cached && <Tag>缓存</Tag>}
        {l2.degraded && <Tag color="orange">降级：{l2.degraded_reason || ''}{(l2.degraded_scopes || []).length ? `（块：${l2.degraded_scopes.join('、')}）` : ''}</Tag>}
      </Space>
      <Space wrap>
        <Text strong>影子(laya)：</Text>
        {!hasShadow && <Text type="secondary">无影子数据（影子未开 / backend=laya / 未进 L2）</Text>}
        {l2.shadow_degraded && <Tag color="orange">影子失败：{String(l2.shadow_degraded).slice(0, 80)}</Tag>}
        {l2.shadow_label && (
          <>
            <Tag color={l2.shadow_label === 'CONFIDENTIAL' ? 'red' : 'green'}>{l2.shadow_label}</Tag>
            <Text type="secondary">置信度：{l2.shadow_conf ?? '?'}</Text>
            <Text type="secondary">{l2.shadow_reason || ''}</Text>
            {String(l2.label || '') !== String(l2.shadow_label || '') && <Tag color="purple">与主判定不一致</Tag>}
          </>
        )}
      </Space>
    </Space>
  );
};

// 命中构成：文本段=L1 风险分拆解 + 关键词命中 + 逐条 PII 命中（数据源=落库 findings JSON）；
// 文件段=file_inspector 扁平标志/计数/sheet_names（数据源=落库 file_findings JSON）。
// 2026-10-29 起两段同显：PII 命中不再挤掉文件视图。兼容老行——没有 file_findings
// 键时，文件形态 findings（无 PII 命中的旧审计行）仍从 f 读。
const FindingsDetail = ({ f, file }) => {
  const fd = f || {};
  const isText = fd.risk_score != null || Array.isArray(fd.keyword_hits) || Array.isArray(fd.findings);
  const ff = (file && typeof file === 'object') ? file : (!isText ? fd : null);
  const SKIP = new Set(['sample_preview', 'markdown_preview', 'chunks', 'tables', 'text']);
  const pairs = ff ? Object.entries(ff).filter(([k, v]) => !SKIP.has(k) && (typeof v === 'boolean' || typeof v === 'number')) : [];
  const lists = ff ? Object.entries(ff).filter(([k, v]) => !SKIP.has(k) && Array.isArray(v) && v.length > 0) : [];
  const hasFile = pairs.length > 0 || lists.length > 0;
  if (!isText && !hasFile) return null;
  const hits = Array.isArray(fd.findings) ? fd.findings : [];
  const encoded = new Set(fd.encoded_keyword_hits || []);
  const byType = Object.entries(fd.by_type || {});
  return (
    <Space direction="vertical" size={2} style={{ fontSize: 'var(--gw-fs-12)' }}>
      {isText && (<>
        <Space wrap>
          <Text strong>L1 风险分构成：</Text>
          <Text>总分：{fd.risk_score ?? '—'}</Text>
          <Text type="secondary">PII 加权：{fd.weighted_pii_score ?? 0}</Text>
          <Text type="secondary">关键词：{fd.keyword_score ?? 0}</Text>
          {byType.length > 0 && <Text type="secondary">类型：{byType.map(([k, v]) => `${k}×${v}`).join('、')}</Text>}
        </Space>
        {(fd.keyword_hits || []).length > 0 && (
          <Space wrap size={4}>
            <Text strong>关键词命中：</Text>
            {fd.keyword_hits.map((k) => (
              <Tag key={k} color={encoded.has(k) ? 'purple' : 'orange'}
                title={encoded.has(k) ? '解码层命中（原文不可见，单独触发转内网）' : ''}>{k}{encoded.has(k) ? '（解码层）' : ''}</Tag>
            ))}
          </Space>
        )}
        {hits.length > 0 && (
          <Space direction="vertical" size={2}>
            <Text strong>逐条命中（{hits.length}）：</Text>
            {hits.slice(0, 20).map((h, i) => (
              <Text key={i} type="secondary">
                {h.type}｜{h.value_preview || ''}｜位置 {h.start}-{h.end}｜权重 {h.weight}{h.via && h.via !== 'raw' ? `｜${h.via} 层` : ''}
              </Text>
            ))}
            {hits.length > 20 && <Text type="secondary">…仅显示前 20 条（完整数据见 CSV 导出）</Text>}
          </Space>
        )}
      </>)}
      {hasFile && (<>
        <Text strong>文件预检结果：</Text>
        {pairs.length > 0 && <Text type="secondary">{pairs.map(([k, v]) => `${k}=${v}`).join('　')}</Text>}
        {lists.map(([k, v]) => (
          <Text key={k} type="secondary">{k}：{String(v).slice(0, 120)}{v.length > 5 ? ` …共 ${v.length} 项` : ''}</Text>
        ))}
      </>)}
    </Space>
  );
};

// ---------------- 审计日志（原 /admin 审计页） ----------------
const AUDIT_ACTIONS = [
  { value: 'allow', label: '放行' },
  { value: 'route_local', label: '本地路由' },
  { value: 'block', label: '拦截' },
];
// 时间窗选项与总览页共用 RANGES（同一 since 值），避免两处窗口口径漂移
const AUDIT_RANGES = [
  { value: '', label: '全部留存（列表仅最近 5000 条）' },
  ...RANGES.map((r) => ({ value: r.since, label: r.label })),
];

export default function AuditPage() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  // 筛选态入 URL（#/audit?since=..&action=..&rule=..&q=..）：刷新留条件、可分享
  const [f, setF] = useState(() => {
    const p = new URLSearchParams(window.location.hash.split('?')[1] || '');
    return { action: p.get('action') || '', since: p.get('since') || '',
             rule: p.get('rule') || '', q: p.get('q') || '', model: p.get('model') || '',
             ip: p.get('ip') || '' };
  });
  const [pageNo, setPageNo] = useState(1);
  const [tab, setTab] = useState(readTab);
  // 明细表卡片的落位槽（item 2）：恒挂载（审计页签 display:none 而非卸载）——
  // 若随页签卸载/重挂，切页那次 render 里 ref 未赋值，LogsPage 同帧同步渲染
  // （lazy chunk 已缓存）会拿到 null 而把表渲染回外框里
  const logsTableSlotRef = useRef(null);
  useEffect(() => {
    // 外部改 hash（侧栏点「审计日志」、明细页签的「切回审计」链接）⇒ 重读页签
    const onHash = () => setTab(readTab());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  const switchTab = (k) => {
    patchHashQuery((p) => { if (k === 'logs') p.set('tab', 'logs'); else p.delete('tab'); });
    setTab(k);
  };
  const writeUrl = (next) => {
    // 只写审计页签参数，保留明细页签的 l_* 参数（两页签共用一条 URL，互不覆盖）
    patchHashQuery((p) => {
      ['action', 'since', 'rule', 'q', 'model', 'ip', 'tab'].forEach((k) => p.delete(k));
      Object.entries(next).forEach(([k, v]) => { if (v) p.set(k, v); });
    });
  };
  // 规则名下拉来源：与「规则管理」页同源（/admin/api/policies），避免前端硬编码漂移
  const [ruleNames, setRuleNames] = useState([]);

  // silent=true：30s 轮询用（不点 loading 不闪 Spin、后台失败不弹错）
  // f 走 ref：load 身份只随 pageNo 变。若 f 进 deps，搜索框每个键入都换 load 身份
  // → effect 重跑 → 半截关键词就发一次请求 + 每键闪一次 Spin（实测）。
  // 下拉筛选（since/action/rule/model/ip）是离散选择，仍在 effect 依赖里"选完即查"；
  // 全文 q 只在「查询」按钮/回车（search()）时触发。
  const fRef = useRef(f);
  fRef.current = f;
  const load = useCallback(async (page = pageNo, silent = false) => {
    if (!silent) setLoading(true);
    try {
      const p = new URLSearchParams();
      Object.entries(fRef.current).forEach(([k, v]) => { if (v) p.set(k, v); });
      if (page > 1) p.set('page', String(page));
      setData(await fetchJson('/admin/audit/entries?' + p.toString()));
    } catch (e) { if (!silent) message.error(errText(e)); }
    finally { if (!silent) setLoading(false); }
  }, [pageNo]);

  useEffect(() => { load(); }, [load, f.since, f.action, f.rule, f.model, f.ip]);
  // 替代原刷新按钮；明细页签不轮询审计（KPI/列表已隐藏，空转无意义）
  usePoll(() => { if (tab === 'audit') load(pageNo, true); });

  // 规则名清单拉取：失败时静默回退到内置映射（RULE_FILTER_NAMES），不影响筛选可用性
  useEffect(() => {
    api('/policies')
      .then((r) => setRuleNames((r.items || []).map((x) => x.name).filter(Boolean)))
      .catch(() => {});
  }, []);

  // 规则名下拉 = policy 规则名（/policies，与规则管理页同源）+ 内部/历史规则名
  // （L2 判密、系统内置判定等也会写进 audit_logs.rule，必须能筛得到）。
  // 按显示名去重：policy 现行名优先，旧英文原值交给搜索框（中文名照样搜得到）。
  const ruleOptions = useMemo(() => {
    const seen = new Set();
    const out = [];
    for (const v of [...ruleNames, ...RULE_FILTER_NAMES]) {
      const label = ruleZh(v);
      if (seen.has(label)) continue;
      seen.add(label);
      out.push({ value: v, label });
    }
    return out;
  }, [ruleNames]);

  const search = () => { writeUrl(f); setPageNo(1); load(1); };
  // 会话追溯：同会话 ID 作为全文条件重查（q 匹配含 session_id；
  // shortcut: 仅热窗行有 session_id，MySQL 行无此列，超出 5000 条热窗的老记录追不到）
  const traceSession = (sid) => {
    const next = { ...fRef.current, q: sid };
    fRef.current = next; // load 读 fRef，同 tick 内必须手动同步（键入路径靠重渲染更新）
    setF(next); writeUrl(next); setPageNo(1); load(1);
  };
  const exportCsv = () => {
    const p = new URLSearchParams();
    Object.entries(f).forEach(([k, v]) => { if (v) p.set(k, v); });
    window.open('/admin/csv?' + p.toString(), '_blank');
  };

  const setFld = (k) => (e) => { const next = { ...f, [k]: e.target.value }; setF(next); writeUrl(next); };

  /* 列宽全 % + tableLayout=fixed：表跟随容器、列按比例分配，窄屏不再撑横向滚动。
     用户query 给 34% 定比（原 auto 弹性列靠内容顶宽，fixed 下必须有显式宽度；
     2026-10-29 删 Provider 列后 +9%——该列空值多、属单行排查信息，移入展开详情） */
  const cols = [
    /* 时间加 ellipsis 防溢出；cell-center 必需——theme.css 把 ellipsis 列强制
       左对齐，align:center 会被压掉（同模型/动作列） */
    { title: '时间', dataIndex: 'time', width: '11%', align: 'center', ellipsis: true, className: 'cell-center' },
    { title: '规则', width: '12%', align: 'center', ellipsis: true, className: 'cell-center',
      render: (_, r) => {
        const [name, color] = ACTION_INFO[r.action] || [r.action || '—', undefined];
        return (
          <Tag color={color} title={`动作：${name}｜规则：${r.rule || '—'}`} style={{ marginInlineEnd: 0, cursor: 'pointer' }}
            onClick={() => { if (r.rule) window.location.hash = '/rules'; }}>
            {r.rule ? <RuleZh name={r.rule} /> : name}
          </Tag>
        );
      } },
    { title: '模型', dataIndex: 'model', width: '11%', align: 'center', ellipsis: true, className: 'cell-center', render: (v) => v || '—' },
    { title: <>{'调用方名称'}<Tip term="caller" /></>, dataIndex: 'key_name', width: '11%', align: 'center', ellipsis: true,
      render: (v, r) => { const name = v || r.token_masked || ''; return name ? <Text code title={r.token_masked || name}>{name}</Text> : '—'; } },
    { title: <>{'客户端 IP'}<Tip term="client_ip" /></>, dataIndex: 'client_ip', width: '9%', align: 'center', render: (v) => v || '—' },
    { title: '文件名', dataIndex: 'filename', width: '12%', ellipsis: true, render: (v) => v || '—' },
    { title: <>{'用户query'}<Tip term="query" /></>, dataIndex: 'text_preview', width: '34%',
      render: (v) => (
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block', maxWidth: '100%' }}
          title={v}>{v || '—'}</span>
      ) },
  ];

  return (
    /* 两个 item（2026-11）：item1 = 页签栏 +「其他」（筛选/KPI；明细页签 = 筛选）——
       .audit-tabmerge 外框承接卡片边框/圆角，内部 Card 去边框；item2 =「审计日志」表/
       明细表 = 独立普通卡片。明细表在 LogsPage 内，portal 进 logsTableSlot（外框外） */
    <>
    <div className="audit-tabmerge">
      <Tabs className="audit-tabs" activeKey={tab} onChange={switchTab} size="small"
        items={[{ key: 'audit', label: <><span style={{ marginInlineEnd: 2 }}>审计日志（热窗）</span><Tip term="audit" /></> },
                { key: 'logs', label: <><span style={{ marginInlineEnd: 2 }}>请求明细（90 天）</span><Tip text="数据源是 MySQL 明细库，与「审计日志」页签的热窗是两套存储；仅调用元数据，行不可展开。" /></> }]} />
      <Space direction="vertical" style={{ width: '100%' }} size={0}>
      {tab === 'audit' ? (<>
      <Card>
        <Space wrap>
          <Select style={{ width: 140 }} value={f.since} options={AUDIT_RANGES}
            onChange={(v) => { const next = { ...f, since: v }; setF(next); setPageNo(1); writeUrl(next); }} />
          <Select style={{ width: 165 }} value={f.action || undefined} options={AUDIT_ACTIONS} allowClear placeholder="动作"
            onChange={(v) => { const next = { ...f, action: v || '' }; setF(next); setPageNo(1); writeUrl(next); }} />
          <Select style={{ width: 190 }} value={f.rule || undefined} options={ruleOptions} allowClear showSearch
            optionFilterProp="label" placeholder="规则名"
            /* 下拉项显示系统内置标记（label 仍是纯字符串，搜索按名字匹配不受影响） */
            optionRender={(opt) => <RuleZh name={opt.value} />}
            onChange={(v) => { const next = { ...f, rule: v || '' }; setF(next); setPageNo(1); writeUrl(next); }} />
          {/* 筛选行 = 表头列（时间/动作规则/模型/客户端IP）：无列的条件（影子/状态码/Provider）
              不占位，仍可由全文搜索与 API 参数触达 */}
          <Select style={{ width: 170 }} value={f.model || undefined} allowClear showSearch optionFilterProp="label"
            placeholder="模型"
            options={((data?.facets?.model) || []).map((v) => ({ value: v, label: v }))}
            onChange={(v) => { const next = { ...f, model: v || '' }; setF(next); setPageNo(1); writeUrl(next); }} />
          <Select style={{ width: 150 }} value={f.ip || undefined} allowClear showSearch optionFilterProp="label"
            placeholder="客户端 IP"
            options={((data?.facets?.client_ip) || []).map((v) => ({ value: v, label: v }))}
            onChange={(v) => { const next = { ...f, ip: v || '' }; setF(next); setPageNo(1); writeUrl(next); }} />
          {/* 筛选行 = 表头列（时间/动作规则/模型）：无列的条件（影子/状态码/Provider）不占位，
              仍可由全文搜索与 API 参数触达 */}
          <Input style={{ width: 210 }} placeholder="全文搜索（内容/IP/调用方/模型/provider/L2/影子…）" allowClear value={f.q} onChange={setFld('q')} onPressEnter={search} />
          <Button type="primary" icon={<SearchOutlined />} onClick={search}>查询</Button>
           <Button type="primary" icon={<DownloadOutlined />} onClick={exportCsv}>导出 CSV</Button>
        </Space>
      </Card>
      {data && (
        <>
          <Card>
            <Space wrap style={{ marginLeft: 'var(--gw-sp-16)', columnGap: 60, rowGap: 'var(--gw-sp-12)' }}>
              <Statistic data-testid="audit-stat-total" title={<>命中总数<Tip text="审计库＝MySQL 90 天全留存（不受热窗限制）；列表＝Redis 热窗最近 5000 条（更老记录见「请求明细」或 CSV）。流式失败补一行，含模型登记/注销行。" /><span style={{ color: 'var(--gw-muted)', fontSize: 'var(--gw-fs-12)' }}>（{data.kpi.source === 'window' ? '热窗' : '审计库'}）</span></>} value={data.kpi.total} />
              <Statistic data-testid="audit-stat-allow" title={<>放行<Tip term="allow" /></>} value={data.kpi.c_allow} valueStyle={{ color: '#389e0d' }} />
              <Statistic data-testid="audit-stat-route" title={<>本地路由<Tip term="route_local" /></>} value={data.kpi.c_route} valueStyle={{ color: '#d46b08' }} />
              <Statistic data-testid="audit-stat-block" title={<>阻断<Tip term="block" /></>} value={data.kpi.c_block} valueStyle={{ color: '#cf1322' }} />
            </Space>
          </Card>
        </>
      )}
      </>) : (
        <Suspense fallback={<div style={{ padding: 64, textAlign: 'center' }}><Spin /></div>}>
          <LogsTab tableSlotRef={logsTableSlotRef} />
        </Suspense>
      )}
      </Space>
    </div>
    {/* item 2：「审计日志」表（独立卡片；明细页签时是 LogsPage portal 进来的表） */}
    {tab === 'audit' && (
    <Card title="审计日志"
        extra={<Text type="secondary" style={{ fontSize: 'var(--gw-fs-12)' }}>查为什么拦/转内网（规则 / L2 判定）；展开行带该请求耗时 / token，90 天全量明细 <a onClick={() => switchTab('logs')}>切「请求明细」页签</a></Text>}>
      <Table className="audit-table" rowKey={(_, i) => i} columns={cols} dataSource={data?.entries || []} loading={loading} size="small"
        sticky={wideSticky()}
        tableLayout="fixed"
        locale={{ emptyText: '当前筛选无审计记录：可扩大时间范围、清空筛选条件，或换关键词试试' }}
        expandable={{ expandedRowRender: (rec) => (
          /* 单行一站式详情：meta 排（状态码/风险分/层级/会话追溯）+ L1 命中构成 + L2 判定（复用 L2Detail）。
             provider 不入展开区（2026-10-29 二轮）：模型名/请求模型已隐含落点，常规行显示即噪音；
             异常落点（模型像内网、实际外发，如 10-21 事故行）用全文搜索 provider 名或 CSV 查。
             输出格式标准：字段对一律「标签：值」（全角冒号；主标签 strong、补充项 secondary），
             段标题一律 strong+全角冒号；原始数据行（逐条命中 ｜ 分列 / 文件预检 k=v）保持紧凑记录式 */
          <Space className="audit-l2detail" direction="vertical" size={6} style={{ fontSize: 'var(--gw-fs-12)', maxWidth: 880, margin: '0 auto' }}>
            <Space wrap size={12}>
              <Text><Text strong>状态码：</Text>{rec.status_code || '—'}</Text>
              {/* 行级联查（req_id → request_log）：401/非请求行/req_id 上线前的老行无明细，不渲染 */}
              {rec.usage && <Text title="数据源：请求明细库（req_id 联查）"><Text strong>耗时：</Text>{rec.usage.duration_ms ?? 0}ms<Text type="secondary">（网关 {rec.usage.gateway_internal_ms ?? 0} / 上游 {rec.usage.upstream_ms ?? 0}）</Text></Text>}
              {rec.usage && (rec.usage.prompt_tokens || rec.usage.completion_tokens)
                ? <Text><Text strong>Token：</Text>{rec.usage.prompt_tokens || 0} / {rec.usage.completion_tokens || 0}{rec.usage.cached_tokens ? <Text type="secondary"> / {rec.usage.cached_tokens}</Text> : null}</Text>
                : null}
              {rec.risk_score != null && <Text><Text strong>风险分：</Text>{rec.risk_score}</Text>}
              {rec.layer && <Text><Text strong>层级：</Text>{rec.layer}</Text>}
              {rec.session_id
                ? <a onClick={() => traceSession(rec.session_id)} title="查同会话 ID 的全部请求">会话：{rec.session_id.slice(0, 14)}… 追溯</a>
                : <Text type="secondary">会话：—</Text>}
              {rec.downgraded_from && <Text type="secondary">降级自：{rec.downgraded_from}</Text>}
              {rec.requested_model && rec.requested_model !== rec.model && <Text type="secondary">请求模型：{rec.requested_model}</Text>}
            </Space>
            {(typeof rec.findings === 'object' && rec.findings !== null || rec.file_findings) &&
              <FindingsDetail f={typeof rec.findings === 'object' ? rec.findings : undefined} file={rec.file_findings} />}
            <L2Detail rec={rec} />
          </Space>
        ),
          /* 展开列原本是 48px 空白表头，装饰一个朝下箭头提示「这列能展开看完整详情」 */
          columnTitle: <Tooltip title="展开查看该行完整判定详情（L1 命中构成 / L2 判定）"><DownOutlined style={{ color: 'rgba(0,0,0,0.35)' }} /></Tooltip> }}
        pagination={{
          current: data?.page || 1, pageSize: data?.limit || 50, total: data?.total || 0,
          onChange: (pg) => setPageNo(pg), showSizeChanger: false,
          showTotal: (t, rg) => `第 ${rg[0]}-${rg[1]} 条 / 共 ${t} 条`,
        }} />
        <SystemBuiltinLegend />
      </Card>
    )}
    {/* 明细表落位槽：恒挂载（审计页签隐藏），portal 目标在 render 时恒可用 */}
    <div ref={logsTableSlotRef} style={{ display: tab === 'logs' ? undefined : 'none' }} />
    </>
  );
}

