/** Execute the unmodified reference gate with deterministic, offline HTTP fixtures. */
import fs from 'node:fs';
import vm from 'node:vm';

const reference = fs.readFileSync(new URL('../fixtures/release-gate-sdd-reference.mjs', import.meta.url), 'utf8');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const defaults = {key: 'ST-1', labels: ['SDD'], issue_id: '42', repo_count: 1, ...(input.defaults ?? {})};
const cases = Array.isArray(input) ? input : input.cases;
const results = [];

for (const original of cases) {
  const test = {...defaults, ...original};
  const trace = [], sleeps = [], output = [];
  let detailAttempt = 0;
  let resolveExit;
  const exited = new Promise(resolve => { resolveExit = resolve; });
  let exitCode = null;
  const own = (name, fallback) => Object.hasOwn(test, name) ? test[name] : fallback;
  const response = (status, payload) => ({
    ok: status >= 200 && status < 300,
    status,
    statusText: 'Fixture status',
    json: async () => structuredClone(payload),
    text: async () => 'Fixture HTTP error body',
  });
  const context = vm.createContext({
    URLSearchParams,
    console: {log: (...args) => output.push(args.join(' '))},
    process: {
      argv: ['node', 'release-gate-sdd-reference.mjs', 'REL-1'],
      env: {JIRA_BASE_URL: 'https://jira.example', JIRA_API_TOKEN: 'fixture-token', DEBUG: '0'},
      stderr: {write: () => {}},
      exit: code => {
        if (exitCode === null) {
          exitCode = code;
          resolveExit(code);
        }
      },
    },
    setTimeout: (callback, milliseconds) => { sleeps.push(milliseconds); callback(); return 0; },
    fetch: async rawUrl => {
      const url = new URL(rawUrl);
      const params = Object.fromEntries([...url.searchParams].filter(([name]) => name !== '_'));
      trace.push({path: url.pathname, params});
      if (url.pathname.endsWith('/search')) {
        return response(own('search_status', 200), own('search_payload', {
          issues: own('issues', [{key: test.key}]),
        }));
      }
      if (url.pathname.endsWith('/issue/summary')) {
        return response(own('summary_status', 200), own('summary_payload', {
          summary: {repository: {overall: {count: test.repo_count}}},
        }));
      }
      if (url.pathname.endsWith('/issue/detail')) {
        const sequence = test.detail_responses;
        const item = sequence?.[Math.min(detailAttempt, sequence.length - 1)];
        detailAttempt++;
        return response(item?.status ?? 200, item ? item.payload : own('detail', {detail: []}));
      }
      if (/\/rest\/api\/2\/issue\/[^/]+$/.test(url.pathname)) {
        if (params.fields === 'labels') {
          return response(own('issue_status', 200), own('labels_payload', {fields: {labels: test.labels}}));
        }
        return response(own('resolved_status', 200), own('resolved_payload', {id: test.issue_id}));
      }
      throw new Error(`Unexpected offline fixture request: ${url.pathname}`);
    },
  });
  vm.runInContext(reference, context, {filename: 'release-gate-sdd-reference.mjs', timeout: 1000});
  await exited;
  const stories = [];
  for (const line of output) {
    const match = /^\s*\[([^\]]+)\]\([^)]*\)\s+#SDD: (🟢|🔴)\s+openspec: (🟢|🔴|⚪)/u.exec(line);
    if (match) stories.push({key: match[1], hasSddLabel: match[2] === '🟢',
                            hasOpenspec: match[3] === '⚪' ? null : match[3] === '🟢'});
  }
  results.push({id: test.id, exit_code: exitCode, stories, trace, sleeps});
}
process.stdout.write(JSON.stringify(results));
