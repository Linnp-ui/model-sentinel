#!/usr/bin/env python3
"""管理台无头验收模板（复制后填 check()，别从零写——常见坑已内置）：

  cp scripts/ui_check.py /tmp/opencode/check_xxx.py
  .venv/bin/python /tmp/opencode/check_xxx.py

内置坑位处理：
  - hash 导航不重载 SPA ⇒ open() 里 goto 后强制 reload（多 case 不串状态）
  - antd 两字中文按钮中间有空格（「导 出」）⇒ btn() 按字间 \\s* 正则匹配
  - 点展开后等 1.5s、不用 :visible 过滤（800ms+:visible 会 30s 超时）
  - 断言前查「页面加载失败」假阳性（ERR_TOO_MANY_RETRIES 懒加载 chunk 失败）
  - ignore_https_errors 在 new_context（不是 goto 参数）
"""
import re
from playwright.sync_api import sync_playwright

ENV = {}
for _line in open("/home/your-user/gateway/.env", encoding="utf-8"):
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1)
        ENV[_k] = _v
BASE = "https://127.0.0.1:8080"


class Page:
    def __init__(self, pg):
        self.pg = pg

    def open(self, route, wait=2500):
        """route: hash 路由，如 '/audit?q=春晓' 或 '/l2'"""
        self.pg.goto(BASE + "/admin/app#" + route, wait_until="networkidle")
        self.pg.reload(wait_until="networkidle")  # 坑①：hash 变化不重载 SPA
        self.pg.wait_for_timeout(wait)
        assert "页面加载失败" not in self.pg.content(), "页面加载失败：懒加载 chunk 失败，刷新重试"
        return self

    def click_query(self):
        self.btn("查询").click()
        self.pg.wait_for_timeout(1500)
        return self

    def expand(self, marker):
        """展开首个含 marker 的数据行，返回展开行全文（坑④：1.5s、不用 :visible）"""
        self.pg.locator("tbody tr.ant-table-row", has_text=marker).first \
            .locator(".ant-table-row-expand-icon").click()
        self.pg.wait_for_timeout(1500)
        return self.pg.locator("tr.ant-table-expanded-row").first.inner_text()

    def btn(self, label):
        """坑②：antd 两字中文按钮中间有空格（「导 出」），字间插 \\s* 匹配"""
        return self.pg.get_by_role("button", name=re.compile(r"\s*".join(label)))

    def row_text(self, marker, i=0):
        return self.pg.locator("tbody tr.ant-table-row", has_text=marker).nth(i).inner_text()


def check(pg):
    # ← 填这里：每个 case 先 p.open(route)，断言失败即停
    p = Page(pg)
    t = p.open("/audit?q=春晓").expand("春晓")
    assert "主判定" not in t, t[:200]  # 示例：主影一致行展开区隐藏主影判定


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path="/tmp/opencode/chrome/chrome", args=["--no-sandbox"])
        ctx = b.new_context(ignore_https_errors=True)
        pg = ctx.new_page()
        r = pg.request.post(BASE + "/admin/login", data={"password": ENV["AI_GATEWAY_ADMIN_PASSWORD"]})
        assert r.ok, f"login {r.status}"
        check(pg)
        b.close()
        print("ALL PASS")


if __name__ == "__main__":
    main()
