# Beancount Personal System（公开演示版）

一个基于 [Beancount](https://beancount.github.io/) 的个人复式记账系统示例：
把支付宝、微信、京东、银行、信用卡等来源的原始账单，经处理器标准化为 Excel，
再转换为 Beancount 账本，并完成规则分类、基金净值报告和账本校验。

> ⚠️ **隐私声明**
>
> 本仓库只包含虚拟演示数据。所有用户、商户、银行、卡号、账户、金额和分类规则
> 均为示例，不对应任何真实个人；账本账户也已统一为普通分类（如
> `Expenses:Food`、`Expenses:Shopping`、`Assets:Cash:Bank:BankA:Checking`）。
> 请勿把真实账单、真实 `main.bean`、`account.bean`、`rules.yaml` 或日志提交到公开仓库。

---

## 快速开始

```bash
# 1. 安装依赖（需要 Python 3.10+）
pip install -r requirements.txt

# 2. 可选：重新生成虚拟原始数据
python 脚本/生成演示数据.py

# 3. 一键跑通演示账期 2501
python 脚本/pipeline.py 2501

# 4. 浏览器查看账本
fava bean_files/main.bean
```

`pipeline.py` 会依次执行：

```text
原始数据/{来源}/2501
  → processor_*           标准化成 整理后数据/2501/*.xlsx
  → bean_edit_*           生成    bean_files/2501/*.bean
  → reclassifier          按 rules.yaml 细化消费分类
  → merge_bean_headers    整理文件头
  → fund_report           生成 reports/fund_report_2501.md
  → verify_accounts       检查账户体系 + bean-check
```

---

## 目录结构

```text
beancount_personal_system/
├── 原始数据/                     # 虚拟原始账单（保持真实导出格式）
│   ├── 支付宝/2501/
│   ├── 支付宝基金/2501/
│   ├── 微信/2501/
│   ├── 京东/2501/
│   ├── 示例银行A/2501/
│   ├── 示例银行B/2501/
│   ├── 示例银行C/2501/
│   ├── 示例银行A信用卡/2501/
│   └── 支付宝余额/2501/
├── 整理后数据/2501/              # processor 标准化后的 Excel
├── bean_files/
│   ├── account.bean              # 示例账户表
│   ├── main.bean                 # 主账本
│   ├── manual.bean               # 手工补充分录
│   ├── prices.bean               # 基金市价
│   └── 2501/                     # 由 bean_edit_* 生成的账期文件
├── reports/
│   └── fund_report_2501.md       # 示例基金报告
└── 脚本/
    ├── config.py                 # 全局配置（账期、账户映射、支付规则、银行卡）
    ├── rules.yaml                # 虚拟消费分类规则
    ├── fund_map.json             # 虚拟基金名 → FUND 代码
    ├── pipeline.py               # 一键流水线
    ├── processor_*.py            # 原始账单 → 标准化 Excel
    ├── bean_edit_*.py            # 标准化 Excel → Beancount
    ├── reclassifier.py           # 规则分类
    ├── fund_report.py            # 基金 XIRR / 市值报告
    ├── verify_accounts.py        # 账户体系校验 + bean-check
    └── 生成演示数据.py            # 重新生成虚拟原始数据
```

---

## 虚拟主体说明

为了演示真实系统的目录和格式，仓库使用以下虚拟名称：

| 真实场景 | 演示名称 |
|---|---|
| 用户姓名 | 示例用户 |
| 银行 | 示例银行A / 示例银行B / 示例银行C |
| 借记卡尾号 | 1001 / 1002 / 1003 / 2001 / 3001 |
| 信用卡 | 示例银行A信用卡 9001 |
| 基金 | 示例稳健基金A（`FUND999999`） |
| 商户 | 示例咖啡、示例超市、示例书店、示例餐厅、示例地铁等 |

这些名称只用于演示格式，没有任何真实含义。

---

## 使用心得

> 这部分来自实际使用后的体会，重点不是工具本身，而是怎么把它用顺。

1. **先梳理好账户体系。** 账户表是整套系统的基础。先把资产、负债、收入、支出、权益的边界定清楚，后面的分类、对账和报表才稳定。

2. **用好 `rules.yaml` 规则分类。** 先让脚本做初步分类，再手工修正一批数据。积累一到两个月后，更新 `rules.yaml`，通常可以覆盖 90% 以上的交易记录。

3. **每月仍然过一遍账本。** 每月新账单进来后，仍建议人工浏览一遍生成的 `bean_files`，重点看：
    - 分类是否正确；
    - 脚本没覆盖的边界情况；
    - 是否需要给 agent 补充规则或修脚本。

4. **回溯旧账期要循序渐进。** 建议从 1 个月开始，再扩展到半年、1 年。回溯性记账会遇到不少边界情况：
    - 数据完整时，当前脚本基本可以处理；
    - 数据缺失时，需要专项突破，例如早期支付宝基金交易缺失、部分支出账户为空等。

5. **数据积累起来后，就可以做分析了。** 账本稳定后，可以生成消费结构、资产变化、基金 XIRR、长期趋势等报告。统计和复盘会方便很多，统计人狂喜。

---

## 常用命令

```bash
# 跑完整流水线
python 脚本/pipeline.py 2501

# 只跑指定来源
python 脚本/pipeline.py 2501 --only alipay,bank
python 脚本/pipeline.py 2501 --only reclassifier

# 预览规则替换但不写文件
python 脚本/reclassifier.py --dry-run

# 校验账户体系
python 脚本/verify_accounts.py

# 重新生成基金报告（离线时回退到 bean_files/prices.bean）
python 脚本/fund_report.py
```

---

## 回溯记账与对账

补记过去月份时，系统提供一套机械化的期初反推与对账工具：

- `restructure_main_bean.py`：自动判断 FIX/ADD 模式，改写 `main.bean`，把旧 init 变成 balance 断言。
- `verify_period.py`：生成银行/基金每日 balance sidecar，用 delta-jump 一次列出所有错误日期。
- `inspect_date.py`：精查某一天，横向对照银行/支付宝/微信/京东原始记录与 bean 分录。
- `read_bank_balance.py`：从银行 Excel 反推期初和每日末余额。
- `back_fund.py`：根据下期持仓反推基金期初份额。

完整说明见 [`脚本/回溯记账说明.md`](脚本/回溯记账说明.md)。

## 已知限制

- 虚拟原始 PDF 使用简单表格/文本生成，目的是演示 processor 的解析路径，不保证覆盖真实 PDF 的所有版式。
- 演示基金代码 `FUND999999` 不在公开净值接口中，`fund_report.py` 会自动回退到 `bean_files/prices.bean` 中的虚拟净值。
- 生成的账期文件已包含在仓库中，便于直接检查；重新运行 `pipeline.py` 会覆盖它们。

---

## 许可

仅作个人学习与交流示例使用。使用真实数据前请自行评估并做好脱敏。
