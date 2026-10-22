# -*- coding: utf-8 -*-
"""Rebuild week_scoring_dataset.ipynb with MPD target + form answers."""
import nbformat as nbf
from pathlib import Path

nb = nbf.v4.new_notebook()
cells = []

def md(s): cells.append(nbf.v4.new_markdown_cell(s.strip()))
def code(s): cells.append(nbf.v4.new_code_cell(s.strip()))

md("""
# Кредитный скоринг: от бизнеса до датасета

Бизнес: «Просрочка выросла. Нужна модель надёжности».

**Модель не учим.** Делаем: постановка → разведка → **target (MPD)** → анализ → фичи as-of → схема проверки.

Snapshot: **2025-05-31**. Только `scoring_*`.
""")

code("""
# pip install psycopg2-binary pandas sqlalchemy matplotlib
import os
import pandas as pd
from sqlalchemy import create_engine

DSN = os.environ["ACADEMY_DSN"]
engine = create_engine(DSN)

def q(sql: str) -> pd.DataFrame:
    return pd.read_sql(sql, engine)

SNAPSHOT = "2025-05-31"
""")

code("""
q('''
select tablename
from pg_tables
where schemaname = 'public' and tablename like 'scoring\\\\_%'
order by 1
''')
""")

md("""
## Ответы для формы (Q1–Q3)

| Q | Ответ |
|---|------:|
| Q1 всего заявок | **18000** |
| Q2 rejected | **6723** |
| Q3 заявок без `scoring_customers` | **7137** |
""")

code("""
print('Q1', q('SELECT COUNT(*) AS n FROM scoring_applications').iloc[0,0])
print('Q2', q(\"SELECT COUNT(*) AS n FROM scoring_applications WHERE final_decision='rejected'\").iloc[0,0])
print('Q3', q('''
SELECT COUNT(*) AS n FROM scoring_applications a
WHERE NOT EXISTS (
  SELECT 1 FROM scoring_customers c WHERE c.customer_id = a.client_id
)
''').iloc[0,0])
""")

md("""
## Шаг 1. Постановка задачи

1. **Решение:** одобрение / отказ (`final_decision`).  
2. **Событие:** MPD (материальная и устойчивая просрочка) за **6 месяцев** после выдачи.  
3. **Популяция:** заявки; target только у одобренных с закрытым окном.  
4. **Данные на момент решения:** анкета, бюро as-of, карты/inquiries/loans с датой ≤ `decision_date`.  
5. **Критерий успеха:** AUC/Gini + ниже bad rate при сопоставимом approval (out-of-time).
""")

md("""
## Шаг 2. Разведка / Q4 забракованные

| Таблица | Вердикт |
|--------|---------|
| `scoring_applications`, `contracts`, `payments` | основа |
| `scoring_bureau_snapshots`, `bureau_loans`, `bureau_inquiries` | фичи as-of |
| `scoring_card_transactions`, `cards`, `card_disputes`, `customers` | фичи |
| `scoring_payments_backup` | бэкап, неполная копия |
| `scoring_clients_old` | устаревший срез |
| `scoring_txn_sample` | sample 5k, не полные txn |
| `scoring_temp` | мусор |
| `scoring_model_v1_scores` | готовый скор = leakage |
| `scoring_applications_full` | поля после выдачи = leakage |
| `scoring_collection_actions` | после просрочки нашего договора = leakage для фич |
""")

md("""
## Q5. Утечки из будущего

- `scoring_model_v1_scores.score_v1` — скор уже по заявке.  
- `applications_full.dpd_max / contract_status_now / collection_calls_cnt` — итог после выдачи.  
- `collection_actions` — коллекшн по нашему contract после дефолта.  
- Платежи после issue — источник **таргета**, не фич на decision_date.
""")

md("""
## Шаг 3. Target = MPD (не простой DPD30)

### Почему не «просто DPD≥30»
Из всех с max DPD≥30 за 6м (~24%) около **26%** — разовые: один раз опоздали, заплатили, до 60 не ушли.  
Из достигших 30 дальше в 60+ уходят ~**73%**. Важна устойчивость и сумма, не флажок.

### Правило Bad=1 за 6 месяцев после `issue_date`
1. неоплаченный взнос с DPD ≥ 30, **или**  
2. max DPD ≥ 60, **или**  
3. сумма взносов с DPD≥30 ≥ **15%** от `issued_amount_uzs`

Отказы — без target. Незрелые (`issue+6м > snapshot`) — исключены.
""")

