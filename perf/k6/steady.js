// Average-load ("steady") test: constant arrival rate on the read mix.
// Run: k6 run perf/k6/steady.js   (override with -e BASE_URL=... -e RATE=...)
import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8080';
const RATE = Number(__ENV.RATE || 200);
const DURATION = __ENV.DURATION || '60s';

export const options = {
  scenarios: {
    steady: {
      executor: 'constant-arrival-rate', // open model: avoids coordinated omission
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(50, RATE),
      maxVUs: Math.max(200, RATE * 4),
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.001'],
    http_req_duration: ['p(99)<150'],
  },
};

const READ_PATHS = ['/health', '/health', '/health', '/ready', '/metrics'];

export default function () {
  const path = READ_PATHS[Math.floor(Math.random() * READ_PATHS.length)];
  const res = http.get(`${BASE_URL}${path}`);
  check(res, { 'status is 2xx': (r) => r.status >= 200 && r.status < 300 });
}
