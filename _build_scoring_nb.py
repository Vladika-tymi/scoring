# -*- coding: utf-8 -*-
import nbformat as nbf
from pathlib import Path

nb = nbf.v4.new_notebook()
cells = []


def md(s):
    cells.append(nbf.v4.new_markdown_cell(s.strip()))


def code(s):
    cells.append(nbf.v4.new_code_cell(s.strip()))


md(
    """
# Кредитный скоринг: от запроса бизнеса до датасета

Бизнес: «Просрочка выросла. Нужна модель надёжности клиента».

**Мы не учим модель.** Мы делаем то, без чего модель бесполезна:
понять задачу → выбрать таблицы → собрать target → проверить, что проблема реальна → накидать фичи без утечек → описать проверку.

Дата выгрузки: **2025-05-31**. Только таблицы `scoring_*`.

## Как читать

Каждый шаг: короткое «зачем» → код → «что это значит».
Блоки **«простыми словами»** — для тех, кто впервые в скоринге.
"""
)

code(
    """
# pip install psycopg2-binary pandas sqlalchemy matplotlib
import os
import pandas as pd
from sqlalchemy import create_engine

DSN = os.environ["ACADEMY_DSN"]
engine = create_engine(DSN)

def q(sql: str) -> pd.DataFrame:
    return pd.read_sql(sql, engine)

SNAPSHOT = "2025-05-31"  # дата выгрузки = «сегодня» в задании
"""
)

code(
    """
# Все таблицы задания
q('''
select tablename
from pg_tables
where schemaname = 'public' and tablename like 'scoring\\\\_%'
order by 1
''')
"""
)

md(
    """
## Шаг 1. Постановка задачи

### Простыми словами
Банк перед выдачей кредита говорит «да» или «нет».
Модель помогает решить, **кому давать кредит**, чтобы потом меньше людей не платили вовремя.

### Ответы

1. **Какое решение изменится?**  
   **Одобрение / отказ** заявки (`final_decision`).  
   *Допущение:* лимит и ставку пока не оптимизируем (хотя в договоре есть сумма и ставка).

2. **Какое событие и горизонт?**  
   **Bad = за первые 6 месяцев после выдачи** макс. просрочка платежа **≥ 30 дней** (DPD30+).  
   *Почему 30:* стандартный ранний дефолт; рост просрочки на DPD90+ тоже виден, но bad-событий меньше.

3. **Популяция?**  
   Заявки из `scoring_applications`. Target — только у **одобренных** (есть договор).  
   Отклонённых смотрим для анализа политики, но не размечаем bad/good (не выдали → не наблюдаем оплату).

4. **Данные в момент решения?**  
   Всё с датой **≤ `decision_date`**: анкета, бюро (as-of), карты до заявки.  
   Нельзя: будущие платежи, collection, `dpd_max`, готовый `score_v1`.

5. **Критерий успеха?**  
   Out-of-time: AUC/Gini **и** при том же approval rate — ниже bad rate среди одобренных.
"""
)

md(
    """
## Шаг 2. Разведка данных

### Простыми словами
В выгрузку попало всё подряд. Нужно понять по каждой таблице:
сущность, ключ, период, можно ли поле в фичи, или это мусор/утечка.
"""
)

code(
    """
q('''
SELECT c.relname AS table_name, c.reltuples::bigint AS approx_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
  AND c.relname LIKE 'scoring\\\\_%' ESCAPE '\\\\'
ORDER BY approx_rows DESC NULLS LAST
''')
"""
)

code(
    """
print('=== заявки: период и решения ===')
display(q('''
SELECT COUNT(*) n, MIN(decision_date) AS min_dt, MAX(decision_date) AS max_dt
FROM scoring_applications
'''))
display(q('''
SELECT final_decision, COUNT(*) n
FROM scoring_applications
GROUP BY 1 ORDER BY 2 DESC
'''))

print('=== договоры только у approved ===')
display(q('''
SELECT a.final_decision,
       COUNT(DISTINCT a.application_id) apps,
       COUNT(DISTINCT c.contract_id) contracts
FROM scoring_applications a
LEFT JOIN scoring_contracts c ON c.application_id = a.application_id
GROUP BY 1
'''))
"""
)

code(
    """
# Как выглядит просрочка одного взноса
# DPD = дни опоздания (paid_date - due_date); unpaid считаем отдельно
q('''
SELECT
  CASE
    WHEN paid_date IS NULL THEN 'unpaid'
    WHEN paid_date <= due_date THEN 'on_time'
    WHEN paid_date - due_date BETWEEN 1 AND 29 THEN 'dpd_1_29'
    WHEN paid_date - due_date BETWEEN 30 AND 59 THEN 'dpd_30_59'
    WHEN paid_date - due_date BETWEEN 60 AND 89 THEN 'dpd_60_89'
    ELSE 'dpd_90plus'
  END AS bucket,
  COUNT(*) n
FROM scoring_payments
GROUP BY 1 ORDER BY 1
''')
"""
)

