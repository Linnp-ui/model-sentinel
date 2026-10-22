import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import { Button, ConfigProvider, Tooltip, theme as antdTheme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { DesktopOutlined, MoonOutlined, SunOutlined } from '@ant-design/icons';

// 主题模式：system（跟随系统，默认）/ light / dark，选择存 localStorage（gw-theme）。
// 深浅判定只此一处：antd 算法（ConfigProvider）、CSS 变量层（<html data-theme>）、
// Sider 主题、uPlot 轴色、热力图合成底色全从这里取，别处各自 matchMedia 会走偏。
// index.html 里有同口径的内联脚本（首屏前定 data-theme，否则深色用户闪一下白底）。
const KEY = 'gw-theme';
const Ctx = createContext(null);

const readMode = () => {
  try {
    const v = localStorage.getItem(KEY);
    return v === 'light' || v === 'dark' ? v : 'system';
  } catch (e) {
    return 'system';
  }
};

const systemDark = () => typeof window !== 'undefined'
  && window.matchMedia('(prefers-color-scheme: dark)').matches;

// 深色下 antd 走 darkAlgorithm；语义色/主色换深色档——浅底上的深色文字
// （#389e0d 等）在深底上不可读，对比度是反的。
const TOKEN = {
  light: {
    colorPrimary: '#2f54eb',
    colorLink: '#2f54eb',        // 与主色一致：antd 默认 #1677ff 在白底 4.1:1 不达 WCAG AA
    colorTextDescription: 'rgba(0, 0, 0, 0.6)',  // 默认 0.45 在白底约 3.35:1（说明文字/统计标题/菜单分组），不达 AA
    colorSuccess: '#389e0d',   // 色阶亮色只留给图形；文本/图标用深一档（3.46:1）
    colorWarning: '#d46b08',
    colorError: '#cf1322',
    colorBgLayout: '#f5f6f8',
  },
  dark: {
    colorPrimary: '#597ef7',
    colorLink: '#597ef7',
    colorTextDescription: 'rgba(255, 255, 255, 0.65)',
    colorSuccess: '#95de64',
    colorWarning: '#ffc069',
    colorError: '#ff7875',
    colorBgLayout: '#141414',
  },
};
const COMMON = { fontSize: 14, fontSizeSM: 13, borderRadius: 8, motionDurationMid: '0.2s' };

export function ThemeProvider({ children }) {
  const [mode, setModeState] = useState(readMode);
  const [sysDark, setSysDark] = useState(systemDark);

  // system 模式跟随系统实时变化；手动模式不听系统
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const on = (e) => setSysDark(e.matches);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);

  const isDark = mode === 'dark' || (mode === 'system' && sysDark);

  // CSS 变量层靠 <html data-theme> 切换（theme.css 用 [data-theme='dark'] 选规则）
  useEffect(() => {
    document.documentElement.dataset.theme = isDark ? 'dark' : 'light';
  }, [isDark]);

  const value = useMemo(() => ({
    mode,
    isDark,
    setMode: (m) => {
      setModeState(m);
      try { localStorage.setItem(KEY, m); } catch (e) { /* 隐私模式写不了：本次会话内仍生效 */ }
    },
  }), [mode, isDark]);

  return (
    <Ctx.Provider value={value}>
      <ConfigProvider locale={zhCN} theme={{
        algorithm: isDark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
        token: { ...COMMON, ...(isDark ? TOKEN.dark : TOKEN.light) },
        components: {
          Table: { headerBg: isDark ? '#1d1d1d' : '#FAFAFA' },
          Card: { paddingLG: 16 },
        },
      }}>
        {children}
      </ConfigProvider>
    </Ctx.Provider>
  );
}

export const useTheme = () => useContext(Ctx);

// 顶栏切换按钮：三态循环（跟随系统 → 浅色 → 深色），图标=当前状态，
// tooltip 说当前状态与下一状态（图标按钮必须有可访问名）。
const MODES = [
  { key: 'system', label: '跟随系统', icon: <DesktopOutlined /> },
  { key: 'light', label: '浅色', icon: <SunOutlined /> },
  { key: 'dark', label: '深色', icon: <MoonOutlined /> },
];

export function ThemeToggle() {
  const { mode, setMode } = useTheme();
  const i = Math.max(0, MODES.findIndex((m) => m.key === mode));
  const cur = MODES[i];
  const next = MODES[(i + 1) % MODES.length];
  const tip = `主题：${cur.label}（点击切换为${next.label}）`;
  return (
    <Tooltip title={tip}>
      <Button type="text" aria-label={tip} icon={cur.icon} onClick={() => setMode(next.key)} />
    </Tooltip>
  );
}
