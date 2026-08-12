// Stress / breakpoint test: ramp arrival rate to find the "knee" where p99 or
// error rate breaches SLO. Report the knee, not just pass/fail.
// Run: k6 run perf/k6/stress.js
import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8080';
const PEAK = Number(__ENV.PEAK || 2000);

export const options = {
  scenarios: {
    ramp: {
      executor: 'ramping-arrival-rate',
      startRate: 50,
      timeUnit: '1s',
      preAllocatedVUs: 200,
      maxVUs: Math.max(1000, PEAK * 2),
      stages: [
        { target: Math.round(PEAK * 0.25), duration: '30s' },
        { target: Math.round(PEAK * 0.5), duration: '30s' },
        { target: Math.round(PEAK * 0.75), duration: '30s' },
        { target: PEAK, duration: '30s' },
        { target: PEAK, duration: '30s' },
      ],
    },
  },
  // Deliberately loose: we want the run to complete so the summary reveals the
  // knee rather than aborting at the first breach.
  thresholds: {
    http_req_failed: ['rate<0.05'],
  },
};

export default function () {
  const res = http.get(`${BASE_URL}/health`);
  check(res, { 'status is 2xx': (r) => r.status >= 200 && r.status < 300 });
}
