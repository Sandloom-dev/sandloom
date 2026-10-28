-- wrk / wrk2 script for the four benchmark scenarios (shared by both tools).
--
-- Configured through the environment (set by benchsuite.loadgen):
--   BENCH_SCENARIO  s1 | s2 | s3 | s4
--   BENCH_TOKEN     the access token for S2
--   BENCH_SEED_ROWS ids of S3 are drawn uniformly from 1..(BENCH_SEED_ROWS-1)
--   BENCH_S5_ROUTE  S5 only: the variant's route prefix (/s5/, /s5ro/,
--                   /s5fast/, /s5fn/); ids 1..BENCH_S5_ROWS (tenant A's rows),
--                   BENCH_TOKEN must be a tenant-A token (python -m benchsuite.s5 token)
--
-- S1/S2 use wrk's static pre-built request (the fast path: no Lua per request).
-- S3 draws a random id per request; S4 sends a JSON body with a per-request
-- UNIQUE name/value (thread id + counter), with Content-Type set.
--
-- No response() callback on purpose: defining one makes wrk parse every body
-- through Lua and lowers its ceiling. Status accounting comes from wrk's own
-- counter instead: summary.errors.status counts every response with status
-- >= 400 (non-2xx/3xx). done() prints one machine-readable line,
--   WRKJSON {...}
-- with the totals, the error breakdown and latency percentiles (microseconds;
-- under wrk2 -R they are coordinated-omission corrected).

local scenario = os.getenv("BENCH_SCENARIO") or "s1"
local token = os.getenv("BENCH_TOKEN") or ""
local max_id = (tonumber(os.getenv("BENCH_SEED_ROWS") or "10000") or 10000) - 1

local s5_route = os.getenv("BENCH_S5_ROUTE") or "/s5/"
local s5_rows = tonumber(os.getenv("BENCH_S5_ROWS") or "5000") or 5000

local threads = {}
local counter = 0

function setup(thread)
   table.insert(threads, thread)
   thread:set("tid", #threads)
end

function init(args)
   math.randomseed(((tid or 1) * 7919) + os.time())
   if scenario == "s2" or scenario == "s5" then
      wrk.headers["Authorization"] = "Bearer " .. token
   elseif scenario == "s4" then
      wrk.method = "POST"
      wrk.headers["Content-Type"] = "application/json"
   end
end

if scenario == "s3" then
   request = function()
      return wrk.format("GET", "/s3/" .. math.random(1, max_id))
   end
elseif scenario == "s5" then
   request = function()
      return wrk.format("GET", s5_route .. math.random(1, s5_rows))
   end
elseif scenario == "s4" then
   request = function()
      counter = counter + 1
      local body = string.format(
         '{"name":"w%d-%d","value":%d}', tid or 0, counter, counter % 1000000)
      return wrk.format("POST", "/s4", nil, body)
   end
end

function done(summary, latency, requests)
   local e = summary.errors
   io.write(string.format(
      'WRKJSON {"duration_us":%d,"requests":%d,"bytes":%d,' ..
      '"errors":{"connect":%d,"read":%d,"write":%d,"status":%d,"timeout":%d},' ..
      '"latency_us":{"p50":%.1f,"p90":%.1f,"p99":%.1f,"p99.9":%.1f,' ..
      '"mean":%.1f,"max":%.1f}}\n',
      summary.duration, summary.requests, summary.bytes,
      e.connect, e.read, e.write, e.status, e.timeout,
      latency:percentile(50), latency:percentile(90), latency:percentile(99),
      latency:percentile(99.9), latency.mean, latency.max))
end
