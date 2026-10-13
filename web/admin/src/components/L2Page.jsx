import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert, Button, Card, Input, Modal, Popconfirm, Select, Space, Spin, Statistic, Table, Tag, Tooltip, Typography, message,
} from 'antd';
import { DownloadOutlined } from '@ant-design/icons';
import { api, errText } from '../api';
import WheelGate from './WheelGate.jsx';

const { Text } = Typography;

// 2×2 混淆矩阵：行=主判、列=影判。环图把矩阵结构抹平（且实测 nc=0 一格是 0 缝），
// 矩阵让"漏报嫌疑/误报嫌疑"结构性突出；格色与下方分歧表四列一致
function CellsMatrix({ cells, agreeRate, testid }) {
  const cell = (v, bg, label) => (
    <div style={{ background: bg, borderRadius: 6, padding: '12px 8px', textAlign: 'center' }}>
      <div style={{ fontSize: 22, fontWeight: 700, color: '#fff' }}>{v}</div>
      <div style={{ fontSize: 11, color: 'rgba(255,255,255,0.85)' }}>{label}</div>
    </div>
  );
  return (
    <div data-testid={testid} style={{ display: 'grid', gridTemplateColumns: 'auto 1fr 1fr', gap: 6, maxWidth: 360 }}>
      <div style={{ fontSize: 14, color: '#666', alignSelf: 'center', paddingRight: 4 }}>一致率 {agreeRate}</div>
      <div style={{ textAlign: 'center', fontSize: 14, color: '#666' }}>影判 密</div>
      <div style={{ textAlign: 'center', fontSize: 14, color: '#666' }}>影判 放</div>
      <div style={{ fontSize: 14, color: '#666', alignSelf: 'center' }}>主判 密</div>
      {cell(cells.cc, '#52c41a', '一致')}
      {cell(cells.cn, '#faad14', '漏报嫌疑')}
      <div style={{ fontSize: 14, color: '#666', alignSelf: 'center' }}>主判 放</div>
      {cell(cells.nc, '#ff4d4f', '误报嫌疑')}
      {cell(cells.nn, '#1677ff', '一致')}
    </div>
  );
}

// UTC 字符串 → 北京时间显示（sv-SE 输出 "YYYY-MM-DD HH:mm:ss"，无逗号）。
// 格式化器只建一次：Intl.DateTimeFormat 构造是 ms 级，表格逐行新建会卡渲染。
const _bjFmt = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai',
  dateStyle: 'short', timeStyle: 'medium', hour12: false });
const bj = (ts) => {
  const t = Date.parse(String(ts).replace(' ', 'T').slice(0, 19) + 'Z');
  if (Number.isNaN(t)) return String(ts || '');
  return _bjFmt.format(t);
};

const labelTag = (l) => (l === 'CONFIDENTIAL'
  ? <Tag color="red" style={{ marginInlineEnd: 0 }}>CONFIDENTIAL</Tag>
  : <Tag color="green" style={{ marginInlineEnd: 0 }}>NORMAL</Tag>);

