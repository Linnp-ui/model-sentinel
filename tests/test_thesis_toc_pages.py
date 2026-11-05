"""build_thesis_docx 目录页码估算的纯函数检查（不依赖 soffice，不生成 docx）。"""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bt():
    spec = importlib.util.spec_from_file_location(
        "build_thesis_docx", os.path.join(ROOT, "scripts", "build_thesis_docx.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bt = _bt()


def test_toc_page_disp_basic_and_toc_lines_ignored():
    """目录条目行（标题后带点线引导符）不得命中；摘要节 lowerRoman、正文 decimal。"""
    heads = [(1, "摘要"), (1, "Abstract"), (1, "第 1 章 绪论"), (2, "1.1 研究背景")]
    pages = [
        ["（学校名称）"],
        ["摘要", "大语言模型在企业里的普及带出了新的数据出境风险。"],
        ["Abstract", "As large language models spread through enterprises."],
        ["目录", "摘要........................",
         "Abstract..........",
         "第1 ", "章绪论....",
         "1.1 研究背景............"],
        ["第1 ", "章绪论", "1.1 研究背景", "构建在Transformer架构上的大语言模型。"],
    ]
    assert bt.toc_page_disp(pages, heads) == ["i", "ii", "1", "1"]


def test_toc_page_disp_crossref_paragraph_ignored():
    """正文概述段的整段交叉引用（「5.2 鉴权、…5.3 文件预检、…」长行）不得命中。"""
    heads = [(1, "摘要"), (1, "Abstract"), (1, "第 1 章 系统实现"),
             (2, "1.2 鉴权、配额与限流"), (2, "1.3 文件预检模块")]
    pages = [
        ["摘要", "本文设计了系统。"],
        ["Abstract", "This thesis designs a system."],
        ["目录", "1.2 鉴权、配额与限流....", "1.3 文件预检模块...."],
        ["第 1 章 系统实现",
         "本章正文按请求穿过管道的顺序展开：1.2 鉴权是请求的第一关；",
         "1.3 文件预检、1.4 L1 规则引擎构成审查与决策链。"],
        ["1.2 鉴权、配额与限流", "鉴权在auth()中完成。",
         "1.3 文件预检模块", "文件内容到达网关的主要形态是请求体内联。"],
    ]
    # 1.2/1.3 的真实标题在第 4 页（逻辑页 2）；第 3 页概述段的交叉引用不得抢先命中
    assert bt.toc_page_disp(pages, heads) == ["i", "ii", "1", "2", "2"]


def test_toc_page_disp_missing_heading_fails():
    heads = [(1, "摘要"), (1, "Abstract"), (1, "第 1 章 绪论")]
    pages = [["摘要", "正文。"], ["Abstract", "Body."]]
    with pytest.raises(SystemExit):
        bt.toc_page_disp(pages, heads)
