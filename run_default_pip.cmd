@echo off
:: 若使用需要 API Key 的 Provider，请先设置环境变量：
:: set GENSOKYOAI_API_KEY=your-api-key
:: 首次启动会自动从 tmp\template-conf.yaml 生成 config\local.yaml
python -m GensokyoAI.cli.main --character "characters\zh_cn\KirisameMarisa.yaml" --config "config\local.yaml" --new-session
