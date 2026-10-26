import { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import { Table, Button, Input, Select, Tag, Space, message, Typography, Card, Statistic, Tooltip } from 'antd';
import { SearchOutlined, DownloadOutlined, CopyOutlined, DownOutlined } from '@ant-design/icons';
import { api, errText, fetchJson, copyText, usePoll, RANGES } from '../api.js';
import { wideSticky } from '../utils.js';
import { Tip } from '../terms.jsx';

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
  const hasShadow = l2.shadow_label || l2.shadow_conf !== undefined || l2.shadow_degraded;
  return (
    /* 展开行是单 td 横跨整表且被 theme 强制居中：限宽 + 块居中，
       文字左对齐由 theme.css 的 .audit-l2detail 翻回（同 ellipsis 例外口径），
       不然宽屏下详情像悬浮在超宽空带中间 */
    <Space className="audit-l2detail" direction="vertical" size={2} style={{ fontSize: 'var(--gw-fs-12)', maxWidth: 880, margin: '0 auto' }}>
      <Space wrap>
        <Text strong>主判定：</Text>
        <Tag color={l2.label === 'CONFIDENTIAL' ? 'red' : 'green'}>{l2.label || '?'}</Tag>
        <Text type="secondary">conf={l2.confidence ?? '?'}</Text>
        <Text type="secondary">{l2.reason || ''}</Text>
        {l2.latency_ms !== undefined && <Text type="secondary">{l2.latency_ms}ms</Text>}
        {l2.cached && <Tag>缓存</Tag>}
        {l2.degraded && <Tag color="orange">降级:{l2.degraded_reason || ''}{(l2.degraded_scopes || []).length ? `（块: ${l2.degraded_scopes.join('、')}）` : ''}</Tag>}
      </Space>
      <Space wrap>
        <Text strong>影子(laya)：</Text>
        {!hasShadow && <Text type="secondary">无影子数据（影子未开 / backend=laya / 未进 L2）</Text>}
        {l2.shadow_degraded && <Tag color="orange">影子失败:{String(l2.shadow_degraded).slice(0, 80)}</Tag>}
        {l2.shadow_label && (
          <>
            <Tag color={l2.shadow_label === 'CONFIDENTIAL' ? 'red' : 'green'}>{l2.shadow_label}</Tag>
            <Text type="secondary">conf={l2.shadow_conf ?? '?'}</Text>
            <Text type="secondary">{l2.shadow_reason || ''}</Text>
            {String(l2.label || '') !== String(l2.shadow_label || '') && <Tag color="purple">与主判定不一致</Tag>}
          </>
        )}
      </Space>
    </Space>
  );
};

// ---------------- 审计日志（原 /admin 审计页） ----------------
const AUDIT_ACTIONS = [
  { value: '', label: '全部动作' },
  { value: 'allow', label: '放行' },
  { value: 'route_local', label: '本地路由' },
  { value: 'block', label: '拦截' },
];
// 时间窗选项与总览页共用 RANGES（同一 since 值），避免两处窗口口径漂移
const AUDIT_RANGES = [
  { value: '', label: '全部留存（热窗 5000 条）' },
  ...RANGES.map((r) => ({ value: r.since, label: r.label })),
];

