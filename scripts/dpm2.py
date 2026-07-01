#!/usr/bin/env python3
"""
Скрипт Саши Клименко (asklimenko@sberbank.ru) для обновления архива релизов с правильным выделением ячеек:
- Hotfix - желтый
- Отменено - красный
- Предстоящие релизы (включая "сегодня") - зеленый
"""

import logging
from datetime import datetime
from typing import Any
import html
import urllib3
from collections import defaultdict
from atlassian import Confluence
from jira import JIRA
import os
import sys

try:
    from scripts import _bootstrap  # noqa: F401
except ImportError:
    import _bootstrap  # noqa: F401

from config import config

# Отключаем предупреждения о SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class ReleaseArchiveUpdater:
    def __init__(self):
        """Инициализация подключений к JIRA и Confluence"""
        self.jira = JIRA(
            options=config['jira']['options'],
            token_auth=config['jira']['token']
        )
        self.confluence = Confluence(
            url=config['confluence']['url'],
            token=config['confluence']['token'],
            verify_ssl=config['confluence']['verify_ssl']
        )

        # Текущий квартал и год
        now = datetime.now()
        self.current_quarter = (now.month - 1) // 3 + 1
        self.current_year = now.year

    def get_jira_issues(self):
        """Получение релизов из JIRA"""
        try:
            ke_ids = [
                2298599, 8553253, 3589425, 3304476, 3303802,
                3191860, 2288712, 2257858, 2935717, 3521872, 6355438,
                5452084, 5452083, 5452082, 5452085, 3303802, 3304476,
                7993288, 2257817, 2644268, 2298078, 5366436, 2503797,
                3930742, 2836020, 3173847, 2295205, 2288712, 9643400,
                9643401, 9644023, 9644025, 9362600, 9535069, 8553253,
                9644020, 10743713
            ]
            unique_ke_ids = list(dict.fromkeys(ke_ids))
            ke_ids_jql = ", ".join(map(str, unique_ke_ids))
            jql_query = (
                'project=HRPRELEASE '
                f'AND КЭ in ({ke_ids_jql}) '
                'AND type = "Release 2.0" '
                'AND created >= "2025-09-01"'
            )
            fields = [
                'summary',
                'status',
                config['jira']['fields']['prod_installed_date_id'],
                config['jira']['fields']['assignee_id'],
                'customfield_18300',
            ]
            return self.jira.search_issues(
                jql_query,
                maxResults=1000,
                fields=','.join(fields),
            )
        except Exception as e:
            logging.error(f"Ошибка получения задач из JIRA: {str(e)}")
            raise

    def get_quarter_info(self, date_str):
        """Определение квартала и года по дате"""
        try:
            date = datetime.strptime(date_str, '%Y-%m-%d')
            quarter = (date.month - 1) // 3 + 1
            return quarter, date.year
        except (ValueError, TypeError) as e:
            logging.warning(f"Неверный формат даты: {date_str}. Ошибка: {e}")
            return None, None

    def _extract_jira_field_text(
        self,
        raw_value: Any,
        *,
        prefer: str = "displayName",
        depth: int = 0,
    ) -> str:
        """Безопасно извлекает текст из Jira-поля без str() на неизвестных объектах."""
        if raw_value is None or depth > 4:
            return ""
        if isinstance(raw_value, str):
            return raw_value.strip()
        if isinstance(raw_value, (int, float, bool)):
            return str(raw_value)
        if isinstance(raw_value, list):
            normalized_items = [
                self._extract_jira_field_text(item, prefer=prefer, depth=depth + 1)
                for item in raw_value[:50]
            ]
            normalized_items = [item for item in normalized_items if item]
            return ", ".join(normalized_items)

        attr_order = (
            ("displayName", "value", "name")
            if prefer == "displayName"
            else ("value", "name", "displayName")
        )

        if isinstance(raw_value, dict):
            for key in attr_order:
                nested_value = raw_value.get(key)
                if nested_value is None or nested_value == "":
                    continue
                extracted = self._extract_jira_field_text(
                    nested_value,
                    prefer=prefer,
                    depth=depth + 1,
                )
                if extracted:
                    return extracted
            return ""

        for attr in attr_order:
            if not hasattr(raw_value, attr):
                continue
            nested_value = getattr(raw_value, attr, None)
            if nested_value is None or nested_value == "" or nested_value is raw_value:
                continue
            extracted = self._extract_jira_field_text(
                nested_value,
                prefer=prefer,
                depth=depth + 1,
            )
            if extracted:
                return extracted
        return ""

    def get_custom_field_value(self, issue, field_id, prefer: str = "displayName"):
        """Безопасное получение значения кастомного поля"""
        try:
            value = getattr(issue.fields, field_id, "")
            if value:
                return self._extract_jira_field_text(value, prefer=prefer)
            return ""
        except Exception as e:
            logging.warning(f"Ошибка получения поля {field_id}: {str(e)}")
            return ""

    def create_jira_macro(self, release_key):
        """Создание макроса JIRA для Confluence"""
        return f'<ac:structured-macro ac:name="jira">' \
               f'<ac:parameter ac:name="key">{release_key}</ac:parameter>' \
               f'</ac:structured-macro>'

    def process_issues(self, issues):
        """Обработка задач JIRA и подготовка данных"""
        data_by_quarter = defaultdict(list)

        total_issues = len(issues)
        logging.info("Обработка %s релизов из JIRA...", total_issues)

        for index, issue in enumerate(issues, start=1):
            try:
                if index == 1 or index % 50 == 0 or index == total_issues:
                    logging.info("Обработано релизов: %s/%s", index, total_issues)

                main_issue = issue
                custom_date_value = self.get_custom_field_value(
                    main_issue,
                    config['jira']['fields']['prod_installed_date_id']
                )

                if not custom_date_value:
                    continue

                quarter, year = self.get_quarter_info(custom_date_value)
                if quarter is None:
                    continue

                try:
                    date_obj = datetime.strptime(custom_date_value, '%Y-%m-%d')
                except (ValueError, TypeError):
                    continue

                main_issue_key = main_issue.key
                main_issue_name = main_issue.fields.summary

                release_type = 'Hotfix' if 'Hotfix' in main_issue_name else 'Плановый релиз'
                jira_macro = self.create_jira_macro(main_issue_key)

                status_name = getattr(main_issue.fields.status, 'name', 'Неизвестно')
                if status_name == 'Отменено':
                    release_info = 'Отменено'
                else:
                    today = datetime.now()
                    delta = (date_obj - today).days
                    if delta > 0:
                        release_info = f'Релиз будет через: {delta} дней'
                    elif delta == 0:
                        release_info = 'Релиз сегодня'
                    else:
                        release_info = f'Релиз состоялся {-delta} дней назад'

                assignee_name = self.get_custom_field_value(
                    main_issue,
                    config['jira']['fields']['assignee_id']
                )
                ke_value = self.get_custom_field_value(
                    main_issue,
                    'customfield_18300',
                    prefer='value',
                )

                data_by_quarter[(quarter, year)].append({
                    'Дата': date_obj.strftime('%Y-%m-%d'),
                    'Тип релиза': release_type,
                    'Ссылка на релиз': jira_macro,
                    'До/После релиза': release_info,
                    'Статус': status_name,
                    'КЭ': ke_value or '-',
                    'Ответственный': assignee_name or 'Не назначен'
                })

            except Exception as e:
                logging.warning(f"Ошибка обработки задачи {issue.key}: {str(e)}")
                continue

        return data_by_quarter

    def generate_confluence_content(self, data_by_quarter):
        """Генерация контента для Confluence с правильным выделением ячеек"""
        # Сортируем кварталы: текущий, будущие, прошлые
        sorted_quarters = sorted(
            data_by_quarter.keys(),
            key=lambda q: (
                0 if (q[0] == self.current_quarter and q[1] == self.current_year) else
                1 if (q[1] > self.current_year or (q[1] == self.current_year and q[0] > self.current_quarter)) else
                2,
                q[1],
                q[0]
            ),
            reverse=False
        )

        html_parts = ['''
        <style>
            .confluenceTable {
                width: 100%;
                margin-bottom: 20px;
                border-collapse: collapse;
            }
            .confluenceTable th {
                background-color: #f0f0f0;
                padding: 8px;
                text-align: left;
                border: 1px solid #ddd;
            }
            .confluenceTable td {
                padding: 8px;
                border: 1px solid #ddd;
            }
        </style>
        ''']

        for quarter, year in sorted_quarters:
            data = data_by_quarter[(quarter, year)]
            # Сортируем данные внутри квартала по дате (новые сверху)
            sorted_data = sorted(data, key=lambda x: x['Дата'], reverse=True)

            quarter_title = f'Квартал {quarter} {year}'
            is_current_quarter = quarter == self.current_quarter and year == self.current_year
            if is_current_quarter:
                quarter_title += ' (Текущий)'
                html_parts.append(f'<h2>{quarter_title}</h2>')
            else:
                html_parts.append(
                    '<ac:structured-macro ac:name="expand">'
                    f'<ac:parameter ac:name="title">{html.escape(quarter_title)}</ac:parameter>'
                    '<ac:rich-text-body>'
                )

            html_parts.append('<table class="confluenceTable"><tbody>')
            html_parts.append('<tr>')
            html_parts.append('<th>Тип релиза</th>')
            html_parts.append('<th>Ссылка на релиз</th>')
            html_parts.append('<th>Дата</th>')
            html_parts.append('<th>До/После релиза</th>')
            html_parts.append('<th>Статус</th>')
            html_parts.append('<th>КЭ</th>')
            html_parts.append('<th>Ответственный</th>')
            html_parts.append('</tr>')

            for row in sorted_data:
                # Определяем стили для ячеек
                type_style = 'background-color: #FFEB99;' if row['Тип релиза'] == 'Hotfix' else ''

                # Стиль для "До/После релиза"
                days_style = ''
                if 'Релиз будет через' in row['До/После релиза'] or 'Релиз сегодня' in row['До/После релиза']:
                    days_style = 'background-color: #DFF2BF;'
                elif row['До/После релиза'] == 'Отменено':
                    days_style = 'background-color: #FFCCCC;'

                # Стиль для "Статус"
                status_style = 'background-color: #FFCCCC;' if row['Статус'] == 'Отменено' else ''

                html_parts.append('<tr>')
                html_parts.append(f'<td style="{type_style}">{html.escape(row["Тип релиза"])}</td>')
                html_parts.append(f'<td>{row["Ссылка на релиз"]}</td>')
                html_parts.append(f'<td>{row["Дата"]}</td>')
                html_parts.append(f'<td style="{days_style}">{html.escape(row["До/После релиза"])}</td>')
                html_parts.append(f'<td style="{status_style}">{html.escape(row["Статус"])}</td>')
                html_parts.append(f'<td>{html.escape(row["КЭ"])}</td>')
                html_parts.append(f'<td>{html.escape(row["Ответственный"])}</td>')
                html_parts.append('</tr>')

            html_parts.append('</tbody></table>')
            if not is_current_quarter:
                html_parts.append('</ac:rich-text-body></ac:structured-macro>')

        return ''.join(html_parts)

    def update_confluence_page(self, data_by_quarter):
        """Обновление страницы в Confluence"""
        content = self.generate_confluence_content(data_by_quarter)

        page_id = self.confluence.get_page_id(
            config['confluence']['space'],
            config['confluence']['archive_page_title']
        )

        if not page_id:
            raise Exception("Страница архива релизов не найдена")

        result = self.confluence.update_page(
            page_id=page_id,
            body=content,
            title=config['confluence']['archive_page_title']
        )

        if not result:
            raise Exception("Не удалось обновить страницу архива релизов")

        return f"{config['confluence']['url']}/pages/viewpage.action?pageId={page_id}"

    def run(self):
        """Основной метод выполнения скрипта"""
        try:
            logging.info("Начало обновления архива релизов...")

            issues = self.get_jira_issues()
            if not issues:
                raise Exception("Не найдено задач для обработки")

            data_by_quarter = self.process_issues(issues)
            page_url = self.update_confluence_page(data_by_quarter)

            logging.info(f"Архив релизов успешно обновлен: {page_url}")
            print(f"CONFLUENCE_PAGE_URL={page_url}")
            return True

        except Exception as e:
            logging.error(f"Критическая ошибка: {str(e)}")
            raise

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('release_archive_update.log'),
            logging.StreamHandler()
        ]
    )

    try:
        updater = ReleaseArchiveUpdater()
        updater.run()
    except Exception as e:
        logging.critical(f"Скрипт завершился с ошибкой: {str(e)}")
        sys.exit(1)
