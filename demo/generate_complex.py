"""
生成复杂文档样例：多Sheet+合并单元格+公式+透视
运行: python demo/generate_complex.py
"""
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

wb = openpyxl.Workbook()

# Sheet1: 工资汇总（合并标题+财务表）
ws1 = wb.active
ws1.title = "工资汇总"
ws1.merge_cells("A1:E1")
ws1["A1"] = "2024年度工资汇总表（机密）"
ws1["A1"].font = Font(bold=True, size=14)
ws1["A1"].alignment = Alignment(horizontal="center")
ws1.append([])  # 空行
ws1.append(["部门", "姓名", "身份证号", "基本工资", "实发工资"])
for row in [
    ["研发部", "张三", "110101199001011234", 30000, "=D4*0.9"],
    ["研发部", "李四", "110101199002021235", 28000, "=D5*0.9"],
    ["财务部", "王五", "110101199003031236", 35000, "=D6*0.9"],
]:
    ws1.append(row)
# 合计
ws1.append(["合计", "", "", "=SUM(D4:D6)", "=SUM(E4:E6)"])

# Sheet2: 预算明细（多表头）
ws2 = wb.create_sheet("预算明细2024")
ws2.append(["项目", "Q1预算", "Q2预算", "Q3预算", "备注"])
ws2.append(["人力成本", 500000, 520000, 550000, "含工资"])
ws2.append(["研发投入", 800000, 850000, 900000, "机密"])
ws2.append(["合计", "=SUM(B2:B3)", "=SUM(C2:C3)", "=SUM(D2:D3)", ""])

# Sheet3: 普通公开数据（应放行）
ws3 = wb.create_sheet("公开资料")
ws3.append(["产品", "销量", "地区"])
ws3.append(["A", 100, "华东"])
ws3.append(["B", 200, "华南"])

wb.save("demo/complex_financial.xlsx")
print("已生成 demo/complex_financial.xlsx (3Sheet, 合并, 公式)")

# 生成复杂 PDF：扫描模拟（需 reportlab）
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    c = canvas.Canvas("demo/complex_drawing.pdf", pagesize=A4)
    c.setFont("Helvetica", 10)
    # 模拟多页图纸
    for i in range(1, 4):
        c.drawString(50, 800, f"Design Drawing - Project Alpha - Sheet {i}/3")
        c.drawString(50, 780, f"Drawing No: A01-2024-{i:02d}  confidential - internal only")
        c.drawString(50, 760, f"Section {i}: Structural Plan - confidential data, do not distribute")
        c.drawString(50, 740, "Watermark: 机密 保密")
        c.showPage()
    c.save()
    print("已生成 demo/complex_drawing.pdf (3页, 水印)")
except Exception as e:
    print(f"PDF 生成需 reportlab: {e}")
