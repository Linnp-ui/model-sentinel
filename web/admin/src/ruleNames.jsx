// 2026-10-14 起 policy.yaml 规则名本身即中文，旧英文→中文映射作废；
// 本文件只剩内部系统规则 + 历史旧名 + 空值兜底的映射。
import { Tooltip, Typography } from 'antd';
import { InfoCircleOutlined } from '@ant-design/icons';

export const RULE_ZH = {
  default_allow: '默认放行',
  allow: '放行',
  route_local: '本地路由',
  block: '阻断',
  l2_confidential: 'L2 判密',
  l2_unavailable: 'L2 不可用',
  first_chunk_fallback: '首块转内网',
  prompt_too_long_fallback: '长提示转内网',
  key_blacklist: '密钥黑名单',
  encoded_keyword: '编码关键词转内网',
  review_text_overflow: '超审查预算转内网',
  // 历史旧规则名→现名
  financial_local_only: '财务表转内网',
  drawing_local_only: '图纸转内网',
  pii_weighted_route_local: '隐私加权转内网',
  pii_critical_block: '高危隐私拦截',
  block_secrets: '拦截密钥',
  session_route_local: '会话机密转内网',
  tainted_file_ref: '文件名转内网',
  '未解析二进制转内网': '二进制转内网',
  '文件名污染转内网': '文件名转内网',
  empty_image: '空图转内网',
};

// 系统内置判定：网关自己的 fail-safe / 降级 / L2 结论，不是 policy.yaml 里的可配置规则
const SYSTEM_RULES = new Set([
  'l2_confidential', 'l2_unavailable',
  'encoded_keyword', 'review_text_overflow',
  'first_chunk_fallback', 'prompt_too_long_fallback',
]);

// 动作（audit_logs.action）→ [中文名, Tag 底色]：审计页与总览降级事件流共用一张表
export const ACTION_INFO = {
  block: ['阻断', 'red'],
  route_local: ['本地路由', 'orange'],
  allow: ['放行', 'green'],
};

// 可作为审计页「规则」筛选项的内部/历史规则名（policy 规则名由 /policies 动态给出）。
// 排除 allow/route_local/block —— 那是 action 值，不是规则名。
export const RULE_FILTER_NAMES = Object.keys(RULE_ZH).filter((k) => !['allow', 'route_local', 'block'].includes(k));

export function ruleZh(name) {
  if (!name) return '—';
  return RULE_ZH[name] || name;
}

// ---------------- 系统内置判定：标记 + 说明 ----------------
// 旧口径是把「（系统内置）」直接拼在名字后面（"首块转内网（系统内置）"）⇒ 名字变长、
// 表格里挤。现在改成：名字后跟一个 ⓘ 标记，悬停给说明。
export const SYSTEM_RULE_HINT =
  '系统内置判定：网关自己的 fail-safe / 降级 / L2 结论，不是 policy.yaml 里可配置的规则（规则管理页里找不到它）';

export function isSystemRule(name) {
  return !!name && SYSTEM_RULES.has(name);
}

// 标记本身（ⓘ 图标，悬停出说明）
export function SystemBuiltinMark() {
  return (
    <Tooltip title={SYSTEM_RULE_HINT}>
      <InfoCircleOutlined aria-label="系统内置判定" data-testid="system-builtin-mark"
        style={{ color: 'var(--gw-muted)', fontSize: 12, marginInlineStart: 4 }} />
    </Tooltip>
  );
}

// 规则名 + （系统内置时）标记。字符串场景用 ruleZh，界面展示用这个组件。
export function RuleZh({ name, fallback = '—' }) {
  if (!name) return fallback;
  return (
    <>
      {RULE_ZH[name] || name}
      {isSystemRule(name) ? <SystemBuiltinMark /> : null}
    </>
  );
}

// 图例：把「标记代表什么」写在明处（光看 ⓘ 不知道含义）
export function SystemBuiltinLegend() {
  return (
    <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 8 }}>
      <InfoCircleOutlined style={{ marginInlineEnd: 4 }} />
      ⓘ 系统内置判定：网关自己的 fail-safe / 降级 / L2 结论，不是可配置的规则（规则管理页里没有它）
    </Typography.Text>
  );
}

