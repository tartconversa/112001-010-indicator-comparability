# 绿色转型指标可比性工作台

用于跨城市、跨国家绿色转型比较的可审计工作台。它把「指标定义—来源观测—映射与审核—
项目比较—对外分享—修订影响」串成一条可追踪的链路，并拒绝把资料缺口或制度差异
悄悄折叠成一个统一分数。

仅使用 Python 3.11+ 标准库。

## 它解决什么问题

各地提供的能源结构、交通、电力排放和生态修复数据往往在定义、单位、统计边界和
更新周期上不一致。直接换算成统一分数会把缺资料或制度差异误当成表现差距；完全
拒绝比较又无法支持合作项目选择。本工作台的处理方式是：

1. **先分级、再比较。** 每一个「地区 × 指标」单元先被判定为证据等级：
   - `可直接比较`（定义、单位、统计边界一致且有已认可的映射）
   - `有依据换算后可比`（单位换算有系数、有依据；边界一致）
   - `只能并列呈现`（统计边界不同、来源覆盖率过低或资料不完整）
   - `仅供讨论的估算`（映射标记为估算）
   - `未纳入比较`（映射被拒绝、待审核或缺失）
   - `缺少观测`（该期间没有观测，**绝不插值**）
2. **只有真正可比的单元进入排序。** 并列呈现、估算与缺失值不会混入排名。
3. **覆盖变化必须显式。** 每次结果都携带「可比单元/总单元」覆盖数字；低于项目阈值时
   不输出综合排名，也不保留旧排名。
4. **分享即冻结。** 对外分享会保存快照；之后的来源修订或伙伴意见会通过影响报告
   与快照对比呈现，而不是改写已分享的结论。
5. **逐项可追踪。** 每个单元都能追到原始定义及其版本、来源版本、转换依据、审核记录
   与分类理由。

## 快速开始

```bash
python -m greenbench --db demo.json import targets data/targets.csv
python -m greenbench --db demo.json import metrics data/metrics.csv
python -m greenbench --db demo.json import observations data/observations.csv
python -m greenbench --db demo.json import mappings_reviews data/mappings_reviews.csv
python -m greenbench --db demo.json project-create examples/project_clean_energy.json
python -m greenbench --db demo.json compare PRJ-2026-CE --output report.md --json-output report.json
```

输出包含供当地伙伴复核的 Markdown 复核包与完整 JSON。Markdown 中：

- 「总体结论」只有在覆盖与可比性都满足时才给出综合排序；
- 每个指标分别列出排名、并列呈现表与逐项追踪；
- 每个单元列出定义、单位、边界、观测版本、映射、审核状态、转换依据与限制。

### 分享、审核与修订影响

```bash
# 冻结一次对外分享
python -m greenbench --db demo.json share PRJ-2026-CE RST-1 --with "Hill-Region partner"

# 记录当地伙伴意见（可指定只影响某个地区）
python -m greenbench --db demo.json review X01 rejected --region Harbor-City --note "复核后拒绝"

# 比较分享快照与当前结果
python -m greenbench --db demo.json impact RST-1 --output impact.json --markdown impact.md

# 汇总映射审核状态与失效审核
python -m greenbench --db demo.json status

# 追踪单个单元的完整链路
python -m greenbench --db demo.json trace PRJ-2026-CE clean_energy_share Hill-Region
```

影响报告会给出：

- 快照与当前的可比覆盖对比；
- 综合结论状态是否变化；
- 受影响的单元及其字段级差异（证据等级、值、定义版本、来源版本、映射修订、审核状态）；
- 对应的项目说明与注意事项（分享时与当前）差异。

## 项目文件格式

```json
{
  "project_id": "PRJ-2026-CE",
  "title": "2025 年区域清洁能源占比比较",
  "regions": ["Harbor-City", "Hill-Region", "Delta-Port"],
  "coverage_threshold": "90",
  "min_source_coverage": "80",
  "notes": "向伙伴说明的口径与限制",
  "targets": [
    {
      "target_metric": "clean_energy_share",
      "period": "2025",
      "sources": {"Hill-Region": "M01"},
      "mappings": {"Harbor-City": "X01"}
    }
  ]
}
```

- `targets[].sources` 可把某地区固定到指定来源指标；`mappings` 可固定采用的映射。
  两者不能对同一地区同时使用。
- 未固定的地区会按证据等级择优选择已登记映射，并在 `alternatives` 中列出其他候选。
- `coverage_threshold` 是项目层面的可比覆盖门槛；`min_source_coverage` 是单个来源
  观测的最低覆盖率，低于它时该单元只能并列呈现。

## 方法学约束（有意为之）

| 规则 | 实现位置 |
| --- | --- |
| 单位换算不能修复统计边界差异 | `greenbench/compare.py:_classify` |
| 缺失年份不插值 | `_select_observation` 只在精确期间匹配 |
| 来源修订不覆盖分享快照 | `Store.save_share` 拒绝重复编号；快照独立存储 |
| 映射修订使旧审核失效 | `Store.effective_review` 检查 `proposal_rev` |
| 伙伴拒绝只影响对应单元 | `effective_review(mapping_id, region)` |
| 综合结论必须有足够覆盖 | `Comparator._overall` |
| 项目修订哈希忽略时间戳 | `Project.revision` |

## 本地验证

```bash
python -m unittest discover -s tests -v
python -m compileall -q greenbench tests
```

测试覆盖：直接比较与排序方向、单位换算、边界差异、估算隔离、缺失不插值、来源/定义
版本修订、审核拒绝与失效、覆盖变化、分享快照不可覆盖、影响报告、逐项追踪与端到端 CLI。

## 目录

```
greenbench/
  models.py    记录类型、版本键、期间与数值处理
  store.py     追加式版本化存储、CSV 导入、审核与快照
  compare.py   可比性分级、排序、覆盖、复核包与影响报告
  cli.py       命令行入口
data/          合成样例（目标、定义、观测、映射与审核）
examples/      示例项目文件
tests/         行为测试与样例结构校验
```

## 数据说明

所有随附记录均为虚构，仅供本地开发与演示使用。字段与约定见 `data/README.md`。
