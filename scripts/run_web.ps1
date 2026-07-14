$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
& "C:\Users\salim\AppData\Local\Programs\Python\Python310\python.exe" -m uvicorn polyvoice.webapp:app --host 127.0.0.1 --port 8000
