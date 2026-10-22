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
import { useTheme } from '../theme.jsx';

// 格子底 = rgba(22,119,255,alpha) 叠在卡片底上 ⇒ 实际呈现色 = 合成色。
// 文字色按合成色亮度选黑/白：浅色模式合成色始终够亮 ⇒ 恒黑（与原口径一致，
// 最深格 5.1:1）；深色模式低 alpha 的格子合成后接近卡片底 ⇒ 必须白字。
const BLUE = [22, 119, 255];
const lum = (c) => {
  const [r, g, b] = c.map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};

export default function DailyHeatmap({ points, unit = '次', testid, fill = false }) {
  const { isDark } = useTheme();
  const SURFACE = isDark ? [31, 31, 31] : [255, 255, 255];
  if (!points || !points.length) return null;
  const maxV = Math.max(...points.map((p) => p.v));
  // fill 模式：卡片体约 2:1（宽:高）⇒ 列数取接近方形的值（√(n×2)），
  // 行高再由 1fr 拉满卡高 ⇒ 格子接近正方，不像 15 列细长条。
  // 非 fill（统计页）保持原口径不动。
  const columns = fill
    ? `repeat(${Math.max(2, Math.round(Math.sqrt(points.length * 2)))}, minmax(0, 1fr))`
    : points.length > 14
      ? `repeat(${Math.ceil(points.length / 2)}, minmax(0, 1fr))`
      : 'repeat(auto-fit, minmax(64px, 1fr))';
  return (
    // fill：占满卡片剩余高度（30d 窗下热力图只有两行，卡片下半空着）——行高跟着拉满
    <div data-testid={testid} style={{
      display: 'grid', gridTemplateColumns: columns, gap: 8,
      ...(fill ? { flex: 1, minHeight: 0, gridAutoRows: 'minmax(0, 1fr)' } : {}),
    }}>
      {points.map((p) => {
        // sqrt 映射：低流量下个别主导日（灌数/压测）会把线性映射的其他日全压成最浅色
        const f = maxV > 0 ? Math.sqrt(p.v / maxV) : 0;
        const alpha = p.v > 0 ? 0.15 + 0.85 * f : 0;
        const comp = SURFACE.map((s, i) => Math.round(s * (1 - alpha) + BLUE[i] * alpha));
        const fg = p.v > 0 ? (lum(comp) > 0.18 ? '#000000' : '#ffffff') : 'var(--gw-muted)';
        return (
          <div key={p.day} title={`${p.day}：${p.v} ${unit}`}
            style={{ background: p.v > 0 ? `rgba(22,119,255,${alpha.toFixed(3)})` : 'var(--gw-track)',
              borderRadius: 4, padding: '8px 4px', textAlign: 'center', color: fg,
              // 格子自身是查询容器 ⇒ 下面字号用 cqw 跟着格子宽度走：
              // 格子大了不再显得字太小，格子小了不溢出（固定 px 做不到）
              containerType: 'inline-size',
              // 拉高后内容垂直居中（否则数字贴顶，格子下半空）
              ...(fill ? { display: 'flex', flexDirection: 'column', justifyContent: 'center' } : {}) }}>
            <div style={{ fontSize: 'clamp(13px, 24cqw, 34px)', fontWeight: 600, lineHeight: 1.2 }}>{p.v || '·'}</div>
            <div style={{ fontSize: 'clamp(11px, 16cqw, 22px)', lineHeight: 1.3, whiteSpace: 'nowrap' }}>{p.day.slice(5)}</div>
          </div>
        );
      })}
    </div>
  );
}