export default function L2Page() {
  // 只看本轮灌数（round = gray_flood 写的轮次起点 var/l2-round.json）：这页是评测/训练用的，
  // 生产流量不在这看（小时窗会把两轮灌数装进同一窗，同一文本成 dup×2，看着像重复分歧）。
  // 没灌过数时退回最近 1 小时，免得整页空白。
  const [round, setRound] = useState({ since: '' });
  const [rounds, setRounds] = useState([]);
  useEffect(() => {
    const tick = async () => {
      try {
        setRound(await api('/l2-round'));
        setRoundReady(true); // 只在拿到轮次后才放行汇总请求（见 roundReady 注释）
      } catch { /* 忽略：没灌过数时端点可能没有内容 */ }
      // 轮次历史：这页的判定流量是脉冲式的（一轮 ~50 条集中在 40 秒内，其余时间零流量），
      // 按小时窗看会误读成"没数据"。轮次是真实的计量单位，见 /l2-rounds。
      api('/l2-rounds').then((d) => setRounds(d.rounds || [])).catch(() => {});
    };
    tick(); // 首屏必须立刻取轮次：否则第一分钟 win=hours=1，过点后图表空窗
    const t = setInterval(tick, 60000);
    return () => clearInterval(t);
  }, []);
  const [summary, setSummary] = useState(null);
  const [divs, setDivs] = useState([]);
  const [cellF, setCellF] = useState('');
  const [labeledF, setLabeledF] = useState('all');
  const [q, setQ] = useState('');
  /* 权威后端取运行时真值（/l2-config 的 AI_GATEWAY_L2_BACKEND，默认 qwen）：
     顶栏按它动态显示，切到 laya 后不再谎称 qwen 权威（backend=laya 时影子自动停） */
  const [backend, setBackend] = useState('qwen');
  useEffect(() => {
    api('/l2-config').then((d) => setBackend(String(d.AI_GATEWAY_L2_BACKEND || 'qwen').toLowerCase())).catch(() => {});
  }, []);
  const isLaya = backend === 'laya';

  /* roundReady：/l2-round 返回前 win 只能是占位的 hours=1，等它回来 win 变成
     since=...&hours=24 会让下面两个 useCallback 换身份、useEffect 重跑 → 首屏把
     shadow-stats / divergences 各拉两遍（实测 9 请求里 6 个是重复的）。门控一下：
     轮次没到就不拉汇总，拿到后只拉一次正确的窗。 */
  const [roundReady, setRoundReady] = useState(false);

  const win = round.since
    ? `since=${encodeURIComponent(round.since)}&hours=24`
    : 'hours=1';

  const loadHead = useCallback(async () => {
    /* 只取汇总：原还并发拉 /l2-shadow-samples 喂「主影一致性」柱状图，该卡已删，
       留着会白拉一份数据（后端端点保留，脚本与 API 消费方还在用） */
    try {
      setSummary(await api(`/l2-shadow-stats?${win}`));
    } catch (e) {
      message.error(errText(e));
      setSummary(null);
    }
  }, [win]);

  const loadDivs = useCallback(async (cell = cellF, qq = q) => {
    try {
      const d = await api(`/l2-divergences?${win}&cell=${encodeURIComponent(cell)}&q=${encodeURIComponent(qq)}&limit=500`);
      setDivs(d.items || []);
    } catch (e) {
      message.error(errText(e));
      setDivs([]);
    }
  }, [win, cellF, q]);

  useEffect(() => { if (roundReady) loadHead(); }, [loadHead, roundReady]);
  useEffect(() => { if (roundReady) loadDivs(); }, [loadDivs, roundReady]);
  // ---------------- 新分歧横幅：30s 后台静默拉，只弹条数，点才加载（不打断标注/滚动位） ----------------
  const [pendingNew, setPendingNew] = useState(0);
  const knownRef = useRef(new Set());
  useEffect(() => { knownRef.current = new Set(divs.map((x) => x.req_id)); }, [divs]);
  useEffect(() => {
    setPendingNew(0);
    const t = setInterval(async () => {
      if (document.hidden) return;
      try {
        const d = await api(`/l2-divergences?${win}&cell=${encodeURIComponent(cellF)}&q=${encodeURIComponent(q)}&limit=500`);
        setPendingNew((d.items || []).filter((x) => !knownRef.current.has(x.req_id)).length);
      } catch { /* 静默，下次 tick 重试 */ }
    }, 30000);
    return () => clearInterval(t);
  }, [win, cellF, q]);

  // ---------------- 优化管道：标注 → 数据集 → 微调 → 候选评测 → 晋升影子 ----------------
  const [promVer, setPromVer] = useState('');
  const [vers, setVers] = useState({ versions: [], current: null });
  const loadVers = useCallback(async () => {
    try { setVers(await api('/l2/retrain/versions')); } catch { /* 忽略 */ }
  }, []);
  const [job, setJob] = useState(null);
  const loadJob = useCallback(async () => {
    try { setJob(await api('/l2/retrain/status')); } catch { setJob(null); }
  }, []);
  useEffect(() => { loadJob(); }, [loadJob]);
  /* 版本下拉只在训练状态**真的变了**时刷新。原先依赖 job?.state：首屏 status 返回后
     state 由 undefined 变成 'idle' 也算一次变化 → versions 被拉两遍（实测 9 个请求里
     有 3 个重复）。用 ref 记住上次状态，只在真变化时跑。 */
  const lastState = useRef(undefined);
  useEffect(() => {
    if (lastState.current === job?.state) return;
    lastState.current = job?.state;
    loadVers();
  }, [loadVers, job?.state]);
  useEffect(() => {
    if (!job || job.state !== 'running') return undefined;
    const t = setInterval(loadJob, 10000);
    return () => clearInterval(t);
  }, [job, loadJob]);

  const startTrain = async () => {
    try {
      await api('/l2/retrain', { method: 'POST' });
      message.success('优化训练已启动（约 10-20 分钟）');
      loadJob();
    } catch (e) { message.error(errText(e)); }
  };
  const [hardAdded, setHardAdded] = useState([]);  // 已入 hard 的错题名（后端去重，状态常驻防重复点）
  const addHard = async (items) => {
    try {
      const d = await api('/l2/retrain/hard', {
        method: 'POST',
        body: JSON.stringify({ items: items.map((w) => ({ text: w.text, label: w.want, filename: w.filename || '' })) }),
      });
      message.success(`已入 hard ${d.added} 条（重复/过短跳过 ${d.skipped} 条），联动标分歧 ${d.labeled || 0} 行，下版训练生效`);
      if (d.added > 0) {
        setHardAdded((s) => [...new Set([...s, ...items.map((w) => w.name)])]);
        loadDivs();  // 分歧表即时刷新联动标注
      }
    } catch (e) { message.error(errText(e)); }
  };
  const promote = async (v) => {
    try {
      const d = await api('/l2/retrain/promote', { method: 'POST', body: JSON.stringify({ version: v }) });
      message.success(`影子已切换 ${d.model}`);
      loadJob();
      loadVers();
    } catch (e) { message.error(errText(e)); }
  };

  const candVer = (job?.candidate || '').match(/v(\d+)$/)?.[1] || '?';
  const canStart = job && job.state !== 'running' && job.labeled_count >= job.min_labels;
  const JobCard = () => (
    <Card title="优化管道（标注 → 训练 → 候选评测 → 晋升影子）" style={{ marginBottom: 12 }}>
      <Space wrap size="middle" align="center">
        <Text>人工标注 <Text strong>{job?.labeled_count ?? '-'}</Text> / 门槛 {job?.min_labels ?? '-'}</Text>
        {job?.state === 'running' && (
          <><Spin size="small" /><Text>阶段 {job.stage}（{job.elapsed_s}s）</Text></>
        )}
        {job?.state === 'done' && (() => {
          const b = job.report?.battery || {};
          const base = job.report?.baseline;
          const lb = job.report?.labeled || {};
          const dg = job.report?.divergences;
          const cx = (k) => (b.cats?.[k] ? `${b.cats[k].correct}/${b.cats[k].total}` : '-');
          return (
            <>
              <Tag color="green">完成</Tag>
              <Text>候选 v{candVer}：battery {b.correct}/{b.total}
                （探针 {cx('probe')} · 真实 {cx('real')} · 回归 {cx('reg')}）
                {base ? `，现影子 ${base.correct}/${base.total}` : ''}；
                标注回归 {lb.matched}/{lb.total}
                {dg ? `，分歧回归 ${dg.matched}/${dg.total}` : ''}
                {job.report?.prompt ? `；qwen prompt ${job.report.prompt.sha1}` : ''}</Text>
              {dg && dg.missed?.length > 0 && (
                <div style={{ fontSize: 13, width: '100%' }}>
                  <Text type="danger">分歧回归漏判 {dg.missed.length} 条：</Text>
                  {dg.missed.map((m, i) => (
                    <Text key={i} style={{ display: 'block' }}>
                      want={m.want} got={m.got} p={m.p} dup×{m.dup}｜{m.text}
                    </Text>
                  ))}
                </div>
              )}
              {job.report?.regressions && (
                <div style={{ fontSize: 13, width: '100%' }}>
                  <Text>相对 v{job.report.regressions.against}：</Text>
                  {job.report.regressions.new.length > 0
                    ? <Text type="danger">新错 {job.report.regressions.new.length} 条：{job.report.regressions.new.join('；')}</Text>
                    : <Text type="success">零新错</Text>}
                  {job.report.regressions.fixed.length > 0 && (
                    <Text type="success">；修好 {job.report.regressions.fixed.length} 条：{job.report.regressions.fixed.join('；')}</Text>
                  )}
                </div>
              )}
              {String(vers.current) === candVer
                ? <Tag color="blue">v{candVer} 已是当前影子</Tag>
                : (
                  <Popconfirm title={isLaya
                    ? `已切 laya 权威、影子管道不适用（backend=laya 时 laya 即主判，无独立影子可晋升）`
                    : `晋升 v${candVer} 为影子模型？（仅影子打标，权威仍 ${backend}）`} onConfirm={() => !isLaya && promote(candVer)}>
                    <Button type="primary" size="small">晋升影子 v{candVer}</Button>
                  </Popconfirm>
                )}
              {b.wrong?.length > 0 && (
                <details style={{ width: '100%' }} open>
                  <summary style={{ cursor: 'pointer', fontSize: 13 }}>
                    错题 {b.wrong.length} 条（want 存疑的别点，去下面分歧表标注）
                  </summary>
                  <Button size="small" style={{ margin: '4px 0' }}
                    onClick={() => addHard(b.wrong)}>全部按 want 入 hard</Button>
                  {b.wrong.map((w, i) => (
                    <div key={i} style={{ fontSize: 13, marginTop: 6, borderTop: '1px solid #f0f0f0', paddingTop: 4 }}>
                      <Space size={6} wrap>
                        <Text strong>{w.name}</Text>
                        <Text>want={w.want} got={w.got} p={w.p}</Text>
                        <Button size="small" disabled={hardAdded.includes(w.name)}
                          onClick={() => addHard([w])}>
                          {hardAdded.includes(w.name) ? '已入 hard' : '入 hard'}
                        </Button>
                      </Space>
                      <div style={{ color: '#666' }}>{w.preview}…</div>
                    </div>
                  ))}
                </details>
              )}
              {lb.missed?.length > 0 && (
                <Text type="danger">标注回归错 {lb.missed.length} 条：{lb.missed.map((m) => `${m.req_id}(want${m.want}/p${m.p})`).join('，')}</Text>
              )}
            </>
          );
        })()}
        {job?.state === 'failed' && <Tag color="red">失败：{String(job.error || '').slice(0, 120)}</Tag>}
        {job?.state !== 'running' && (
          <Button size="small" type={job?.state === 'done' ? 'default' : 'primary'}
            disabled={!canStart} onClick={startTrain}>
            {job?.state === 'failed' ? '重新训练' : '启动优化训练'}
          </Button>
        )}
        {job?.state !== 'running' && (
          <Space size={4}>
            <Select size="small" placeholder="版本" value={promVer || undefined}
              onChange={setPromVer} style={{ width: 130 }}
              options={vers.versions.map((v) => ({
                value: String(v), label: `v${v}${v === vers.current ? '（现影子）' : ''}`,
              }))} />
            <Popconfirm title={isLaya
              ? `已切 laya 权威、影子管道不适用（backend=laya 时 laya 即主判，无独立影子可晋升）`
              : `晋升 v${promVer || '?'} 为影子模型？（仅影子打标，权威仍 ${backend}）`}
              onConfirm={() => promVer && !isLaya && promote(promVer)}>
              <Button size="small" disabled={!promVer}>晋升指定版本</Button>
            </Popconfirm>
          </Space>
        )}
      </Space>
      {job?.state === 'running' && job.log_tail?.length > 0 && (
        <WheelGate>
        <pre style={{ maxHeight: 320, overflow: 'auto', overscrollBehavior: 'contain', fontSize: 13, lineHeight: 1.6, marginTop: 8, marginBottom: 0 }}>
          {job.log_tail.join('\n')}
        </pre>
        </WheelGate>
      )}
    </Card>
  );

  const submitLabel = async (req_id, label) => {
    setDivs((d) => d.map((x) => (x.req_id === req_id ? { ...x, label } : x)));
    try {
      await api('/l2-divergences/label', { method: 'POST', body: JSON.stringify({ req_id, label }) });
      loadJob();  // 同步管道卡标注计数（人工标注 x/10），免刷新
    } catch (e) {
      message.error(errText(e));
      loadDivs();
    }
  };
  const doLabel = (req_id, label) => {
    // 反向标注确认：与主判（backend，qwen 或 laya）相反时弹确认（人工最高优先级，标错即投毒，4aa937b4 实测）
    const row = divs.find((x) => x.req_id === req_id);
    if (row && label != null
        && ((label === 0 && row.main === 'CONFIDENTIAL') || (label === 1 && row.main === 'NORMAL'))) {
      Modal.confirm({
        title: `${backend} 判${row.main === 'CONFIDENTIAL' ? '密' : '放行'}，你标${label === 1 ? '机密' : '非密'}，确认吗？`,
        content: '人工标注会覆盖规则进训练集，标错往后每版都带毒。',
        okText: '确认标注',
        cancelText: '取消',
        onOk: () => submitLabel(req_id, label),
      });
      return;
    }
    submitLabel(req_id, label);
  };

  // 一致/分歧 堆叠柱状图（绿=一致、红=分歧，柱高=该桶 L2 判定量）。
  // 桶宽随窗口自适应：≤3h 用 5min、≤48h 用 1h、更长用 3h——灌数一轮只有几十秒，
  const shown = useMemo(() => {
    if (labeledF === 'labeled') return divs.filter((x) => x.label != null);
    if (labeledF === 'unlabeled') return divs.filter((x) => x.label == null);
    // 主判错题集：已标且与 qwen 主判相反 = 改 prompt 的工作台（标注不自动改 prompt，须人走 prompt-test→PUT）
    if (labeledF === 'qwenwrong') return divs.filter((x) => x.label != null
      && x.label !== (x.main === 'CONFIDENTIAL' ? 1 : 0));
    return divs;
  }, [divs, labeledF]);

  /* 列宽用 % 自适应（与本页灌数轮次表同口径）；标注列是按钮组，保留定宽 */
  const cols = [
    /* 时间是纯文本无省略号：窄列溢出到邻列，加 ellipsis 裁切 */
    { title: '时间 (北京)', dataIndex: 'ts', width: '15%', ellipsis: true, render: (v) => bj(v) },
    { title: '方向', dataIndex: 'cell', width: '10%',
      render: (c) => (c === 'nc'
        ? <Tag color="red" style={{ marginInlineEnd: 0 }}>主放·影密</Tag>
        : <Tag color="orange" style={{ marginInlineEnd: 0 }}>主密·影放</Tag>) },
    { title: '主 → 影', width: '22%',
      render: (_, x) => <Space size={4}>{labelTag(x.main)}<span>→</span>{labelTag(x.shadow)}</Space> },
    { title: '影 p', dataIndex: 'shadow_prob', width: '8%',
      render: (v) => (v == null ? '-' : Number(v).toFixed(3)) },
    { title: '内容', dataIndex: 'text_preview', width: '34%', ellipsis: true,
      render: (t, x) => (
        <div>
          <Space size={4}>
            <span>{t || <Text type="secondary">（无预览）</Text>}</span>
            {x.dup > 1 && <Tag>×{x.dup}</Tag>}
          </Space>
          {x.filename && <div><Text type="secondary" style={{ fontSize: 12 }}>{x.filename}</Text></div>}
        </div>
      ) },
    { title: '标注', width: 190,
      render: (_, x) => (
        <Space size={4} wrap>
          <Button size="small" type={x.label === 1 ? 'primary' : 'default'}
            danger={x.label === 1} onClick={() => doLabel(x.req_id, 1)}>机密</Button>
          <Button size="small" type={x.label === 0 ? 'primary' : 'default'}
            onClick={() => doLabel(x.req_id, 0)}>非密</Button>
          {x.label != null && (
            <Button size="small" type="text" onClick={() => doLabel(x.req_id, null)}>取消</Button>
          )}
        </Space>
      ) },
  ];

  /* 影子模型名取最新一轮灌数记录的 laya 字段（var/l2-rounds.jsonl，gray_flood 每轮写入
     当时的权重），不写死 'laya'：切版本后标题自动跟着变。取不到就退回通用说法 */
  const shadowVer = (() => {
    const v = rounds.find((r) => r.laya);
    return v ? String(v.laya).split('/').pop().replace('laya-finetuned-confidential-', '') : '';
  })();

  return (
    <div>
      <Alert type={isLaya ? 'warning' : 'info'} showIcon style={{ marginBottom: 12 }}
        message={isLaya
          ? '权威判定 = laya（本地 /classify 直判）；影子已停、不并行打标 —— 已切权威，不是影子调优'
          : '权威判定 = qwen（真正决定请求是否出境）；本页的 laya 只是影子，并行打标、不改路由 —— 影子调优 ≠ 已切权威'} />
      <Card title={`影子模型汇总${shadowVer ? `（${shadowVer}）` : ''}`} style={{ marginBottom: 12 }}
        extra={<Text type="secondary" style={{ fontSize: 12 }}>
          {round.since
            ? `本轮灌数 ${bj(round.since).slice(6, 16)} 起${round.sent ? ` · ${round.sent} 条` : ''}`
            : '本轮灌数（暂无记录）'}
        </Text>}>
        {/* space-evenly + 无 column gap：左、中、右三段空白恒等（center+gap 做不到，中缝总比两边多一个 gap） */}
        <div style={{ display: 'flex', rowGap: 16, flexWrap: 'wrap', alignItems: 'center', justifyContent: 'space-evenly' }}>
        <Space size="large" wrap>
          <Statistic title={<Tooltip title="口径：主影 label 相同 / 可比条数（主判定降级不计入）；数据源审计热缓存（近 5000 条）">主影一致率</Tooltip>}
            value={summary?.agree_rate == null ? '-' : (summary.agree_rate * 100).toFixed(1)} suffix="%"
            valueStyle={{ fontSize: 24 }} />
          <Statistic title="影子样本" value={summary ? `${summary.agree}/${summary.compared}` : '-'}
            valueStyle={{ fontSize: 24 }} />
          <Statistic title="影子覆盖率"
            value={summary?.l2_total ? (summary.shadowed / summary.l2_total * 100).toFixed(1) : '-'}
            suffix="%" valueStyle={{ fontSize: 24 }} />
          <Statistic title="影子失败" value={summary?.shadow_degraded ?? '-'} valueStyle={{ fontSize: 24 }} />
        </Space>
        {summary && summary.shadowed ? (
          <div style={{ minWidth: 340 }}>
            <CellsMatrix cells={summary.cells}
              agreeRate={summary.agree_rate != null ? (summary.agree_rate * 100).toFixed(1) + '%' : '-'}
              testid="shadow-cells-matrix" />
          </div>
        ) : (
          <Alert type="info" showIcon style={{ maxWidth: 460 }}
            message="本轮暂无影子数据（影子只在进 L2 的灰区流量上产生；backend=laya 时影子自动停）" />
        )}
        </div>
      </Card>
      <Card title="灌数轮次" style={{ marginBottom: 12 }}
        extra={<Text type="secondary" style={{ fontSize: 12 }}>
          轮次时间为北京时区；一轮 ~50 条集中在 40 秒内，其余时间零流量 ⇒ 按小时窗看会误读
        </Text>}>
        <Table size="small" rowKey={(r) => r.started} dataSource={rounds}
          pagination={false} locale={{ emptyText: '还没跑过 scripts/gray_flood.py' }}
          /* 百分比铺满卡片（antd 表格默认 width:100%）。不设 width 会被 auto 布局
             均分，实测「轮次」内容 75px 却分到 666px（占 55%）；钉死 px 又在窄屏溢出 */
          columns={[
            { title: '轮次（起）', dataIndex: 'started', width: '22%',
              render: (v) => bj(v).slice(5, 16) },
            { title: '影子版本', dataIndex: 'laya', width: '22%',
              render: (v) => (v ? <Text type="secondary" style={{ fontSize: 12 }}>
                {String(v).split('/').pop().replace('laya-finetuned-confidential-', '')}</Text> : '-') },
            { title: '发送', dataIndex: 'sent', width: '9%' },
            { title: 'L2 判定', dataIndex: 'l2_total', width: '11%' },
            { title: '一致率', width: '12%', align: 'center',
              /* 按显示精度（1 位小数）判定，不按原始值：agree_rate=0.9796 显示
                 "98.0%" 却因 0.9796<0.98 染成橙色，看着像 bug（实测有 2 轮如此）。
                 颜色只表达「这轮是否够绿」，与用户看到的数字自洽即可 */
              render: (_, r) => (r.agree_rate == null ? '-'
                : <Text type={Number((r.agree_rate * 100).toFixed(1)) >= 98 ? 'success' : 'warning'}>
                  {(r.agree_rate * 100).toFixed(1)}%</Text>) },
            /* 四格混淆矩阵：拆成 4 列而不是一列 "12 / 3 / 0 / 45"——合并写法看不出
               哪个是影子漏报，而漏报方向是要重点盯的。cn=主密影放(漏报嫌疑)、
               nc=主放影密(误报嫌疑)，用色与下方环形图一致 */
            { title: <Tooltip title="主判密 · 影密（一致）"><Text type="success">主密影密</Text></Tooltip>,
              width: '6%', align: 'center',
              render: (_, r) => <Text type={(r.cells || {}).cc ? 'success' : undefined}>{(r.cells || {}).cc ?? 0}</Text> },
            { title: <Tooltip title="主判密 · 影放 —— 影子漏报嫌疑"><Text type="warning">主密影放</Text></Tooltip>,
              width: '6%', align: 'center',
              render: (_, r) => <Text type={(r.cells || {}).cn ? 'warning' : undefined}>{(r.cells || {}).cn ?? 0}</Text> },
            { title: <Tooltip title="主判放 · 影密 —— 影子误报嫌疑"><Text type="danger">主放影密</Text></Tooltip>,
              width: '6%', align: 'center',
              render: (_, r) => <Text type={(r.cells || {}).nc ? 'danger' : undefined}>{(r.cells || {}).nc ?? 0}</Text> },
            { title: <Tooltip title="主判放 · 影放（一致）"><Text type="secondary">主放影放</Text></Tooltip>,
              width: '6%', align: 'center',
              render: (_, r) => <Text type="secondary">{(r.cells || {}).nn ?? 0}</Text> },
          ]} />
      </Card>
      <JobCard />
      {pendingNew > 0 && (
        <Alert type="info" showIcon style={{ marginBottom: 8 }}
          message={`${pendingNew} 条新分歧`}
          action={<Button size="small" type="primary"
            onClick={() => { setPendingNew(0); loadDivs(); }}>查看</Button>} />
      )}
      <Card title="分歧样本"
        extra={
          <Space wrap>
            <Select size="small" value={cellF} onChange={setCellF} style={{ width: 150 }}
              options={[
                { value: '', label: '全部方向' },
                { value: 'nc', label: 'nc 主放·影密' },
                { value: 'cn', label: 'cn 主密·影放' },
              ]} />
            <Select size="small" value={labeledF} onChange={setLabeledF} style={{ width: 150 }}
              options={[
                { value: 'all', label: '全部标注' },
                { value: 'labeled', label: '已标注' },
                { value: 'unlabeled', label: '未标注' },
                { value: 'qwenwrong', label: '主判错的（改prompt）' },
              ]} />
            <Input.Search size="small" allowClear placeholder="搜内容/文件名" style={{ width: 200 }}
              onSearch={(v) => setQ(v)} />
            <Button size="small" icon={<DownloadOutlined />}
              onClick={() => window.open(`/admin/api/l2-divergences/export?${win}&cell=${encodeURIComponent(cellF)}&labeled=${labeledF === 'qwenwrong' ? 'labeled' : labeledF}`)}>
              导出 JSONL
            </Button>
          </Space>
        }>
        {shown.length ? (
          <Table rowKey="req_id" className="tbl-divs" size="small" columns={cols} dataSource={shown}
            pagination={{ pageSize: 20, showSizeChanger: false }} />
        ) : (
          <Alert type="info" showIcon
            message="本窗口无分歧样本（主影判定全部一致，或窗口内无 L2 落库数据）" />
        )}
      </Card>
    </div>
  );
}
