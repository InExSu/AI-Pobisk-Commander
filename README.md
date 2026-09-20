# AI-Pobisk-Commander

Control and Management of AI Agents Based on the Systems-Engineering Approach of Pobisk Kuznetsov

Управление агентами ИИ по методам Побиска Кузнецова.

ai_Pobisk — консольная утилилита, при запуске смотри параметры и конфиги.
Параметры превыше конфигов.
Используя ии агент (агенты могут быть любыми — локальными или облачными), ai_Pobisk анализирует состояние и организует выполнение задач в условиях:

- модель перестала отвечать — нужно её пинать, пока не станет понятно — работает она или уже нет.
- у модели кончились лимиты — нужно переключится на другую модель
- в аккаунте агента кончились лимиты всех моделей — нужно переключиться в другой аккаунт.
- во всех аккаунтах агента кончились лимиты — передать задачу другому агенту.
- нельзя впдадать в бесконечный цикл.
- модель что-то спросила — нужно понять и решить — дать ответ или спросить хозяина.

Чтоб тебе легче было создавать силуэт ДРАКОНа создай управляющую машину состояний и из неё создавай силуэт.

---

## ai_Pobisk

```bash
./ai_Pobisk.sh                              # все задачи из configs/tasks/
./ai_Pobisk.sh --task "01 добавка.md"
./ai_Pobisk.sh --dry-run                    # план, ни одна модель не вызвана
./ai_Pobisk.sh list                         # найденные задачи
./ai_Pobisk.sh skills                       # агенты и доступность
./ai_Pobisk.sh self-test                    # мета-модель + адаптеры + задачи
./ai_Pobisk.sh status                       # состояние и вопросы к хозяину
```

Супервизор: берёт задачу из `configs/tasks/*.md`, нанимает рабочую модель через
`.agents/skills/`, следит за ней. Модель остановилась — определяет, фатально ли,
и продолжает если нет. Модель спросила — решает, в его ли компетенции вопрос.

Стартует с настройки себе **мета-модели** (быстрая и дешёвая, отдельно от
рабочих). Если мета недоступна — деградирует на таблицу правил, не замирает.

```
задача -> адаптер скилла -> Outcome (7 исходов)
  ok/retry/rotate_model  -> продолжить
  rotate_account         -> другой аккаунт/скилл
  question               -> компетенция: ответить самому или хозяину
  fatal/loop_guard       -> стоп, доложить
```

Ключевое решение: **единый словарь исхода** (`_shared/outcome.py`). Пять скиллов
сообщали об ошибках на пяти диалектах — cline текстом баннера, opencode кодами
0/1/2, freebuff 1/4/5, nvidia/th exit 3 + HTTP. Адаптеры в `src/adapters/`
переводят всё в семь исходов; **сами скиллы не менялись**.

Состояние и разбор действий — `.ai_pobisk/{state.json,journal.md}`. Убили
процесс на середине — перезапуск продолжает с того же шага.

### Мониторинг

```bash
./ai_Pobisk.sh status              # состояние задач
./ai_Pobisk.sh status --live       # агрегаты текущего прогона
./ai_Pobisk.sh status --live --json
./ai_Pobisk.sh watch               # живой вид во время прогона
./ai_Pobisk.sh check               # пороги; exit 1 если что-то не так
./ai_Pobisk.sh serve               # /metrics в формате Prometheus
./ai_Pobisk.sh report              # разбор прошедшего прогона
./ai_Pobisk.sh skills --probe      # реально пингануть модели (тратит запросы)
```

Каждое событие пишется и в `journal.md` (человеку), и в `events.jsonl`
(машине) — из одной точки, поэтому они не расходятся. Агрегаты: длительность,
п50/р95 шага, доля ошибок, ok% по скиллам, число подталкиваний и ротаций.

Пороги в `check`: доля `fatal`, число подталкиваний, р95 против медианы,
приближение к таймауту шага. Всё на stdlib — Prometheus не нужен.

