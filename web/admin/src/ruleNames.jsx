// 2026-10-14 起 policy.yaml 规则名本身即中文，旧英文→中文映射作废；
// 本文件只剩内部系统规则 + 历史旧名 + 空值兜底的映射。
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

export function ruleZh(name) {
  if (!name) return '—';
  return RULE_ZH[name] || name;
}

