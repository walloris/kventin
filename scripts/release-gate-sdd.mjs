/**
 * Проверка релизной задачи на соблюдение требований SDD.
 * Проверяет: 1) лейбл SDD на связанных Story; 2) артефакты openspec/<ID> в Bitbucket.
 *
 * Использование:
 *   JIRA_API_TOKEN=<token> node release-gate-sdd.mjs <key>
 */

const JIRA_ID = process.argv[2];
if (!JIRA_ID) {
  debug('Usage: JIRA_API_TOKEN=<token> node release-gate-sdd.mjs <key>');
  process.exit(1);
}

const JIRA_HOST = process.env.JIRA_BASE_URL || 'https://jira.sberbank.ru';
const TOKEN = process.env.JIRA_API_TOKEN;
if (!TOKEN) {
  debug('Error: JIRA_API_TOKEN environment variable is required.');
  process.exit(1);
}

const JIRA_REST = `${JIRA_HOST}/rest/api/2`;
const DEV_STATUS = `${JIRA_HOST}/rest/dev-status/1.0/issue/detail`;
const DEV_SUMMARY = `${JIRA_HOST}/rest/dev-status/1.0/issue/summary`;

// ── Constants ────────────────────────────────────────────────────────────────

/** Задержка перед каждым fetch dev-status detail. */
const SLEEP_DETAIL_BEFORE_MS = 11_000;

/** Максимум попыток на detail. */
const MAX_RETRIES = 3;

const DEBUG = process.env.DEBUG === '1';

// ── Helpers ──────────────────────────────────────────────────────────────────

async function jiraGet(path, params = {}) {
  const qs = new URLSearchParams(params).toString();
  const url = `${JIRA_REST}${path}${qs ? `?${qs}` : ''}`;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${TOKEN}`, Accept: 'application/json' },
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`Jira ${res.status} ${res.statusText}: ${url}\n${body}`);
  }
  return res.json();
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function debug(...args) {
  if (DEBUG) process.stderr.write(args.join(' ') + '\n');
}

// ── Core checks ──────────────────────────────────────────────────────────────

async function checkStories(releaseKey) {
  const search = await jiraGet('/search', {
    jql: `issue IN linkedIssues("${releaseKey}") AND type = Story`,
    maxResults: 200,
    fields: 'key',
  });

  const stories = (search?.issues ?? []).map((i) => i.key.toUpperCase());
  const results = [];

  for (const key of stories) {
    debug(`\n[Story] ${key}`);

    // — лейбл SDD —
    const issue = await jiraGet(`/issue/${key}`, { fields: 'labels' });
    const labels = issue?.fields?.labels ?? [];
    const hasSddLabel = labels.some((l) => l.toLowerCase() === 'sdd');

    // — артефакты openspec —
    let hasOpenspec = null; // null = неизвестно
    const resolved = await jiraGet(`/issue/${key}`, { fields: 'summary' });
    const id = resolved?.id;

    if (id) {
      // Summary — мгновенный, без cache-miss проблем.
      const summaryRes = await fetchSummary(id);
      const repoCount = summaryRes?.summary?.repository?.overall?.count ?? 0;

      if (repoCount === 0) {
        debug(`  [summary] repoCount=0 → openspec=false`);
        hasOpenspec = false;
      } else {
        debug(`  [summary] repoCount=${repoCount} → fetching detail`);
        const data = await fetchDevStatusDetail(id);
        if (!data) {
          debug(`  [detail] all retries failed → unknown`);
          results.push({ key, hasSddLabel, hasOpenspec: null, url: `${JIRA_HOST}/browse/${key}` });
          continue;
        }

        const detailList = data.detail ?? [];

        // Если detail полностью пустой + таймауты → unknown
        if (detailList.length === 0) {
          const errors = data.errors ?? [];
          const hasTimeoutErrors = errors.some(e => e.error?.toLowerCase().includes('timeout'));
          if (hasTimeoutErrors) {
            debug(`  [detail] empty + timeout → unknown`);
            results.push({ key, hasSddLabel, hasOpenspec: null, url: `${JIRA_HOST}/browse/${key}` });
            continue;
          }
          // empty но без ошибок таймаута — пустой результат, false
        }
        // иначе detailList не пустой — парсим его, ошибки других инстансов игнорируем

        const keyLower = key.toLowerCase();
        let matchCount = 0;
        for (const repoGroup of detailList) {
          for (const repo of repoGroup.repositories ?? []) {
            for (const commit of repo.commits ?? []) {
              for (const file of commit.files ?? []) {
                const p = file.path.toLowerCase();
                if (p.startsWith('openspec/') && new RegExp(`(?:/|^)${keyLower}`, 'i').test(p)) {
                  matchCount++;
                }
              }
            }
          }
        }
        hasOpenspec = matchCount > 0;
        debug(`  [detail] matches=${matchCount}`);
      }
    }

    results.push({ key, hasSddLabel, hasOpenspec: hasOpenspec ?? false, url: `${JIRA_HOST}/browse/${key}` });
  }

  return results;
}

/** Fetch dev-status summary endpoint (fast, no cache-miss issues). */
async function fetchSummary(issueId) {
  const url = `${DEV_SUMMARY}?issueId=${issueId}&_=${Date.now()}`;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${TOKEN}`, Accept: 'application/json' },
  });
  if (!res.ok) return null;
  return res.json();
}