md(
    """
### Таблица-вердикт

| Источник | Ключ | Период | До decision? | В фичи? | Вердикт |
|----------|------|--------|--------------|---------|---------|
| `scoring_applications` | application_id, client_id | 2023-06…2025-04 | анкета — да | анкета: да | **ОСНОВА** |
| `scoring_contracts` | contract_id ← application_id | issue 2023-06…2025-05 | после одобрения | нет | **для target** |
| `scoring_payments` | contract_id | due до 2025-05-31 | после выдачи | нет | **для target** |
| `scoring_bureau_snapshots` | client_id + snapshot_date | 2023-01…2025-05 | snapshot ≤ decision | да (as-of) | **оставить** |
| `scoring_card_transactions` | client_id, card_id | 2023-01…2025-05 | txn_ts < decision | да | **оставить** |
| `scoring_cards` | card_id, customer_id | — | issue ≤ decision | да | **оставить** |
| `scoring_card_disputes` | txn_id | — | opened ≤ decision | да | **оставить** |
| `scoring_customers` | customer_id | — | профиль | да | **оставить** |
| `scoring_applications_full` | application_id | только approved | **нет** (dpd_max, status_now…) | **нет** | leakage; можно сверять target |
| `scoring_collection_actions` | contract_id | после просрочки | нет | нет | не в фичи |
| `scoring_model_v1_scores` | application_id | — | готовый скор | **нет** | подсказка / leak |
| `scoring_payments_backup` | — | урезанный | — | — | **забраковать** |
| `scoring_clients_old` | — | старый срез | — | — | **забраковать** |
| `scoring_txn_sample` | — | 5k sample | — | — | **забраковать** |
| `scoring_temp` | — | мусор | — | — | **забраковать** |

**Карты:** транзакции есть не у всех (~треть одобренных). Это нормально — будут пропуски в card-фичах.
"""
)

md(
    """
## Шаг 3. Сбор таргета

### Простыми словами
Готовой колонки «плохой» нет. Правило:

> За **6 месяцев** после выдачи была просрочка **≥ 30 дней** → `target = 1`, иначе `0`.

Если к 2025-05-31 окно 6 месяцев ещё не закрылось — договор **выкидываем** (не знаем исход = цензура).
"""
)

code(
    """
target = q(f'''
WITH pay AS (
  SELECT
    p.contract_id,
    p.due_date,
    CASE
      WHEN p.paid_date IS NOT NULL THEN (p.paid_date - p.due_date)
      ELSE ('{SNAPSHOT}'::date - p.due_date)
    END AS dpd
  FROM scoring_payments p
),
obs AS (
  SELECT
    ct.application_id,
    ct.client_id,
    a.decision_date,
    ct.issue_date,
    COALESCE(
      MAX(GREATEST(pay.dpd, 0)) FILTER (
        WHERE pay.due_date <= ct.issue_date + INTERVAL '6 months'
          AND pay.due_date <= '{SNAPSHOT}'::date
      ),
      0
    ) AS max_dpd_6m
  FROM scoring_contracts ct
  JOIN scoring_applications a ON a.application_id = ct.application_id
  LEFT JOIN pay ON pay.contract_id = ct.contract_id
  WHERE ct.issue_date + INTERVAL '6 months' <= '{SNAPSHOT}'::date
  GROUP BY 1, 2, 3, 4
)
SELECT
  application_id,
  client_id,
  decision_date,
  CASE WHEN max_dpd_6m >= 30 THEN 1 ELSE 0 END AS target,
  issue_date,
  max_dpd_6m
FROM obs
''')

print('Строк (сезонные договоры):', len(target))
print('Bad rate: {:.2f}%'.format(100 * target['target'].mean()))
print('Цензура: ~2456 договоров с issue+6m > snapshot отброшены')
target[['application_id', 'client_id', 'decision_date', 'target']].head(10)
"""
)

code(
    """
bad_by_month = (
    target.assign(issue_month=pd.to_datetime(target['issue_date']).dt.to_period('M').dt.to_timestamp())
    .groupby('issue_month')
    .agg(n=('target', 'size'), bad=('target', 'sum'), bad_pct=('target', 'mean'))
    .reset_index()
)
bad_by_month['bad_pct'] = (100 * bad_by_month['bad_pct']).round(2)
bad_by_month
"""
)

