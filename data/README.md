# 合成输入数据

用于跨地区比较的合成指标定义、目标指标、区域观测、映射提案与合作方审核记录。
所有记录均为虚构，仅供本地开发与演示使用；日期、机构与数值均为示例。

## 文件

| 文件 | 内容 | 关键字段 |
| --- | --- | --- |
| `targets.csv` | 项目层面的目标指标概念 | `target_metric`、`canonical_unit`、`canonical_boundary`、`direction` |
| `metrics.csv` | 各地区的来源指标定义（带版本） | `metric_id`、`region`、`unit`、`boundary`、`frequency`、`valid_from`、`valid_to`、`definition_version` |
| `observations.csv` | 期间观测（可多版本） | `metric_id`、`region`、`period`、`value`、`coverage_pct`、`source_version`、`definition_version` |
| `mappings_reviews.csv` | 映射提案与初始审核意见 | `mapping_id`、`region`、`transform`、`target_unit`、`conversion_factor`、`review_status`、`reviewed_at` |
| `fixture_manifest.csv` | 各文件记录数与用途 | `dataset`、`records`、`purpose` |

## 字段约定

- `unit` 与 `boundary` 是判别可比性的核心字段。`percent` 与 `fraction` 不同的单位可以经
  有依据的换算变为可比；`electricity_consumed` 与 `electricity_generated` 属于不同统计边界，
  **任何单位换算都不能消除该差异**。
- `definition_version` 与 `source_version` 分别标识定义修订和来源数据修订。修订必须换新版本号，
  旧版本保留，以便追溯分享时采用的口径。
- `review_status` 取 `accepted`、`rejected`、`pending`。`rejected` 表示当地伙伴明确拒绝该映射；
  该地区仍可保留其他已认可指标，但覆盖会下降且不得沿用旧排名。
- `shared_result_id` 记录分享快照编号；快照一旦登记不可覆盖。
- `coverage_pct` 为来源自身的覆盖率，低于项目 `min_source_coverage` 时该单元只能并列呈现。

## 演示数据要点

- Harbor-City 与 Delta-Port 的 `clean_energy_share` 在单位与边界上一致，可直接比较。
- Hill-Region 的清洁能源指标按**发电口径**统计，只能与消费口径并列呈现。
- `X02` 是 Hill-Region 提出的范围调整映射，已被当地伙伴拒绝。
- `X03` 的绿色出行映射仍为 `pending`，未获认可前不进入比较。
