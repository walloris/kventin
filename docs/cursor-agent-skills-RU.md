# Agent Skills в этом проекте

Используется пакет **[addyosmani/agent-skills](https://github.com/addyosmani/agent-skills)** — Markdown-процессы для ИИ-агента (шаги, проверки, «анти-отмазки»).

## Что уже сделано за вас

1. **`vendor/agent-skills/`** — полная копия репозитория (все 22 скилла, `agents/`, `references/`, `docs/`).
2. **`.cursor/rules/agent-skills-router.mdc`** — короткое правило с **включённым `alwaysApply`**: оно не дублирует тысячи строк в контекст, а даёт **таблицу путей** и приказ агенту **по ходу задачи читать** нужный `vendor/agent-skills/skills/<имя>/SKILL.md` через инструменты чтения файлов.

Ручное копирование `SKILL.md` в `.cursor/rules/` **не требуется**.

## Как это работает

- При серьёзной задаче агент сначала ориентируется по мета-скиллу `using-agent-skills`, затем открывает **только** релевантные скиллы из `vendor/`, чтобы не забивать лимит контекста.
- Персоны ревью: `vendor/agent-skills/agents/` (например `code-reviewer.md`).
- Чеклисты: `vendor/agent-skills/references/`.
- Обновление пакета: заменить содержимое `vendor/agent-skills/` свежей версией с GitHub.

## Важно

- Скиллы **дополняют** корневой `.cursorrules` и пользовательские правила Cursor.
- Лицензия пакета — **MIT**, текст в `vendor/agent-skills/LICENSE`.