### Кто решает: модель или код

**Решатель — мета-модель.** Код описывает только инварианты:

| Инвариант (код, модель не голосует) | Почему |
|---|---|
| бюджеты и лимит итераций | иначе «повтори» навсегда |
| креденшелы и пустой кошелёк | ретраем не починится |
| «нет файловой системы» у голых API | отсутствие способности, не невезение |
| подтверждения (`Press Enter`) | за клавиатурой никого; ответ всегда «продолжай» |
| `loop_guard` | уже крутится |

Всё остальное — многообразие поведения моделей — уходит мета-модели. Она
отвечает одним из оговоренных исходов, код проверяет ответ и блюдёт бюджеты.
Если мета недоступна, работает таблица правил: деградация, не остановка.

### Контекст кода (Graft)

ai_Pobisk добавляет в промпт рабочей модели карту репозитория, чтобы модель не
переоткрывала код с нуля на каждой задаче. Заявлено Graft: **−42% токенов,
−46% вызовов инструментов, −60% времени** при той же или лучшей корректности.

```bash
./ai_Pobisk.sh graft              # статус + карта репо
./ai_Pobisk.sh graft build        # построить граф (tree-sitter, без ключа)
./ai_Pobisk.sh graft ask "вопрос" # что граф знает по вопросу
```

`graft build` и все команды чтения — детерминированный tree-sitter: **без
модели, без ключа, без сети**, на этом репо ~0.4 с. Граф `graft/` —
локальный кэш, в git не коммитится. Если Graft не установлен или граф не
построен — промпт передаётся как есть, ничего не ломается.

Отключить: `enabled = false` в секции `[graft]` файла `configs/ai_pobisk.toml`.

Установка CLI: `npm install -g @nanonets/graft`

---

## Запуск агентов с ротацией аккаунтов и моделей

Живые агенты — в `.agents/skills/`. У каждого свой супервизор: он сам выбирает
рабочую модель, а когда лимиты кончаются — переключает модель, потом аккаунт,
потом передаёт задачу другому агенту.

| Агент | Супервизор | Что ротирует | Моделей |
|---|---|---|---|
| **Cline** | `cline/cline-rotate.sh` | аккаунты × модели | 21 |
| **OpenCode** | `OpenCode/opencode-rotate.sh` | аккаунты × модели | 8 |
| **FreeBuff** | `FreeBuff/freebuff-rotate.sh` | аккаунты × модели (по квоте) | 10 |
| **NVIDIA NIM** | `NVIDIA/nvidia-rotate.sh` | модели, **параллельно** | 11 |
| **Token Harbor** | `TokenHarbor/tokenharbor-rotate.sh` | модели | 4 |

```bash
cd /Users/michaelpopov/Documents/GitHub/_Different/AI/AI-Pobisk-Commander

# Cline — акт-режим, автоподтверждение
.agents/skills/cline/cline-rotate.sh "сделай рассылку материалов про игру"

# OpenCode — one-shot с авто-ротацией
.agents/skills/opencode/opencode-rotate.sh -- "fix the failing test in src/auth.ts"

# FreeBuff — preflight по квоте, затем интерактивный TUI
.agents/skills/freebuff/freebuff-rotate.sh
.agents/skills/freebuff/freebuff-rotate.sh quota      # таблица квот, без трат

# NVIDIA NIM — спросить модель
.agents/skills/nvidia/nvidia-rotate.sh ask "explain this diff"
.agents/skills/nvidia/nvidia-rotate.sh rpm            # бюджет: 11 моделей × 40 RPM

# Token Harbor
.agents/skills/tokenharbor/tokenharbor-rotate.sh ask "explain this diff"
```

### Регенерация списков моделей