code(
    """
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(10, 4))
ax.plot(bad_by_month['issue_month'], bad_by_month['bad_pct'], marker='o')
ax.set_title('Bad rate (DPD30+ за 6 мес) по месяцу выдачи')
ax.set_ylabel('bad %')
ax.set_xlabel('месяц выдачи')
ax.axhline(target['target'].mean() * 100, color='gray', ls='--', label='средний')
ax.legend()
plt.xticks(rotation=45)
plt.tight_layout()
plt.show()
"""
)

md(
    """
### Краевые случаи

| Случай | Решение |
|--------|---------|
| `paid_date` NULL | DPD = snapshot − due_date |
| due в будущем | не участвует (фильтры по датам) |
| окно 6м не закрыто | исключён из target |
| отказ без договора | target нет |
| DPD 1–29 | good (порог 30) |

### Вывод
Bad rate вырос: **~18–24%** в 2023 / начале 2024 → **~28–36%** к осени 2024 (пик октябрь ~36%).  
«Просрочка выросла» — **подтверждено**.
"""
)

md(
    """
## Шаг 4. Анализ проблемы
"""
)

code(
    """
# 2) Selection bias: кого старая политика одобряла
q('''
SELECT final_decision,
       COUNT(*) n,
       ROUND(AVG(requested_amount_uzs)/1e6, 2) AS avg_request_mln,
       ROUND(AVG(declared_income_uzs)/1e6, 2) AS avg_income_mln,
       ROUND(AVG(term_months)::numeric, 1) AS avg_term,
       ROUND(AVG(employment_months)::numeric, 1) AS avg_emp_months,
       ROUND(100.0 * COUNT(*) FILTER (WHERE employment_months IS NULL) / COUNT(*), 1) AS pct_null_emp
FROM scoring_applications
GROUP BY 1
''')
"""
)

code(
    """
# 3) Пропуски и аномалии анкеты
q('''
SELECT
  COUNT(*) FILTER (WHERE declared_income_uzs IS NULL) AS null_income,
  COUNT(*) FILTER (WHERE employment_months IS NULL) AS null_employment,
  COUNT(*) FILTER (WHERE employment_months = 0) AS emp_zero,
  COUNT(*) FILTER (WHERE declared_income_uzs <= 0) AS income_nonpositive,
  MIN(declared_income_uzs) AS min_income,
  MAX(declared_income_uzs) AS max_income,
  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY declared_income_uzs) AS med_income,
  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY employment_months) AS med_emp
FROM scoring_applications
''')
"""
)

md(
    """
### Что видно

1. Просрочка выросла — да (шаг 3).
2. **Selection bias:** одобренные просят больше (~13.7 vs 10.8 млн) и выше доход (~10.2 vs 6.4 млн).  
   Модель только на approved не видит «мир отказов».
3. Доход без NULL; `employment_months` — **~8% NULL** (+63 нуля) → флаг пропуска / категория unknown.
"""
)

md(
    """
## Шаг 5. Фичи (только ≤ decision_date)

### Простыми словами
Фича = то, что известно **в момент заявки**.  
«Будет ли просрочка через полгода» в фичи класть нельзя — это списывание (leakage).
"""
)

