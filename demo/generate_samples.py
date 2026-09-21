"""
生成演示用 财务表格 + 图纸PDF
运行: python demo/generate_samples.py
"""
import os

def gen_excel():
    try:
        import openpyxl
    except ImportError:
        print("pip install openpyxl 后重试")
        return
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "工资表2024"
    ws.append(["姓名","身份证号","部门","工资","银行账号"])
    ws.append(["张三","110101199001011234","研发部", 35000, "6217001234567890123"])
    ws.append(["李四","110101199002021235","财务部", 28000, "6217001234567890124"])
    ws.append(["王五","110101199003031236","设计部", 42000, "6217001234567890125"])
    os.makedirs("demo", exist_ok=True)
    wb.save("demo/salary_2024.xlsx")
    print("已生成 demo/salary_2024.xlsx")

def gen_pdf():
    try:
        from pypdf import PdfWriter
        from pypdf.generic import NameObject, TextStringObject
    except ImportError:
        print("pip install pypdf 后重试")
        return
    # 用最简方式生成一个含文本的PDF（依赖 reportlab 更好，这里手写最小PDF）
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4
        os.makedirs("demo", exist_ok=True)
        c = canvas.Canvas("demo/drawing_A01.pdf", pagesize=A4)
        c.setFont("Helvetica", 12)
        c.drawString(100, 800, "Design Drawing - A01  Structural Plan")
        c.drawString(100, 780, "Project: XX Commercial Complex")
        c.drawString(100, 760, "Confidential - Internal Use Only")
        c.drawString(100, 740, "Watermark: 机密 保密")
        c.drawString(100, 720, "Drawing No: SM-2024-A01")
        c.drawString(100, 700, "This document contains confidential design data, do not distribute.")
        c.showPage()
        c.save()
        print("已生成 demo/drawing_A01.pdf")
    except ImportError:
        # fallback: 空PDF
        print("pip install reportlab 后可生成更逼真的图纸PDF，当前跳过")
        open("demo/drawing_A01.pdf","wb").write(b"%PDF-1.4 minimal")

if __name__ == "__main__":
    gen_excel()
    gen_pdf()
