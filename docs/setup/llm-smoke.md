# LLM 接入冒烟验证

记录 GNOSIS Agent 的模型接入实测结果：环境、端点形态、思考强度参数、两次真实调用的响应与耗时。

## 环境

Windows，Python 3.11.4 原生解释器，仓库根目录建立 venv：

```bash
cd "E:\AI\GNOSIS Agent"
python -m venv .venv
.venv/Scripts/python.exe -m pip install openai==3.24.0 python-dotenv==1.2.4
```

固定版本（实测安装结果）：

| 包 | 版本 |
| --- | --- |
| openai | 3.24.0 |
| python-dotenv | 1.2.4 |
| httpx2 | 2.13.1（openai 3.x 的传输层依赖） |
| pydantic | 2.13.5 |

openai 3.x 已不再依赖 `httpx`，改用 `httpx2`，排查网络问题时不要按旧版依赖去查 `httpx`。

## 配置

`.env` 位于仓库根目录，只使用三个键名，脚本通过 python-dotenv 在运行时读取，代码与文档都不出现键值：

| 键名 | 用途 |
| --- | --- |
| `MODER_PORVIDER` | OpenAI 兼容端点 base URL |
| `MODEL_API` | API key |
| `MODER_NAME` | 模型 ID |

`engine/llm_client.py` 在导入时加载 `.env`，缺少任一键会以 `KeyError` 就地崩溃，不做兜底。

## 端点形态

- Base URL：`https://api.commandcode.ai/provider/v1`
- 使用端点：`POST {base}/chat/completions`，请求与响应体遵循 OpenAI Chat Completions schema
- 鉴权：`Authorization: Bearer <MODEL_API>`
- 客户端构造：`OpenAI(base_url=MODER_PORVIDER, api_key=MODEL_API)`

响应头暴露的网关信息：

```
http/1.1 200
server: cloudflare
x-powered-by: Hono
cf-ray: <id>-SIN
server-timing: total;dur=1508.0;desc="Total Response Time"
x-trace-id: <id>
```

出口是 Cloudflare 新加坡节点，网关实现为 Hono。响应头里没有任何上游推理引擎标识（无 vllm 相关字段），无法从响应头推断服务端用的是什么引擎。

网络层的坑：用 `urllib` 之类裸 HTTP 客户端直连会拿到 403，响应体为 `error code: 1010`，这是 Cloudflare 按 User-Agent 拦截，不是鉴权失败。必须走 openai SDK（或其底层 `httpx2` 客户端）。

## 思考强度

参数名是 `reasoning_effort`，作为 Chat Completions 的顶层字段发送，OpenAI 风格写法直接可用。Command Code 的 provider 文档没有提到这个参数，参数名和取值集合都是实测确定的。

被接受的取值集合，来自 API 的原始报错：

```json
{
  "error": {
    "message": "Invalid option: expected one of \"off\"|\"low\"|\"medium\"|\"high\"|\"xhigh\"|\"max\"",
    "type": "invalid_request_error",
    "param": "reasoning_effort"
  }
}
```

即发送 `off`、`low`、`medium`、`high`、`xhigh`、`max` 中任意一个都不报错，其余值返回 400。这本身就说明字段被网关校验并向下透传，而不是被静默丢弃。

`engine/llm_client.py` 把默认值固定为 `high`。

参数确实作用于模型的直接证据（同一道鸡兔同笼题，逐次调用）：

```
effort=off   耗时  4.29s reasoning_tokens=   0 completion_tokens= 311 reasoning_content=无
effort=high  耗时  2.84s reasoning_tokens= 108 completion_tokens= 224 reasoning_content=有
effort=high  耗时  2.94s reasoning_tokens= 127 completion_tokens= 268 reasoning_content=有
effort=off   耗时  2.72s reasoning_tokens=   0 completion_tokens= 283 reasoning_content=无
```

`off` 时 `reasoning_tokens` 为 0 且不返回 `reasoning_content`；`high` 时两者都出现。

`high` 与 `low` 之间的差异不稳定，单次调用中 `low` 反而可能给出更多推理 token（实测 low 135、high 108~131），所以在这个模型上无法用 token 数区分 high 与 low，只能确认思考开启与关闭。

响应里模型侧返回的思考内容在 `choices[0].message.reasoning_content`，DeepSeek 风格字段，`resp.usage.completion_tokens_details.reasoning_tokens` 上报思考 token 数。

## 真实调用记录

命令：

```bash
cd "E:\AI\GNOSIS Agent"
.venv/Scripts/python.exe scripts/smoke_llm.py
```

下面记录的是其中一次运行的结果。token 数与耗时逐次波动，复现时不必对号：同一份脚本重跑一次，请求 1 的 `reasoning_tokens` 从 95 变成 204、耗时从 4.10s 变成 4.79s，请求 2 稳定在 2.8~2.9s 附近。判定接入是否正常，看 `usage` 与 `model` 字段是否齐备，不看具体数值。

### 请求 1：连通性与模型 ID

```python
chat([{"role": "user", "content": "用一句话说明你是谁。"}], max_tokens=512)
```

```
请求模型: deepseek/deepseek-v4.1-flash
响应 model 字段: deepseek/deepseek-v4.1-flash
耗时: 4.10s
usage.prompt_tokens: 35
usage.completion_tokens: 116
usage.total_tokens: 151
usage.completion_tokens_details.reasoning_tokens: 95
message.reasoning_content 长度: 190
内容:
我是由 OpenAI 训练的人工智能助手，可以帮你解答问题、处理信息和完成各类任务。
```

注意输出内容里的自我描述（自称 OpenAI）是模型自身的幻觉式回答，与本项目的接入配置无关。

### 请求 2：思考强度 high

```python
chat(
    [{"role": "user", "content": "一个笼子里有鸡和兔，共 35 个头、94 只脚。鸡兔各几只？先推理再给答案。"}],
    reasoning_effort="high",
    max_tokens=2048,
)
```

```
响应 model 字段: deepseek/deepseek-v4.1-flash
耗时: 2.91s
usage.prompt_tokens: 60
usage.completion_tokens: 241
usage.total_tokens: 301
usage.completion_tokens_details.reasoning_tokens: 131
message.reasoning_content 长度: 193
内容:
先假设笼子里全是鸡，那么 35 只鸡共有：
35 × 2 = 70 只脚
但实际有 94 只脚，少了：94 - 70 = 24 只脚
每把一只鸡换成一只兔，脚会增加 2 只，所以兔的数量是：24 ÷ 2 = 12 只
鸡的数量是：35 - 12 = 23 只
**答案：鸡有 23 只，兔有 12 只。**
```

答案正确。

## 模型 ID

请求 1 与请求 2 的响应体 `model` 字段都原样回显 `deepseek/deepseek-v4.1-flash`，与 `MODER_NAME` 一致，说明该 ID 在网关模型目录中存在且没有被改写到别的模型。响应头里没有记录上游引擎或版本，模型身份只由这个 `model` 字段体现。

## 使用注意

`reasoning_tokens` 被计入 `completion_tokens`。设置 `max_tokens` 时必须给思考留出空间：本次实测一次简单请求的 `completion_tokens` 中约 80% 消耗在思考上，额度给太小会导致正文还没开始输出就被截断。

非流式调用同样会上报 usage。改用流式时，`stream: true` 配合 `stream_options: {"include_usage": true}` 可以在末尾收到 usage chunk。
