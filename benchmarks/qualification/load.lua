-- wrk response checks. TOKEN comes from synthetic local credentials, never a CLI arg.
local threads = {}
local profile = os.getenv('SANDLOOM_QUALIFICATION_PROFILE') or 'read'
local token = os.getenv('SANDLOOM_QUALIFICATION_TOKEN')
local read_route = os.getenv('SANDLOOM_QUALIFICATION_READ_ROUTE') or '/s5/'
local user_agent = os.getenv('SANDLOOM_QUALIFICATION_USER_AGENT')
local observe_edge = os.getenv('SANDLOOM_QUALIFICATION_OBSERVE_EDGE') == '1'
local run = os.getenv('SANDLOOM_QUALIFICATION_RUN') or 'local'
local next_thread = 0

local function json_string(value)
  value = tostring(value or ''):gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('%c', ' ')
  return '"' .. value .. '"'
end

function setup(thread)
  next_thread = next_thread + 1
  thread:set('thread_id', next_thread)
  table.insert(threads, thread)
end

function init(args)
  ok = 0
  bad = 0
  body_bad = 0
  sequence = 0
  codes = {}
  error_times = {}
  error_samples = {}
  edge_colos = {}
  edge_ingresses = {}
  wrk.headers['Authorization'] = 'Bearer ' .. token
  wrk.headers['Content-Type'] = 'application/json'
  if user_agent then wrk.headers['User-Agent'] = user_agent end
end

function request()
  sequence = sequence + 1
  if observe_edge then
    wrk.headers['X-SANDLOOM-Load-Request'] = run .. '-' .. thread_id .. '-' .. sequence
  end
  if profile == 'write' then
    local body = string.format('{"name":"q-%s-%d-%d","value":7}', run, thread_id, sequence)
    return wrk.format('POST', '/dw/3', nil, body)
  end
  return wrk.format('GET', read_route .. ((sequence - 1) % 5000 + 1))
end

function response(status, headers, body)
  if observe_edge then
    local colo = 'missing'
    local ingress = 'missing'
    for name, value in pairs(headers) do
      if name:lower() == 'cf-ray' then
        colo = value:match('%-([A-Z]+)$') or 'unknown'
      elseif name:lower() == 'x-sandloom-ingress' then
        ingress = (value == 'witness' or value == 'qualification') and value or 'other'
      end
    end
    edge_colos[colo] = (edge_colos[colo] or 0) + 1
    edge_ingresses[ingress] = (edge_ingresses[ingress] or 0) + 1
  end
  codes[status] = (codes[status] or 0) + 1
  local expected = profile == 'write' and 201 or 200
  if status == expected then
    ok = ok + 1
    local id = tonumber(body:match('"id"%s*:%s*(%d+)'))
    if not id or (profile == 'read' and (id < 1 or id > 5000)) then
      body_bad = body_bad + 1
    end
  else
    bad = bad + 1
    local key = tostring(os.time()) .. ':' .. tostring(status)
    error_times[key] = (error_times[key] or 0) + 1
    if #error_samples < 4 then
      local chosen = {}
      for name, value in pairs(headers) do
        local lower = name:lower()
        if lower == 'server' or lower == 'cf-ray' or lower == 'x-request-id'
          or lower == 'cf-error-type' or lower == 'cf-error-origin'
          or lower == 'content-encoding' or lower == 'content-length'
          or lower == 'cf-cache-status' or lower == 'date'
          or lower == 'x-sandloom-ingress' or lower == 'cache-control' then
          table.insert(chosen, json_string(lower) .. ':' .. json_string(value))
        end
      end
      table.insert(error_samples, string.format('{"status":%d,"epoch":%d,"headers":{%s},"body_prefix":%s}',
        status, os.time(), table.concat(chosen, ','), json_string(body:sub(1, 300))))
    end
  end
end

function done(summary, latency, requests)
  local good, errors, malformed = 0, 0, 0
  local statuses = {}
  local error_times = {}
  local error_samples = {}
  local edge_colos = {}
  local edge_ingresses = {}
  for _, thread in ipairs(threads) do
    good = good + thread:get('ok')
    errors = errors + thread:get('bad')
    malformed = malformed + thread:get('body_bad')
    for status, count in pairs(thread:get('codes')) do
      statuses[status] = (statuses[status] or 0) + count
    end
    for key, count in pairs(thread:get('error_times')) do
      error_times[key] = (error_times[key] or 0) + count
    end
    for _, sample in ipairs(thread:get('error_samples')) do
      table.insert(error_samples, sample)
    end
    if observe_edge then
      for colo, count in pairs(thread:get('edge_colos')) do
        edge_colos[colo] = (edge_colos[colo] or 0) + count
      end
      for ingress, count in pairs(thread:get('edge_ingresses')) do
        edge_ingresses[ingress] = (edge_ingresses[ingress] or 0) + count
      end
    end
  end
  local parts = {}
  for status, count in pairs(statuses) do
    table.insert(parts, string.format('"%d":%d', status, count))
  end
  local error_parts = {}
  for key, count in pairs(error_times) do
    table.insert(error_parts, string.format('"%s":%d', key, count))
  end
  io.write(string.format('QUALIFICATION {"ok":%d,"http_errors":%d,"invalid_bodies":%d,"statuses":{%s},"error_times":{%s},"error_samples":[%s],"p50_ms":%.3f,"p95_ms":%.3f,"p99_ms":%.3f,"socket_errors":%d,"socket_errors_detail":{"connect":%d,"read":%d,"write":%d,"timeout":%d},"goodput_rps":%.3f}\n',
    good, errors, malformed, table.concat(parts, ','), table.concat(error_parts, ','), table.concat(error_samples, ','), latency:percentile(50) / 1000,
    latency:percentile(95) / 1000, latency:percentile(99) / 1000,
    summary.errors.connect + summary.errors.read + summary.errors.write + summary.errors.timeout,
    summary.errors.connect, summary.errors.read, summary.errors.write, summary.errors.timeout,
    good / (summary.duration / 1000000)))
  if observe_edge then
    local parts = {}
    for colo, count in pairs(edge_colos) do
      table.insert(parts, json_string(colo) .. ':' .. tostring(count))
    end
    io.write('EDGE_COLOS {' .. table.concat(parts, ',') .. '}\n')
    parts = {}
    for ingress, count in pairs(edge_ingresses) do
      table.insert(parts, json_string(ingress) .. ':' .. tostring(count))
    end
    io.write('EDGE_INGRESSES {' .. table.concat(parts, ',') .. '}\n')
  end
end
