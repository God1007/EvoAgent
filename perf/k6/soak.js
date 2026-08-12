// Soak / endurance test: moderate steady load for a long window to surface
// memory growth, connection/thread leaks, and queue backlog.
// Run: k6 run perf/k6/soak.js   (default 30m; override -e DURATION=2h)
import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8080';
const RATE = Number(__ENV.RATE || 100);
const DURATION = __ENV.DURATION || '30m';

export const options = {
  scenarios: {
    soak: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(50, RATE),
      maxVUs: Math.max(200, RATE * 4),
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.001'],
    http_req_duration: ['p(99)<200'],
  },
};

export default function () {
  const res = http.get(`${BASE_URL}/health`);
  check(res, { 'status is 2xx': (r) => r.status >= 200 && r.status < 300 });
}