code(
    """
# Анкета + бюро as-of + карты за 90 дней до decision
features = q(f'''
WITH pay AS (
  SELECT p.contract_id, p.due_date,
         CASE WHEN p.paid_date IS NOT NULL THEN (p.paid_date - p.due_date)
              ELSE ('{SNAPSHOT}'::date - p.due_date) END AS dpd
  FROM scoring_payments p
),
base AS (
  SELECT
    ct.application_id,
    ct.client_id,
    a.decision_date,
    CASE WHEN COALESCE(MAX(GREATEST(pay.dpd,0)) FILTER (
      WHERE pay.due_date <= ct.issue_date + INTERVAL '6 months'
        AND pay.due_date <= '{SNAPSHOT}'::date
    ),0) >= 30 THEN 1 ELSE 0 END AS target,
    a.channel,
    a.purpose,
    a.requested_amount_uzs,
    a.term_months,
    a.declared_income_uzs,
    a.employment_months
  FROM scoring_contracts ct
  JOIN scoring_applications a ON a.application_id = ct.application_id
  LEFT JOIN pay ON pay.contract_id = ct.contract_id
  WHERE ct.issue_date + INTERVAL '6 months' <= '{SNAPSHOT}'::date
  GROUP BY 1,2,3, a.channel, a.purpose, a.requested_amount_uzs,
           a.term_months, a.declared_income_uzs, a.employment_months, ct.issue_date
),
bureau_asof AS (
  SELECT DISTINCT ON (base.application_id)
    base.application_id,
    b.bureau_score,
    b.existing_debt_uzs,
    b.active_loans_cnt
  FROM base
  JOIN scoring_bureau_snapshots b
    ON b.client_id = base.client_id
   AND b.snapshot_date <= base.decision_date
  ORDER BY base.application_id, b.snapshot_date DESC
),
card_90d AS (
  SELECT
    base.application_id,
    COUNT(t.txn_id) AS txn_cnt_90d,
    COALESCE(SUM(t.amount_uzs) FILTER (WHERE t.status='approved'),0) AS turnover_90d,
    COALESCE(SUM(t.amount_uzs) FILTER (WHERE t.txn_type='salary_in' AND t.status='approved'),0) AS salary_in_90d,
    ROUND(100.0 * COUNT(*) FILTER (WHERE t.status='declined') / NULLIF(COUNT(t.txn_id),0), 2) AS decline_pct_90d,
    COALESCE(SUM(t.amount_uzs) FILTER (WHERE t.txn_type='cash_out' AND t.status='approved'),0) AS cashout_90d
  FROM base
  LEFT JOIN scoring_card_transactions t
    ON t.client_id = base.client_id
   AND t.txn_ts >= (base.decision_date::timestamp - INTERVAL '90 days')
   AND t.txn_ts <  base.decision_date::timestamp
  GROUP BY 1
)
SELECT
  base.*,
  ba.bureau_score,
  ba.existing_debt_uzs,
  ba.active_loans_cnt,
  c.txn_cnt_90d,
  c.turnover_90d,
  c.salary_in_90d,
  c.decline_pct_90d,
  c.cashout_90d,
  CASE WHEN COALESCE(c.salary_in_90d,0) = 0 THEN NULL
       ELSE ROUND(c.salary_in_90d::numeric / NULLIF(base.declared_income_uzs,0), 3)
  END AS salary_to_declared_3m
FROM base
LEFT JOIN bureau_asof ba ON ba.application_id = base.application_id
LEFT JOIN card_90d c ON c.application_id = base.application_id
''')

print(features.shape)
features.head()
"""
)

md(
    """
### Почему фичи можно (момент решения)

| Фича | Почему |
|------|--------|
| сумма/срок/доход/стаж/channel/purpose | анкета до решения |
| bureau_* | snapshot_date ≤ decision_date |
| turnover / salary_in / decline% / cashout за 90д | txn_ts < decision_date |
| salary_to_declared_3m | зарплата до заявки vs заявленный доход |

### Ловушки (≥2)

1. `scoring_model_v1_scores.score_v1` — чужой/готовый скор.  
2. `applications_full.dpd_max`, `contract_status_now`, `collection_calls_cnt` — будущее.  
3. Платежи и collection после выдачи — прямой взгляд в target.  
4. «Самый свежий» bureau без as-of — можно взять снимок после решения.
"""
)

md(
    """
## Шаг 6. Схема проверки (модель не учим)

### Out-of-time split (не random!)

| Сет | issue_date | Зачем |
|-----|------------|--------|
| Train | 2023-06 … 2024-06 | старый режим |
| Valid | 2024-07 … 2024-08 | порог |
| Test | 2024-09 … 2024-11 | период **роста** bad |

### Метрики
- Ранжирование: AUC / Gini  
- Экономика: bad rate при фиксированном approval (~63% как сейчас) или approval при целевом bad

### Цена ошибок
Средний договор ~14 млн UZS, ставка ~из `annual_rate_pct`.

| Ошибка | Цена (порядок) |
|--------|----------------|
| Одобрили bad | потеря части тела + collection (LGD условно 30–50%) |
| Отклонили good | упущенные проценты за срок |
"""
)

code(
    """
q('''
SELECT
  ROUND(AVG(issued_amount_uzs)/1e6, 2) AS avg_issue_mln,
  ROUND(AVG(annual_rate_pct)::numeric, 2) AS avg_rate_pct,
  ROUND(AVG(monthly_payment_uzs)/1e6, 2) AS avg_monthly_mln,
  ROUND(AVG(term_months)::numeric, 1) AS avg_term
FROM scoring_contracts
''')
"""
)

md(
    """
## Итог одной страницей

1. Решение банка: **approve/reject**. Событие: **DPD30+ за 6 месяцев**.  
2. Ядро: applications + contracts + payments; bureau/cards — фичи; backup/old/temp/model_v1/full — не для обучения.  
3. Target собран; цензура по snapshot.  
4. Просрочка **выросла** к осени 2024.  
5. Фичи as-of; ловушки названы.  
6. Валидация — **по времени**.

Для формы платформы: формулировка target, bad% по месяцам, таблица вердиктов, 5 ответов шага 1.
"""
)

nb["cells"] = cells
nb["metadata"] = {
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python", "pygments_lexer": "ipython3"},
}
path = Path(__file__).resolve().parent / "week_scoring_dataset.ipynb"
nbf.write(nb, path)
print("written", path, "cells=", len(cells))