export default function AuditPage() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  // 筛选态入 URL（#/audit?since=..&action=..&rule=..&q=..）：刷新留条件、可分享
  const [f, setF] = useState(() => {
    const p = new URLSearchParams(window.location.hash.split('?')[1] || '');
    return { action: p.get('action') || '', since: p.get('since') || '',
             rule: p.get('rule') || '', q: p.get('q') || '', shadow: p.get('shadow') || '' };
  });
  const [pageNo, setPageNo] = useState(1);
  const writeUrl = (next) => {
    const p = new URLSearchParams();
    Object.entries(next).forEach(([k, v]) => { if (v) p.set(k, v); });
    const qs = p.toString();
    history.replaceState(null, '', '#/audit' + (qs ? '?' + qs : ''));
  };
  // 规则名下拉来源：与「规则管理」页同源（/admin/api/policies），避免前端硬编码漂移
  const [ruleNames, setRuleNames] = useState([]);

  // silent=true：30s 轮询用（不点 loading 不闪 Spin、后台失败不弹错）
  // f 走 ref：load 身份只随 pageNo 变。若 f 进 deps，搜索框每个键入都换 load 身份
  // → effect 重跑 → 半截关键词就发一次请求 + 每键闪一次 Spin（实测）。
  // 下拉筛选（since/action/rule/shadow）是离散选择，仍在 effect 依赖里"选完即查"；
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

  useEffect(() => { load(); }, [load, f.since, f.action, f.rule, f.shadow]);
  usePoll(() => load(pageNo, true)); // 替代原刷新按钮

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
  const exportCsv = () => {
    const p = new URLSearchParams();
    Object.entries(f).forEach(([k, v]) => { if (v) p.set(k, v); });
    window.open('/admin/csv?' + p.toString(), '_blank');
  };

  const setFld = (k) => (e) => { const next = { ...f, [k]: e.target.value }; setF(next); writeUrl(next); };

  /* 列宽全 % + tableLayout=fixed：表跟随容器、列按比例分配，窄屏不再撑横向滚动。
     用户query 给 25% 定比（原 auto 弹性列靠内容顶宽，fixed 下必须有显式宽度） */
  const cols = [
    /* 时间/Provider 加 ellipsis 防溢出；cell-center 必需——theme.css 把 ellipsis 列强制
       左对齐，align:center 会被压掉（同模型/动作列） */
    { title: '时间', dataIndex: 'time', width: '11%', align: 'center', ellipsis: true, className: 'cell-center' },
    { title: 'Provider', dataIndex: 'provider', width: '9%', align: 'center', ellipsis: true, className: 'cell-center', render: (v) => v || '—' },
    { title: '动作 / 规则', width: '12%', align: 'center', ellipsis: true, className: 'cell-center',
      render: (_, r) => {
        const [name, color] = ACTION_INFO[r.action] || [r.action || '—', undefined];
        return (
          <Tag color={color} title={`动作：${name}｜规则：${r.rule || '—'}`} style={{ marginInlineEnd: 0 }}>
            {r.rule ? <RuleZh name={r.rule} /> : name}
          </Tag>
        );
      } },
    { title: '模型', dataIndex: 'model', width: '11%', align: 'center', ellipsis: true, className: 'cell-center', render: (v) => v || '—' },
    { title: <>{'调用方名称'}<Tip term="caller" /></>, dataIndex: 'key_name', width: '11%', align: 'center',
      render: (v, r) => { const name = v || r.token_masked || ''; return name ? <Text code title={r.token_masked || name}>{name}</Text> : '—'; } },
    { title: <>{'客户端 IP'}<Tip term="client_ip" /></>, dataIndex: 'client_ip', width: '9%', align: 'center', render: (v) => v || '—' },
    { title: '文件名', dataIndex: 'filename', width: '12%', ellipsis: true, render: (v) => v || '—' },
    { title: <>{'用户query'}<Tip term="query" /></>, dataIndex: 'text_preview', width: '25%',
      render: (v) => (
        <span style={{ display: 'flex', alignItems: 'center', gap: 'var(--gw-sp-4)', maxWidth: '100%' }}>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0, flex: 1 }}
            title={v}>{v || '—'}</span>
          {v && <Button type="text" size="small" icon={<CopyOutlined />} aria-label="复制用户query"
            onClick={() => copyText(v)} />}
        </span>
      ) },
  ];

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Card>
        <Space wrap>
          <Select style={{ width: 140 }} value={f.since} options={AUDIT_RANGES}
            onChange={(v) => { const next = { ...f, since: v }; setF(next); setPageNo(1); writeUrl(next); }} />
          <Select style={{ width: 165 }} value={f.action} options={AUDIT_ACTIONS}
            onChange={(v) => { const next = { ...f, action: v }; setF(next); setPageNo(1); writeUrl(next); }} />
          <Select style={{ width: 190 }} value={f.rule || undefined} options={ruleOptions} allowClear showSearch
            optionFilterProp="label" placeholder="规则名"
            /* 下拉项显示系统内置标记（label 仍是纯字符串，搜索按名字匹配不受影响） */
            optionRender={(opt) => <RuleZh name={opt.value} />}
            onChange={(v) => { const next = { ...f, rule: v || '' }; setF(next); setPageNo(1); writeUrl(next); }} />
          <Select style={{ width: 150 }} value={f.shadow}
            options={[{ value: '', label: '影子：全部' }, { value: 'mismatch', label: '主影不一致' }]}
            onChange={(v) => { const next = { ...f, shadow: v || '' }; setF(next); setPageNo(1); writeUrl(next); }} />
          <Input style={{ width: 210 }} placeholder="全文搜索（内容/IP/调用方/模型/L2/影子…）" allowClear value={f.q} onChange={setFld('q')} onPressEnter={search} />
          <Button type="primary" icon={<SearchOutlined />} onClick={search}>查询</Button>
           <Button type="primary" icon={<DownloadOutlined />} onClick={exportCsv}>导出 CSV</Button>
        </Space>
      </Card>
      {data && (
        <>
          <Card>
            <Space wrap style={{ marginLeft: 'var(--gw-sp-16)', columnGap: 60, rowGap: 'var(--gw-sp-12)' }}>
              <Statistic data-testid="audit-stat-total" title={<>命中总数<Tip text="审计记录条数（流式失败会补一行；含模型登记/注销行）。" /><span style={{ color: 'var(--gw-muted)', fontSize: 'var(--gw-fs-12)' }}>（{data.kpi.source === 'window' ? '热窗' : '审计库'}）</span></>} value={data.kpi.total} />
              <Statistic data-testid="audit-stat-allow" title={<>放行<Tip term="allow" /></>} value={data.kpi.c_allow} valueStyle={{ color: '#389e0d' }} />
              <Statistic data-testid="audit-stat-route" title={<>本地路由<Tip term="route_local" /></>} value={data.kpi.c_route} valueStyle={{ color: '#d46b08' }} />
              <Statistic data-testid="audit-stat-block" title={<>阻断<Tip term="block" /></>} value={data.kpi.c_block} valueStyle={{ color: '#cf1322' }} />
            </Space>
          </Card>
        </>
      )}
      <Card title={<>审计日志<Tip term="audit" /></>}>
      <Table className="audit-table" rowKey={(_, i) => i} columns={cols} dataSource={data?.entries || []} loading={loading} size="small"
        sticky={wideSticky()}
        tableLayout="fixed"
        locale={{ emptyText: '当前筛选无审计记录：可扩大时间范围、清空筛选条件，或换关键词试试' }}
        expandable={{ expandedRowRender: (rec) => <L2Detail rec={rec} />,
          rowExpandable: (rec) => !!normL2(rec.l2),
          /* 展开列原本是 48px 空白表头，装饰一个朝下箭头提示「这列能展开看 L2 详情」 */
          columnTitle: <Tooltip title="展开查看该行的 L2 判定详情"><DownOutlined style={{ color: 'rgba(0,0,0,0.35)' }} /></Tooltip> }}
        pagination={{
          current: data?.page || 1, pageSize: data?.limit || 50, total: data?.total || 0,
          onChange: (pg) => setPageNo(pg), showSizeChanger: false,
          showTotal: (t, rg) => `第 ${rg[0]}-${rg[1]} 条 / 共 ${t} 条`,
        }} />
        <SystemBuiltinLegend />
      </Card>
    </Space>
  );
}

