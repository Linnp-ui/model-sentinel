// L1 规则名 -> 中文显示（只改显示：后端 name/action 保持英文标识，审计历史与接口不受影响）。
// 未收录的新增规则回退显示原名。
export const RULE_ZH = {
  session_route_local: '\u4f1a\u8bdd\u673a\u5bc6\u8f6c\u5185\u7f51',
  financial_local_only: '\u8d22\u52a1\u8868\u8f6c\u5185\u7f51',
  ocr_empty_image_route_local: '\u7a7a\u56fe\u8f6c\u5185\u7f51',
  drawing_local_only: '\u56fe\u7eb8\u8f6c\u5185\u7f51',
  block_secrets: '\u62e6\u622a\u5bc6\u94a5',
  pii_critical_block: '\u9ad8\u5371\u9690\u79c1\u62e6\u622a',
  pii_weighted_route_local: '\u9690\u79c1\u52a0\u6743\u8f6c\u5185\u7f51',
  unparsed_binary_route_local: '\u672a\u89e3\u6790\u4e8c\u8fdb\u5236\u8f6c\u5185\u7f51',
  default_allow: '\u9ed8\u8ba4\u653e\u884c',
};

export function ruleZh(name) {
  if (!name) return '\u2014';
  return RULE_ZH[name] || name;
}
