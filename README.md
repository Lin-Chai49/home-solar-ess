# 户用光伏储能

本地跑的家用光伏 + 储能模拟。只用 Python 3 标准库，浏览器打开就能看。

默认按光伏/用电的短期预测和峰谷电价自动排充放。十六串电芯单独监视：充电看电压最高那节，放电看最低那节。可以选配大模型出建议，功率仍受电量窗口和电芯保护限制。

当前是模拟数据，没有接真逆变器。

## 运行

```bash
python3 server.py
```

浏览器打开 http://127.0.0.1:8765

需要 Python 3.9 或更高，不用装别的库。

## 页面上能做什么

- 自动 / 自发自用 / 谷充峰放 / 手动 / 停机
- 改装机容量、电价、能不能上网
- 十六串电芯柱状图，均衡可自动、被动、主动或关掉
- 后面十二小时的充放计划

电费是按你填的单价估算的，不是电力公司账单。第 7 节电芯容量故意设低一点，方便看出均衡。

核对模拟：

```bash
python3 server.py check
```

## 大模型（可选）

复制 `llm.example.json` 为 `data/llm.json`，填入密钥：

```json
{
  "enabled": true,
  "base_url": "https://api.openai.com/v1",
  "api_key": "你的密钥",
  "model": "gpt-4o-mini",
  "timeout": 8
}
```

也可用环境变量 `ESS_LLM_KEY`、`ESS_LLM_URL`、`ESS_LLM_MODEL`。

接口兼容 OpenAI 的 `/v1/chat/completions`（DeepSeek、通义、本地 vLLM 改一下地址即可）。模型只出建议：

```json
{"p_kw": -1.2, "why": "谷电且晚高峰还缺电", "balance": "auto"}
```

`p_kw` 放电为正、充电为负。没配密钥时不会发请求。

- 状态：`GET /api/llm`
- 立刻问一次：`POST /api/llm`

## 目录

```
server.py          模拟、调度、电芯、HTTP
llm.py             大模型接入
llm.example.json   密钥模板
web/               页面
说明.txt           本地短说明
```

`data/` 是本机配置和密钥，不进仓库。
