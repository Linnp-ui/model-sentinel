"""把第 4/5 章的 Mermaid 图渲染为 PNG，并把 md 中的代码块替换为图片；
md 已替换时，直接从 mermaid-src/ 源码重渲染全部图

依赖（临时环境，不入库）：
  npm i @mermaid-js/mermaid-cli        # 见 /tmp/opencode/mermaid
  mmdc 通过 puppeteer 调系统 Chrome；本机路径见下

用法:
  PATH=$HOME/.node-v22/bin:$PATH /tmp/opencode/presidio-venv/bin/python \
      scripts/render_thesis_diagrams.py [--dry]

环境变量:
  MMDC        mmdc 可执行文件（默认 /tmp/opencode/mermaid/node_modules/.bin/mmdc）
  PUPPETEER_CFG  puppeteer 配置（默认 /tmp/opencode/mermaid/puppeteer.json）
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, "docs", "thesis")
FIG = os.path.join(D, "figures")
SRC = os.path.join(D, "mermaid-src")
MMDC = os.environ.get("MMDC", "/tmp/opencode/mermaid/node_modules/.bin/mmdc")
PUP = os.environ.get("PUPPETEER_CFG", "/tmp/opencode/mermaid/puppeteer.json")

FILES = ["12-ch4-design.md", "08-ch5-implementation.md"]
SLUG = {
    "4-1": "architecture", "4-2": "sequence", "4-3": "deploy",
    "5-1": "l1-flow", "5-2": "l2-flow",
}
SIZE = {"4-2": 1800}


def main() -> None:
    dry = "--dry" in sys.argv
    os.makedirs(FIG, exist_ok=True)
    os.makedirs(SRC, exist_ok=True)
    pat = re.compile(
        r"\*\*图 (\d+-\d+) ([^*\n]+)\*\*\n\n```mermaid\n(.*?)```", re.S)
    rendered = set()

    def render(num: str, mmd: str, png: str) -> None:
        if dry:
            return
        cmd = [MMDC, "-i", mmd, "-o", png, "--size", str(SIZE.get(num, 1600)),
               "-s", "2", "-b", "white", "-p", PUP]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"mmdc 失败 {num}: {r.stderr[-500:]}")
        assert os.path.getsize(png) > 5000, f"产物过小: {png}"

    for fn in FILES:
        p = os.path.join(D, fn)
        s = open(p, encoding="utf-8").read()

        def repl(m):
            num, title, code = m.group(1), m.group(2).strip(), m.group(3)
            slug = SLUG.get(num, num.replace("-", "_"))
            mmd = os.path.join(SRC, f"fig{num}.mmd")
            png = os.path.join(FIG, f"fig{num}-{slug}.png")
            open(mmd, "w", encoding="utf-8").write(code)
            print("render", num, "->", os.path.basename(png))
            render(num, mmd, png)
            rendered.add(num)
            return f"![图 {num} {title}](figures/fig{num}-{slug}.png)\n\n**图 {num} {title}**"

        new, n = pat.subn(repl, s)
        print(fn, "替换", n, "处")
        if not dry and n:
            open(p, "w", encoding="utf-8").write(new)

    # md 已替换为图片后，mermaid-src/ 是重新出图的唯一来源
    for name in sorted(os.listdir(SRC)):
        if not name.endswith(".mmd"):
            continue
        num = name[3:-4]
        if num in rendered:
            continue
        slug = SLUG.get(num, num.replace("-", "_"))
        png = os.path.join(FIG, f"fig{num}-{slug}.png")
        print("render", num, "->", os.path.basename(png))
        render(num, os.path.join(SRC, name), png)


if __name__ == "__main__":
    main()
