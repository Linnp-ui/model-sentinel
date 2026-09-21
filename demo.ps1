Write-Host "=== Secure Gateway Demo 启动 ===" -ForegroundColor Cyan
Write-Host "1. 启动网关 (mock ollama, 无需GPU)"
docker compose up -d gateway postgres redis
Start-Sleep 3
Invoke-WebRequest http://localhost:8080/health | Select-Object -ExpandProperty Content

Write-Host "`n2. 测试文本 -> 外网 allow"
curl -s http://localhost:8080/v1/chat/completions -H "Authorization: Bearer pk_live_dev_changeme" -H "Content-Type: application/json" -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"总结CAP定理"}]}'

Write-Host "`n3. 测试财务表格 -> 内网 route_local"
python demo/generate_samples.py
curl -s http://localhost:8080/v1/files/check -H "Authorization: Bearer pk_live_dev_changeme" -F "file=@demo/salary_2024.xlsx"

Write-Host "`n4. 测试图纸PDF -> 内网 route_local"
curl -s http://localhost:8080/v1/files/check -H "Authorization: Bearer pk_live_dev_changeme" -F "file=@demo/drawing_A01.pdf"

Write-Host "`n打开 http://localhost:8080/ 和 http://localhost:8080/admin 查看"
