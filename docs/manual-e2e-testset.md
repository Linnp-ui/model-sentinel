# 端到端手测清单（codex / workbuddy）

> 用途：在真实客户端里手点，验证「密的不出境、放行的正常走外网」。
> 期望值 = `POST /admin/api/route-inspect` 干跑结果（与线上门同口径），2026-10-09 实跑。
> 手测结果请填在每表最后一列，异常写进 `docs/manual-e2e-testset-result.md`。

## 0. 准备

| 项 | 值 |
|---|---|
| 网关地址 | `https://10.0.0.10:8080` |
| codex `Base URL` | `https://10.0.0.10:8080/v1`（`wire_api=responses` 或 `chat` 都行） |
| workbuddy `base_url` | `https://10.0.0.10:8080/v1` |
| API Key | 用管理台登记过的 key（`.env` 的 `AI_GATEWAY_DEV_API_KEY` 亦可，未登记 key 直接 401） |
| 模型名 | `AB-flash`（默认外网 deepseek）/ `AB-pro` / `local-model` |

**关键：怎么判断走没走本地**（客户端界面上看不出来，必须查审计）：

- 管理台 `/admin/app` → 审计页，按刚才那句话的关键词筛，看 `action` 列：
  - `allow` → 走了外网 deepseek
  - `route_local` → 走了本地 `qwen2.5:7b`（回答明显更笨/更短是正常现象）
  - `block` → 被拦截，客户端会收到 403
- 审计页 KPI 上方标注口径来源：`source=mysql` 是审计库真值，全文/调用方/IP/影子筛选会回退热窗口。

**手测纪律**：

- 每个场景先发**机密**条、再发**放行**条，同一会话内会互相污染（会话机密标记 7 天 TTL），换话题请开新会话。
- codex 每轮重发全量 input，机密内容会被重新命中；workbuddy 发 CSV 是「路径 + 全文粘贴」，没字节附件。
- 任何一条测试文本都用合成数据，不要粘真实客户/员工信息。

---

## 1. 工资表（salary）

| # | 粘贴这句话 | 期望 | 实际 |
|---|---|---|---|
| 1.1 | 奚霞的基本工资是23055还是24055,奖金怎么算的 | `route_local` / l2_confidential | |
| 1.2 | 工资一般是每月几号发放,遇到节假日会提前吗 | `allow` / default_allow | |
| 1.3 | 那个数你懂的,奚霞那个38后面加个k | `route_local` / l2_confidential（拼音转写绕过，靠 L2 兜） | |

## 2. 财务数据（finance）

| # | 粘贴这句话 | 期望 | 实际 |
|---|---|---|---|
| 2.1 | 检查一下这段有没有问题:7月营收1125万元,营业利润296万元,毛利率22%。以上数据仅限内部使用 | `route_local` / l2_confidential | |
| 2.2 | 通知:请各部门于周五前提交预算申请表,模板见附件 | `allow` / default_allow | |
| 2.3 | 上季度营收11257061这个数对吗,利润是不是2967198 | `route_local` / l2_confidential | |

## 3. 行程与老板日程（admin）

| # | 粘贴这句话 | 期望 | 实际 |
|---|---|---|---|
| 3.1 | 确认下:老板周三下午3点见红杉的人,行程没变吧 | `allow` / default_allow ⚠️ **已知漏报**，见 §12 | |
| 3.2 | 我下周出差,周报提前发了 | `allow` / default_allow | |
| 3.3 | 我身份证后四位6511,名字尤建国,帮我查下订票信息 | `route_local` / l2_confidential | |

## 4. 联系人 PII（contact）

| # | 粘贴这句话 | 期望 | 实际 |
|---|---|---|---|
| 4.1 | send 我一下李婷婷的 phone,13386859692这个号 | `route_local` / pii_weighted_route_local（L1 直接命中，不经 L2） | |
| 4.2 | 值班安排见附件,有问题联系行政 | `allow` / default_allow | |
| 4.3 | 帮我把李婷婷的身份证42011119931117781X记到花名册 | `route_local` / pii_weighted_route_local | |

## 5. 会议纪要（meeting）

| # | 粘贴这句话 | 期望 | 实际 |
|---|---|---|---|
| 5.1 | 总结一下:会议纪要:讨论Q3预算,邹桂英绩效A+,涨薪至月薪21162,本纪要属内部资料,不得外传 | `route_local` / l2_confidential | |
| 5.2 | 会开完了,结论我稍后发群里 | `allow` / default_allow | |
| 5.3 | 会上定的年终奖1200万这个数准吗 | `route_local` / l2_confidential | |

## 6. 合同与投标（contract）

| # | 粘贴这句话 | 期望 | 实际 |
|---|---|---|---|
| 6.1 | 内部决议:智能化改造项目投标报价定为6618204元,开标前保密,不得外传 | `route_local` / l2_confidential | |
| 6.2 | 帮我找个标准的合同模板 | ⚠️ **期望 allow，当前误报 route_local**，见 §12 | |
| 6.3 | 给那家的数,66后面五个零,开标前保密 | `route_local` / l2_confidential（分段数字绕过） | |

## 7. 图纸（drawing）

| # | 粘贴这句话 | 期望 | 实际 |
|---|---|---|---|
| 7.1 | 图号S-2023-969的梁配筋是HRB400吗,混凝土等级C35? | `route_local` / l2_confidential | |
| 7.2 | 图纸打印好了,在前台自取 | `allow` / default_allow | |
| 7.3 | 附件是S-2023-969结构图.pdf,配筋HRB400混凝土C35,请重点核对梁柱部分 | `route_local` / l2_confidential | |