```bash
.agents/skills/cline/cline_Models_Free_Make.sh
.agents/skills/opencode/opencode_Models_Free_Make.sh
.agents/skills/freebuff/FreeBuff_Models_Free_Make.sh
.agents/skills/nvidia/nvidia-rotate.sh probe           # какие отвечают сейчас
.agents/skills/tokenharbor/tokenharbor-rotate.sh models --refresh
```

Все списки — **только id, без названий**, по одному на строку.

---

## Общая инфраструктура

### `.agents/skills/_shared/model-stats.py` — здоровье моделей

Одно хранилище `~/.ai-rotate/model-stats.json` на все скиллы: модель, которую
один агент признал мёртвой, не_probe-ится вторым.

- **Stability Score** — `0.30·p95 + 0.30·jitter + 0.20·spike + 0.20·uptime`
  (формула из [free-coding-models](https://github.com/vava-nessa/free-coding-models),
  кэпы подняты под медленные CLI-пробы). Средняя латентность врёт: модель со
  средним 250 мс, но p95 6 с, ранжируется **ниже** стабильных 400 мс.
- **Circuit breaker** — 3 провала подряд → cooldown 900 с. `auth_error`
  (401/403/плохой ключ) паркуется навсегда: ретраем не починится.
- **Порядок ротации** — healthy (по стабильности) → untested → cooling.
- **Family failover** — мёртвый `deepseek/*` переходит на другой `deepseek/*`,
  а не на случайную модель: середина сессии сохраняет характер модели.

```bash
S=.agents/skills/_shared/model-stats.py
python3 "$S" show                 # таблица: state, stability, p95, uptime
python3 "$S" show cline           # один скилл
python3 "$S" reset cline model    # забыть модель
```

Отключить: `CLINE_NO_STATS=1`, `OC_NO_STATS=1`, `FB_NO_STATS=1`,
`QWEN_NO_STATS=1`.

### `.agents/skills/_shared/secrets.py` — ключи API

Один файл `~/.ai-rotate/secrets.json` (**chmod 600, вне репозитория**). В
репозитории ключей нет, и `check` ругается, если они там появятся.

```bash
S=.agents/skills/_shared/secrets.py
python3 "$S" set nvidia "nvapi-..."       # добавить/заменить
python3 "$S" list                          # nvap…-ddy (70 chars) — маскирует
python3 "$S" get nvidia                    # полное значение, для скриптов
python3 "$S" check                         # права + скан репо на ключи
python3 "$S" fix-perms                     # chmod 600 после случайного 644
```

С файлом слабее 600 скрипт **отказывается работать** (exit 2) — не отдаст
ключи из доступного всем файла. Сканер ищет полные ключи, не префиксы, поэтому
`AI-Pobisk-Commander` (содержит `sk-`) не даёт ложных срабатываний.

Скрипты читают ключ так, ничего не печатая:

```bash
. "$(dirname "$0")/../_shared/require-secret.sh"
require_secrets NVIDIA_API_KEY:nvidia       # exit 2 с подсказкой, если нет
```

Подробнее: [`_shared/SECRETS.md`](.agents/skills/_shared/SECRETS.md),
[`_shared/README.md`](.agents/skills/_shared/README.md).

---

## Что важно знать

**NVIDIA: лимит 40 RPM на каждую модель отдельно, не на ключ.** Поэтому
`parallel` — ключевой режим: 11 моделей дают 440 RPM агрегатно против 40 при
последовательном переборе.

**Каталоги врут.** NVIDIA `/v1/models` отдаёт 82 id — отвечают 11, остальные
404 «Function not found». Token Harbor: `th-orchestra` показывает нулевую цену,
но отвечает **402**; надёжный признак бесплатности — суффикс `:free`.

**Медленные ≠ мёртвые.** `z-ai/glm-5.3-flash` и `moonshotai/kimi-k3`
отвечают за 60-130 с. Короткий таймаут (`NVIDIA_TIMEOUT`, по умолчанию 240)
заставит принять их за сломанные.

Детали и грабли — в `SKILL.md` каждого агента.