/** Fetch dev-status detail с retry. */
async function fetchDevStatusDetail(issueId) {
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    await sleep(SLEEP_DETAIL_BEFORE_MS);

    const url = `${DEV_STATUS}?issueId=${issueId}&applicationType=stash&dataType=repository&_=${Date.now()}`;
    const res = await fetch(url, {
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        Accept: 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
      },
    });
    if (!res.ok) continue;

    const data = await res.json();
    const detailLen = (data.detail ?? []).length;
    debug(`  [attempt #${attempt}] status=${res.status} detail=${detailLen} errors=${(data.errors ?? []).length}`);

    if (detailLen > 0) return data;
    if ((data.errors?.length ?? 0) === 0) return data;
  }
  return null;
}

// ── Main ─────────────────────────────────────────────────────────────────────

function formatLine(key, hasSdd, hasOpenspec, url) {
  const sdd = hasSdd ? '🟢' : '🔴';
  const op = hasOpenspec === null ? '⚪' : (hasOpenspec ? '🟢' : '🔴');
  return `  [${key}](${url})   #SDD: ${sdd}   openspec: ${op}`;
}

async function main() {
  const releaseUrl = `${JIRA_HOST}/browse/${JIRA_ID}`;
  console.log(`\n[${JIRA_ID}](${releaseUrl})\n`);

  const results = await checkStories(JIRA_ID);

  if (results.length === 0) {
    console.log('  ⚠️  No linked stories found.\n');
    process.exit(0);
  }

  for (const r of results) {
    console.log(formatLine(r.key, r.hasSddLabel, r.hasOpenspec, r.url));
  }

  const failures = results.filter((r) => {
    if (r.hasOpenspec === null) return false;
    return !r.hasSddLabel || !r.hasOpenspec;
  });

  const unknown = results.filter((r) => r.hasOpenspec === null);

  if (unknown.length > 0) {
    console.log('\n  ⚠️  Не удалось проверить:\n');
    for (const u of unknown) {
      console.log(`  [${u.key}](${u.url}) — не удалось получить ответ (таймаут / ошибка подключения)`);
    }
    console.log('\n');
  }

  if (failures.length > 0) {
    console.log('\n  ⚠️  Отклонения:\n');
    for (const f of failures) {
      const reasons = [];
      if (!f.hasSddLabel) reasons.push('нет #SDD');
      if (!f.hasOpenspec) reasons.push('нет openspec');
      console.log(`  [${f.key}](${f.url}) — ${reasons.join(', ')}`);
    }
    console.log('\n');
  }

  const allOk = results.every((r) => {
    if (r.hasOpenspec === null) return true;
    return r.hasSddLabel && r.hasOpenspec;
  });

  console.log(allOk ? '✅ PASS\n' : '❌ FAIL\n');

  process.exit(allOk ? 0 : 1);
}

main().catch((err) => {
  debug('Fatal error:', err);
  process.exit(2);
});
