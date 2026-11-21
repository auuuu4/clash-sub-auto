# clash-sub-auto

自动生成并每日更新 Clash 订阅：从公开订阅源抓取节点 → TCP 预筛 → 真实代理测活 → 质量筛选 → 按国家/类型分组 → 输出 Clash YAML。

## 订阅地址

```
https://raw.githubusercontent.com/<OWNER>/clash-sub-auto/main/out/valid_nodes_clash.yaml
```

国内访问慢时用 ghproxy 镜像：

```
https://ghproxy.net/https://raw.githubusercontent.com/<OWNER>/clash-sub-auto/main/out/valid_nodes_clash.yaml
```

## 工作原理

```
GitHub Actions（每天 04:17 / 10:17 / 16:17 / 22:17 CST）
    ↓
fetch_public.py      抓 6 个公开订阅源（Barabama / free18 / cbusi / NoMoreWalls / ermao / zhuhaiuk）
    ↓
prefilter_tcp.py     TCP 预筛（1293 → 约 350）
    ↓
auto_refresh.py      逐节点建立真实代理隧道，验证出口 IP + HTTPS 204 + 下载测速
    ↓
out/valid_nodes_clash.yaml
```

每个节点都经过：
- 通过 SOCKS 隧道访问 `ip-api.com` 拿到真实出口 IP / 国家
- 访问 `gstatic.com/generate_204` 必须返回 204
- 下载 256KB 测速，低于 20 KB/s 淘汰
- 延迟 > 3000ms 淘汰
- 出口 IP 去重、`server:port` 去重

## 分组

| 分组 | 说明 |
|---|---|
| `AUTO` | 全部节点自动选优 |
| `AUTO-<CC>` | 按出口国家分组（US / JP / DE / NL / SG …） |
| `AUTO-STATIC` | 无过期时间的节点 |
| `STATIC` | 手动挑静态节点 |
| `PROXY` | 主入口 |

节点名格式：`[US] PUB-018 pub`

## 手动触发

Actions → refresh-subscription → Run workflow

或本地跑：

```bash
pip install requests PyYAML
# 需要 mihomo 二进制
export MIHOMO_BIN=/usr/local/bin/mihomo
python work/auto_refresh.py --mode fast --public-only --out-dir out --no-gist
```

## 说明

- 节点全部来自公开订阅源，仅作连通性测试与学习用途
- 免费节点稳定性有限，脚本每天自动换一批
- 公开节点不适合登录重要账号或传输敏感数据
