# 渠道与模型

渠道、密钥与默认模型放在 `~/.aaa/secret.yaml`，与可调设置 `config.toml` 分开：**含密钥的内容只在 secret.yaml**。

## 优先用向导

`/provide` 打开渠道编辑：填地址、密钥，探测可用模型，选择默认。它会替你处理字段、权限与回写。
手改文件也能生效，但下面这些约束必须自己守住。

## 结构（v2）

```yaml
version: 2
default:
  provider: deepseek
  model: deepseek-chat
enabled_models: []
providers:
  deepseek:
    name: DeepSeek
    api: deepseek
    base_url: https://api.deepseek.com
    api_key: sk-...
    models:
      - id: deepseek-chat
        context_window: 65536
      - id: deepseek-reasoner
        context_window: 65536
        cost: {input: 2, output: 8}
```

## 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `version` | 否 | 固定为 2；旧文件由程序在内存里归一，下次保存才改写 |
| `default.provider` / `default.model` | 否 | 失效时回落到第一个渠道的第一个模型 |
| `enabled_models` | 否 | 可切换范围；空列表表示全部 |
| `providers.<id>.base_url` | 是 | 去掉尾部斜杠 |
| `providers.<id>.api_key` | 是 | 明文存放，文件权限固定 0600 |
| `providers.<id>.models` | 是 | 至少一条 |
| `providers.<id>.api` | 否 | 默认 `deepseek`；换成别的值等于换一套协议实现 |
| `providers.<id>.name` | 否 | 展示名，缺省用渠道 id |

模型条目：

- `id`（必填）：模型标识；
- `context_window`：上下文窗口；缺省或 0 时取 `model.default_context_window`；
- `cost`：可选价目，形状不合法时只丢弃价目并告警，不影响启动。

## 可切换范围

`enabled_models` 是模式列表，用 `fnmatch` 同时匹配 `渠道/模型` 与裸模型 id：

```yaml
enabled_models:
  - "deepseek/*"
  - "gpt-5*"
```

`/model`、`Ctrl-P` 循环与 `--model` 都受此限制；不在范围内的模型会提示检查 `enabled_models`。

## 失败语义

- 文件不存在：首次启动进入配置向导。
- 文件存在但解析失败、或没有任何可用渠道（缺 `base_url`、`api_key`、`models` 中任一项）：报错退出，**不覆盖原文件**。
- 单个渠道缺字段：该渠道被丢弃，其余照常。
- 写入是原子的，权限固定 0600。

## v1 兼容

旧版 `providers` 为列表时在内存里转换成 v2 映射；启动时不改用户文件，只有下次保存才落新格式。

## 生效

- 修改渠道、密钥或默认模型后重启 aaa。
- 会话中切换模型不写回 secret.yaml 的 `default`，除非显式保存。
- 切换模型会重建 Agent；不原地修改模型或 fallback 配置。

## 排查

1. `/model` 里没有某个模型：先看 `enabled_models` 是否把它排除，再看该渠道是否因缺字段被丢弃。
2. 启动报“没有可用的渠道”：检查三个必填项，或重新跑 `/provide`。
3. 鉴权失败：确认 `api_key` 与 `base_url` 属于同一服务商；`api` 的值决定协议实现。