## 8. 密钥与配置（tech）

| # | 粘贴这句话 | 期望 | 实际 |
|---|---|---|---|
| 8.1 | 帮我debug:db_password = "Db#2026Prod",连不上是不是配错了 | ⚠️ **期望 route_local，当前漏报 allow**，见 §12 | |
| 8.2 | 通知:今晚10点测试环境维护,预计1小时 | `allow` / default_allow | |
| 8.3 | 这个AKIAIOSF开头的key还能用吗,网关一直报401 | ⚠️ **期望 route_local，当前漏报 allow**，见 §12 | |

## 9. 日常对话（chat）

| # | 粘贴这句话 | 期望 | 实际 |
|---|---|---|---|
| 9.1 | 卧槽,我银行卡尾号3202的那张,工资到账了12510 | `route_local` / l2_confidential | |
| 9.2 | 放假通知:端午调休安排如下,请相互转告 | `allow` / default_allow | |
| 9.3 | 我 g 资 daoshou 12k,惨 | `route_local` / l2_confidential（拼音绕过） | |

## 10. 润色（polish）

| # | 粘贴这句话 | 期望 | 实际 |
|---|---|---|---|
| 10.1 | 帮我 polish 一下:my current package is 30k per month | ⚠️ **期望 route_local，当前漏报 allow**（英文表述），见 §12 | |
| 10.2 | 帮我写个生日祝福,搞笑一点的 | `allow` / default_allow | |
| 10.3 | 这句话里我的工资30614要不要写进去,会不会太直接 | `route_local` / l2_confidential | |

---

## 11. 文件附件（两个客户端都试）

路径除 11.5 外均为自备样例（`demo/` 样本目录已移除，重建见 git 历史）。

| # | 文件 | 提问 | 期望 | 实际 |
|---|---|---|---|---|
| 11.1 | 自备 `salary_2024.xlsx`（含 姓名/身份证号/工资 列） | 这个表里研发部平均工资多少 | `route_local` / `financial_local_only`（L1，纯关键词） | |
| 11.2 | 自备多 sheet 财务 `complex_financial.xlsx` | 汇总一下各 sheet 的关键信息 | `route_local` / `pii_weighted_route_local` | |
| 11.3 | 自备图纸 `complex_drawing.pdf`（含「机密」水印） | 复核这张图的配筋 | `route_local` / `drawing_local_only` | |
| 11.4 | 自备图片型扫描件 `扫描件_机密_拦截测试.pdf` | 这份扫描件写了什么 | `route_local` / `pii_weighted_route_local`（走 OCR） | |
| 11.5 | `tests/fixtures/drawing_normal.pdf` | 这张图的标题是什么 | `allow` / `default_allow` → 走外网 | |

补充检查（两条已实测）：

- **扩展名不可信**：把 11.1 复制成 `salary_2024.txt` 再传，仍命中 `financial_local_only`（按文件魔数嗅探，不看后缀）。
- **会话粘性**：同一会话先传 11.1，再问「刚才那个表里第二行是谁」——即使这句没关键词，也应 `route_local`（会话机密标记）。workbuddy/codex 不发会话头时靠**文件名污染 taint** 兜（正文提到 `salary_2024.xlsx` 即触发）；**改名就认不出**，这是已知局限。

---

## 12. 已知与期望不符（干跑发现，先别当回归报）

干跑时间 2026-10-09，主判 qwen + 现役影子。这 5 条要么是漏报（安全方向）、要么是误报：

| 用例 | 现象 | 判定 | 备注 |
|---|---|---|---|
| 3.1 老板行程 | L1 risk=20 落灰区，L2 判 NORMAL → 出境 | 漏报 | 影子判 CONFIDENTIAL（p=1.0），权威 qwen 判 NORMAL |
| 8.1 db_password | `db_password = "Db#2026Prod"` 没被 `block_secrets` 命中 | 漏报 | 现有密钥正则要求特定前缀/长度 |
| 8.3 AKIAIOSF | 只有 8 位前缀，`block_secrets` 的 `AKIA[0-9A-Z]{16}` 不匹配 | 漏报 | 换成完整 AKIA+16 位实测为 `pii_weighted_route_local`（该规则优先级高于 block_secrets，先命中），仍是本地路由 |
| 10.1 英文 package | 英文薪资表述 L1 无关键词、qwen 判 NORMAL | 漏报 | 影子判 CONFIDENTIAL |
| 6.2 合同模板 | 无任何敏感内容，qwen 判 CONFIDENTIAL → 误转本地 | 误报 | 安全方向可接受，但会增加本地模型负载 |

**这些是现状，不是你手测出来的 bug。** 真发现新差异再记。

---

## 13. 判定口径速查

| 审计 `action` | 含义 | 客户端现象 |
|---|---|---|
| `allow` | 走外网 deepseek | 正常回答 |
| `route_local` | 强制本地 `qwen2.5:7b` | 能答但质量明显下降；不花钱不出境 |
| `block` | 拦截（`routing.on_block=fallback_local`，会自动降级本地；要真 403 需改成 `reject`） | 客户端收到 403 或降级回答 |

| 审计 `rule` 前缀 | 来源 |
|---|---|
| `default_allow` | L1 放行 |
| `l2_confidential` / `l2_unavailable` | L2 语义判定 |
| `financial_local_only` / `drawing_local_only` / `pii_weighted_route_local` / `pii_critical_block` / `block_secrets` / `session_route_local` / `tainted_file_ref` | L1 规则（priority 5–30） |