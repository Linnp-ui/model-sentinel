// stats/chart 分桶序列 → 按日（北京时区）聚合调用量。
// 全部窗口恒 8640 桶；逐桶 toLocaleDateString({timeZone}) 走 ICU 时区格式化
// 实测 ~50µs/次 → 单次数百 ms 冻结主线程（切窗遮罩"慢半拍"根因，2026-10-12 实锤）。
// Asia/Shanghai 自 1991 起恒 UTC+8 无夏令时 → +8h 后 toISOString 取前 10 位，µs 级。
export const dailyPointsFromChart = (ch) => {
  const m = new Map();
  (ch.ts || []).forEach((t, i) => {
    const day = new Date((t + 28800) * 1000).toISOString().slice(0, 10);
    m.set(day, (m.get(day) || 0) + ((ch.counts && ch.counts[i]) || 0));
  });
  return [...m.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1)).map(([day, v]) => ({ day, v }));
};

// 日粒度活动热力（GitHub 风格）：低流量窗口的主视图。
// 全窗只有十余个活跃时刻时，8640 桶折线只是平 0 线；每格一天画调用量，一眼看分布。
export default function DailyHeatmap({ points, unit = '次', testid }) {
  if (!points || !points.length) return null;
  const maxV = Math.max(...points.map((p) => p.v));
  return (
    <div data-testid={testid} style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(64px, 1fr))', gap: 8 }}>
      {points.map((p) => {
        // sqrt 映射：低流量下个别主导日（灌数/压测）会把线性映射的其他日全压成最浅色
        const f = maxV > 0 ? Math.sqrt(p.v / maxV) : 0;
        return (
          <div key={p.day} title={`${p.day}：${p.v} ${unit}`}
            style={{ background: p.v > 0 ? `rgba(22,119,255,${(0.15 + 0.85 * f).toFixed(3)})` : '#f5f5f5',
              borderRadius: 4, padding: '8px 4px', textAlign: 'center', color: p.v > 0 && f > 0.5 ? '#fff' : '#666' }}>
            <div style={{ fontSize: 14, fontWeight: 600 }}>{p.v || '·'}</div>
            <div style={{ fontSize: 11 }}>{p.day.slice(5)}</div>
          </div>
        );
      })}
    </div>
  );
}
