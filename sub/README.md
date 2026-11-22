<div align="center">

# SUB

**四面板订阅聚合**

自动抓取 · 分类缓存 · 合并去重

</div>

---

### 配置

只需添加一个 GitHub Actions Secret：

| 名称 | 内容 |
|:--|:--|
| `sub` | 四个面板的订阅地址，每行一个 |

路径：`Settings → Secrets and variables → Actions → New repository secret`

```text
面板 1 订阅地址
面板 2 订阅地址
面板 3 订阅地址
面板 4 订阅地址
```

按 **BestCF 中四个面板的顺序**填写，必须恰好 4 个有效的 HTTP(S) 地址。

> 基础运行只需配置 `sub`，无需添加其他 Secret 或 Repository Variables。

### 运行

- BestCF 工作流成功后自动运行。
- 也可前往 **Actions → 订阅聚合 → Run workflow** 手动运行。

### 输出

| 文件 | 内容 |
|:--|:--|
| `sub.txt` | 四个面板合并去重后的 `HOST:PORT` |
| `sub_stats.txt` | 本次运行统计报告 |

<sub>第 3 面板按 `bestcf/bestcf.txt` 中的订阅器标签分类；面板 1、2、4 独立缓存，最终统一合并去重。</sub>