code("""
# MPD target
target = q(f'''
WITH pay AS (
  SELECT contract_id, due_date, paid_date, due_amount_uzs,
         CASE WHEN paid_date IS NOT NULL THEN paid_date - due_date
              ELSE DATE '{SNAPSHOT}' - due_date END AS dpd
  FROM scoring_payments
  WHERE due_date <= DATE '{SNAPSHOT}'
),
obs AS (
  SELECT
    ct.application_id,
    ct.client_id,
    a.decision_date,
    ct.issue_date,
    ct.issued_amount_uzs,
    COALESCE(MAX(GREATEST(pay.dpd,0)) FILTER (
      WHERE pay.due_date <= ct.issue_date + INTERVAL '6 months'),0) AS max_dpd_6m,
    COUNT(*) FILTER (
      WHERE pay.due_date <= ct.issue_date + INTERVAL '6 months'
        AND pay.paid_date IS NULL AND GREATEST(pay.dpd,0) >= 30) AS n_unpaid30,
    COALESCE(SUM(pay.due_amount_uzs) FILTER (
      WHERE pay.due_date <= ct.issue_date + INTERVAL '6 months'
        AND GREATEST(pay.dpd,0) >= 30),0) AS amt_late30
  FROM scoring_contracts ct
  JOIN scoring_applications a ON a.application_id = ct.application_id
  LEFT JOIN pay ON pay.contract_id = ct.contract_id
  WHERE ct.issue_date + INTERVAL '6 months' <= DATE '{SNAPSHOT}'
  GROUP BY 1,2,3,4,5
)
SELECT
  application_id,
  client_id,
  decision_date,
  issue_date,
  CASE
    WHEN n_unpaid30 >= 1 THEN 1
    WHEN max_dpd_6m >= 60 THEN 1
    WHEN max_dpd_6m >= 30 AND amt_late30 >= 0.15 * issued_amount_uzs THEN 1
    ELSE 0
  END AS target,
  max_dpd_6m,
  n_unpaid30,
  ROUND(amt_late30::numeric / NULLIF(issued_amount_uzs,0), 3) AS late_amt_share
FROM obs
''')

print('N=', len(target))
print('Bad=', int(target.target.sum()), f'({100*target.target.mean():.2f}%)')
target[['application_id','client_id','decision_date','target']].head(10)
""")

code("""
bad_by_month = (
    target.assign(issue_month=pd.to_datetime(target.issue_date).dt.to_period('M').dt.to_timestamp())
    .groupby('issue_month')
    .agg(n=('target','size'), bad=('target','sum'), bad_pct=('target','mean'))
    .reset_index()
)
bad_by_month['bad_pct'] = (100 * bad_by_month['bad_pct']).round(2)
bad_by_month
""")

code("""
import matplotlib.pyplot as plt
fig, ax = plt.subplots(figsize=(10,4))
ax.plot(bad_by_month.issue_month, bad_by_month.bad_pct, marker='o', color='#e85d5d')
ax.axhline(100*target.target.mean(), ls='--', color='gray', label='средний MPD')
ax.set_title('MPD bad rate по месяцу выдачи')
ax.set_ylabel('bad %')
ax.legend()
plt.xticks(rotation=45)
plt.tight_layout()
plt.show()
""")

md("""
### Q6 — кратко для формы

- **Bad:** MPD за 6м (unpaid30+ / DPD60+ / late amount ≥15% выдачи).  
- **Таблицы:** contracts + payments.  
- **Отказы:** не размечаем. **Незрелые:** исключили.  
- **Результат:** 8821 договор, bad **1641 (18.60%)**.  
- **Динамика:** вырос, пик окт 2024 ≈ **29.8%**.
""")

md("""
## Шаг 4. Анализ проблемы
""")

code("""
# Selection bias: approved vs rejected (до решения)
q('''
SELECT final_decision,
       COUNT(*) n,
       ROUND(AVG(requested_amount_uzs)/1e6,2) AS avg_req_mln,
       ROUND(AVG(declared_income_uzs)/1e6,2) AS avg_inc_mln,
       ROUND(AVG(employment_months)::numeric,1) AS avg_emp,
       ROUND(100.0*COUNT(*) FILTER (WHERE employment_months IS NULL)/COUNT(*),1) AS pct_null_emp
FROM scoring_applications
GROUP BY 1
''')
""")

md("""
## Шаг 5–6. Фичи и проверка

Фичи только ≤ `decision_date`: анкета, bureau as-of, карты 3–6м, `bureau_loans`/`inquiries` as-of.  
Ловушки: score_v1, dpd_max, collection, платежи после выдачи.  

Валидация: **out-of-time** (train до 2024-06, test 2024-09..11), не random.  
Метрики: AUC/Gini + порог → approval% → bad% / потери.
""")

md("""
## Артефакты рядом с ноутбуком

- `scoring_target.csv` — MPD target  
- `Scoring_Target_MPD.xlsx`, `scoring-target-MPD.html`  
- `Scoring_Target_Vtornik.pptx` / `.xlsx` — на показ  
- `scoring_standout_dataset.csv` — фичи
""")

nb["cells"] = cells
nb["metadata"] = {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "pygments_lexer": "ipython3"},
}
path = Path(__file__).resolve().parent / "week_scoring_dataset.ipynb"
nbf.write(nb, path)
print("updated", path, "cells=", len(cells))
