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

## 改造成自己的账本

1. 安装依赖并复制本仓库。
2. 把真实账单放入 `原始数据/{来源}/{账期}/`，保持与演示文件相同的文件名前缀和列结构。
3. 修改 `bean_files/account.bean`，声明自己的账户体系。
4. 修改 `脚本/config.py`：
   - `PERIOD`：默认账期；
   - `ACCOUNTS`：账户映射；
   - `BANK_CARDS`：银行卡尾号、完整卡号、银行标签、币种子账户；
   - `PAYMENT_RULES_*`：支付方式关键词映射；
   - `ALIPAY_EXPENSE_MAP` / `JINGDONG_EXPENSE_MAP_*`：初步消费分类。
5. 修改 `脚本/rules.yaml`，按自己的商户/商品关键词细化分类。
6. 运行 `python 脚本/pipeline.py {账期}`。

> 真实使用时，请把数据、账户和规则文件放在私有仓库；公开仓库只保留代码和脱敏样例。

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

## 已知限制

- 虚拟原始 PDF 使用简单表格/文本生成，目的是演示 processor 的解析路径，不保证覆盖真实 PDF 的所有版式。
- 演示基金代码 `FUND999999` 不在公开净值接口中，`fund_report.py` 会自动回退到 `bean_files/prices.bean` 中的虚拟净值。
- 生成的账期文件已包含在仓库中，便于直接检查；重新运行 `pipeline.py` 会覆盖它们。

---

## 许可

仅作个人学习与交流示例使用。使用真实数据前请自行评估并做好脱敏。
