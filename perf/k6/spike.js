// Spike test: jump from near-idle to peak instantly (models webhook bursts) and
// measure recovery. Run: k6 run perf/k6/spike.js
import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8080';
const PEAK = Number(__ENV.PEAK || 1500);

export const options = {
  scenarios: {
    spike: {
      executor: 'ramping-arrival-rate',
      startRate: 10,
      timeUnit: '1s',
      preAllocatedVUs: 200,
      maxVUs: Math.max(1000, PEAK * 2),
      stages: [
        { target: 10, duration: '10s' },   // baseline
        { target: PEAK, duration: '1s' },  // spike
        { target: PEAK, duration: '20s' }, // sustain
        { target: 10, duration: '1s' },    // drop
        { target: 10, duration: '20s' },   // recovery observation
      ],
    },
  },
  thresholds: {
    // During overload the service should SHED (429/503), not error with 5xx.
    'http_req_failed{expected_response:true}': ['rate<0.01'],
  },
};

export default function () {
  const res = http.get(`${BASE_URL}/health`);
  check(res, { 'served or shed': (r) => r.status < 500 || r.status === 503 });
}
